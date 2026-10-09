"""多轮对话的工具上下文（决定性断言，**不依赖 LLM 行为**）。

## 本文件针对的真实缺陷

会话原先只存 `role + content`（问题 + 自然语言答案），追问时模型
看不到上一轮执行过什么查询、拿到过哪些行，只能重新推导。
实测（真实 LLM，见 docs/经验教训.md L-022）：

    第 1 轮「Sales 部门有多少人？」           -> 1042
    第 2 轮「那 Engineering 呢？两个部门合起来一共多少人？」
          -> 只答出 Engineering=1010，**合计算不出来**（自称"资料未找到"）

而 1042 就在它的上下文里 —— 缺的不是数字，而是"这些数字怎么来的"。

## 修复与边界（重要）

修复：把本轮的工具轨迹（执行的 SQL + 结果行）存进 `Message.tool_context`
（**独立列**，不拼进 content，避免污染前端对话显示），
下一轮构造 prompt 时附在对应的 assistant 消息里。

⚠️ **本文件只断言机制，不断言模型的算术成功率。**
实测同一段对话连跑多次，模型是否用上这份轨迹是**概率性的**
（曾观测到 1/3、2/5 等不同结果）—— 把它写成确定性断言会得到 flaky 测试
（L-012 的教训）。机制正确性是确定性的，那才是该锁的东西。
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from app.agent.loop import MAX_TRACE_ROWS, AgentLoop, AgentState
from app.agent.tools import AgentTools, ToolOutcome
from app.llm.tools import ToolChatResult


class CapturingLLM:
    """记录每次调用收到的 messages，然后直接收尾（不真调模型）。"""

    def __init__(self) -> None:
        self.calls: List[List[Dict[str, Any]]] = []

    def chat_with_tools(self, messages, tools=None, temperature=None):
        import copy
        self.calls.append(copy.deepcopy(messages))
        return ToolChatResult(content="收到。", tool_calls=[], finish_reason="stop")


class FakeTools(AgentTools):
    def __init__(self, sql_rows=None, sql="SELECT 1") -> None:
        self._rows = sql_rows or []
        self._sql = sql

    def dispatch(self, name, args, tenant_id=None, redact=None, actor=""):
        return ToolOutcome(ok=True, name=name, observation="ok", summary="ok",
                           payload={"chunks": [], "dropped": 0,
                                    "retrieved_count": 0, "sources": []})


def _state(**kw) -> AgentState:
    return AgentState(question=kw.pop("question", "q"), **kw)


# ==================== 轨迹渲染 ====================

class TestBuildToolContext:
    def test_empty_when_no_query(self):
        assert AgentLoop.build_tool_context(_state()) == ""

    def test_includes_sql_and_rows(self):
        st = _state()
        st.executed_sql.append("SELECT COUNT(*) FROM employees WHERE department='Sales'")
        st.sql_result_columns = ["销售部门人数"]
        st.sql_result_rows = [[1042]]
        ctx = AgentLoop.build_tool_context(st)
        assert "SELECT COUNT(*) FROM employees" in ctx
        assert "1042" in ctx
        assert "销售部门人数" in ctx, "列名应一并带上，否则模型不知道数字是什么"

    def test_keeps_only_last_sql(self):
        """只留最后一条 SQL：追问通常接着最近一次查询，全留会膨胀 prompt。"""
        st = _state()
        st.executed_sql.extend(["SELECT 1", "SELECT 2", "SELECT 3"])
        ctx = AgentLoop.build_tool_context(st)
        assert "SELECT 3" in ctx
        assert "SELECT 1" not in ctx

    def test_null_cell_rendered(self):
        st = _state()
        st.executed_sql.append("SELECT 1")
        st.sql_result_columns = ["a", "b"]
        st.sql_result_rows = [[None, "x"]]
        assert "NULL" in AgentLoop.build_tool_context(st)

    def test_long_cell_truncated(self):
        st = _state()
        st.executed_sql.append("SELECT 1")
        st.sql_result_columns = ["a"]
        st.sql_result_rows = [["y" * 200]]
        ctx = AgentLoop.build_tool_context(st)
        assert "..." in ctx
        assert "y" * 200 not in ctx, "超长单元格应截断，避免撑爆 prompt"


# ==================== 注入 prompt ====================

class TestTraceInjectedIntoPrompt:
    """直接用 `build_messages` 断言注入，**不依赖路由走到 LLM 循环**。

    ⚠️ 踩过的坑：一开始我用真实问题（"Sales 有多少人？"）驱动 AgentLoop，
    但这类问题会被**规则强路由到 SQL**，根本不进 LLM 循环，
    于是 `llm.calls` 为空、测试拿到 IndexError ——
    测的其实是路由，不是注入。改用纯函数断言，确定且直接。
    """

    def test_prior_trace_appears_in_next_turn(self):
        """核心断言：上一轮轨迹必须出现在下一轮的 prompt 里。"""
        state = _state(question="那 Engineering 呢？")
        state.history = [
            {"role": "user", "content": "Sales 部门有多少人？"},
            {"role": "assistant", "content": "Sales 部门有 1042 人。",
             "tool_context": "已执行 SQL: SELECT COUNT(*) FROM employees\n  1042"},
        ]
        joined = "\n".join(str(m.get("content", ""))
                           for m in AgentLoop.build_messages(state))
        assert "SELECT COUNT(*) FROM employees" in joined, "上一轮 SQL 未进入 prompt"
        assert "1042" in joined, "上一轮结果未进入 prompt"
        assert "上一轮工具执行记录" in joined, "缺少注记措辞，模型可能不懂这段是什么"

    def test_history_without_trace_is_unchanged(self):
        """老会话（没有 tool_context）行为不变 —— 向后兼容。"""
        state = _state(question="你好")
        state.history = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好，有什么可以帮你？"},
        ]
        msgs = AgentLoop.build_messages(state)
        asst = next(m for m in msgs if m.get("role") == "assistant")
        assert asst["content"] == "你好，有什么可以帮你？"

    def test_user_messages_never_polluted(self):
        """轨迹只附在 assistant 消息上，绝不污染 user 消息。"""
        state = _state(question="问三")
        state.history = [
            {"role": "user", "content": "问一"},
            {"role": "assistant", "content": "答一", "tool_context": "SQL: SELECT 1"},
            {"role": "user", "content": "问二"},
        ]
        for m in AgentLoop.build_messages(state):
            if m.get("role") == "user":
                assert "SQL: SELECT 1" not in str(m.get("content", ""))

    def test_only_last_six_history_items_used(self):
        """历史只取最近 6 条（控制 prompt 体积）。"""
        state = _state(question="当前问题")
        state.history = [{"role": "user", "content": f"旧{i}"} for i in range(20)]
        msgs = AgentLoop.build_messages(state)
        contents = [m["content"] for m in msgs]
        assert "旧19" in contents and "旧0" not in contents
        assert contents[-1] == "当前问题"


# ==================== 端到端（假工具，真状态机）====================

class TestEndToEndWithFakeSql:
    def test_forced_sql_path_records_trace(self):
        """强制 SQL 路由也必须记轨迹 —— 追问常常接在这条路径上。

        实测：第 1 轮"Sales 部门有多少人"走的就是强制路由，
        第 2 轮追问正需要它。
        """

        class SqlTools(AgentTools):
            def __init__(self) -> None:
                pass

            def dispatch(self, name, args, tenant_id=None, redact=None, actor=""):
                if name == "list_data_tables":
                    return ToolOutcome(ok=True, name=name, observation="表清单",
                                       summary="ok", payload={})
                return ToolOutcome(ok=True, name=name, observation="ok",
                                   summary="ok", payload={})

            def sql_answer(self, question, tenant_id=None, redact=None, actor=""):
                class R:
                    ok = True
                    sql = "SELECT COUNT(*) FROM employees WHERE department='Sales'"
                    columns = ["人数"]
                    rows = [[1042]]
                    row_count = 1
                    elapsed_ms = 5
                    truncated = False
                    answer = "1042"
                    needs_clarification = False
                    clarification = ""
                    attempts = 1
                    errors: list = []
                    redacted_columns: list = []
                return R()

        llm = CapturingLLM()
        loop = AgentLoop(tools=SqlTools(), llm=llm)
        list(loop.run("Sales 部门有多少人？", forced_route=None))

        ctx = loop.last_tool_context
        assert "SELECT COUNT(*)" in ctx, f"强制路由未记录 SQL 轨迹: {ctx!r}"
        assert "1042" in ctx, "强制路由未记录结果行"

    def test_trace_respects_row_limit(self):
        """行数受 MAX_TRACE_ROWS 限制 —— 轨迹会进入每一轮 prompt，不能无限增长。"""

        class ManyRowsTools(AgentTools):
            def __init__(self) -> None:
                pass

            def dispatch(self, name, args, tenant_id=None, redact=None, actor=""):
                return ToolOutcome(ok=True, name=name, observation="ok",
                                   summary="ok", payload={})

            def sql_answer(self, question, tenant_id=None, redact=None, actor=""):
                class R:
                    ok = True
                    sql = "SELECT id FROM employees"
                    columns = ["id"]
                    rows = [[i] for i in range(100)]
                    row_count = 100
                    elapsed_ms = 5
                    truncated = False
                    answer = "100"
                    needs_clarification = False
                    clarification = ""
                    attempts = 1
                    errors: list = []
                    redacted_columns: list = []
                return R()

        loop = AgentLoop(tools=ManyRowsTools(), llm=CapturingLLM())
        list(loop.run("列出所有员工 id"))
        # 每行一条 "  <id>" 记录，行数应被限制
        lines = [ln for ln in loop.last_tool_context.splitlines() if ln.startswith("  ")]
        assert len(lines) <= MAX_TRACE_ROWS, f"轨迹行数未受限: {len(lines)}"


# ==================== 持久化契约 ====================

class TestPersistenceContract:
    def test_repo_signature_accepts_tool_context(self):
        import inspect

        from app.repositories.session_repo import SessionRepository

        assert "tool_context" in inspect.signature(
            SessionRepository.add_message).parameters

    def test_service_forwards_tool_context(self):
        """服务层必须把 tool_context 落库（不能收了参数却丢掉）。"""
        import inspect

        from app.services.session_service import SessionService

        src = inspect.getsource(SessionService.append)
        assert "tool_context" in src

    def test_get_history_returns_tool_context(self):
        """回源重建历史时必须带出 tool_context，否则 Redis 失效后就丢了上下文。"""
        import inspect

        from app.services.session_service import SessionService

        src = inspect.getsource(SessionService.get_history)
        assert "tool_context" in src

    def test_api_persists_loop_trace(self):
        import inspect

        from app.api.v1 import agent as agent_api

        src = inspect.getsource(agent_api)
        assert "last_tool_context" in src, "API 没有把本轮轨迹落库"

    def test_model_has_column(self):
        from app.models.chat import Message

        assert "tool_context" in Message.__table__.columns
