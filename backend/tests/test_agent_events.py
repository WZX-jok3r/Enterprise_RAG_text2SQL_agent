"""Agent 事件契约测试（**扩帧不改帧**的守卫）。

这个文件的作用是**防止有人改坏既有 4 类事件**：
它们被前端 `stores/chat.ts` 与评测共同依赖，字段一变前端就崩。

契约来源：改造方案 1.7「契约清单」+ 5.4.4「事件契约扩展」。
"""

from __future__ import annotations

import json

import pytest

from app.agent import events as ev


class TestFrozenEvents:
    """既有 4 类事件：字段集合与语义**冻结**，改动即破坏前端。"""

    def test_meta_exact_fields(self):
        e = ev.ev_meta([{"source": "a.md"}], 5, 2)
        assert set(e.keys()) == ev.FROZEN_META_FIELDS
        assert e["type"] == "meta"
        assert e["sources"] == [{"source": "a.md"}]
        assert e["retrieved_count"] == 5
        assert e["hidden_count"] == 2

    def test_delta_exact_fields(self):
        e = ev.ev_delta("hello")
        assert set(e.keys()) == ev.FROZEN_DELTA_FIELDS
        assert e == {"type": "delta", "text": "hello"}

    def test_done_minimal_fields(self):
        e = ev.ev_done("答案")
        assert ev.FROZEN_DONE_FIELDS <= set(e.keys())
        assert e["type"] == "done" and e["answer"] == "答案"

    def test_error_exact_fields(self):
        e = ev.ev_error("出错了")
        assert set(e.keys()) == ev.FROZEN_ERROR_FIELDS
        assert e["type"] == "error" and e["message"] == "出错了"

    def test_frozen_field_sets_unchanged(self):
        """把字段集合写成断言：任何增删都会让这条测试失败。"""
        assert ev.FROZEN_META_FIELDS == {"type", "sources", "retrieved_count", "hidden_count"}
        assert ev.FROZEN_DELTA_FIELDS == {"type", "text"}
        assert ev.FROZEN_DONE_FIELDS == {"type", "answer"}
        assert ev.FROZEN_ERROR_FIELDS == {"type", "message"}


class TestNewEvents:
    """新增 6 类事件：旧前端不认识就走 default 忽略，不会崩。"""

    def test_route_event(self):
        e = ev.ev_route("sql", "同时命中聚合词与列名词")
        assert e["type"] == "route"
        assert e["target"] == "sql"
        assert e["reason"]

    def test_tool_call_event(self):
        e = ev.ev_tool_call("id1", "sql_query", {"sql": "SELECT 1"}, purpose="统计人数")
        assert e["type"] == "tool_call"
        assert e["id"] == "id1" and e["name"] == "sql_query"
        assert e["args"] == {"sql": "SELECT 1"}
        assert e["purpose"] == "统计人数"

    def test_tool_result_event(self):
        e = ev.ev_tool_result("id1", True, "返回 2 行")
        assert e["type"] == "tool_result"
        assert e["ok"] is True and e["summary"] == "返回 2 行"

    def test_sql_event_carries_truncated(self):
        """truncated 必须透传：否则用户会把"前 N 行"当成全部数据。"""
        e = ev.ev_sql("SELECT * FROM t", 200, 12, truncated=True)
        assert e["type"] == "sql"
        assert e["truncated"] is True
        assert e["row_count"] == 200 and e["elapsed_ms"] == 12

    def test_clarify_event(self):
        e = ev.ev_clarify("请确认口径")
        assert e["type"] == "clarify" and e["question"] == "请确认口径"

    def test_degraded_event(self):
        e = ev.ev_degraded("max_steps", message="已达最大步数")
        assert e["type"] == "degraded"
        assert e["reason"] == "max_steps"

    def test_agent_event_types_complete(self):
        assert ev.AGENT_EVENT_TYPES == {
            "route", "tool_call", "tool_result", "sql", "clarify", "degraded",
            # P1-2：追问改写只在改写生效时发，前端未识别时走 default 忽略分支
            "rewrite",
        }

    def test_rewrite_event(self):
        e = ev.ev_rewrite("那 Engineering 呢？", "Engineering 部门有多少人", "rewritten")
        assert e["type"] == "rewrite"
        assert e["original"] == "那 Engineering 呢？"
        assert e["rewritten"] == "Engineering 部门有多少人"
        assert e["reason"] == "rewritten"


class TestSerializability:
    """所有事件必须可直接 json.dumps（SSE 推帧的前置条件）。"""

    ALL = [
        ev.ev_meta([{"source": "a.md", "format": "md", "score": 0.9}], 3, 1),
        ev.ev_delta("文本"),
        ev.ev_done("答案", session_id="abc", history_length=4),
        ev.ev_error("err"),
        ev.ev_route("sql", "原因", matched=["多少"]),
        ev.ev_tool_call("id", "kb_search", {"query": "质保"}),
        ev.ev_tool_result("id", True, "摘要", sources=[], retrieved_count=0, hidden_count=0),
        ev.ev_sql("SELECT 1", 1, 5, retries=0, truncated=False),
        ev.ev_clarify("口径？"),
        ev.ev_degraded("wall_clock"),
        ev.ev_rewrite("那 Engineering 呢？", "Engineering 部门有多少人", "rewritten"),
    ]

    @pytest.mark.parametrize("event", ALL, ids=lambda e: e["type"])
    def test_json_serializable(self, event):
        s = json.dumps(event, ensure_ascii=False)
        assert json.loads(s) == event

    @pytest.mark.parametrize("event", ALL, ids=lambda e: e["type"])
    def test_all_events_have_type(self, event):
        assert "type" in event and isinstance(event["type"], str)
