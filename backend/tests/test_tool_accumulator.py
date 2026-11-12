"""ToolCallAccumulator 单元测试 —— 用手写 SSE 最容易出错的地方做穷举验证。

本文件里的分片序列**逐帧照抄自真实 API 响应**（见改造方案 4.2 节），
不是构造的假数据。这样测试才能真正锁住线上行为。

三个必须守住的性质：
  1. id / name 只在首帧出现，后续帧为 null —— 不能被空值覆盖
  2. arguments 逐字符分片，必须全部拼接后才能 json.loads
  3. 多工具并发时按 index 正确分流，不串味
"""

from __future__ import annotations

import json

import pytest

from app.llm.tool_client import ToolCall, ToolCallAccumulator


class TestRealFrameSequence:
    """照抄真实响应的帧序列（DeepSeek-V3.2 流式 tool_calls）。"""

    def test_reproduces_real_sequence(self):
        acc = ToolCallAccumulator()

        # 帧 #13：首帧给出 index + id + type + name，arguments 为空串
        acc.feed_delta([{
            "index": 0,
            "id": "01a11a3cb93b5595944e5b1fe664bf2f",
            "type": "function",
            "function": {"name": "sql_query", "arguments": ""},
        }])
        # 帧 #14..#35：id/type/name 全为 null/空，只有 arguments 分片
        for frag in ['{', '"sql": "SELECT', ' COUNT', '(*)', ' as', ' 202',
                     '6', '年', '入职', '员工', '数', ' FROM', ' employees',
                     ' WHERE', ' YEAR', '(', 'hire', '_date', ')', ' =',
                     ' 202', '6"}']:
            acc.feed_delta([{
                "index": 0, "id": None, "type": None,
                "function": {"name": "", "arguments": frag},
            }])

        calls = acc.finish()
        assert len(calls) == 1
        c = calls[0]

        # 性质 1：id 与 name 必须保住（不能被后续 null 覆盖）
        assert c.id == "01a11a3cb93b5595944e5b1fe664bf2f"
        assert c.name == "sql_query", f"name 被空值覆盖了: {c.name!r}"

        # 性质 2：分片拼完后必须是合法 JSON，且语义正确
        args = c.parse_args()
        assert "SELECT" in args["sql"]
        assert "COUNT(*)" in args["sql"]
        assert "employees" in args["sql"]
        assert "hire_date" in args["sql"]

    def test_naive_overwrite_would_break_name(self):
        """反证：朴素的"后写覆盖"合并会把 name 变成空串。

        这条测试用来说明"为什么必须有累加器"而不是简单 dict 合并。
        """
        acc = ToolCallAccumulator()
        acc.feed_delta([{"index": 0, "id": "x", "function": {"name": "sql_query", "arguments": ""}}])
        acc.feed_delta([{"index": 0, "id": None, "function": {"name": "", "arguments": "{}"}}])
        c = acc.finish()[0]
        assert c.name == "sql_query"      # 朴素覆盖会得到 ""


class TestIdAndNameRecordedOnce:
    def test_id_only_in_first_frame(self):
        acc = ToolCallAccumulator()
        acc.feed_delta([{"index": 0, "id": "abc", "function": {"name": "f", "arguments": ""}}])
        acc.feed_delta([{"index": 0, "function": {"arguments": "1"}}])
        acc.feed_delta([{"index": 0, "function": {"arguments": "2"}}])
        c = acc.finish()[0]
        assert c.id == "abc"
        assert c.name == "f"
        assert c.arguments == "12"

    def test_missing_index_falls_back(self):
        """少数实现不带 index：应退化为当前槽位而不是崩掉。"""
        acc = ToolCallAccumulator()
        acc.feed_delta([{"id": "a", "function": {"name": "f", "arguments": "{"}}])
        acc.feed_delta([{"function": {"arguments": "}"}}])
        calls = acc.finish()
        assert len(calls) == 1
        assert calls[0].arguments == "{}"

    def test_empty_arguments_is_safe(self):
        acc = ToolCallAccumulator()
        acc.feed_delta([{"index": 0, "id": "a", "function": {"name": "f", "arguments": ""}}])
        c = acc.finish()[0]
        assert c.parse_args() == {}


class TestMultipleToolCalls:
    """并行工具调用必须按 index 分流，不能串味。"""

    def test_two_calls_interleaved(self):
        acc = ToolCallAccumulator()
        acc.feed_delta([
            {"index": 0, "id": "id0", "function": {"name": "sql_query", "arguments": ""}},
            {"index": 1, "id": "id1", "function": {"name": "kb_search", "arguments": ""}},
        ])
        # 交替喂入分片
        acc.feed_delta([{"index": 0, "function": {"arguments": '{"sql"'}}])
        acc.feed_delta([{"index": 1, "function": {"arguments": '{"query"'}}])
        acc.feed_delta([{"index": 0, "function": {"arguments": ': "SELECT 1"}'}}])
        acc.feed_delta([{"index": 1, "function": {"arguments": ': "质保"}'}}])

        calls = acc.finish()
        assert [c.name for c in calls] == ["sql_query", "kb_search"]
        assert calls[0].parse_args() == {"sql": "SELECT 1"}
        assert calls[1].parse_args() == {"query": "质保"}

    def test_out_of_order_indices_sorted(self):
        acc = ToolCallAccumulator()
        acc.feed_delta([{"index": 2, "id": "c", "function": {"name": "third", "arguments": "{}"}}])
        acc.feed_delta([{"index": 0, "id": "a", "function": {"name": "first", "arguments": "{}"}}])
        acc.feed_delta([{"index": 1, "id": "b", "function": {"name": "second", "arguments": "{}"}}])
        assert [c.name for c in acc.finish()] == ["first", "second", "third"]


class TestParseRobustness:
    def test_invalid_json_returns_empty_dict(self):
        c = ToolCall(arguments="{not json", id="x", name="f")
        assert c.parse_args() == {}

    def test_parse_is_cached(self):
        c = ToolCall(arguments='{"a": 1}', id="x", name="f")
        first = c.parse_args()
        second = c.parse_args()
        assert first is second

    def test_real_sql_payload_parses(self):
        payload = json.dumps({"sql": "SELECT count(*) FROM employees"}, ensure_ascii=False)
        c = ToolCall(arguments=payload, id="x", name="sql_query")
        assert c.parse_args()["sql"] == "SELECT count(*) FROM employees"

    def test_chinese_arguments_parse(self):
        """中文参数（本项目大量使用）必须正确解析。"""
        payload = json.dumps({"query": "路由器质保多久", "top_k": 5}, ensure_ascii=False)
        c = ToolCall(arguments=payload, id="x", name="kb_search")
        args = c.parse_args()
        assert args["query"] == "路由器质保多久"
        assert args["top_k"] == 5


class TestAccumulatorTruthiness:
    def test_empty_is_falsy(self):
        assert not ToolCallAccumulator()

    def test_nonempty_is_truthy(self):
        acc = ToolCallAccumulator()
        acc.feed_delta([{"index": 0, "id": "a", "function": {"name": "f", "arguments": ""}}])
        assert acc

    def test_feeding_empty_list_is_safe(self):
        acc = ToolCallAccumulator()
        acc.feed_delta([])
        acc.feed_delta(None)  # type: ignore[arg-type]
        assert acc.finish() == []
