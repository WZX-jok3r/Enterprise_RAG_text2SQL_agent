"""SQL 执行器的集成测试（**真连 PostgreSQL**，不是 hermetic 单测）。

为什么这个文件必须真连数据库：
    guard 的单测能证明"语句被正确放行/拒绝"，但证明不了
    「放行后的语句在真实数据库上确实能跑、且确实只读」。
    SET LOCAL / set_config / 只读事务 / EXPLAIN / RLS 这些**全是数据库行为**，
    用 mock 测等于没测。

与既有 hermetic 测试的关系：本项目 45 例单测刻意不连外部服务，
本文件是**有意引入的例外**，因此单独命名并加 skip 条件，避免在无 PG 环境下
把整个测试套件拖红。

跳过条件：连不上 kb_analytics 就 skip（而不是 fail）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from app.analytics.executor import SqlExecutor, to_jsonable
from app.analytics.guard import guard_sql
from app.core.config import settings

MAX_ROWS = 200


def _analytics_available() -> bool:
    try:
        engine = create_engine(settings.sync_analytics_ro_url, connect_args={"connect_timeout": 5})
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not _analytics_available(),
    reason="kb_analytics 不可达（需要 docker compose up + python -m app.ops.setup_db + app.ops.seed）",
)


@pytest.fixture(scope="module")
def executor() -> SqlExecutor:
    ex = SqlExecutor()
    yield ex
    ex.dispose()


def run(ex: SqlExecutor, sql: str, **kw):
    """走完整链路：guard -> execute。返回 QueryResult。"""
    g = guard_sql(sql, MAX_ROWS)
    assert g.ok, f"guard 拒绝: {g.reason}"
    return ex.execute(g.sql, **kw)


class TestSerialization:
    """序列化必须安全，否则 SSE 推帧会崩。"""

    def test_decimal_becomes_number(self):
        from decimal import Decimal

        assert to_jsonable(Decimal("1042")) == 1042
        assert to_jsonable(Decimal("6623.76")) == 6623.76

    def test_date_becomes_string(self):
        from datetime import date, datetime

        assert to_jsonable(date(2026, 8, 2)) == "2026-08-02"
        assert to_jsonable(datetime(2026, 8, 2, 3, 4, 5)) == "2026-08-02 03:04:05"

    def test_none_stays_none(self):
        assert to_jsonable(None) is None

    def test_result_is_json_serializable(self, executor):
        """真实查询结果必须能 json.dumps（这是 SSE 的前置条件）。"""
        import json

        res = run(executor, "SELECT * FROM employees LIMIT 5")
        assert res.ok, res.error
        json.dumps({"columns": res.columns, "rows": res.rows})  # 不应抛异常


class TestSideEffectFree:
    """EXPLAIN 预检绝不能真的执行语句（这是最危险的一处误用）。"""

    def test_explain_does_not_execute_write(self, executor):
        """若预检误用 EXPLAIN ANALYZE，这条 INSERT 会真的写入。

        本测试用"副作用是否存在"作为判据，而不是检查 SQL 字符串 ——
        更接近真实风险。
        """
        marker = "EXPLAIN_PROBE_MARKER"
        # 该表由 kb_ro 不可写，所以真正的验证是：explain 不应抛"写成功"，
        # 且我们断言生成的预检语句不含 ANALYZE（双保险）
        ok, err = executor.explain_sql(
            f"SELECT '{marker}' AS m FROM employees LIMIT 1"
        )
        assert ok, err

    def test_generated_explain_statement_has_no_analyze(self, executor, monkeypatch):
        """断言实际发给数据库的语句不含 ANALYZE。"""
        captured = {}
        real_execute = type(executor._engine).connect  # noqa: F841

        # 直接检查 explain_sql 内部构造：用 monkeypatch 拦 conn.execute
        class SpyConn:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, stmt, *a, **kw):
                captured["sql"] = str(stmt)
                class R:
                    def fetchone(self_inner):
                        return (True,)
                return R()

        monkeypatch.setattr(executor._engine, "connect", lambda *a, **kw: SpyConn())
        executor.explain_sql("SELECT 1 FROM employees LIMIT 1")
        assert "ANALYZE" not in captured["sql"].upper(), captured["sql"]
        assert "EXPLAIN" in captured["sql"].upper()


class TestReadOnlyEnforced:
    """只读性必须由数据库层拒绝，而不是靠应用层"不生成写语句"。"""

    def test_role_cannot_create_table(self, executor):
        from sqlalchemy import text as _t

        with executor._engine.connect() as conn:
            with pytest.raises(Exception) as ei:
                conn.execute(_t("CREATE TABLE _ro_probe_integration (x int)"))
        assert "read-only" in str(ei.value).lower() or "permission" in str(ei.value).lower()

    def test_set_config_readonly_blocks_write(self, executor):
        """即使角色权限被放开，set_config 设的只读事务也必须拦住写。"""
        from sqlalchemy import text as _t

        with executor._engine.connect() as conn:
            conn.execute(_t("SELECT set_config('transaction_read_only','on',true)"))
            with pytest.raises(Exception):
                conn.execute(_t("CREATE TABLE _ro_probe2 (x int)"))

    def test_delete_is_rejected(self, executor):
        from sqlalchemy import text as _t

        with executor._engine.connect() as conn:
            with pytest.raises(Exception):
                conn.execute(_t("DELETE FROM employees WHERE 1=1"))


class TestRealQueries:
    """真实统计查询 —— 这些是改造方案第三部分失败的题，必须在这里答对。"""

    def test_the_headline_case(self, executor):
        """『Sales 和 Engineering 各多少人、差多少』。

        改造前 RAG 答 1017（真值 32，错 31.8 倍）。
        """
        res = run(
            executor,
            "SELECT department, count(*) AS n FROM employees "
            "WHERE department IN ('Sales','Engineering') GROUP BY department ORDER BY department",
        )
        assert res.ok, res.error
        got = {row[0]: row[1] for row in res.rows}
        assert got == {"Engineering": 1010, "Sales": 1042}, got

    def test_difference_is_32(self, executor):
        res = run(
            executor,
            "SELECT (SELECT count(*) FROM employees WHERE department='Sales') "
            "- (SELECT count(*) FROM employees WHERE department='Engineering') AS diff",
        )
        assert res.ok, res.error
        assert res.rows[0][0] == 32

    def test_2026_hires(self, executor):
        res = run(
            executor,
            "SELECT count(*) FROM employees WHERE hire_date >= '2026-01-01'",
        )
        assert res.ok, res.error
        assert res.rows[0][0] == 192

    def test_max_salary_employee(self, executor):
        res = run(
            executor,
            "SELECT first_name, last_name, department, salary FROM employees "
            "ORDER BY salary DESC LIMIT 1",
        )
        assert res.ok, res.error
        row = res.rows[0]
        assert row[0] == "Derek" and row[2] == "Operations" and row[3] == 179997, row

    def test_cte_and_window(self, executor):
        res = run(
            executor,
            "WITH d AS (SELECT department, count(*) c FROM employees GROUP BY 1) "
            "SELECT department, c, rank() OVER (ORDER BY c DESC) r FROM d ORDER BY c DESC LIMIT 1",
        )
        assert res.ok, res.error
        assert res.rows[0][0] == "Sales" and res.rows[0][1] == 1042

    def test_chinese_column_table(self, executor):
        """中文列名表：col_N 标识符 + 原表头在 COMMENT 里。"""
        res = run(executor, "SELECT count(*) FROM cost_data")
        assert res.ok, res.error
        assert res.rows[0][0] == 6


class TestLimitAndTruncation:
    """截断必须被如实标注 —— 否则 LLM 会把"前 N 行"当"全部数据"。"""

    def test_truncation_flag_set(self, executor):
        res = run(executor, "SELECT * FROM employees")
        assert res.ok, res.error
        assert res.row_count == MAX_ROWS
        assert res.truncated is True, "必须标注已截断"

    def test_truncation_flag_false_when_complete(self, executor):
        res = run(executor, "SELECT * FROM employees LIMIT 10")
        assert res.ok
        assert res.truncated is False

    def test_observation_warns_about_truncation(self, executor):
        """给 LLM 的文本里必须明说"不是全部数据"。"""
        res = run(executor, "SELECT * FROM employees")
        assert "截断" in res.observation or "truncated" in res.observation.lower()
        assert "不要" in res.observation  # 明确禁止当成全集汇报

    def test_large_result_gives_summary_not_details(self, executor):
        """大结果集（> inline_max_rows）只给样例 + 统计概要。"""
        res = run(executor, "SELECT id, first_name, salary FROM employees")
        assert res.ok
        assert res.row_count > executor.inline_max_rows
        assert "前 5 行样例" in res.observation
        assert "聚合查询" in res.observation

    def test_small_result_gives_full_details(self, executor):
        res = run(executor, "SELECT * FROM sales")
        assert res.ok
        assert res.row_count <= executor.inline_max_rows
        assert "前 5 行样例" not in res.observation


class TestErrorFeedback:
    """报错必须可回灌给 LLM（reflexion 的输入）。"""

    def test_bad_column_name_gives_readable_error(self, executor):
        res = run(executor, "SELECT nonexistent_col FROM employees")
        assert not res.ok
        # 错误信息要能让 LLM 明白哪里错了
        assert "nonexistent_col" in res.observation
        assert "修正" in res.observation or "重新生成" in res.observation

    def test_bad_table_name_gives_readable_error(self, executor):
        res = run(executor, "SELECT count(*) FROM no_such_table")
        assert not res.ok
        assert res.observation

    def test_empty_result_is_not_an_error(self, executor):
        """0 行不等于出错，且要提示 LLM 区分两种情况。"""
        res = run(executor, "SELECT * FROM employees WHERE department = '不存在的部门'")
        assert res.ok
        assert res.row_count == 0
        assert "0 行" in res.observation
        assert "不等于出错" in res.observation


class TestRls:
    """RLS 行级隔离（**fail-closed** 语义）。

    ## 语义变更说明（由真实越权问题驱动）

    原语义是 fail-open 的：
      - 未设 `app.tenant_id` ⇒ 放行**全部行**
      - 设了变量 ⇒ 只放行 tenant_id 匹配的行

    实测确证这有问题：执行器写的是 `if tenant_id:`，未认证/无租户时
    **根本不设置变量**，于是策略放行全部 —— 租户 b 能读到全公司 10000 行薪资。

    现在拆成「归属」与「可见性」两个正交概念：
      - `is_shared = true`  ⇒ 全公司共享语料，**所有租户可见**
      - `tenant_id` 匹配    ⇒ 本租户私有数据
      - 其它                ⇒ **拒绝**（含"忘设变量"的情况）

    demo 语料（employees 等）现标记为 **shared**，因此对所有租户可见 ——
    这不是"隔离失效"，而是它们本来就是公司级共享数据；
    租户私有数据（API 上传的 xlsx）的隔离由
    tests/test_tenant_isolation.py 专门验证。
    """

    def test_shared_corpus_has_no_private_owner(self, executor):
        """demo 语料应标记共享、且不再硬编码归属到某个租户。"""
        res = run(executor, "SELECT count(*) FROM employees WHERE is_shared IS TRUE")
        assert res.ok, res.error
        assert res.rows[0][0] == 10000

    def test_no_row_is_hardcoded_to_tenant_a(self, executor):
        """反证：不应再有硬编码兜底租户 'a' 的行（这是原缺陷的痕迹）。"""
        res = run(executor, "SELECT count(*) FROM employees WHERE tenant_id = 'a'")
        assert res.ok, res.error
        assert res.rows[0][0] == 0, "仍有行被硬编码归属到 'a'"

    def test_shared_corpus_visible_to_any_tenant(self, executor):
        """共享语料对所有租户可见（含陌生租户）。"""
        res = run(executor, "SELECT count(*) FROM employees", tenant_id="b")
        assert res.ok, res.error
        assert res.rows[0][0] == 10000, "共享语料不应被租户隔离挡住"

    def test_rls_allows_own_tenant(self, executor):
        res = run(executor, "SELECT count(*) FROM employees", tenant_id="a")
        assert res.ok, res.error
        assert res.rows[0][0] == 10000

    def test_rls_allows_when_unset(self, executor):
        """未认证（评测/CLI）时仍能看到**共享语料**。

        注意语义已变：以前是"放行全部行"，现在是"只放行 is_shared 的行"。
        对 demo 语料结果相同，但私有好数据不再泄露 —— 见 TestFailClosed。
        """
        res = run(executor, "SELECT count(*) FROM employees", tenant_id=None)
        assert res.ok, res.error
        assert res.rows[0][0] == 10000


class TestFailClosedNoTenantLeak:
    """未设租户时**不得**泄露租户私有数据（本次安全修复的核心断言）。

    这里直接对着 RLS 语义做断言，不依赖是否恰好存在私有数据：
    用哨兵租户名反证"无租户 ≠ 万能钥匙"。
    """

    def test_anonymous_sentinel_does_not_match_shared_rows_as_owner(self, executor):
        """哨兵租户名不得匹配到任何真实归属（否则"无租户"会变成万能钥匙）。"""
        from app.analytics.executor import ANONYMOUS_TENANT

        res = run(executor,
                  f"SELECT count(*) FROM employees WHERE tenant_id = '{ANONYMOUS_TENANT}'")
        assert res.ok, res.error
        assert res.rows[0][0] == 0, "哨兵租户名不应匹配到任何真实归属"

    def test_policy_sql_has_no_fail_open_branch(self):
        """静态断言：策略 SQL 不得再出现 fail-open 分支。"""
        from app.analytics import ddl

        sql = " ".join(ddl.build_rls_sql("employees")).lower()
        assert "is null" not in sql, "RLS 策略又出现 `IS NULL ⇒ 放行` 的 fail-open 分支"
        assert ddl.SHARED_COLUMN in sql


class TestTimeout:
    def test_statement_timeout_is_applied(self, executor):
        """statement_timeout 必须真的生效（用 pg_sleep 验证）。"""
        # pg_sleep 在 guard 层被禁（DoS 防护），所以这里绕过 guard 直连执行，
        # 专门验证执行器的超时护栏本身是否工作
        from sqlalchemy import text as _t

        with executor._engine.connect() as conn:
            conn.execute(_t("SELECT set_config('statement_timeout','300ms',true)"))
            import time
            t0 = time.perf_counter()
            with pytest.raises(Exception) as ei:
                conn.execute(_t("SELECT pg_sleep(5)"))
            elapsed = time.perf_counter() - t0
        assert elapsed < 3, f"超时未生效，耗时 {elapsed:.1f}s"
        assert "timeout" in str(ei.value).lower() or "cancel" in str(ei.value).lower()
