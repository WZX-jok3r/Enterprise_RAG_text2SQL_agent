"""ddl.py - 业务表建表（在 kb_analytics 库，幂等）。

设计取舍：
- **业务表不走 Alembic**。理由：业务表随数据源变化（上传一个新 xlsx 就可能多一张表），
  若走 Alembic 就变成"上传一个文件生成一个迁移文件"，运维上不可接受。
  元数据两表（analytics_tables/columns）走 Alembic，因为它们描述的是系统能力。
- **表结构与数据装载同源**：都由 schema_infer 的推断结果驱动，
  避免"DDL 手写、装载按另一套规则"导致列错位。
- **标识符一律经 sqlglot 转义**。虽然 schema_infer 产出的标识符已只含 [a-z0-9_]，
  但仍走转义（双保险），确保任何情况下都不会因标识符拼接而注入。

RLS 设计（多租户）：
  业务表带 `tenant_id`，启用 RLS 并绑定 `app.tenant_id` 会话变量。
  策略写成 **未设置变量时放行全部**，原因：
    - 演示语料是全公司共享数据（不属于某个租户），若默认拒绝会导致什么都查不到；
    - "未认证 = 见全部" 与既有 RAG 通道的 `auth_enabled=False` 软关闭语义一致；
    - 一旦设置了变量（认证用户），就**只**能看到本租户数据 —— 这是硬隔离。
  这个取舍必须显式记录，因为它与"默认拒绝"的安全直觉相反。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlglot import exp

from app.analytics.schema_infer import InferredSchema

logger = logging.getLogger(__name__)

# 多租户列名与 RLS 会话变量（与既有 RAG 通道的 tenant_field 语义保持一致）
TENANT_COLUMN = "tenant_id"
TENANT_GUC = "app.tenant_id"

# "共享语料"标记列。
#
# ## 为什么需要它（一次真实安全问题的产物）
#
# 本项目有两类结构化数据，最初的 RLS 策略无法区分，导致两个真实漏洞：
#   ① **租户私有数据**（某个租户通过 API 上传的 xlsx）—— 必须只有该租户可见
#   ② **全公司共享语料**（demo 知识库、公司级报表）—— 应该所有租户可见
#
# 最初只有一个 tenant_id 列，且 RLS 写成"会话变量未设置 ⇒ 放行全部"。后果：
#   - ETL 把上传数据的 tenant_id 一律写成 DEFAULT_TENANT('a')
#     ⇒ 租户 b 上传后查不到自己的数据（表现为"传了但查不到"）
#   - 一旦执行路径没设置 app.tenant_id（`if tenant_id:` 在 None 时跳过）
#     ⇒ 策略放行**所有行**，租户 b 能看到全公司薪资（跨租户可见）
#
# 修复思路：把"归属"和"可见性"两个概念拆开
#   - `tenant_id`：数据归属（谁传的）
#   - `is_shared`：是否全公司共享（默认 false）
# 并让策略**默认拒绝**：只有 is_shared 或 tenant_id 匹配才放行。
SHARED_COLUMN = "is_shared"

# 未归属数据的 tenant_id 默认值。
#
# ⚠️ 刻意**不用空串 ''**：
#   执行器在"无租户"时会把 app.tenant_id 设成哨兵值 `@anonymous@`
#   （见 executor.ANONYMOUS_TENANT，刻意非空以免与"未设置"混淆）。
#   但如果列默认值是 ''，那么只要有人把哨兵改成 ''（或某条路径用了空串），
#   策略里 `tenant_id = current_setting(...)` 就会变成 `'' = ''` ⇒ **匹配**，
#   于是**全部未归属行立刻对那个身份可见** —— 一次静默的权限放开。
#
#   用一个不可能与真实租户名或任何哨兵相同的常量做默认值，
#   让"未归属"在**数据库层面**就无法被匹配到。
#   这样即使应用层传错值，也不会意外读到未归属数据。
UNOWNED_TENANT = "@unowned@"


def q(identifier: str) -> str:
    """把标识符转义为 PostgreSQL 双引号形式（防注入）。"""
    return exp.to_identifier(str(identifier), quoted=True).sql(dialect="postgres")


def build_create_table_sql(schema: InferredSchema, tenant_column: bool = True) -> str:
    """由推断结果生成 CREATE TABLE IF NOT EXISTS 语句。

    列定义来自 schema.columns；主键取第一个 is_primary_key 的列，
    若没有主键列则不加主键约束（不伪造 ID —— 伪造会掩盖数据问题）。
    """
    lines: List[str] = []
    pk_cols: List[str] = []

    for col in schema.columns:
        parts = [f"  {q(col.name)} {col.sql_type}"]
        if col.is_primary_key:
            parts.append("NOT NULL")
            pk_cols.append(col.name)
        lines.append(" ".join(parts))

    if tenant_column:
        # tenant_id 的默认值用 UNOWNED_TENANT（不可匹配的哨兵）而不是空串：
        # 空串会与应用层可能传入的空串身份碰撞，导致未归属行意外可见。
        lines.append(
            f"  {q(TENANT_COLUMN)} VARCHAR(64) NOT NULL DEFAULT '{UNOWNED_TENANT}'"
        )
        lines.append(f"  {q(SHARED_COLUMN)} BOOLEAN NOT NULL DEFAULT FALSE")

    if pk_cols:
        lines.append(f"  PRIMARY KEY ({', '.join(q(c) for c in pk_cols)})")

    body = ",\n".join(lines)
    return f"CREATE TABLE IF NOT EXISTS {q(schema.table_name)} (\n{body}\n)"


def build_comments_sql(schema: InferredSchema) -> List[str]:
    """生成 COMMENT ON 语句（表 + 列）。

    COMMENT 是 PostgreSQL schema introspection 的一等公民，
    也是喂给 LLM 的「数据字典」的原始来源 —— 枚举值内联就在这里落地。
    """
    stmts: List[str] = []
    table_comment = schema.description or schema.display_name
    if table_comment:
        escaped = table_comment.replace("'", "''")
        stmts.append(f"COMMENT ON TABLE {q(schema.table_name)} IS '{escaped}'")

    for col in schema.columns:
        comment = col.comment().replace("'", "''")
        stmts.append(f"COMMENT ON COLUMN {q(schema.table_name)}.{q(col.name)} IS '{comment}'")

    # 数值列建索引（LLM 高频做聚合/排序/过滤）
    for col in schema.columns:
        if col.sql_type.startswith(("INTEGER", "NUMERIC", "DATE")) or col.is_primary_key:
            stmts.append(
                f"CREATE INDEX IF NOT EXISTS {q(f'ix_{schema.table_name}_{col.name}')} "
                f"ON {q(schema.table_name)} ({q(col.name)})"
            )
    return stmts


def build_rls_sql(table_name: str) -> List[str]:
    """启用 RLS 并绑定 app.tenant_id 会话变量。

    ## 策略语义（**fail-closed**，这是修复安全问题的关键）

    只有满足以下**任一**条件才放行：
      1. `is_shared = true` —— 全公司共享语料（demo 知识库、公司级报表）
      2. `tenant_id = current_setting('app.tenant_id')` —— 本租户私有数据

    否则**一律拒绝**。

    ## 为什么改成 fail-closed（原策略是 fail-open，有真实漏洞）

    原策略写成：
        `app.tenant_id` IS NULL OR = '' OR tenant_id = app.tenant_id
    即"会话变量没设置就放行全部"。当时理由是"未认证时看全部，与 RAG 侧软关闭一致"。
    但它有一个致命后果：
        **任何忘记设置 app.tenant_id 的执行路径都会拿到全量数据。**
    实测确证：执行器里写的是 `if tenant_id:` —— 当 tenant_id 为 None 时
    根本不设置变量，于是策略放行所有行，租户 b 能看到全公司薪资。

    现在改为 fail-closed：忘设变量 ⇒ 只放行 is_shared 的行（共享语料），
    拿不到任何租户私有数据。**忘设只损失功能，不泄露数据。**

    注：`current_setting(..., true)` 在未设置时返回 NULL，
    所以未设变量时条件 2 为 NULL（不成立），自然落到"拒绝" —— 正是我们要的。
    """
    return [
        f"ALTER TABLE {q(table_name)} ENABLE ROW LEVEL SECURITY",
        # 幂等：先删后建
        f"DROP POLICY IF EXISTS {q(f'tenant_isolation_{table_name}')} ON {q(table_name)}",
        (
            f"CREATE POLICY {q(f'tenant_isolation_{table_name}')} ON {q(table_name)} "
            f"USING ("
            f"  {q(SHARED_COLUMN)} IS TRUE "
            f"  OR {q(TENANT_COLUMN)} = current_setting('{TENANT_GUC}', true)"
            f")"
        ),
    ]


def ensure_tenant_columns(
    engine: Engine, table_name: str, mark_shared: bool = False,
    apply_rls: bool = True,
) -> dict:
    """为**已存在**的表补齐租户列并启用 fail-closed RLS（幂等）。

    ## 为什么需要补列

    `create_or_replace_table` 会 DROP 重建，因此新表天然带两列；
    但**升级前就已存在**的表没有 `is_shared` 列 —— 此时新的 RLS 策略引用它会
    直接报 `column "is_shared" does not exist`，导致整表不可读。

    ## ⚠️ 这里曾经是一次 fail-open 迁移（已修正）

    初版在补列之后**自动把存量行全部标记为 `is_shared = TRUE`**，
    理由是"它们是升级前的共享语料"。这个判断有一个致命问题：

        **共享 = 最宽松的可见级别。自动选择最宽松的级别，
        正是 fail-open 的定义。**

    如果某张表其实是租户私有数据（例如升级前用 API 上传、带着真实 tenant_id 的表），
    这次迁移会把它的所有行**对所有租户开放** —— 一次静默的权限放开，
    而且看起来像"兼容性处理"，不会有人怀疑。

    初版还用一个"该表是否已有共享行"的启发式来决定要不要标记，
    但**没有任何启发式能证明"存量数据本来就该共享"**。

    ## ⚠️ 第二个缺陷：只补列不装策略，等于"升级"了个假的安全

    初版只执行 `ALTER TABLE ... ADD COLUMN`，**没有调用 `build_rls_sql`**。
    实测一张"旧结构"表补列后：`RLS 启用=False，策略=[]` ——
    也就是**完全没有租户保护**，任何租户都能读全部行。

    这解释了一个诡异的测试现象：改完 fail-closed 之后，
    原测试 `assert _count(ro_engine, TENANT_A) == 1`（断言"补列后仍可见"）
    **依然通过**。它通过不是因为 fail-closed 失效，
    而是因为**那张表根本没有策略**，所以当然可见 ——
    **测试通过的理由和它声称的理由完全无关。**

    现在默认 `apply_rls=True`，补列的同时装上 fail-closed 策略。

    ## 语义：fail-closed + 显式 opt-in

    - 补列时 `is_shared` 默认 **FALSE**，**绝不自动标记任何行**
    - 需要共享的表由调用方**显式**声明（`mark_shared=True`，
      或在重灌时用 `load_file(..., is_shared=True)`）
    - 迁移后若有行"因 fail-closed 而变得不可见"，函数会**返回计数并告警**，
      让这件事被看见，而不是静默发生

    Args:
        mark_shared: 仅当调用方**明确知道**该表是共享语料时才传 True。
            默认 False —— 拿不准时保持不可见（fail-closed）。
        apply_rls: 是否同时装上 RLS 策略。默认 True（安全默认）。
            仅在做纯结构迁移（如逐表分批上线策略）时才传 False。

    Returns:
        {"added_columns": [...], "private_rows": n, "marked_shared": n,
         "rls_applied": bool}
    """
    stmts = [
        f"ALTER TABLE {q(table_name)} "
        f"ADD COLUMN IF NOT EXISTS {q(TENANT_COLUMN)} "
        f"VARCHAR(64) NOT NULL DEFAULT '{UNOWNED_TENANT}'",
        f"ALTER TABLE {q(table_name)} "
        f"ADD COLUMN IF NOT EXISTS {q(SHARED_COLUMN)} BOOLEAN NOT NULL DEFAULT FALSE",
    ]
    result = {"added_columns": [TENANT_COLUMN, SHARED_COLUMN],
              "private_rows": 0, "marked_shared": 0, "rls_applied": False}

    with engine.begin() as conn:
        for stmt in stmts:
            conn.execute(text(stmt))

        if apply_rls:
            # 补列之后必须装策略 —— 否则"升级"出来的仍是一张裸表
            for stmt in build_rls_sql(table_name):
                conn.execute(text(stmt))
            result["rls_applied"] = True

        if mark_shared:
            marked = conn.execute(text(
                f"UPDATE {q(table_name)} SET {q(SHARED_COLUMN)} = TRUE "
                f"WHERE {q(SHARED_COLUMN)} IS NOT TRUE"
            )).rowcount
            result["marked_shared"] = int(marked or 0)
            logger.warning(
                "[ddl] 表 %s 的 %d 行被**显式**标记为共享（对所有租户可见）—— "
                "请确认该表确实是共享语料", table_name, result["marked_shared"],
            )
        else:
            # fail-closed：统计有多少行会因既不共享、又无归属而不可见
            n = conn.execute(text(
                f"SELECT count(*) FROM {q(table_name)} "
                f"WHERE {q(SHARED_COLUMN)} IS NOT TRUE "
                f"  AND ({q(TENANT_COLUMN)} IS NULL OR {q(TENANT_COLUMN)} = '')"
            )).scalar_one()
            result["private_rows"] = int(n or 0)
            if result["private_rows"]:
                logger.warning(
                    "[ddl] 表 %s 补列后有 %d 行既非共享、又无归属租户 —— "
                    "在 fail-closed 策略下它们对所有人不可见。"
                    "若这些是共享语料，请显式调用 mark_table_shared() 或 "
                    "用 load_file(..., is_shared=True) 重灌",
                    table_name, result["private_rows"],
                )
    return result


def mark_table_shared(engine: Engine, table_name: str) -> int:
    """**显式**把一张表的全部行标记为共享语料（对所有租户可见）。返回影响行数。

    刻意做成独立函数、且不放进任何自动流程：把权限放开变回
    **一个有名字、需要被调用的动作**，而不是迁移的副作用。
    """
    with engine.begin() as conn:
        n = conn.execute(text(
            f"UPDATE {q(table_name)} SET {q(SHARED_COLUMN)} = TRUE "
            f"WHERE {q(SHARED_COLUMN)} IS NOT TRUE"
        )).rowcount
    logger.warning("[ddl] 表 %s 的 %d 行已标记为共享（对所有租户可见）",
                   table_name, int(n or 0))
    return int(n or 0)


class TableOwnershipError(RuntimeError):
    """不能安全覆盖目标表（表里有别的租户的数据）。

    刻意**中断入库**而不是静默覆盖：数据丢失是不可逆的，
    而入库失败是可重试/可提示的。
    """


def inspect_table_ownership(engine: Engine, table_name: str) -> Dict[str, Any]:
    """查看目标表里已有哪些**归属**的行。

    Returns:
        {"exists": bool, "tenants": {tenant_id: 行数}, "shared_rows": n,
         "unowned_rows": n, "total_rows": n}
    """
    info: Dict[str, Any] = {"exists": False, "tenants": {}, "shared_rows": 0,
                            "unowned_rows": 0, "total_rows": 0}
    if not table_exists(engine, table_name):
        return info
    info["exists"] = True

    with engine.connect() as conn:
        cols = {r[0] for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=:t"), {"t": table_name})}
        if TENANT_COLUMN not in cols:
            # 老表没有租户列：视为"未知归属"，按不可安全覆盖处理
            info["unknown_ownership"] = True
            return info

        has_shared = SHARED_COLUMN in cols
        # 分组列 = 选出的非聚合列（**不要**把 count(*) 也放进 GROUP BY）
        group_cols = ([q(SHARED_COLUMN)] if has_shared else []) + [q(TENANT_COLUMN)]
        sel = group_cols + ["count(*)"]
        rows = conn.execute(text(
            f"SELECT {', '.join(sel)} FROM {q(table_name)} "
            f"GROUP BY {', '.join(group_cols)}")).fetchall()

    for r in rows:
        if has_shared:
            shared, tid, n = bool(r[0]), r[1], int(r[2])
        else:
            shared, tid, n = False, r[0], int(r[1])
        if shared:
            info["shared_rows"] += n
        elif not tid or tid == UNOWNED_TENANT:
            info["unowned_rows"] += n
        else:
            info["tenants"][tid] = info["tenants"].get(tid, 0) + n
        info["total_rows"] += n
    return info


def assert_can_replace(
    engine: Engine, table_name: str, tenant_id: str, is_shared: bool
) -> None:
    """覆盖前确认"这张表是不是**本次调用方自己的**"，否则拒绝。

    ## 为什么必须做这个检查（一次真实的跨租户数据破坏）

    表名由 `文件名 + sheet 名` 生成，**不含租户**（见 schema_infer），
    而建表走的是 `DROP TABLE IF EXISTS ... CASCADE`。实测复现：

        租户 A 上传 shared_report.xlsx(sheet=Shared) -> 表 shared，A 的 2 行
        租户 B 上传**同名文件**                      -> 表名同样是 shared
        DROP 执行 -> **A 的数据被物理删除**，只剩 B 的 2 行

    RLS 挡不住这个：行级安全只管"读"，`DROP TABLE` 是 DDL，
    一旦执行 A 的数据就没了 —— 比"读到别人的数据"更严重（不可逆）。

    判定规则：
      - 表不存在                      -> 允许（正常新建）
      - 表里**只有**本次调用方自己的行 -> 允许（正常重灌，CLI 反复导入的场景）
      - 表里全是共享行                -> 仅当本次也是共享装载时允许
      - 表里有**别人的行**或旧表归属未知 -> **拒绝**，抛 TableOwnershipError

    刻意选择"拒绝"而不是"自动改名"：自动改名会让同一份文件
    在不同租户下产生不同表名，LLM 的表枚举与用户预期都会变得不可预测；
    而"拒绝 + 明确报错"把决定权交回给人（改名后重传，或先下线旧表）。
    """
    info = inspect_table_ownership(engine, table_name)
    if not info["exists"]:
        return

    if info.get("unknown_ownership"):
        raise TableOwnershipError(
            f"表 {table_name} 缺少租户列，无法确认归属，拒绝覆盖。"
            f"如确认可覆盖，请先手工删除该表。"
        )

    mine = tenant_id if tenant_id else UNOWNED_TENANT
    others = {t: n for t, n in info["tenants"].items() if t != mine}
    has_mine = info["tenants"].get(mine, 0) > 0

    def _describe() -> str:
        parts = []
        if info["tenants"]:
            parts.append("归属行: " + ", ".join(
                f"{t}={n}" for t, n in sorted(info["tenants"].items())))
        if info["shared_rows"]:
            parts.append(f"共享行={info['shared_rows']}")
        if info["unowned_rows"]:
            parts.append(f"无归属行={info['unowned_rows']}")
        return "；".join(parts) or "空表"

    if others:
        raise TableOwnershipError(
            f"表 {table_name} 中已有**其它租户**的数据（{others}），"
            f"本次归属={mine or '无'}，拒绝覆盖以免删除他人数据。"
            f"现状：{_describe()}。"
            f"请改用不同的文件名/sheet 名，或先由数据归属方下线该表。"
        )
    if info["shared_rows"] and not is_shared:
        raise TableOwnershipError(
            f"表 {table_name} 已作为**共享语料**存在（{info['shared_rows']} 行），"
            f"而本次是租户私有装载，拒绝覆盖。现状：{_describe()}。"
        )
    if has_mine and is_shared:
        # 反向：把某租户私有的表改成共享 —— 是"权限放开"，也要显式确认
        raise TableOwnershipError(
            f"表 {table_name} 已有租户 {mine} 的私有数据（{info['tenants'][mine]} 行），"
            f"而本次要写成共享语料（对所有租户可见），拒绝自动放开权限。"
            f"如确实要转为共享，请显式调用 mark_table_shared()。"
        )
    if info["unowned_rows"] and not (has_mine or info["shared_rows"]):
        # 只有无归属行：本次若也是无归属装载，属于同质重灌，放行
        if mine != UNOWNED_TENANT:
            raise TableOwnershipError(
                f"表 {table_name} 存在 {info['unowned_rows']} 行无归属数据，"
                f"无法确认是否为本次调用方所有，拒绝覆盖。现状：{_describe()}。"
            )


def create_or_replace_table(
    engine: Engine,
    schema: InferredSchema,
    tenant_id: str = "",
    is_shared: bool = False,
) -> None:
    """建表 + 注释 + 索引 + RLS。**会先 DROP 再建**（重灌语义）。

    ⚠️ 覆盖前会做**归属检查**（`assert_can_replace`）：
    表名不含租户，同名文件会被不同租户算出同一个表名，
    无条件 DROP 会物理删除他人的数据（已实测复现）。
    发现表里有别人的行时抛 `TableOwnershipError` 中断入库，而不是静默覆盖。

    注意：本函数只建**表结构**并写列注释；行数据的 tenant_id / is_shared
    由 `seed._copy_rows` 在 COPY 时逐行写入（见该函数的 tenant_id 参数）。
    """
    assert_can_replace(engine, schema.table_name, tenant_id, is_shared)

    with engine.begin() as conn:
        # 重灌语义：先删旧表，保证列结构变化时不会残留旧列
        conn.execute(text(f"DROP TABLE IF EXISTS {q(schema.table_name)} CASCADE"))
        conn.execute(text(build_create_table_sql(schema)))
        for stmt in build_comments_sql(schema):
            conn.execute(text(stmt))
        for stmt in build_rls_sql(schema.table_name):
            conn.execute(text(stmt))
    logger.info("[ddl] 已重建表 %s（%d 列）", schema.table_name, len(schema.columns))


def table_exists(engine: Engine, table_name: str) -> bool:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name=:n"
            ),
            {"n": table_name},
        ).first()
    return row is not None
