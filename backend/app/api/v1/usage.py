"""usage.py - 用量与成本看板路由（P7）。

为什么单独一个路由：
    Agent 一次问答可调 1~8 次 LLM，加上 SQL 执行，
    "花多少 token、多少次调用、哪儿慢"必须能被直接看到 ——
    否则这就是个不可运营的系统。

两个端点：
    GET /usage       LLM token 用量聚合（按场景/模型/租户/角色分组）
    GET /usage/sql   SQL 执行的健康度（成功/拒绝/脱敏/耗时）

两者都走**只读聚合查询**，不触发任何模型调用。
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Text, func, select
from sqlalchemy import cast as sa_cast
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_principal
from app.core.security import Principal
from app.models.sql_audit import SqlAudit

logger = logging.getLogger(__name__)

router = APIRouter(tags=["usage"])


@router.get("/usage", summary="LLM token 用量聚合（成本看板）")
async def llm_usage(
    days: int = Query(7, ge=1, le=90, description="统计最近多少天"),
    group_by: str = Query("scene", pattern="^(scene|model|tenant_id|actor)$"),
    principal: Principal = Depends(get_principal),
) -> dict:
    """按维度聚合 LLM 用量。

    非特权角色只看得到**自己租户**的数据（与 RAG/SQL 的租户隔离一致）——
    成本数据同样是数据，不应越权可见。
    """
    from app.observability.usage import summarize_usage

    tenant_filter = None
    if principal.tenant_id is not None and not principal.can_see_sensitive:
        # 非特权角色限制到本租户（analyst 可看全局，便于运营视角）
        tenant_filter = principal.tenant_id

    try:
        return summarize_usage(days=days, tenant_id=tenant_filter, group_by=group_by)
    except Exception as e:  # noqa: BLE001
        # 表可能尚未迁移 —— 返回可读错误而不是 500
        logger.warning("[usage] 聚合失败: %s", e)
        return {
            "error": "用量数据暂不可用（表未迁移或数据库不可达）",
            "detail": str(e)[:200],
        }


@router.get("/usage/sql", summary="SQL 执行健康度（成功/拒绝/脱敏/耗时）")
async def sql_usage(
    days: int = Query(7, ge=1, le=90),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> dict:
    """SQL 执行统计：成功数、脱敏触发数、平均耗时、token 无关但反映用量。

    这些数字就是简历上可以写的运营指标来源：
      - SQL 拒绝率（安全网关在真实流量下挡住了多少）
      - 脱敏触发次数（有多少人在试探敏感数据）
      - 平均查询耗时
    """
    # ⚠️ 判断"是否发生脱敏"不能用 `redacted_columns IS NOT NULL`。
    #    实测发现：JSONB 列里存 JSON null 时，SQL 的 `IS NOT NULL` **仍然为真** ——
    #    141 行全部被误计为"已脱敏"，而实际只有 3 行真的脱敏了。
    #    正确做法是把 JSONB 值取文本再比较（JSON null 的文本形态是 'null'）。
    #    这个坑不会报错，只会让指标静默失真 —— 见 docs/经验教训.md L-016。
    redacted_json_expr = func.coalesce(sa_cast(SqlAudit.redacted_columns, Text), "null") != "null"

    since = func.now() - func.make_interval(0, 0, 0, days)

    stmt = select(
        func.count(SqlAudit.id),
        func.count(SqlAudit.id).filter(SqlAudit.ok.is_(True)),
        func.count(SqlAudit.id).filter(redacted_json_expr),
        func.coalesce(func.avg(SqlAudit.elapsed_ms), 0),
        func.coalesce(func.max(SqlAudit.elapsed_ms), 0),
    ).where(SqlAudit.created_at >= since)

    # 租户隔离：非特权角色只看本租户
    if principal.tenant_id is not None and not principal.can_see_sensitive:
        stmt = stmt.where(SqlAudit.tenant_id == principal.tenant_id)

    total, ok, redacted, avg_ms, max_ms = (await session.execute(stmt)).one()

    return {
        "window_days": days,
        "total_queries": int(total or 0),
        "succeeded": int(ok or 0),
        "failed": int((total or 0) - (ok or 0)),
        "redacted_queries": int(redacted or 0),
        "avg_elapsed_ms": round(float(avg_ms or 0), 1),
        "max_elapsed_ms": int(max_ms or 0),
    }
