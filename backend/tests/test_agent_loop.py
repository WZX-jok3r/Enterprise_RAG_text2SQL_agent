"""Agent Loop 测试（hermetic：用假 LLM / 假工具，不连外部服务）。

重点验证**五重防死循环**——这是 Agent 类项目面试必问、生产必备的部分：
    1. MAX_STEPS 硬上界
    2. WALL_CLOCK 墙钟超时
    3. MAX_LLM_CALLS 成本上界
    4. 相同工具+相同参数去重
    5. 失败降级到纯 RAG（而非抛错）

以及**降级必须补发 meta 帧**（否则前端来源列表与徽标永久空着）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from app.agent.loop import AgentLoop
from app.agent.router import Route
from app.agent.tools import ToolOutcome
from app.llm.tools import ToolCall, ToolChatResult


# ==================== 测试替身 ====================

class FakeLLM:
    """按脚本返回预设的 tool_calls 序列。记录调用次数以便断言成本上界。"""

    def __init__(self, script: Optional[List[List[ToolCall]]] = None,
                 final_content: str = "这是最终答案"):
        self.script = list(script or [])
        self.final_content = final_content
        self.calls = 0
        self.seen_messages: List[List[Dict[str, Any]]] = []

    def chat_with_tools(self, messages, tools=None, temperature=None, **kw) -> ToolChatResult:
        self.calls += 1
        self.seen_messages.append(list(messages))
        if self.script:
            calls = self.script.pop(0)
            return ToolChatResult(content="", tool_calls=calls, finish_reason="tool_calls")
        return ToolChatResult(content=self.final_content, tool_calls=[], finish_reason="stop")


class FakeTools:
    """假工具：可配置成功/失败/返回内容。

    ⚠️ 签名必须与 AgentTools 保持一致（含 redact / actor）——
    否则测试会在"接口漂移"时报 TypeError，看起来像测试坏了，
    实际是**提供了接口不匹配的实现**。这里显式声明，不吞多余 kwargs：
    静默吞掉 **kwargs 会让真实集成错误被掩盖。
    """

    def __init__(self, kb_ok=True, sql_ok=True, kb_text="知识库内容：路由器质保 24 个月",
                 sql_rows=None):
        self.kb_ok = kb_ok
        self.sql_ok = sql_ok
        self.kb_text = kb_text
        self.sql_rows = sql_rows if sql_rows is not None else [["Sales", 1042]]
        self.calls: List[str] = []
        # 记录收到的权限参数，便于断言"权限确实被传到了工具层"
        self.last_redact = None
        self.last_actor = ""

    def dispatch(self, name, args, tenant_id=None, redact=None, actor="") -> ToolOutcome:
        self.calls.append(name)
        self.last_redact = redact
        self.last_actor = actor
        if name == "kb_search":
            if not self.kb_ok:
                return ToolOutcome(ok=False, name=name, observation="检索失败", summary="检索失败")
            chunks = [{"text": self.kb_text, "score": 0.9, "metadata": {"source": "warranty.md"}}]
            return ToolOutcome(
                ok=True, name=name, observation=self.kb_text,
                summary="检索到 1 段相关内容",
                payload={"chunks": chunks, "dropped": 2, "retrieved_count": 1,
                         "sources": [{"source": "warranty.md"}]},
            )
        if name == "sql_query":
            if not self.sql_ok:
                return ToolOutcome(ok=False, name=name, observation="SQL 失败", summary="SQL 失败")
            return ToolOutcome(
                ok=True, name=name, observation="结果 2 行",
                summary="查询返回 2 行（4ms）",
                payload={"sql": "SELECT 1", "columns": ["a", "b"], "rows": self.sql_rows,
                         "row_count": len(self.sql_rows), "elapsed_ms": 4,
                         "truncated": False},
            )
        if name == "list_data_tables":
            return ToolOutcome(ok=True, name=name, observation="表：employees", summary="1 张表",
                               payload={"tables": ["employees"]})
        return ToolOutcome(ok=False, name=name, observation="未知工具", summary="未知工具")

    def sql_answer(self, question, tenant_id=None, redact=None, actor=""):
        """模拟 Text2SQL 引擎（结构对齐 Text2SqlResult，不碰数据库）。"""
        self.calls.append("sql_answer")
        self.last_redact = redact
        self.last_actor = actor
        return FakeSqlResult()


def tc(name: str, args: dict, call_id: str = "c1") -> ToolCall:
    import json
    return ToolCall(id=call_id, name=name, arguments=json.dumps(args, ensure_ascii=False))


class FakeSqlResult:
    """假 Text2SQL 结果（结构对齐 Text2SqlResult）。"""

    def __init__(self) -> None:
        self.ok = True
        self.question = "q"
        self.sql = "SELECT count(*) FROM employees"
        self.columns = ["count"]
        self.rows = [[1042]]
        self.row_count = 1
        self.elapsed_ms = 4
        self.truncated = False
        self.answer = "共 1042 人"
        self.needs_clarification = False
        self.clarification = ""
        self.attempts = 1
        self.errors: List[str] = []
        self.redacted_columns: List[str] = []


def collect(loop: AgentLoop, question: str, **kw) -> List[Dict[str, Any]]:
    return list(loop.run(question, **kw))


def types_of(events: List[Dict[str, Any]]) -> List[str]:
    return [e["type"] for e in events]


# ==================== 规则强路由路径 ====================

class TestForcedSQLRoute:
    def test_sql_question_skips_llm_decision(self):
        """规则命中统计题的聚合+列名 -> 强路由 SQL。

        注意：`_exec_sql_via_engine` 走真实 Text2SQL 引擎，这里只验证
        **路由决策**发生在 LLM 之前（route 事件的 target 为 sql）。
        """
        loop = AgentLoop(tools=FakeTools(), llm=FakeLLM())
        # 只跑到 route 事件即可判断路由正确性
        gen = loop.run("哪个部门人数最多？")
        first = next(gen)
        assert first["type"] == "route" and first["target"] == "sql"

    def test_rag_question_routes_to_rag(self):
        loop = AgentLoop(tools=FakeTools(), llm=FakeLLM())
        first = next(loop.run("路由器的质保期是多久？"))
        assert first["type"] == "route" and first["target"] == "rag"


class TestForcedRAGRoute:
    def test_rag_path_emits_meta_then_delta_then_done(self):
        """RAG 路径的事件顺序必须是 route -> tool_call -> tool_result -> meta -> delta -> done。"""
        loop = AgentLoop(tools=FakeTools(), llm=FakeLLM(final_content="路由器质保 24 个月"))
        events = collect(loop, "路由器的质保期是多久？")
        t = types_of(events)
        assert t[0] == "route"
        assert "meta" in t, f"必须补发 meta 帧，实际事件: {t}"
        assert t[-1] == "done"
        assert "delta" in t

    def test_meta_frame_has_frozen_fields(self):
        """降级/强路由路径补发的 meta 帧必须与既有契约完全一致。"""
        loop = AgentLoop(tools=FakeTools(), llm=FakeLLM())
        events = collect(loop, "路由器的质保期是多久？")
        meta = next(e for e in events if e["type"] == "meta")
        assert set(meta.keys()) == {"type", "sources", "retrieved_count", "hidden_count"}
        assert meta["retrieved_count"] == 1
        assert meta["hidden_count"] == 2          # dropped 透传为 hidden_count

    def test_meta_comes_before_delta(self):
        """前端按顺序处理：meta 必须在 delta 之前，否则来源列表渲染不出来。"""
        loop = AgentLoop(tools=FakeTools(), llm=FakeLLM())
        t = types_of(collect(loop, "路由器的质保期是多久？"))
        assert t.index("meta") < t.index("delta")

    def test_answer_is_generated(self):
        loop = AgentLoop(tools=FakeTools(), llm=FakeLLM(final_content="路由器质保 24 个月"))
        events = collect(loop, "路由器的质保期是多久？")
        done = next(e for e in events if e["type"] == "done")
        assert "24" in done["answer"]


# ==================== 五重防死循环 ====================

class TestMaxSteps:
    def test_steps_are_bounded(self):
        """模型每轮都要求调工具 -> 必须在 max_steps 内停下并降级。"""
        script = [[tc("kb_search", {"query": f"q{i}"}, f"c{i}")] for i in range(20)]
        llm = FakeLLM(script=script)
        loop = AgentLoop(tools=FakeTools(), llm=llm, max_steps=3)
        events = collect(loop, "帮我看看这个业务问题")
        assert "degraded" in types_of(events)
        stats = next(e for e in events if e["type"] == "done")["agent_stats"]
        assert stats["steps"] <= 3, f"步数越界: {stats}"
        assert "done" in types_of(events), "降级后仍必须给出答案"

    def test_degraded_reason_is_max_steps(self):
        script = [[tc("kb_search", {"query": f"q{i}"}, f"c{i}")] for i in range(20)]
        loop = AgentLoop(tools=FakeTools(), llm=FakeLLM(script=script), max_steps=2)
        events = collect(loop, "帮我看看这个业务问题")
        deg = next(e for e in events if e["type"] == "degraded")
        assert deg["reason"] == "max_steps"


class TestMaxLLMCalls:
    def test_llm_calls_are_bounded(self):
        """成本上界：LLM 调用次数必须受限（框架默认往往没有这一条）。"""
        script = [[tc("kb_search", {"query": f"q{i}"}, f"c{i}")] for i in range(20)]
        llm = FakeLLM(script=script)
        loop = AgentLoop(tools=FakeTools(), llm=llm, max_steps=50, max_llm_calls=3)
        events = collect(loop, "帮我看看这个业务问题")
        # 3 次工具选择 + 可能 1 次答案生成
        assert llm.calls <= 4, f"LLM 调用超出成本上界: {llm.calls}"
        assert next(e for e in events if e["type"] == "done")["agent_stats"]["llm_calls"] <= 3


class TestRepeatedToolCall:
    def test_identical_call_detected_as_loop(self):
        """相同工具 + 相同参数重复 -> 判为循环，立即跳出（保护 4）。"""
        same = tc("kb_search", {"query": "一样的问题"}, "c1")
        script = [[same], [same], [same]]
        loop = AgentLoop(tools=FakeTools(), llm=FakeLLM(script=script), max_steps=10)
        events = collect(loop, "帮我看看这个业务问题")
        deg = [e for e in events if e["type"] == "degraded"]
        assert deg, f"应检测到重复调用，事件: {types_of(events)}"
        assert deg[0]["reason"] == "repeated_tool_call"
        assert "done" in types_of(events)

    def test_different_args_not_treated_as_loop(self):
        script = [
            [tc("kb_search", {"query": "问题一"}, "c1")],
            [tc("kb_search", {"query": "问题二"}, "c2")],
            [tc("kb_search", {"query": "问题三"}, "c3")],
        ]
        loop = AgentLoop(tools=FakeTools(), llm=FakeLLM(script=script), max_steps=3)
        events = collect(loop, "帮我看看这个业务问题")
        reasons = [e.get("reason") for e in events if e["type"] == "degraded"]
        assert "repeated_tool_call" not in reasons, "参数不同不应判为重复"


class TestWallClock:
    def test_timeout_triggers_degradation(self):
        """墙钟超时（保护 2）：把 wall_clock 设为 0 即刻超时。"""
        script = [[tc("kb_search", {"query": "q"}, "c1")] for _ in range(5)]
        loop = AgentLoop(tools=FakeTools(), llm=FakeLLM(script=script),
                         max_steps=10, wall_clock=-1)
        events = collect(loop, "帮我看看这个业务问题")
        deg = [e for e in events if e["type"] == "degraded"]
        assert deg and deg[0]["reason"] == "wall_clock"
        assert "done" in types_of(events)


class TestAllToolsFailed:
    def test_failure_degrades_not_raises(self):
        """工具全失败 -> 降级到 RAG（保护 5），绝不把异常抛给用户。"""
        tools = FakeTools(kb_ok=False, sql_ok=False)
        script = [[tc("sql_query", {"sql": "SELECT 1"}, "c1")]]
        loop = AgentLoop(tools=tools, llm=FakeLLM(script=script), max_steps=3)
        events = collect(loop, "帮我看看这个业务问题")
        # 不应抛异常；必须给出 done
        assert "done" in types_of(events)

    def test_llm_error_degrades(self):
        class BoomLLM:
            calls = 0

            def chat_with_tools(self, *a, **kw):
                self.calls += 1
                raise RuntimeError("API 挂了")

        loop = AgentLoop(tools=FakeTools(), llm=BoomLLM())
        events = collect(loop, "帮我看看这个业务问题")
        assert "degraded" in types_of(events)
        assert "done" in types_of(events), "LLM 挂了也必须给出答案"


# ==================== 事件契约 ====================

class TestEventSequenceContract:
    def test_always_ends_with_done(self):
        for q in ["路由器的质保期是多久？", "帮我看看这个业务问题"]:
            loop = AgentLoop(tools=FakeTools(), llm=FakeLLM())
            events = collect(loop, q)
            assert events[-1]["type"] == "done", f"{q} 未以 done 结束"

    def test_route_is_always_first(self):
        for q in ["路由器的质保期是多久？", "哪个部门人数最多？", "你好"]:
            loop = AgentLoop(tools=FakeTools(), llm=FakeLLM())
            events = collect(loop, q)
            assert events[0]["type"] == "route", f"{q} 首帧不是 route"

    def test_tool_call_followed_by_tool_result(self):
        loop = AgentLoop(tools=FakeTools(), llm=FakeLLM())
        events = collect(loop, "路由器的质保期是多久？")
        t = types_of(events)
        assert "tool_call" in t and "tool_result" in t
        assert t.index("tool_call") < t.index("tool_result")

    def test_every_event_is_json_serializable(self):
        import json
        loop = AgentLoop(tools=FakeTools(), llm=FakeLLM())
        for e in collect(loop, "路由器的质保期是多久？"):
            json.dumps(e, ensure_ascii=False)


class TestPermissionsAreThreaded:
    """RBAC 权限必须真的传到工具层。

    这类"参数没传下去"的缺陷**不会报错**，只会让权限静默失效 ——
    即用户以为有脱敏，实际看到了全部数据。因此必须用测试钉死。

    （本测试的由来：给工具层加 redact/actor 参数时，
      假工具没同步签名，25 个测试报 TypeError 才发现漏传。
      真实集成里若假对象用 **kwargs 吞掉，就会完全测不出来。）
    """

    def test_redact_reaches_tools_on_rag_path(self):
        tools = FakeTools()
        loop = AgentLoop(tools=tools, llm=FakeLLM())
        list(loop.run("路由器的质保期是多久？", redact=frozenset({"employees.salary"}),
                      actor="a:employee"))
        assert tools.last_redact == frozenset({"employees.salary"}), "脱敏列未传到工具层"
        assert tools.last_actor == "a:employee", "审计标识未传到工具层"

    def test_redact_reaches_tools_on_sql_path(self):
        tools = FakeTools()
        loop = AgentLoop(tools=tools, llm=FakeLLM())
        list(loop.run("哪个部门人数最多？", redact=frozenset({"employees.salary"}),
                      actor="a:employee"))
        assert tools.last_redact == frozenset({"employees.salary"})
        assert tools.last_actor == "a:employee"

    def test_no_redact_when_not_provided(self):
        """未传权限时必须是 None（走零成本快路径，等价于未启用 RBAC）。"""
        tools = FakeTools()
        loop = AgentLoop(tools=tools, llm=FakeLLM())
        list(loop.run("路由器的质保期是多久？"))
        assert tools.last_redact is None
        assert tools.last_actor == ""


class TestObservability:
    def test_last_stats_recorded(self):
        """成本记账依赖这些计数（P7）。"""
        loop = AgentLoop(tools=FakeTools(), llm=FakeLLM())
        events = collect(loop, "路由器的质保期是多久？")
        st = next(e for e in events if e["type"] == "done")["agent_stats"]
        assert set(st.keys()) >= {"steps", "llm_calls", "tool_calls", "degraded", "elapsed_ms"}
        assert st["elapsed_ms"] >= 0

    def test_done_carries_stats(self):
        loop = AgentLoop(tools=FakeTools(), llm=FakeLLM())
        events = collect(loop, "路由器的质保期是多久？")
        done = next(e for e in events if e["type"] == "done")
        assert "agent_stats" in done
