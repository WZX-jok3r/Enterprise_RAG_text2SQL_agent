"""usage.py - LLM 用量记账的写入与聚合查询。

唯一的写入方是 `llm/tool_client.py`（每次 tool-calling 调用后记一条）；
`llm/provider.py` 只从响应里提取 usage 返回给调用方，**不落库**。
**失败绝不阻断主流程**（记账不该让一次问答失败）。

为什么在 observability 而不是 analytics：这是横切的用量观测，与 Text2SQL
业务无关；放这里也避免 `llm/` 反向依赖 `analytics/`。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.core.config import settings

logger = logging.getLogger(__name__)

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(settings.sync_postgres_url, pool_pre_ping=True)
    return _engine


def record_usage(
    scene: str,
    model: str,
    usage: Optional[Dict[str, Any]] = None,
    actor: str = "",
    tenant_id: Optional[str] = None,
    session_id: Optional[str] = None,
    elapsed_ms: int = 0,
) -> None:
    """写一条用量记录。usage 为 OpenAI 兼容的 usage 字典（可为空）。"""
    if not usage:
        return

    # Prometheus 指标（与落库并行）：即使落库失败，指标仍能反映消耗趋势
    try:
        from app.core.metrics import counter_inc

        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)
        if pt:
            counter_inc("rag_llm_tokens_total", (scene, "prompt"), pt)
        if ct:
            counter_inc("rag_llm_tokens_total", (scene, "completion"), ct)
    except Exception:  # noqa: BLE001
        pass

    try:
        from app.models.llm_usage import LlmUsage

        with Session(_get_engine()) as s:
            s.add(LlmUsage(
                scene=scene,
                model=model or "unknown",
                actor=actor or None,
                tenant_id=tenant_id,
                session_id=session_id,
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                total_tokens=int(usage.get("total_tokens") or 0),
                elapsed_ms=elapsed_ms,
            ))
            s.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("[usage] 记账失败（不影响主流程）: %s", str(e)[:200])


# ==================== 聚合查询（供 /usage 端点与成本看板）====================

def summarize_usage(days: int = 7, tenant_id: Optional[str] = None,
                    group_by: str = "scene") -> Dict[str, Any]:
    """汇总最近 N 天的用量。

    Args:
        group_by: scene | model | tenant_id | actor
    """
    from app.models.llm_usage import LlmUsage

    col_map = {
        "scene": LlmUsage.scene,
        "model": LlmUsage.model,
        "tenant_id": LlmUsage.tenant_id,
        "actor": LlmUsage.actor,
    }
    col = col_map.get(group_by, LlmUsage.scene)

    with Session(_get_engine()) as s:
        stmt = (
            select(
                col,
                func.count(LlmUsage.id),
                func.coalesce(func.sum(LlmUsage.prompt_tokens), 0),
                func.coalesce(func.sum(LlmUsage.completion_tokens), 0),
                func.coalesce(func.sum(LlmUsage.total_tokens), 0),
                func.coalesce(func.avg(LlmUsage.elapsed_ms), 0),
            )
            .where(LlmUsage.created_at >= func.now() - func.make_interval(0, 0, 0, days))
            .group_by(col)
            .order_by(func.sum(LlmUsage.total_tokens).desc())
        )
        if tenant_id is not None:
            stmt = stmt.where(LlmUsage.tenant_id == tenant_id)

        rows = s.execute(stmt).fetchall()

        # 总量
        total_stmt = select(
            func.count(LlmUsage.id),
            func.coalesce(func.sum(LlmUsage.total_tokens), 0),
        ).where(LlmUsage.created_at >= func.now() - func.make_interval(0, 0, 0, days))
        if tenant_id is not None:
            total_stmt = total_stmt.where(LlmUsage.tenant_id == tenant_id)
        total_calls, total_tokens = s.execute(total_stmt).one()

    items: List[Dict[str, Any]] = []
    for name, calls, ptok, ctok, ttok, avg_ms in rows:
        items.append({
            "key": name or "(未标注)",
            "calls": int(calls),
            "prompt_tokens": int(ptok),
            "completion_tokens": int(ctok),
            "total_tokens": int(ttok),
            "avg_elapsed_ms": round(float(avg_ms or 0), 1),
            "tokens_per_call": round(int(ttok) / int(calls), 1) if calls else 0,
        })

    return {
        "window_days": days,
        "group_by": group_by,
        "total_calls": int(total_calls),
        "total_tokens": int(total_tokens),
        "tokens_per_call": round(int(total_tokens) / int(total_calls), 1) if total_calls else 0,
        "items": items,
    }
