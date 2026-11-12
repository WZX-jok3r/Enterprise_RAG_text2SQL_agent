"""追问改写（P1-2）的回归测试。

## 背景（评测集里的真实失败）

强制 SQL / 强制 RAG 把**用户原话**直接当检索词：追问句缺主语时，
Text2SQL 会反过来要求澄清指代（基线 mt-sql-01/05/06），
RAG 会打到上一个版本的片段（mt-rag-01 把 12 个月答成 24 个月）。

## 断言边界

- 纯函数：闸门（首轮 / 短句 / 指代词）与上下文渲染 —— 可穷举，最稳
- 改写调用：四种回退（skip / same / invalid / error）**必须返回原问题**
- 接入（关键，且都是确定性的）：
    * 两条强制路径收到的是**改写词**
    * llm_decide 路径**一次都不调**改写（否则是"多一次调用换零收益"）
    * 改写**不占** MAX_LLM_CALLS（否则预算紧时挤掉答案生成）
    * 最终答案模板**仍用原文**（否则用户看到的是被改写过的问句）
    * 元问题在改写之前就 return —— 两者互斥
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from app.agent.loop import AgentLoop
from app.agent.rewrite import (
    MAX_CONTEXT_CHARS,
    _clean,
    last_round,
    needs_rewrite,
    rewrite_query,
)
from app.agent.tools import AgentTools, ToolOutcome
from app.core.config import settings
from app.llm.tool_client import ToolChatResult

PRIOR_Q = "Sales 部门有多少人？"
PRIOR_A = "Sales 部门有 1042 人。"
PRIOR_TRACE = "已执行 SQL: SELECT COUNT(*) FROM employees\n  1042"
HISTORY: List[Dict[str, Any]] = [
    {"role": "user", "content": PRIOR_Q},
    {"role": "assistant", "content": PRIOR_A, "tool_context": PRIOR_TRACE},
]
FOLLOW_UP = "那 Engineering 呢？两个部门合起来一共多少人？"
REWRITTEN = "Engineering 部门有多少人，以及 Sales 和 Engineering 两个部门合计多少人"


class StubLLM:
    """可控的假模型：返回固定文本或直接抛异常。"""

    def __init__(self, content: str = "", error: Optional[Exception] = None) -> None:
        self.content = content
        self.error = error
        self.kwargs: Dict[str, Any] = {}

    def chat_with_tools(self, messages, tools=None, temperature=None, **kw):
        # 真实客户端的 temperature 是具名参数，不落在 **kw 里 —— 这里统一归档
        self.kwargs = {"tools": tools, "temperature": temperature, **kw}
        if self.error:
            raise self.error
        return ToolChatResult(content=self.content, tool_calls=[], finish_reason="stop")


class RewriteLLM(StubLLM):
    """首次调用给改写结果，之后给固定答案（按调用序号区分用途）。"""

    def __init__(self, rewritten: str = REWRITTEN, answer: str = "1010 人") -> None:
        super().__init__()
        self.rewritten = rewritten
        self.answer = answer
        self.calls: List[List[Dict[str, Any]]] = []

    def chat_with_tools(self, messages, tools=None, temperature=None, **kw):
        self.calls.append(messages)
        text = self.rewritten if len(self.calls) == 1 else self.answer
        return ToolChatResult(content=text, tool_calls=[], finish_reason="stop")

    @property
    def prompts(self) -> List[str]:
        return ["\n".join(str(m.get("content") or "") for m in msgs)
                for msgs in self.calls]


class RecordingTools(AgentTools):
    """记录工具调用与 sql_answer 收到的问题（不碰网络 / 数据库）。"""

    def __init__(self) -> None:  # 不初始化真实 Retriever
        self.calls: List[Dict[str, Any]] = []
        self.sql_questions: List[str] = []

    def dispatch(self, name, args, tenant_id=None, redact=None, actor=""):
        self.calls.append({"name": name, "args": args})
        return ToolOutcome(
            ok=True, name=name, observation="（检索到的片段）", summary="ok",
            payload={"chunks": [], "dropped": 0, "retrieved_count": 1, "sources": []},
        )

    def sql_answer(self, question, tenant_id=None, redact=None, actor=""):
        self.sql_questions.append(question)

        class R:
            ok = True
            sql = "SELECT COUNT(*) FROM employees WHERE department='Engineering'"
            columns = ["人数"]
            rows = [[1010]]
            row_count = 1
            elapsed_ms = 3
            truncated = False
            answer = "1010"
            needs_clarification = False
            clarification = ""
            attempts = 1
            errors: list = []
            redacted_columns: list = []

        return R()


def _run(question: str, history=HISTORY, llm=None, **loop_kw):
    tools = RecordingTools()
    llm = llm if llm is not None else RewriteLLM()
    loop = AgentLoop(tools=tools, llm=llm, **loop_kw)
    events = list(loop.run(question, history=list(history)))
    return tools, llm, events


def _ev(events: List[Dict[str, Any]], type_: str) -> Dict[str, Any]:
    return next((e for e in events if e.get("type") == type_), {})


# ==================== 闸门（省钱的那一半）====================

@pytest.mark.parametrize("question", [
    FOLLOW_UP,                     # 含"那 / 两个"
    "那两个部门一共多少人？",        # 指代词
    "还有呢",                       # 短句
])
def test_needs_rewrite_true_for_follow_ups(question):
    assert needs_rewrite(question, HISTORY) is True


@pytest.mark.parametrize("question", [
    "Engineering 部门的平均薪资是多少？",   # 长句、无指代词：本身就是完整问题
    "列出所有员工的工号和邮箱地址",
])
def test_needs_rewrite_false_for_standalone(question):
    assert needs_rewrite(question, HISTORY) is False


def test_needs_rewrite_false_on_first_turn():
    """首轮没有上文可补 —— 改写必然空转，不该花这次调用。"""
    assert needs_rewrite(FOLLOW_UP, []) is False


# ==================== 上下文渲染 ====================

def test_last_round_includes_tool_trace():
    ctx = last_round(HISTORY)
    assert PRIOR_Q in ctx and PRIOR_A in ctx
    assert PRIOR_TRACE in ctx, "指代（'这两个部门'）常常指向上一轮算出来的数字"


def test_last_round_keeps_only_latest():
    history = HISTORY + [
        {"role": "user", "content": "那 Engineering 呢？"},
        {"role": "assistant", "content": "1010 人。"},
    ]
    ctx = last_round(history)
    assert "那 Engineering 呢？" in ctx
    assert PRIOR_Q not in ctx, "只取最近一轮，早期轮次不进 prompt"


def test_last_round_truncated():
    history = [{"role": "user", "content": "问" * 2000},
               {"role": "assistant", "content": "答" * 2000}]
    assert len(last_round(history)) <= MAX_CONTEXT_CHARS


def test_clean_strips_prefix_and_fence():
    assert _clean("改写后的问题：「Engineering 部门有多少人」") == "Engineering 部门有多少人"
    assert _clean("```text\nEngineering 部门有多少人\n```") == "Engineering 部门有多少人"


# ==================== 改写调用：四种回退都必须给出可用 query ====================

def test_rewrite_returns_query_with_cheap_params():
    llm = StubLLM("Engineering 部门有多少人")
    assert rewrite_query(FOLLOW_UP, HISTORY, llm) == ("Engineering 部门有多少人", "rewritten")
    # 成本约束：temperature=0、小 max_tokens、独立 scene（便于归因）
    assert llm.kwargs.get("temperature") == 0.0
    assert llm.kwargs.get("max_tokens") == 64
    assert llm.kwargs.get("scene") == "query_rewrite"


def test_rewrite_same_question_is_not_a_rewrite():
    llm = StubLLM(FOLLOW_UP)
    assert rewrite_query(FOLLOW_UP, HISTORY, llm) == (FOLLOW_UP, "same")


def test_rewrite_rejects_explanation():
    """模型输出"解释"时不能当检索词（比原问题长得多，只会污染召回）。"""
    llm = StubLLM("原问题已经完整，不需要改写。" * 20)
    assert rewrite_query(FOLLOW_UP, HISTORY, llm) == (FOLLOW_UP, "invalid")


def test_rewrite_error_falls_back_to_original():
    llm = StubLLM(error=TimeoutError("upstream slow"))
    assert rewrite_query(FOLLOW_UP, HISTORY, llm) == (FOLLOW_UP, "error")


def test_rewrite_without_history_is_skipped():
    llm = StubLLM("不应该被调用")
    assert rewrite_query(FOLLOW_UP, [], llm) == (FOLLOW_UP, "skip")


# ==================== 接入：两条强制路径 / 三条约束 ====================

def test_forced_sql_uses_rewritten_query():
    tools, _, events = _run(FOLLOW_UP)
    assert _ev(events, "route").get("target") == "sql"
    assert tools.sql_questions == [REWRITTEN], "Text2SQL 应拿到改写后的问题"
    assert _ev(events, "rewrite").get("rewritten") == REWRITTEN


def test_forced_rag_uses_rewritten_query():
    tools, _, events = _run("那 V0.9 版本呢？质保多长时间？")
    assert _ev(events, "route").get("target") == "rag"
    kb = next(c for c in tools.calls if c["name"] == "kb_search")
    assert kb["args"]["query"] == REWRITTEN


def test_answer_prompt_still_uses_original_question():
    """只改检索词，不改回答对象 —— 否则用户看到的是被改写过的问句。"""
    _, llm, _ = _run(FOLLOW_UP)
    answer_prompt = llm.prompts[-1]
    assert FOLLOW_UP in answer_prompt
    assert REWRITTEN not in answer_prompt


def test_llm_decide_path_is_not_rewritten():
    """LLM 自选工具那条路本来就带历史，再改写一次是纯浪费。"""
    _, llm, events = _run("你们好呀，今天忙不忙")
    assert _ev(events, "route").get("target") == "auto"
    assert _ev(events, "rewrite") == {}
    assert len(llm.calls) == 1, "只应有那一次答案/工具选择调用"


def test_rewrite_does_not_consume_llm_budget():
    """改写不计入 MAX_LLM_CALLS：否则预算紧时会把答案生成的配额挤掉。"""
    _, llm, events = _run(FOLLOW_UP, max_llm_calls=1)
    stats = _ev(events, "done")["agent_stats"]
    assert len(llm.calls) == 2, "一次改写 + 一次答案"
    assert stats["llm_calls"] == 1, "改写不进 llm_calls"
    assert stats["rewrite_calls"] == 1
    assert stats["rewrite_reason"] == "rewritten"
    assert _ev(events, "done")["answer"] == "1010 人"


def test_meta_question_is_never_rewritten():
    """元问题在改写之前就 return —— 两者互斥（元问题不检索，改写无用）。"""
    _, llm, events = _run("我上一次问的是什么？")
    assert _ev(events, "route").get("target") == "meta"
    assert _ev(events, "rewrite") == {}
    assert _ev(events, "done")["agent_stats"]["rewrite_calls"] == 0
    assert len(llm.calls) == 1


def test_rewrite_failure_still_answers():
    """fail-open：改写失败只能是"少省一次 token"，绝不能变成回答失败。"""
    class Boom(RewriteLLM):
        def chat_with_tools(self, messages, tools=None, temperature=None, **kw):
            raise RuntimeError("upstream 500")

    tools, _, events = _run(FOLLOW_UP, llm=Boom())
    done = _ev(events, "done")
    assert done["answer"]
    assert done["agent_stats"]["rewrite_reason"] == "error"
    assert tools.sql_questions == [FOLLOW_UP], "失败时按原问题检索（fail-open）"


def test_rewrite_can_be_disabled(monkeypatch):
    monkeypatch.setattr(settings, "query_rewrite_enabled", False)
    tools, llm, events = _run(FOLLOW_UP)
    assert len(llm.calls) == 1, "关闭后不该多出改写调用"
    assert tools.sql_questions == [FOLLOW_UP]
    assert _ev(events, "done")["agent_stats"]["rewrite_reason"] == "disabled"
