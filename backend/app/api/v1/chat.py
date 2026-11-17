"""多轮对话路由：POST /api/v1/chat 与 /api/v1/chat/stream（SSE）。

> ⚠️ **Step 2 起已不是产品主入口**：前端统一走 `/api/v1/agent/stream`，
>    这两条路由标记为 deprecated，仅保留给旧客户端、评测脚本与回滚路径。
>    真正下线（连同 RAGPipeline 一起删）放在 Step 3 —— 届时评测口径
>    必须已经迁到 Agent，否则会失去 107 题的检索基线。

忠实保留 src/api.py 的会话语义：
- 带有效 session_id 续聊；否则自动新建并回传 session_id（前端只需回传即可维持上下文）。
- 历史仅存干净的 user/assistant（不含参考资料），与 pipeline.query_with_history 入参一致。
- 检索/生成走阻塞的 pipeline，放到线程池；SSE 依次 yield meta -> delta* -> done/error。
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from starlette.concurrency import iterate_in_threadpool, run_in_threadpool

from app.api.deps import enforced_filter, get_pipeline, get_principal, get_session_service, resolve_top_k
from app.core.security import Principal
from app.rag.pipeline import RAGPipeline
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.session_service import SessionService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])


async def _resolve_session(
    req: ChatRequest, principal: Principal, svc: SessionService
) -> str:
    """复用有效 session，否则新建（租户不符视为不存在 -> 新建，行为对齐内存实现）。"""
    if req.session_id and await svc.exists(req.session_id, principal.tenant_id):
        return req.session_id
    return await svc.create(principal.tenant_id)


@router.post(
    "/chat", response_model=ChatResponse,
    summary="多轮对话（后端 session）",
    description="⚠️ 已废弃：请改用 POST /api/v1/agent/stream（单一入口，含意图路由）。",
    deprecated=True,
)
async def chat_query(
    req: ChatRequest,
    principal: Principal = Depends(get_principal),
    svc: SessionService = Depends(get_session_service),
    pipe: RAGPipeline = Depends(get_pipeline),
) -> ChatResponse:
    session_id = await _resolve_session(req, principal, svc)
    history = await svc.get_history(session_id, principal.tenant_id)

    result = await run_in_threadpool(
        pipe.query_with_history,
        req.message,
        history,
        top_k=resolve_top_k(req.top_k),
        filter_dict=enforced_filter(None, principal),
    )

    await svc.append(session_id, principal.tenant_id, {"role": "user", "content": req.message})
    await svc.append(session_id, principal.tenant_id, {"role": "assistant", "content": result["answer"]})
    final_history = await svc.get_history(session_id, principal.tenant_id)

    return ChatResponse(
        session_id=session_id,
        answer=result["answer"],
        sources=result["sources"],
        retrieved_count=result["retrieved_count"],
        history_length=len(final_history),
        hidden_count=result.get("hidden_count", 0),
    )


@router.post(
    "/chat/stream",
    summary="多轮对话（SSE 流式）",
    description="⚠️ 已废弃：请改用 POST /api/v1/agent/stream（单一入口，含意图路由与执行轨迹）。",
    deprecated=True,
)
async def chat_stream(
    req: ChatRequest,
    principal: Principal = Depends(get_principal),
    svc: SessionService = Depends(get_session_service),
    pipe: RAGPipeline = Depends(get_pipeline),
) -> StreamingResponse:
    session_id = await _resolve_session(req, principal, svc)
    # 取"进入前"的历史作上下文（不含本轮 user），随后先把 user 落库
    history = await svc.get_history(session_id, principal.tenant_id)
    await svc.append(session_id, principal.tenant_id, {"role": "user", "content": req.message})

    top_k = resolve_top_k(req.top_k)
    filter_dict = enforced_filter(None, principal)
    # pipeline.stream_chat 是同步阻塞生成器；用线程池逐步驱动，事件循环不被占住
    sync_iter = pipe.stream_chat(req.message, history, top_k=top_k, filter_dict=filter_dict)

    async def event_gen():
        try:
            async for ev in iterate_in_threadpool(sync_iter):
                if ev.get("type") == "done":
                    await svc.append(
                        session_id, principal.tenant_id,
                        {"role": "assistant", "content": ev["answer"]},
                    )
                    ev["session_id"] = session_id
                    final_history = await svc.get_history(session_id, principal.tenant_id)
                    ev["history_length"] = len(final_history)
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001
            logger.error("[API] 流式对话出错: %s", e)
            err = {"type": "error", "message": f"处理出错: {e}"}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
