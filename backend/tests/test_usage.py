"""LLM 用量记账测试（真连 PostgreSQL）。

为什么必须真连：聚合 SQL（`make_interval`、filter 计数、group by）全是数据库语义。
跳过条件：无 PG 时 skip。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from app.observability.usage import record_usage, summarize_usage
from app.core.config import settings


def _pg_available() -> bool:
    try:
        e = create_engine(settings.sync_postgres_url, connect_args={"connect_timeout": 5})
        with e.connect() as c:
            c.execute(text("SELECT 1"))
        e.dispose()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _pg_available(), reason="需要 PostgreSQL")

MARK = "test_scene_marker"


@pytest.fixture
def clean():
    e = create_engine(settings.sync_postgres_url)
    with e.begin() as c:
        c.execute(text("DELETE FROM llm_usage WHERE scene = :s"), {"s": MARK})
    yield e
    with e.begin() as c:
        c.execute(text("DELETE FROM llm_usage WHERE scene = :s"), {"s": MARK})
    e.dispose()


class TestRecordUsage:
    def test_records_tokens(self, clean):
        record_usage(MARK, "test-model", usage={
            "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
        }, actor="a:analyst", tenant_id="a", elapsed_ms=50)

        with clean.connect() as c:
            row = c.execute(text(
                "SELECT scene, model, actor, tenant_id, prompt_tokens, "
                "completion_tokens, total_tokens, elapsed_ms "
                "FROM llm_usage WHERE scene = :s"
            ), {"s": MARK}).one()
        assert row == (MARK, "test-model", "a:analyst", "a", 100, 20, 120, 50)

    def test_empty_usage_is_ignored(self, clean):
        """没有 usage 时不写记录 —— 避免产生一堆 0 token 的噪音行。"""
        record_usage(MARK, "m", usage=None)
        record_usage(MARK, "m", usage={})
        with clean.connect() as c:
            n = c.execute(text("SELECT count(*) FROM llm_usage WHERE scene = :s"),
                          {"s": MARK}).scalar()
        assert n == 0

    def test_partial_usage_defaults_to_zero(self, clean):
        record_usage(MARK, "m", usage={"prompt_tokens": 5})
        with clean.connect() as c:
            row = c.execute(text(
                "SELECT prompt_tokens, completion_tokens, total_tokens "
                "FROM llm_usage WHERE scene = :s"), {"s": MARK}).one()
        assert row == (5, 0, 0)

    def test_never_raises_on_bad_engine(self, monkeypatch):
        """记账失败绝不能影响主流程（它不该让一次问答挂掉）。"""
        import app.observability.usage as u

        class Boom:
            def __call__(self, *a, **kw):
                raise RuntimeError("db down")

        monkeypatch.setattr(u, "_get_engine", Boom())
        record_usage(MARK, "m", usage={"total_tokens": 1})  # 不应抛异常


class TestSummarizeUsage:
    def test_aggregates_by_scene(self, clean):
        for i in range(3):
            record_usage(MARK, "m", usage={"prompt_tokens": 10, "total_tokens": 10 + i},
                         tenant_id="a")
        out = summarize_usage(days=1, group_by="scene")
        item = next(x for x in out["items"] if x["key"] == MARK)
        assert item["calls"] == 3
        assert item["total_tokens"] == 10 + 11 + 12
        assert item["tokens_per_call"] > 0

    def test_group_by_actor(self, clean):
        record_usage(MARK, "m", usage={"total_tokens": 5}, actor="a:analyst")
        record_usage(MARK, "m", usage={"total_tokens": 7}, actor="a:employee")
        out = summarize_usage(days=1, group_by="actor")
        keys = {x["key"] for x in out["items"]}
        assert {"a:analyst", "a:employee"} <= keys

    def test_tenant_filter(self, clean):
        record_usage(MARK, "m", usage={"total_tokens": 5}, tenant_id="tenant_x")
        record_usage(MARK, "m", usage={"total_tokens": 9}, tenant_id="tenant_y")
        out = summarize_usage(days=1, tenant_id="tenant_x", group_by="scene")
        item = next(x for x in out["items"] if x["key"] == MARK)
        assert item["total_tokens"] == 5, "租户过滤应只统计本租户"

    def test_empty_result_is_well_formed(self, clean):
        """无数据时也必须返回结构完整的响应（前端不该拿到缺字段的对象）。"""
        out = summarize_usage(days=1, group_by="scene")
        assert set(out.keys()) >= {
            "window_days", "group_by", "total_calls", "total_tokens",
            "tokens_per_call", "items",
        }
        assert isinstance(out["items"], list)

    def test_invalid_group_by_falls_back(self, clean):
        """未知分组维度应回退到 scene，而不是报错。"""
        out = summarize_usage(days=1, group_by="no_such_dimension")
        assert out["group_by"] == "no_such_dimension"  # 原样回显
        assert isinstance(out["items"], list)
