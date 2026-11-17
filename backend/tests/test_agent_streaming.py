"""test_agent_streaming.py - Agent 答案"真流式"的行为契约。

改造前：`_finalize` 一次性 `yield ev_delta(整段)` —— 前端看到的是
"空白 8~15 秒 -> 整段答案瞬间出现"，即**假流式**。
改造后：答案分片产出，首片立即发。

这里锁四件事（都是可以回归的**行为**，不是实现细节）：
    1. 分片是真的：delta 帧数与"流被消费的进度"对得上，不是先攒后发
    2. 文本不变：delta 拼接 == done.answer，且逐字等于模型输出
    3. 帧格式不变：delta 仍只有 {type, text}（events.py 的冻结契约）
    4. 半句话规则：0 片可回退，≥1 片不再追加
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from app.agent.loop import AgentLoop
from app.agent.tools import ToolOutcome
from app.llm.tool_client import ToolCall, ToolChatResult

# 规则强路由到 RAG 的问题（含"多久"等文档词），可确定性地走到 kb_search
QUESTION = "路由器的质保期是多久？"
KB_TEXT = "知识库内容：路由器质保 24 个月"


class FakeTools:
    """只实现强制 RAG 路径需要的两个方法。"""

    def __init__(self, kb_ok: bool = True) -> None:
        self.kb_ok = kb_ok
        self.calls: List[str] = []

    def dispatch(self, name, args, tenant_id=None, redact=None, actor="") -> ToolOutcome:
        self.calls.append(name)
        chunks = [{"text": KB_TEXT, "score": 0.9, "metadata": {"source": "warranty.md"}}]
        return ToolOutcome(
            ok=self.kb_ok, name=name,
            observation=KB_TEXT if self.kb_ok else "检索失败",
            summary="检索到 1 段相关内容" if self.kb_ok else "检索失败",
            payload={"chunks": chunks, "dropped": 2, "retrieved_count": 1,
                     "sources": [{"source": "warranty.md"}]},
        )

    def sql_answer(self, question, tenant_id=None, redact=None, actor=""):
        raise AssertionError("强制 RAG 路径不应调用 SQL 引擎")


class StreamingLLM:
    """实现 `stream_chat` 契约（ToolLLMClient 的形状）。

    `consumed` 记录"已经吐给调用方"的片段数 —— 这是断言真流式的关键：
    若实现先攒完再发，那么第一帧 delta 到达时 consumed 会等于总片数。
    """

    def __init__(self, pieces: List[str], fail_after: Optional[int] = None,
                 error: str = "上游超时", empty: bool = False) -> None:
        self.pieces = [] if empty else list(pieces)
        self.fail_after = fail_after
        self.error = error
        self.consumed: List[str] = []
        self.tools_arg: Any = "UNSET"
        self.received: List[List[Dict[str, Any]]] = []

    def stream_chat(self, messages, tools=None, temperature=None, max_tokens=None, **kw):
        self.received.append(list(messages))
        self.tools_arg = tools
        for i, piece in enumerate(self.pieces):
            if self.fail_after is not None and i >= self.fail_after:
                raise RuntimeError(self.error)
            self.consumed.append(piece)
            yield {"type": "delta", "text": piece}
        if self.fail_after is not None and self.fail_after >= len(self.pieces):
            raise RuntimeError(self.error)
        yield {"type": "done", "content": "".join(self.pieces),
               "tool_calls": [], "finish_reason": "stop", "usage": {"total_tokens": 1}}

    def chat_with_tools(self, messages, tools=None, temperature=None, **kw) -> ToolChatResult:
        # 强制 RAG 路径不应走到这里；万一走到，也要给出可读结果而不是炸
        return ToolChatResult(content="".join(self.pieces), tool_calls=[], finish_reason="stop")


def run(llm: StreamingLLM, tools: Optional[FakeTools] = None) -> List[Dict[str, Any]]:
    loop = AgentLoop(tools=tools or FakeTools(), llm=llm)
    return list(loop.run(QUESTION))


def deltas(events: List[Dict[str, Any]]) -> List[str]:
    return [e["text"] for e in events if e["type"] == "delta"]


def answer(events: List[Dict[str, Any]]) -> str:
    return next(e for e in events if e["type"] == "done")["answer"]


# ==================== 1. 真的是流式 ====================

def test_first_delta_arrives_before_the_stream_finishes():
    """TTFT 的实质断言：第一帧 delta 到达时，模型只吐了首片。

    这条用例在"先攒后发"的实现下必然失败 —— 那时 consumed 会是全部片段。
    ⚠️ 必须**惰性**遍历：先 list() 再检查等于跑完整个流，测不出任何东西。
    """
    llm = StreamingLLM([f"片段{i}，" for i in range(10)])
    seen_delta = False
    for e in AgentLoop(tools=FakeTools(), llm=llm).run(QUESTION):
        if e["type"] == "delta":
            seen_delta = True
            break

    assert seen_delta
    assert len(llm.consumed) == 1, f"首帧发出时应只消费 1 片，实际 {len(llm.consumed)}"


def test_long_text_is_split_into_several_frames():
    llm = StreamingLLM(["字"] * 200)
    events = run(llm)
    frames = deltas(events)

    assert len(frames) > 1, "多片答案必须分成多帧（否则仍是假流式）"
    # 合并阈值 24 字 -> 200 字不应产生 200 帧（否则每个 token 一次 SSE）
    assert len(frames) <= 20, f"帧数过多: {len(frames)}"
    assert "".join(frames) == "字" * 200


def test_frame_format_is_frozen():
    """delta 帧只有 {type, text} —— 前端与事件契约都依赖这一点。"""
    events = run(StreamingLLM(["甲", "乙"]))
    for e in events:
        if e["type"] == "delta":
            assert set(e.keys()) == {"type", "text"}


def test_answer_equals_concatenated_deltas():
    llm = StreamingLLM(["路由器", "质保", " 24 ", "个月。"])
    events = run(llm)

    assert "".join(deltas(events)) == "路由器质保 24 个月。"
    assert answer(events) == "路由器质保 24 个月。"


def test_final_answer_uses_stream_path_not_chat_with_tools():
    """收尾生成必须走流式通道，且**不给工具**（否则模型会又想调工具）。"""
    llm = StreamingLLM(["24 个月"])
    run(llm)

    assert llm.received, "必须调用过模型"
    assert llm.tools_arg is None
    # 收尾的 prompt 带工具观察
    assert KB_TEXT in llm.received[-1][-1]["content"]


# ==================== 2. 半句话规则 ====================

def test_zero_pieces_failure_falls_back_to_observation():
    """一片都没吐出来 -> 可以安全回退（用户什么都没看到）。"""
    llm = StreamingLLM(["未使用"], fail_after=0)
    events = run(llm)

    assert answer(events) == KB_TEXT
    assert "".join(deltas(events)) == KB_TEXT


def test_partial_failure_does_not_append_fallback():
    """已吐出内容后失败 -> 不再追加，宁可短也不拼接两段话。"""
    llm = StreamingLLM(["路由器质保", "后续内容"], fail_after=1)
    events = run(llm)

    assert answer(events) == "路由器质保"
    assert KB_TEXT not in answer(events)


def test_empty_generation_falls_back():
    """模型返回空串也算失败：不能让用户看到空答案。"""
    llm = StreamingLLM([], empty=True)
    events = run(llm)
    assert answer(events) == KB_TEXT


def test_tool_failure_short_circuits_without_calling_llm():
    """工具明确失败时不应再让模型编答案（分支 2）。"""
    llm = StreamingLLM(["不该被调用"])
    events = run(llm, tools=FakeTools(kb_ok=False))

    assert answer(events) == "检索失败"
    assert llm.received == []


# ==================== 3. 成本口径 ====================

def test_streaming_counts_as_one_llm_call():
    """流式生成仍然是"一次模型调用"，成本上界口径不能变。"""
    llm = StreamingLLM(["24 个月"])
    events = run(llm)
    stats = next(e for e in events if e["type"] == "done")["agent_stats"]

    assert stats["llm_calls"] == 1
    assert stats["tool_calls"] == 1


def test_llm_calls_budget_still_short_circuits_streaming():
    """成本上界已到时直接回观察文本，连流式都不启动。"""
    llm = StreamingLLM(["不该被调用"])
    loop = AgentLoop(tools=FakeTools(), llm=llm, max_llm_calls=0)
    events = list(loop.run(QUESTION))

    assert answer(events) == KB_TEXT
    assert llm.received == []


# ==================== 4. 会话落库仍然拿得到完整答案 ====================

def test_done_frame_carries_full_answer_and_tool_context():
    """API 层用 done.answer 落库、用 _tool_context 供下一轮 —— 两者都要完整。"""
    llm = StreamingLLM(["24", " 个月"])
    events = run(llm)
    done = next(e for e in events if e["type"] == "done")

    assert done["answer"] == "24 个月"
    assert "_tool_context" in done
    # 强制 RAG 没有 SQL，因此轨迹为空串（调用方据此不写该字段）
    assert done["_tool_context"] == ""


def test_events_are_json_serializable():
    """SSE 层直接 json.dumps 每个事件，任何不可序列化的对象都会在这里暴露。"""
    llm = StreamingLLM(["24 个月"])
    for e in run(llm):
        json.dumps(e, ensure_ascii=False)
