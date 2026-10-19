"""executor.py - 只读 SQL 执行器（四层防御的第二层：事务与资源限制）。

职责：
    1. 用**只读角色**连接 kb_analytics
    2. 每条查询都在只读事务里跑：`SET LOCAL transaction_read_only = on`
    3. 语句级超时：`SET LOCAL statement_timeout = '<n>ms'`
    4. 结果行数上限（由 guard 的 LIMIT 注入 + 本层的读取上限双重保证）
    5. 结果序列化（Decimal/date -> str，否则 SSE 序列化会崩）
    6. 大结果集保护：超过阈值时**不返回明细**，只返回统计概要
       —— 因为 LLM 无法预知结果集大小，上千行明细会炸掉 prompt 上下文

⚠️ 安全立场：本层是"纵深防御"的第二层，不是唯一防线。
    📄 PostgreSQL 官方文档自承：
      "This is a **high-level notion of read-only that does not prevent all
       writes to disk**."
    → 只读事务仍有漏网路径（如 nextval()、临时表）。所以必须叠加
      guard（AST 白名单）+ 角色权限（GRANT SELECT only）+ RLS。

⚠️ 关于 EXPLAIN 预检（本项目一个重要的正确性点）：
    📄 PostgreSQL 官方原文：
      "The `ANALYZE` option causes the statement to be **actually executed**,
       not only planned." + "other side effects of the statement will happen as usual."
    → **预检只能用不带 ANALYZE 的 `EXPLAIN`**。写 `EXPLAIN ANALYZE` 等于把用户的
      SQL 真的执行一遍，安全网关形同虚设。本模块的 explain_sql() 从设计上
      不提供 ANALYZE 选项，并有单测断言生成的语句不含 "ANALYZE"。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, FrozenSet, List, Optional, Sequence

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.core.config import settings
from app.core.performance import timed_stage
from app.prompts.text2sql import render_executor_observation

logger = logging.getLogger(__name__)

# 未认证/无租户时使用的哨兵值。
# 它**不可能**与任何真实租户名相同（含 '@' 前缀且带标记），
# 因此在 fail-closed 的 RLS 策略下只能匹配到 is_shared 的共享语料，
# 拿不到任何租户私有数据。
ANONYMOUS_TENANT = "@anonymous@"

# 不允许被当作"租户身份"使用的保留值。
#
# ## 为什么需要这个守卫（两次踩坑的产物）
#
# RLS 的判据是 `tenant_id = current_setting('app.tenant_id')`，
# 因此**任何能出现在 tenant_id 列里的值，都同时是一把潜在的钥匙**。
# 这一条我踩了两次：
#
#   1. 列默认值原为 `''` ⇒ 只要某条路径用空串当身份，
#      `'' = ''` 就成立 ⇒ **全部未归属行立刻可见**（实测过）。
#   2. 改成 `'@unowned@'` 后，用 `'@unowned@'` 当身份又能匹配
#      ⇒ **把哨兵值当身份用，就打开了所有未归属行**（实测过）。
#
# 结论：光挑一个"看起来特殊"的字符串是没用的，
# 必须**在入口处显式禁止**这些保留值被当作身份传入。
# 这样无论列默认值是什么，未归属数据都不会因为"传了某个魔数"而泄露。
RESERVED_TENANT_IDENTITIES = frozenset({"", ANONYMOUS_TENANT, "@unowned@"})


def resolve_tenant_identity(tenant_id: Optional[str]) -> str:
    """把"请求的租户"规范化成可用于 RLS 的身份，并挡住保留值。

    - `None` / 空串 / 任何保留值 -> 一律落到 `ANONYMOUS_TENANT`
      （只可能匹配到 `is_shared` 的行，读不到任何私有/未归属数据）
    - 正常租户名 -> 原样返回

    这是**唯一的租户身份入口**：`_apply_session_guards` 必须经它取值，
    避免各调用点各自判断（那正是"两通道不对称"的成因）。
    """
    if tenant_id is None:
        return ANONYMOUS_TENANT
    value = str(tenant_id).strip()
    if value in RESERVED_TENANT_IDENTITIES:
        if value:
            logger.warning(
                "[executor] 租户身份 %r 是保留值，已按匿名处理"
                "（只可见共享语料）—— 请检查上游是否误传了哨兵值",
                value,
            )
        return ANONYMOUS_TENANT
    return value


@dataclass
class QueryResult:
    """一次 SQL 执行的结果。"""

    ok: bool
    columns: List[str] = field(default_factory=list)
    rows: List[List[Any]] = field(default_factory=list)
    row_count: int = 0
    elapsed_ms: int = 0
    truncated: bool = False          # 是否触达 LIMIT 上限（结果不完整！）
    error: str = ""                  # ok=False 时的数据库报错（可回灌给 LLM）
    # 给 LLM 的紧凑文本表示（大结果集时是统计概要而非明细）
    observation: str = ""
    # RBAC：本次因权限不足被脱敏的列名（透传给前端与 LLM，避免"以为看到了全部"）
    redacted_columns: List[str] = field(default_factory=list)

    def to_payload(self) -> Dict[str, Any]:
        """转成 SSE / JSON 友好的结构（不含 observation 大文本）。"""
        return {
            "ok": self.ok,
            "columns": self.columns,
            "row_count": self.row_count,
            "elapsed_ms": self.elapsed_ms,
            "truncated": self.truncated,
            "error": self.error,
            "redacted_columns": self.redacted_columns,
        }


# ==================== 序列化 ====================

def to_jsonable(v: Any) -> Any:
    """把 PostgreSQL 返回值转成可 JSON 序列化的类型。

    必须做这一步：Decimal / date / datetime 不能被 json.dumps 处理，
    会在 SSE 推帧时抛 TypeError。
    """
    if v is None:
        return None
    if isinstance(v, Decimal):
        # 整数值的 Decimal 去掉小数点，避免 "1042.00" 这种噪音
        f = float(v)
        return int(f) if f.is_integer() else f
    if isinstance(v, (datetime, date)):
        return v.isoformat(sep=" ") if isinstance(v, datetime) else v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return f"<binary {len(v)} bytes>"
    if isinstance(v, (list, tuple)):
        return [to_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: to_jsonable(x) for k, x in v.items()}
    return v


# ==================== 执行器 ====================

class SqlExecutor:
    """只读 SQL 执行器。

    使用方式（同步）：
        ex = SqlExecutor()
        res = ex.execute(guarded_sql)      # guarded_sql 来自 guard.guard_sql()
    """

    def __init__(self, engine: Optional[Engine] = None,
                 employee_engine: Optional[Engine] = None) -> None:
        # 关键：用 sync_analytics_ro_url（psycopg 同步驱动 + kb_ro 只读角色）。
        # 不能用 analytics_url —— 那个是 asyncpg 驱动，create_engine() 会报
        # MissingGreenlet（见 docs/经验教训.md L-008）。
        self._engine = engine or create_engine(
            settings.sync_analytics_ro_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
        )
        self.max_rows = settings.sql_max_rows
        self.timeout_ms = settings.sql_timeout_ms
        self.inline_max_rows = settings.sql_inline_max_rows
        self._employee_engine = employee_engine or create_engine(
            settings.sync_analytics_employee_url, pool_pre_ping=True,
            pool_size=5, max_overflow=5,
        )

    def _execution_engine(self, redact: Optional[FrozenSet[str]]) -> Engine:
        if not redact:
            return self._engine
        return self._employee_engine

    # ---- 生命周期 ----
    def dispose(self) -> None:
        self._engine.dispose()
        if self._employee_engine is not None:
            self._employee_engine.dispose()

    # ---- 会话护栏 ----
    # ⚠️ 为什么用 set_config() 而不是 `SET LOCAL x = :param`：
    #    PostgreSQL 的 `SET` 是 utility 语句，**语法上不接受绑定参数** ——
    #    写成 `SET LOCAL statement_timeout = $1` 会直接报
    #    `syntax error at or near "$1"`（已实测）。
    #    而 `set_config(name, value, is_local)` 是普通函数，**可以**用绑定参数，
    #    且 is_local=true 与 SET LOCAL 语义等价（事务结束即失效）。
    #    这样既避免了字符串拼接，也保留了事务级作用域。
    def _apply_session_guards(self, conn, tenant_id: Optional[str]) -> None:
        # 1) 只读事务：即使 guard 或角色权限被绕过，写操作也会被拒
        conn.execute(text("SELECT set_config('transaction_read_only', 'on', true)"))
        # 2) 语句级超时：保证 PG 侧真的停下，而不是应用侧断开后 PG 还在跑
        conn.execute(
            text("SELECT set_config('statement_timeout', :t, true)"),
            {"t": f"{self.timeout_ms}ms"},
        )
        # 3) RLS：**无条件**设置租户变量（fix：一次真实的越权问题）
        #
        # ⚠️ 原写法是 `if tenant_id:` —— 当 tenant_id 为 None 时**根本不设置变量**。
        #    而旧的 RLS 策略是 fail-open 的（"变量未设置 ⇒ 放行全部行"），
        #    于是"未认证/无租户"的请求会读到**所有租户**的数据。
        #    实测确证：以租户 b 身份但在未设变量的路径上查询，
        #    能看到全公司 10000 行薪资。
        #
        #    现在两道一起改：
        #      - 策略改为 fail-closed（见 ddl.build_rls_sql）：只有
        #        is_shared 或 tenant_id 匹配才放行；
        #      - 这里**永远**设置变量，且身份经 resolve_tenant_identity()
        #        规范化 —— 保留值（空串/@unowned@/哨兵）一律按匿名处理，
        #        从而只能读到 is_shared 的共享语料。
        conn.execute(
            text("SELECT set_config('app.tenant_id', :v, true)"),
            {"v": resolve_tenant_identity(tenant_id)},
        )

    # ---- 预检 ----
    def explain_sql(self, sql: str, tenant_id: Optional[str] = None,
                    redact: Optional[FrozenSet[str]] = None) -> tuple[bool, str]:
        """EXPLAIN 预检：只做计划、**不执行**。

        返回 (是否可规划, 错误信息)。成本极低，却能在真正执行前
        发现列名/类型错误 —— 这是"报错回灌让 LLM 自修正"之外更便宜的一道拦截。

        ⚠️ 绝不使用 EXPLAIN ANALYZE（那会真的执行语句）。
            本函数签名不接受任何"是否 analyze"的参数，从设计上杜绝误用。
        """
        stmt = f"EXPLAIN (FORMAT JSON) {sql}"
        # 防御性断言：即使有人改了上面这行，也不允许 ANALYZE 出现
        assert "ANALYZE" not in stmt.upper(), "EXPLAIN 预检绝不能带 ANALYZE（会真的执行）"

        try:
            with self._execution_engine(redact).connect() as conn:
                self._apply_session_guards(conn, tenant_id)
                conn.execute(text(stmt))
            return True, ""
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            logger.info("[executor] EXPLAIN 预检未通过: %s", msg[:200])
            return False, msg

    # ---- 执行 ----
    @timed_stage("sql")
    def execute(self, sql: str, tenant_id: Optional[str] = None,
                skip_explain: bool = False,
                redact: Optional[FrozenSet[str]] = None,
                actor: str = "") -> QueryResult:
        """执行一条**已通过 guard 校验**的 SQL。

        Args:
            sql: 已被 guard.guard_sql() 改写（含 LIMIT）的语句
            tenant_id: 非空时设置 app.tenant_id，触发 RLS 行级隔离
            skip_explain: 跳过 EXPLAIN 预检（默认执行预检）
            redact: 需要脱敏的列集合（来自 Principal.redact_columns()）；
                    空集走零成本快路径 —— 这是保证"不配置 RBAC 就等于没有该功能"的关键
            actor: 调用方标识（写审计日志用），如 "tenant:role"
        """
        # Reject explicit sensitive references before EXPLAIN, including WHERE,
        # aliases, aggregates, subqueries and CTEs. Database column privileges
        # also block SELECT *, whole-row expressions and parser blind spots.
        if redact:
            from app.analytics.permissions import sensitive_sql_reason
            reason = sensitive_sql_reason(sql, redact)
            if reason:
                self._audit(sql, actor, tenant_id, ok=False, row_count=0,
                            elapsed_ms=0, error=reason, redacted=[])
                self._record_metrics(ok=False, elapsed_ms=0, masked=[])
                return QueryResult(ok=False, error=reason, observation=reason)
        # 1) 预检（便宜且能提前发现列名错误）
        if not skip_explain:
            ok, err = self.explain_sql(sql, tenant_id=tenant_id, redact=redact)
            if not ok:
                self._audit(sql, actor, tenant_id, ok=False, row_count=0,
                            elapsed_ms=0, error=err, redacted=[])
                self._record_metrics(ok=False, elapsed_ms=0, masked=[])
                return QueryResult(
                    ok=False, error=err,
                    observation=f"SQL 无法被数据库规划，错误信息：{err[:400]}\n"
                                f"请检查表名/列名是否准确后重新生成。",
                )

        # 2) 真正的执行
        start = time.perf_counter()
        try:
            with self._execution_engine(redact).connect() as conn:
                self._apply_session_guards(conn, tenant_id)
                result = conn.execute(text(sql))
                columns = list(result.keys())
                # 多读一行用于判断"是否还有更多"。
                # 不变式：guard 注入的外层 LIMIT = max_rows + 1，
                # 因此这里读到 max_rows + 1 行 ⇒ 确实被截断了。
                # 见 guard._inject_limit 的说明；两边必须同步，有单测锁死。
                fetched = result.fetchmany(self.max_rows + 1)
        except Exception as e:  # noqa: BLE001
            elapsed = int((time.perf_counter() - start) * 1000)
            msg = str(e)
            logger.warning("[executor] SQL 执行失败(%dms): %s", elapsed, msg[:300])
            self._record_metrics(ok=False, elapsed_ms=elapsed, masked=[])
            self._audit(sql, actor, tenant_id, ok=False, row_count=0,
                        elapsed_ms=elapsed, error=msg, redacted=[])
            return QueryResult(
                ok=False, elapsed_ms=elapsed, error=msg,
                observation=f"SQL 执行失败，数据库返回：{msg[:400]}\n"
                            f"请据此修正 SQL 后重新生成。",
            )

        elapsed = int((time.perf_counter() - start) * 1000)

        # 3) 截断判定：读到 max_rows+1 行说明还有更多
        truncated = len(fetched) > self.max_rows
        rows = [list(to_jsonable(v) for v in r) for r in fetched[: self.max_rows]]

        # 4) 敏感列脱敏（RBAC 的数据层落地）。
        #    在结果集上做而不是改写 SQL —— 因为 LLM 常写 SELECT *，
        #    按列名脱敏不依赖模型生成什么，是"默认拒绝"式的兜底。
        masked_columns: List[str] = []
        if redact:
            from app.analytics.redact import redact_rows
            rows, masked_columns = redact_rows(columns, rows, redact)

        res = QueryResult(
            ok=True, columns=columns, rows=rows, row_count=len(rows),
            elapsed_ms=elapsed, truncated=truncated,
            redacted_columns=masked_columns,
        )
        res.observation = self.build_observation(res)

        self._record_metrics(ok=True, elapsed_ms=elapsed, masked=masked_columns)
        self._audit(sql, actor, tenant_id, ok=True, row_count=res.row_count,
                    elapsed_ms=elapsed, error="", redacted=masked_columns)
        return res

    @staticmethod
    def _record_metrics(ok: bool, elapsed_ms: int, masked: List[str]) -> None:
        """写 Prometheus 指标（失败不影响主流程）。

        `rag_sql_redactions_total` 按**列**计数 —— 它能回答
        "有没有人在反复试探薪资/邮箱"，是安全运营的观测点。
        """
        try:
            from app.core.metrics import counter_inc, observe

            counter_inc("rag_sql_queries_total", ("true" if ok else "false",))
            observe("rag_sql_duration_seconds", elapsed_ms / 1000.0)
            for col in masked or []:
                counter_inc("rag_sql_redactions_total", (col,))
        except Exception:  # noqa: BLE001
            pass

    # ---- 审计 ----
    def _audit(self, sql: str, actor: str, tenant_id: Optional[str], *,
               ok: bool, row_count: int, elapsed_ms: int, error: str,
               redacted: List[str]) -> None:
        """把每次 SQL 执行落库（审计 + 成本观测）。

        为什么必须做：接入 Text2SQL 后，"谁在什么时候查了什么"是合规要求；
        且 Agent 可能生成意外查询，审计是唯一的追溯手段。
        审计失败**绝不能影响主流程** —— 因此这里吞掉异常只记日志。
        """
        if not settings.sql_audit_enabled:
            return
        try:
            from app.analytics.audit import record_sql_audit
            record_sql_audit(
                sql=sql, actor=actor, tenant_id=tenant_id, ok=ok,
                row_count=row_count, elapsed_ms=elapsed_ms, error=error,
                redacted_columns=redacted,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[executor] 审计写入失败（不影响主流程）: %s", str(e)[:200])

    # ---- 结果 -> LLM observation ----
    def build_observation(self, res: QueryResult) -> str:
        """把结果集转成给 LLM 看的文本。

        **大结果集保护**（关键设计）：超过 inline_max_rows 时**不给明细**，
        只给「列名 + 行数 + 前几行样例 + 截断标志」。

        为什么必须这样做：
          - LLM **无法预知**结果集大小，若把上千行塞进 prompt 会直接炸掉上下文；
          - 更危险的是：如果不标注截断，LLM 会非常自然地把"前 N 行"当成"全部数据"
            来回答 —— 于是又回到"错误但自信"的老问题，只是这次错误来自截断而非检索。
            所以 truncated 标志必须同时出现在 SSE 事件（给用户看）和 observation（给 LLM 看）。
        """
        return render_executor_observation(
            ok=res.ok,
            observation=res.observation,
            redacted_columns=res.redacted_columns,
            row_count=res.row_count,
            columns=res.columns,
            elapsed_ms=res.elapsed_ms,
            rows=res.rows,
            truncated=res.truncated,
            inline_max_rows=self.inline_max_rows,
        )


# ==================== 全局单例 ====================

_executor: Optional[SqlExecutor] = None


def get_executor() -> SqlExecutor:
    """进程级单例（避免每请求重建连接池）。"""
    global _executor
    if _executor is None:
        _executor = SqlExecutor()
    return _executor
