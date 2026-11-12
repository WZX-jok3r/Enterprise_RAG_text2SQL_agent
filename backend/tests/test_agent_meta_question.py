"""元问题（询问对话自身）识别与作答的回归测试。

## 背景（P1 基线暴露的问题）

"我上一次问的是什么？"里含"是什么"，命中 `DOC_WORDS` -> 被规则层判成 RAG 强路由
-> 白做一次向量检索，把无关文档塞进上下文。P0 修好历史注入之后**答案是对的**，
但代价是每轮多一次检索，且上下文被稀释（实测基线：4 条元问题 4/4 都如此）。

## 本文件的断言边界

- 纯函数：判据的**正例与反例**（反例比正例更重要 —— 误判会让真实业务问题跳过检索）
- 集成：元问题**不调用任何工具**、路由事件为 meta、最终 prompt 里带历史
- 边界：**首轮没有历史时不走 meta**（没有上文可依，应让模型如实说明）
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List

import pytest

from app.agent.loop import AgentLoop
from app.agent.router import is_meta_question, route_by_rules
from app.agent.tools import AgentTools, ToolOutcome
from app.llm.tool_client import ToolChatResult

PRIOR_QUESTION = "Sales 部门有多少人？"
PRIOR_ANSWER = "Sales 部门有 1042 人。"
HISTORY: List[Dict[str, Any]] = [
    {"role": "user", "content": PRIOR_QUESTION},
    {"role": "assistant", "content": PRIOR_ANSWER, "tool_context": None},
]


class FakeLLM:
    def __init__(self, reply: str = "您上一次问的是：「Sales 部门有多少人？」") -> None:
        self.reply = reply
        self.calls: List[List[Dict[str, Any]]] = []

    def chat_with_tools(self, messages, tools=None, temperature=None):
        self.calls.append(copy.deepcopy(messages))
        return ToolChatResult(content=self.reply, tool_calls=[], finish_reason="stop")

    @property
    def last_prompt(self) -> str:
        return "\n".join(str(m.get("content") or "") for m in self.calls[-1])


class CountingTools(AgentTools):
    """记录 dispatch 调用次数 —— 元问题的核心断言是“一次都没调”。"""

    def __init__(self) -> None:
        self.calls: List[str] = []

    def dispatch(self, name, args, tenant_id=None, redact=None, actor=""):
        self.calls.append(name)
        return ToolOutcome(
            ok=True, name=name, observation="（检索到的片段）", summary="ok",
            payload={"chunks": [], "dropped": 0, "retrieved_count": 1, "sources": []},
        )


def _run(question: str, history, llm=None, max_llm_calls: int = 8):
    tools, llm = CountingTools(), llm or FakeLLM()
    loop = AgentLoop(tools=tools, llm=llm, max_llm_calls=max_llm_calls)
    events = list(loop.run(question, history=history))
    route_ev = next((e for e in events if e.get("type") == "route"), {})
    done = next((e for e in events if e.get("type") == "done"), {})
    return tools, llm, route_ev, done


# ==================== 判据：正例 ====================

@pytest.mark.parametrize("question", [
    "我上一次问的是什么？",
    "我刚才问了什么",
    "上面那个问题再问一遍",
    "把上一个问题原样复述一遍",
    "我上一轮说了啥",
    "重新说一遍",
])
def test_meta_question_recognized(question):
    assert is_meta_question(question) is True


# ==================== 判据：反例（更重要）====================

@pytest.mark.parametrize("question", [
    "Sales 部门有多少人？",
    # 关键反例："上次"（不带"我/问"）是真实业务词，收进来会误判
    "上次采购的合同金额是多少？",
    "这个月的销售政策有什么变化？",
    "复述一下条款内容",
    "上一季度的营收是多少",
    "工程部门的人数是多少",
])
def test_business_question_not_treated_as_meta(question):
    assert is_meta_question(question) is False


# ==================== 集成：不检索、走 meta、看得到历史 ====================

def test_meta_question_skips_all_tools():
    tools, _, route_ev, done = _run("我上一次问的是什么？", HISTORY)
    assert tools.calls == [], f"元问题不应调用任何工具，实际调了 {tools.calls}"
    assert route_ev.get("target") == "meta"
    assert done.get("answer"), "必须给出答案"


def test_meta_route_overrides_rule_decision():
    """规则会把它判成 RAG（"是什么"）；meta 必须在分派上覆盖它，但原因里保留原判。"""
    assert route_by_rules("我上一次问的是什么？").route.value == "rag"
    _, _, route_ev, _ = _run("我上一次问的是什么？", HISTORY)
    assert route_ev.get("target") == "meta"
    assert "rag" in route_ev.get("reason", ""), "reason 应保留规则原判，便于排查"


def test_meta_answer_uses_history():
    _, llm, _, _ = _run("我上一次问的是什么？", HISTORY)
    prompt = llm.last_prompt
    assert PRIOR_QUESTION in prompt, "历史没进元问题 prompt"
    assert "不需要检索知识库" in prompt, "用了通用答案模板而不是元问题模板"


def test_meta_answer_is_model_output():
    _, _, _, done = _run("我刚才问了什么", HISTORY)
    assert done["answer"].startswith("您上一次问的是")


def test_meta_does_not_web_search_even_for_rag_like_wording():
    """"上面那个问题再问一遍"这类也含文档词，同样不能触发检索。"""
    tools, _, route_ev, _ = _run("上面那个问题再问一遍", HISTORY)
    assert route_ev.get("target") == "meta"
    assert tools.calls == []


# ==================== 边界：首轮没有历史 ====================

def test_first_turn_meta_is_not_shortcut():
    """首轮问"我上次问了什么"没有上文可依 —— 仍走正常流程，由模型说明无上文。"""
    tools, _, route_ev, done = _run("我上一次问的是什么？", history=[])
    assert route_ev.get("target") != "meta"
    assert tools.calls, "首轮不应被 meta 短路（否则模型无法如实说明没有上文）"
    assert done.get("answer")


# ==================== 成本上界：确定性回退 ====================

def test_meta_falls_back_to_prior_question_at_budget_limit():
    """LLM 预算耗尽时直接回上一轮问题原文 —— 确定性，且对这类问题几乎总是对的。"""
    tools, llm, _, done = _run("我上一次问的是什么？", HISTORY, max_llm_calls=0)
    assert tools.calls == []
    assert llm.calls == [], "预算已满不该再调模型"
    assert done["answer"] == PRIOR_QUESTION


def test_meta_llm_failure_falls_back_not_crash():
    class Boom(FakeLLM):
        def chat_with_tools(self, messages, tools=None, temperature=None):
            raise RuntimeError("upstream 500")

    _, _, _, done = _run("我上一次问的是什么？", HISTORY, llm=Boom())
    assert done["answer"] == PRIOR_QUESTION
