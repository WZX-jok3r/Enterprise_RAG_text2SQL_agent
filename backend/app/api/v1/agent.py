"""agent.py - Agent 对话路由（SSE 流式，含执行轨迹）。

设计要点：
1. **新增独立路由，不改动既有 /chat 与 /chat/stream** ——
   既有两条路由是前端与评测的稳定契约，其行为已被冻结。
   Agent 是一条新路径，旧路径零改动，因此**旧前端不会因为本路由而退化**。
2. SSE 事件是"扩帧"后的 10 类（见 app/agent/events.py）。前端对未知 type
   走 default 忽略分支，所以旧前端接这条路由也不会崩。
3. Agent Loop 是**同步生成器**（内部要调同步的 Retriever / SQL 执行器），
   用 `iterate_in_threadpool` 桥接进异步路由 —— 与既有 chat/stream 同款做法，
   事件循环不被阻塞。
4. 与既有会话机制共用 sessions/messages 表，因此 Agent 对话也可多轮。
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from starlette.concurrency import iterate_in_threadpool

from app.agent.loop import get_agent_loop
from app.agent.router import Route, route_by_rules
from app.api.deps import get_principal, get_session_service
from app.core.security import Principal
from app.schemas.agent import AgentChatRequest
from app.services.session_service import SessionService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["agent"])


async def _resolve_session(
    req: AgentChatRequest, principal: Principal, svc: SessionService
) -> str:
    """复用有效 session，否则新建（与 chat.py 同口径，保证租户隔离一致）。"""
    if req.session_id and await svc.exists(req.session_id, principal.tenant_id):
        return req.session_id
    return await svc.create(principal.tenant_id)


def route_preview(question: str) -> dict:
    """把规则路由结果暴露成一个纯函数，便于单测与"路由调试"接口。"""
    d = route_by_rules(question)
    return {
        "route": d.route.value,
        "reason": d.reason,
        "matched_aggregation": d.matched_aggregation,
        "matched_column": d.matched_column,
        "matched_doc_word": d.matched_doc_word,
    }


@router.post("/agent/stream", summary="内部知识库 Agent（SSE 流式，含执行轨迹）")
async def agent_stream(
    req: AgentChatRequest,
    principal: Principal = Depends(get_principal),
    svc: SessionService = Depends(get_session_service),
) -> StreamingResponse:
    """Agent 主入口。

    事件序列（扩帧后的完整契约）：
        route -> [tool_call -> tool_result -> sql?] ... -> [meta]? -> delta -> done
    异常时追加 error 事件；降级时插入 degraded 事件。
    """
    session_id = await _resolve_session(req, principal, svc)
    history = await svc.get_history(session_id, principal.tenant_id)
    await svc.append(session_id, principal.tenant_id,
                     {"role": "user", "content": req.message})

    # 规则路由在**路由层**先算一次：既便于把它透传给 Agent，
    # 也便于将来做"路由调试"接口与统计（哪类问题走了哪条路）。
    decision = route_by_rules(req.message)
    logger.info(
        "[agent] 路由=%s session=%s tenant=%s reason=%s",
        decision.route.value, session_id, principal.tenant_id, decision.reason,
    )

    loop = get_agent_loop()
    # 调试用强制路由：把字符串转成 Route 枚举（pydantic 已用 pattern 限制了取值）
    forced = Route(req.force_route) if req.force_route else None
    # RBAC：把身份权限收敛成两个值传给 Agent ——
    #   redact 为空集时执行器走零成本快路径（等价于未启用 RBAC）
    #   actor 用于审计日志（形如 "tenantA:employee"）
    redact = principal.redact_columns()
    actor = f"{principal.tenant_id or 'anonymous'}:{principal.role}"
    if redact:
        logger.info("[agent] RBAC 生效：角色=%s 将脱敏 %d 个敏感列",
                    principal.role, len(redact))
    # Agent Loop 是同步生成器 -> 用线程池逐步驱动，事件循环不被占住
    sync_iter = loop.run(
        req.message,
        tenant_id=principal.tenant_id,
        history=history,
        forced_route=forced,
        redact=redact,
        actor=actor,
    )

    async def event_gen():
        collected: list = []
        try:
            async for ev in iterate_in_threadpool(sync_iter):
                # done 事件：把 assistant 答案落库，并回传 session_id 供前端续聊
                if ev.get("type") == "done":
                    answer = ev.get("answer", "")
                    # 随 assistant 消息一并落库**本轮工具轨迹**（执行过的 SQL + 结果行），
                    # 供下一轮追问延续上下文。与 content 分开存：
                    # content 供前端渲染，tool_context 只喂模型（见 build_tool_context）。
                    await svc.append(
                        session_id, principal.tenant_id,
                        {"role": "assistant", "content": answer,
                         "tool_context": loop.last_tool_context or None},
                    )
                    ev["session_id"] = session_id
                    final_history = await svc.get_history(session_id, principal.tenant_id)
                    ev["history_length"] = len(final_history)
                collected.append(ev)
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001
            logger.error("[agent] 流式执行出错: %s", e, exc_info=True)
            err = {"type": "error", "message": f"处理出错: {e}"}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",       # 告诉 nginx 不要缓冲（逐帧透传）
            "Connection": "keep-alive",
        },
    )


@router.post("/agent/route", summary="规则路由调试（纯函数，不触发 LLM/数据库）")
async def agent_route(
    req: AgentChatRequest,
    principal: Principal = Depends(get_principal),
) -> dict:
    """暴露规则路由的判定过程，便于调参与排查"为什么走了这条路"。

    刻意不鉴权依赖数据库，只做纯函数计算 —— 因此它也是最快的自检接口。
    """
    return route_preview(req.message)
