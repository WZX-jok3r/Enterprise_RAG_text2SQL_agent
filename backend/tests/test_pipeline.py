"""RAG 链路编排单测（P2 门禁）：注入 FakeRetriever / FakeLLM，验证三条对外链路的
结果键契约、无召回拒答、检索-only 跳过生成、流式事件序列。不触网、不查库。

末尾的 golden 用例锁的是**"抽 executor 前后 messages 逐字符不变"** ——
它们是 Step 1 重构的安全网：任何让 pipeline 发给 LLM 的内容发生漂移的改动都会红。
"""

from app.prompts.rag import SYSTEM_PROMPT
from app.rag.context_builder import build_context
from app.rag.executor import TOOL_CONTEXT_NOTE
from app.rag.pipeline import RAGPipeline

_CHUNKS = [
    {
        "text": "员工表：编号1，姓名Alice，所在城市Beijing。",
        "metadata": {"source": "data/hr.md", "format": "md"},
        "score": 0.88,
    }
]


class FakeRetriever:
    def __init__(self, chunks, dropped=0):
        self._chunks = chunks
        self._dropped = dropped
        self.calls = []

    def search(self, query, top_k=None, filter_dict=None):
        self.calls.append((query, top_k, filter_dict))
        return self._chunks, self._dropped


class FakeLLM:
    model = "fake-model"

    def complete(self, messages, temperature=None, max_tokens=None):
        return "这是答案", {"input": 1, "output": 2, "total": 3}

    def stream(self, messages, temperature=None, max_tokens=None):
        yield "你"
        yield "好"


def test_query_result_contract():
    p = RAGPipeline(retriever=FakeRetriever(_CHUNKS, dropped=2), llm=FakeLLM())
    res = p.query("问题", top_k=5)

    assert set(res.keys()) == {
        "query", "answer", "sources", "retrieved_count", "hidden_count",
    }
    assert res["answer"] == "这是答案"
    assert res["retrieved_count"] == 1
    assert res["hidden_count"] == 2
    # query 走 with_preview=True
    assert res["sources"][0]["source"] == "hr.md"
    assert "text_preview" in res["sources"][0]


def test_query_retrieval_only_skips_llm():
    p = RAGPipeline(retriever=FakeRetriever(_CHUNKS), llm=FakeLLM())
    res = p.query("问题", generate=False)
    assert res["answer"] == "(retrieval-only：已跳过 LLM 生成)"


def test_query_no_context_refuses():
    p = RAGPipeline(retriever=FakeRetriever([], dropped=0), llm=FakeLLM())
    res = p.query("问题")
    assert res["answer"] == "根据现有资料，未能找到相关信息。"
    assert res["sources"] == []
    assert res["retrieved_count"] == 0


def test_query_with_history_uses_fallback_context():
    # 无召回时不拒答，走 "无相关参考资料" 兜底，仍调用 LLM
    p = RAGPipeline(retriever=FakeRetriever([]), llm=FakeLLM())
    res = p.query_with_history("问题", history=[{"role": "user", "content": "早"}])
    assert res["answer"] == "这是答案"
    assert res["sources"] == []


def test_stream_chat_event_sequence():
    p = RAGPipeline(retriever=FakeRetriever(_CHUNKS), llm=FakeLLM())
    events = list(p.stream_chat("问题", history=[]))
    types = [e["type"] for e in events]

    assert types[0] == "meta"
    assert types[-1] == "done"
    assert "delta" in types
    assert events[0]["sources"][0]["source"] == "hr.md"
    assert events[-1]["answer"] == "你好"


def test_stream_chat_no_context_short_circuits():
    p = RAGPipeline(retriever=FakeRetriever([]), llm=FakeLLM())
    events = list(p.stream_chat("问题", history=[]))
    assert events[0]["type"] == "meta"
    assert events[-1]["type"] == "done"
    assert events[-1]["answer"] == "根据现有资料，未能找到相关信息。"


# ==================== golden：发给 LLM 的 messages 不变 ====================

class RecordingLLM(FakeLLM):
    """记录每次收到的 messages。"""

    def __init__(self):
        self.batches = []

    def complete(self, messages, temperature=None, max_tokens=None):
        self.batches.append(messages)
        return "这是答案", {"input": 1, "output": 2, "total": 3}

    def stream(self, messages, temperature=None, max_tokens=None):
        self.batches.append(messages)
        yield "你"
        yield "好"


def test_query_messages_are_system_plus_user():
    llm = RecordingLLM()
    p = RAGPipeline(retriever=FakeRetriever(_CHUNKS), llm=llm)
    p.query("问题", top_k=5)

    messages = llm.batches[0]
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    # system 只由「prompt 模板 + context」构成，不许再拼别的东西
    assert messages[0]["content"] == SYSTEM_PROMPT.format(context=build_context(_CHUNKS))
    # 参考资料段落只出现一次（prompt 自己的示例里也含"【参考1】"，故匹配整行）
    assert messages[0]["content"].count("【参考1】来源: hr.md (md)") == 1
    assert messages[-1] == {"role": "user", "content": "问题"}


def test_query_with_history_messages_order_and_shape():
    llm = RecordingLLM()
    history = [
        {"role": "user", "content": "Sales 有多少人？"},
        {"role": "assistant", "content": "1042 人。"},
    ]
    p = RAGPipeline(retriever=FakeRetriever(_CHUNKS), llm=llm)
    p.query_with_history("问题", history=history, top_k=5)

    messages = llm.batches[0]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[1:3] == history
    assert messages[-1] == {"role": "user", "content": "问题"}
    # 无 tool_context 的历史不应被改写（逐字符等于入参）
    assert all(set(m) == {"role", "content"} for m in messages)


def test_query_with_history_strips_tool_context_field():
    """P0：tool_context 是给模型看的注记，不是合法 messages 字段。"""
    llm = RecordingLLM()
    history = [
        {"role": "assistant", "content": "1042 人。", "tool_context": "SELECT count(*) -> 1042"},
    ]
    p = RAGPipeline(retriever=FakeRetriever(_CHUNKS), llm=llm)
    p.query_with_history("问题", history=history, top_k=5)

    msgs = llm.batches[0]
    assert all(set(m) == {"role", "content"} for m in msgs)
    assert msgs[1]["content"] == f"1042 人。\n\n{TOOL_CONTEXT_NOTE}\nSELECT count(*) -> 1042"


def test_stream_chat_uses_same_messages_as_query_with_history():
    """同一份历史，流式与非流式必须发给 LLM 完全相同的 messages。

    这是"消除两份实现行为不一致"的直接断言：只改一条路的拼接，这条就会红。
    """
    history = [
        {"role": "user", "content": "Sales 有多少人？"},
        {"role": "assistant", "content": "1042 人。", "tool_context": "count=1042"},
    ]
    sync_llm, stream_llm = RecordingLLM(), RecordingLLM()
    RAGPipeline(retriever=FakeRetriever(_CHUNKS), llm=sync_llm).query_with_history(
        "问题", history=history, top_k=5
    )
    list(RAGPipeline(retriever=FakeRetriever(_CHUNKS), llm=stream_llm).stream_chat(
        "问题", history=history, top_k=5
    ))

    assert sync_llm.batches[0] == stream_llm.batches[0]
