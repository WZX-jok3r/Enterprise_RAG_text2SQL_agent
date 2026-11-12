"""Agent 最终答案生成必须带对话历史（回归：历史曾只在关卡二注入）。

## 本文件针对的真实缺陷

历史注入原先只写在 `build_messages` 里，而它**只服务关卡二**（LLM 工具循环）。
强制 SQL / 强制 RAG / 全部降级路径的收尾都走 `_compose_answer`，那里只喂
"本轮问题 + 工具观察"。于是同一个会话里"记不记得上文"取决于路由结果：

    我上一次问的是什么？  -> 命中"是什么" -> RAG 强路由 -> 看不到历史 -> "未找到"
    我刚才问了什么        -> 未命中规则 -> LLM 循环   -> 正常回答

同一语义两种问法结果相反，用户视角就是"这系统没有记忆"。
连带后果：L-022 加的 tool_context 在最典型的追问（如"那 Engineering 呢？
两个部门一共多少人" -> 命中聚合+列名 -> 强制 SQL）上同样失效。

## 断言口径

只断言**机制**：历史与本轮工具轨迹确实进入了最终 prompt。
不断言模型能否答对 —— 那是概率性的（L-012 教训），写进去只会得到 flaky 测试。
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List

import pytest

from app.agent.loop import AgentLoop
from app.agent.router import Route, route_by_rules
from app.agent.tools import AgentTools, ToolOutcome
from app.llm.tool_client import ToolChatResult
from app.prompts.agent import render_answer_prompt

PRIOR_QUESTION = "Sales 部门有多少人？"
PRIOR_ANSWER = "Sales 部门有 1042 人。"
PRIOR_TRACE = (
    "已执行 SQL: SELECT COUNT(*) FROM employees WHERE department='Sales'\n"
    "列: 人数\n  1042"
)

HISTORY: List[Dict[str, Any]] = [
    {"role": "user", "content": PRIOR_QUESTION},
    {"role": "assistant", "content": PRIOR_ANSWER, "tool_context": PRIOR_TRACE},
]


class CapturingLLM:
    """记录每次调用收到的 messages，然后直接收尾（不真调模型）。"""

    def __init__(self) -> None:
        self.calls: List[List[Dict[str, Any]]] = []

    def chat_with_tools(self, messages, tools=None, temperature=None):
        self.calls.append(copy.deepcopy(messages))
        return ToolChatResult(content="好的。", tool_calls=[], finish_reason="stop")

    @property
    def last_messages(self) -> List[Dict[str, Any]]:
        assert self.calls, "本次运行没有调用 LLM —— 断言无从谈起"
        return self.calls[-1]

    @property
    def last_prompt(self) -> str:
        return "\n".join(str(m.get("content") or "") for m in self.last_messages)


class RagTools(AgentTools):
    """kb_search 恒返回一条假片段（不碰向量库）；sql_answer 不存在。"""

    def __init__(self) -> None:  # 不初始化真实 Retriever
        pass

    def dispatch(self, name, args, tenant_id=None, redact=None, actor=""):
        return ToolOutcome(
            ok=True, name=name, observation="（检索到的片段）", summary="ok",
            payload={"chunks": [], "dropped": 0, "retrieved_count": 1, "sources": []},
        )


class SqlTools(RagTools):
    """在 RagTools 之上补一个假的 Text2SQL 结果（不碰数据库）。"""

    def sql_answer(self, question, tenant_id=None, redact=None, actor=""):
        class Result:
            ok = True
            sql = "SELECT COUNT(*) FROM employees WHERE department='Engineering'"
            columns = ["人数"]
            rows = [[1010]]
            row_count = 1
            elapsed_ms = 4
            truncated = False
            answer = "1010"
            needs_clarification = False
            clarification = ""
            attempts = 1
            errors: list = []
            redacted_columns: list = []

        return Result()


def _run(tools, question: str, forced=None, wall_clock: float = 60.0):
    llm = CapturingLLM()
    loop = AgentLoop(tools=tools, llm=llm, wall_clock=wall_clock)
    events = list(loop.run(question, history=HISTORY, forced_route=forced))
    return llm, events


# ==================== 三条路径：最终答案都要看到历史 ====================

def test_forced_rag_answer_prompt_carries_history():
    """强制 RAG（"我上一次问的是什么"就走这条）必须带历史。"""
    llm, _ = _run(RagTools(), "我上一次问的是什么？", forced=Route.RAG)
    prompt = llm.last_prompt
    assert PRIOR_QUESTION in prompt, "上一轮用户问题没进最终答案 prompt"
    assert PRIOR_ANSWER in prompt, "上一轮回答没进最终答案 prompt"


def test_forced_sql_answer_prompt_carries_history():
    """强制 SQL 也要带历史 —— 追问常常接在这条路径的结果上。"""
    llm, _ = _run(SqlTools(), "那 Engineering 呢？", forced=Route.SQL)
    assert PRIOR_QUESTION in llm.last_prompt


def test_forced_sql_answer_prompt_carries_tool_trace():
    """上一轮的工具轨迹（SQL + 结果行）在强制路径上同样要可见。"""
    llm, _ = _run(SqlTools(), "那 Engineering 呢？", forced=Route.SQL)
    prompt = llm.last_prompt
    assert "上一轮工具执行记录" in prompt, "缺少注记措辞，模型可能不懂这段是什么"
    assert "SELECT COUNT(*) FROM employees" in prompt
    assert "1042" in prompt, "上一轮结果行没进 prompt，合计类追问会算不出来"


def test_degraded_path_carries_history():
    """降级路径（超时 -> 退回强制 RAG）同样要带历史。

    问句特意选未命中规则信号的（否则会被直接强路由，根本进不了 LLM 循环）。
    wall_clock 取负数：`monotonic()` 同刻比较不一定严格大于 0 秒后的 deadline。
    """
    llm, events = _run(RagTools(), "那 Engineering 呢？", wall_clock=-1.0)
    assert any(e.get("type") == "degraded" for e in events), "未进入降级路径，断言前提不成立"
    assert PRIOR_QUESTION in llm.last_prompt


def test_meta_question_goes_through_forced_rag_and_still_sees_history():
    """原缺陷的完整复现路径：规则判成 RAG 强路由，但答案仍要看得到上一轮。"""
    question = "我上一次问的是什么？"
    assert route_by_rules(question).route is Route.RAG, "前提变了：该问句不再走强制 RAG"

    llm, _ = _run(RagTools(), question)
    assert PRIOR_QUESTION in llm.last_prompt


# ==================== 结构契约 ====================

def test_history_does_not_duplicate_current_question():
    """API 层取的是"进入前"的历史（不含本轮问题）；本轮问题在 prompt 里只能出现一次。"""
    question = "我上一次问的是什么？"
    llm, _ = _run(RagTools(), question, forced=Route.RAG)
    users = [m for m in llm.last_messages if m.get("role") == "user"]
    assert users[0]["content"] == PRIOR_QUESTION, "历史顺序被打乱"
    assert sum(question in u["content"] for u in users) == 1, "本轮问题被重复注入"
    assert question in users[-1]["content"], "本轮问题必须作为最后一条 user 出现"


def test_history_window_is_still_six():
    """最终答案的历史窗口与关卡二保持一致（同一渲染函数），不能悄悄变成全量。"""
    llm = CapturingLLM()
    loop = AgentLoop(tools=RagTools(), llm=llm)
    long_history = [{"role": "user", "content": f"旧{i}"} for i in range(20)]
    list(loop.run("我上一次问的是什么？", history=long_history, forced_route=Route.RAG))
    prompt = llm.last_prompt
    assert "旧19" in prompt and "旧0" not in prompt


@pytest.mark.parametrize("question", ["我上一次问的是什么？", "我刚才问了什么"])
def test_prompt_template_allows_answering_from_history(question: str):
    """模板必须显式允许"资料没命中但历史里有答案"，否则模型照样拒答。

    这条断言的是文案契约：改了措辞就要重新评估多轮行为（见 docs/经验教训.md L-022）。
    """
    prompt = render_answer_prompt(question, "知识库中未找到相关内容。")
    assert "对话历史" in prompt
    assert "不要声称未找到" in prompt
