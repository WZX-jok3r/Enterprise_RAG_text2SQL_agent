"""test_rag_executor.py - 检索/LLM 唯一出口的契约测试。

这些用例锁的是**"两份实现不会再次漂移"**：
    - 检索只产生一种产物形状（RagContext），sources 只有一种拼法
    - 历史只有一种渲染（剥脏字段 + 工具轨迹注记）
    - 流式只有一种错误语义（异常），两种客户端都被适配
"""

import pytest

from app.core.config import settings
from app.rag.executor import (
    REFUSE_NO_CONTEXT,
    TOOL_CONTEXT_NOTE,
    AnswerStreamError,
    RagContext,
    complete_text,
    history_messages,
    retrieve,
    stream_text,
)

CHUNKS = [
    {
        "text": "员工表：编号1，姓名Alice，所在城市Beijing。",
        "metadata": {"source": "data/hr.md", "format": "md"},
        "score": 0.88,
    },
    {
        "text": "差旅政策：一线城市住宿上限 600 元/晚。",
        "metadata": {"source": "docs/travel.md", "format": "md"},
        "score": 0.71,
    },
]


class FakeRetriever:
    def __init__(self, chunks=None, dropped=0):
        self.chunks = CHUNKS if chunks is None else chunks
        self.dropped = dropped
        self.calls = []

    def search(self, query, top_k=None, filter_dict=None):
        self.calls.append({"query": query, "top_k": top_k, "filter_dict": filter_dict})
        return self.chunks, self.dropped


class FakeStreamLLM:
    """provider.LLMClient 契约：stream 直接产出文本。"""

    model = "fake"

    def __init__(self, pieces=("你", "好")):
        self.pieces = pieces
        self.seen = None

    def stream(self, messages, temperature=None, max_tokens=None):
        self.seen = messages
        for p in self.pieces:
            yield p

    def complete(self, messages, temperature=None, max_tokens=None):
        return "答案", {"total": 3}


class FakeToolLLM:
    """ToolLLMClient 契约：stream_chat 产出事件字典。"""

    model = "fake-tool"

    def __init__(self, events=None):
        self.events = events if events is not None else [
            {"type": "delta", "text": "你"},
            {"type": "delta", "text": "好"},
            {"type": "done", "content": "你好", "tool_calls": [], "usage": {}},
        ]
        self.tools_arg = "UNSET"

    def stream_chat(self, messages, tools=None, temperature=None, max_tokens=None):
        self.tools_arg = tools
        for e in self.events:
            yield e


# ==================== retrieve ====================

def test_retrieve_returns_single_value_object():
    r = FakeRetriever(dropped=3)
    ctx = retrieve("问题", retriever=r, top_k=5)

    assert isinstance(ctx, RagContext)
    assert ctx.query == "问题"
    assert ctx.chunks == CHUNKS
    assert ctx.dropped == 3
    assert ctx.retrieved_count == 2
    assert ctx.has_context is True
    # sources 在这里一次成型，调用方不再各自 format_sources
    assert [s["source"] for s in ctx.sources] == ["hr.md", "travel.md"]
    assert ctx.sources[0]["score"] == 0.88


def test_retrieve_forwards_top_k_and_filter():
    """租户/版本过滤必须原样透传 —— 漏传就是跨租户泄露。"""
    r = FakeRetriever()
    flt = {"tenant_id": "t-1"}
    retrieve("问题", retriever=r, top_k=7, filter_dict=flt)

    assert r.calls == [{"query": "问题", "top_k": 7, "filter_dict": flt}]


def test_retrieve_defaults_top_k_from_settings():
    r = FakeRetriever()
    retrieve("问题", retriever=r)
    assert r.calls[0]["top_k"] == settings.top_k


def test_retrieve_preview_flag():
    r = FakeRetriever()
    with_preview = retrieve("问题", retriever=r, top_k=5, with_preview=True)
    without = retrieve("问题", retriever=r, top_k=5, with_preview=False)

    assert "text_preview" in with_preview.sources[0]
    assert "text_preview" not in without.sources[0]
    # 预览是截断的，但 chunks 本身不变
    assert with_preview.chunks == without.chunks


def test_retrieve_empty_is_not_an_error():
    ctx = retrieve("问题", retriever=FakeRetriever(chunks=[]), top_k=5)
    assert ctx.has_context is False
    assert ctx.sources == []
    assert ctx.retrieved_count == 0


def test_refuse_text_is_single_source_of_truth():
    assert REFUSE_NO_CONTEXT == "根据现有资料，未能找到相关信息。"


# ==================== history_messages ====================

def test_history_messages_strips_unknown_fields():
    """tool_context 不是合法 messages 字段，绝不能透传给上游。"""
    out = history_messages([
        {"role": "user", "content": "问", "tool_context": "不该出现的轨迹"},
        {"role": "assistant", "content": "答", "tool_context": ""},
    ])
    assert out == [
        {"role": "user", "content": "问"},
        {"role": "assistant", "content": "答"},
    ]


def test_history_messages_renders_tool_context_note():
    out = history_messages([
        {"role": "assistant", "content": "1042 人。", "tool_context": "SELECT count(*) -> 1042"},
    ])
    assert out[0]["content"] == f"1042 人。\n\n{TOOL_CONTEXT_NOTE}\nSELECT count(*) -> 1042"
    assert set(out[0]) == {"role", "content"}


def test_history_messages_can_disable_tool_context():
    out = history_messages(
        [{"role": "assistant", "content": "答", "tool_context": "轨迹"}],
        include_tool_context=False,
    )
    assert out == [{"role": "assistant", "content": "答"}]


def test_history_messages_limit_takes_tail():
    history = [{"role": "user", "content": f"q{i}"} for i in range(10)]
    out = history_messages(history, limit=6)
    assert [m["content"] for m in out] == [f"q{i}" for i in range(4, 10)]
    # limit=None 表示不截断（pipeline 的历史已由 SessionService 限长）
    assert len(history_messages(history, limit=None)) == 10


def test_history_messages_handles_none_and_empty():
    assert history_messages(None) == []
    assert history_messages([]) == []


# ==================== stream_text ====================

def test_stream_text_provider_contract():
    llm = FakeStreamLLM(("你", "", "好"))
    assert "".join(stream_text([{"role": "user", "content": "q"}], llm=llm)) == "你好"


def test_stream_text_tool_client_contract():
    llm = FakeToolLLM()
    assert list(stream_text([{"role": "user", "content": "q"}], llm=llm)) == ["你", "好"]
    # 最终答案生成**不能**再给工具（否则模型会在收尾时又想调工具）
    assert llm.tools_arg is None


def test_stream_text_tool_error_becomes_exception():
    """两种客户端的失败语义必须统一成异常，否则回退逻辑要写两遍。"""
    llm = FakeToolLLM([
        {"type": "delta", "text": "半"},
        {"type": "error", "message": "上游 504"},
    ])
    got = []
    with pytest.raises(AnswerStreamError) as ei:
        for piece in stream_text([{"role": "user", "content": "q"}], llm=llm):
            got.append(piece)
    assert got == ["半"]
    assert "504" in str(ei.value)


# ==================== complete_text ====================

def test_complete_text_provider_contract():
    answer, usage = complete_text([{"role": "user", "content": "q"}], llm=FakeStreamLLM())
    assert answer == "答案"
    assert usage["total"] == 3


def test_complete_text_tool_client_contract():
    class _Result:
        content = "工具侧答案"
        usage = {"total": 9}

    class _Llm:
        def chat_with_tools(self, messages, tools=None, temperature=None, max_tokens=None):
            assert tools is None
            return _Result()

    answer, usage = complete_text([{"role": "user", "content": "q"}], llm=_Llm())
    assert answer == "工具侧答案"
    assert usage == {"total": 9}
