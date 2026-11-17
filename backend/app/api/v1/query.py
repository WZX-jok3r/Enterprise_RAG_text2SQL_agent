"""单轮问答路由：POST /api/v1/query。

编排在 `app/rag/oneshot.answer_once`（检索 + 生成各一次，无会话无历史）；
它是阻塞的 requests 调用，放到线程池执行，不阻塞事件循环；
鉴权开启时按当前租户强制过滤检索范围。

为什么用 `Depends(get_oneshot_answerer)` 而不是直接 import 函数：
**压测夹具要能替换它**（eval/performance_fixture 需要一个不含真实检索/生成的替身，
否则每次压测都会打到真模型）。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict

from fastapi import APIRouter, Depends
from starlette.concurrency import run_in_threadpool

from app.api.deps import enforced_filter, get_oneshot_answerer, get_principal, resolve_top_k
from app.core.security import Principal
from app.schemas.chat import QueryRequest, QueryResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["query"])


@router.post("/query", response_model=QueryResponse, summary="单轮问答")
async def single_query(
    req: QueryRequest,
    principal: Principal = Depends(get_principal),
    answer: Callable[..., Dict[str, Any]] = Depends(get_oneshot_answerer),
) -> QueryResponse:
    result = await run_in_threadpool(
        answer,
        req.query,
        top_k=resolve_top_k(req.top_k),
        filter_dict=enforced_filter(req.filter_dict, principal),
    )
    return QueryResponse(**result)
