"""/query（单轮问答）编排单测：注入 FakeRetriever / FakeLLM，验证结果键契约、
空召回拒答、以及发给 LLM 的 messages 形状。不触网、不查库。

它是 `RAGPipeline.query()` 的直系后继：Step 3 删掉 pipeline 时把这段编排原样抽到
`app/rag/oneshot.answer_once`。末尾的 golden 用例锁的是 **"抽 executor 前后
messages 逐字符不变"** —— 任何让 /query 发给 LLM 的内容发生漂移的改动都会红。
"""

from app.prompts.rag import SYSTEM_PROMPT
from app.rag.context_builder import build_context
from app.rag.oneshot import answer_once

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


class RecordingLLM(FakeLLM):
    """记录每次收到的 messages。"""

    def __init__(self):
        self.batches = []

    def complete(self, messages, temperature=None, max_tokens=None):
        self.batches.append(messages)
        return "这是答案", {"input": 1, "output": 2, "total": 3}


def test_result_contract():
    res = answer_once(
        "问题", top_k=5,
        retriever=FakeRetriever(_CHUNKS, dropped=2), llm=FakeLLM(),
    )

    assert set(res.keys()) == {
        "query", "answer", "sources", "retrieved_count", "hidden_count",
    }
    assert res["query"] == "问题"
    assert res["answer"] == "这是答案"
    assert res["retrieved_count"] == 1
    assert res["hidden_count"] == 2
    # /query 走 with_preview=True
    assert res["sources"][0]["source"] == "hr.md"
    assert "text_preview" in res["sources"][0]


def test_no_context_refuses_without_calling_llm():
    """空召回：确定性拒答，且**不调用模型**（省一次调用，也避免编造）。"""
    llm = RecordingLLM()
    res = answer_once("问题", retriever=FakeRetriever([], dropped=1), llm=llm)

    assert res["answer"] == "根据现有资料，未能找到相关信息。"
    assert res["sources"] == []
    assert res["retrieved_count"] == 0
    assert res["hidden_count"] == 1
    assert llm.batches == [], "空召回还去调模型了 —— 会白白多一次调用且可能幻觉"


def test_filter_dict_is_forwarded_to_retriever():
    """tenant 过滤由路由层（enforced_filter）算好传进来，这里必须原样透传。"""
    r = FakeRetriever(_CHUNKS)
    answer_once("问题", filter_dict={"tenant_id": "t1"}, retriever=r, llm=FakeLLM())
    assert r.calls[0][2] == {"tenant_id": "t1"}


def test_messages_are_system_plus_user():
    llm = RecordingLLM()
    answer_once("问题", top_k=5, retriever=FakeRetriever(_CHUNKS), llm=llm)

    messages = llm.batches[0]
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    # system 只由「prompt 模板 + context」构成，不许再拼别的东西
    assert messages[0]["content"] == SYSTEM_PROMPT.format(context=build_context(_CHUNKS))
    # 参考资料段落只出现一次（prompt 自己的示例里也含"【参考1】"，故匹配整行）
    assert messages[0]["content"].count("【参考1】来源: hr.md (md)") == 1
    assert messages[-1] == {"role": "user", "content": "问题"}
