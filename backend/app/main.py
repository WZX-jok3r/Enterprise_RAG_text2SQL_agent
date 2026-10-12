"""
应用工厂 + ASGI 入口

启动:
    cd backend
    uvicorn app.main:app --reload --port 8000
接口文档: http://localhost:8000/docs
健康检查: http://localhost:8000/api/v1/health

采用 create_app() 工厂模式：每个测试可获得全新应用实例，便于依赖覆盖。
lifespan 中集中管理外部资源的启动/关闭（P3 起接入 PostgreSQL / Redis / Qdrant）。
"""

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.router import api_router
from app.cache.redis import redis_client
from app.core.config import settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import setup_logging
from app.core.metrics import counter_inc, observe, render as render_metrics
from app.core.trace import TraceIdMiddleware
from app.core.performance import PerformanceMiddleware
from app.database import db
from app.worker.pool import close_arq_pool, create_arq_pool

logger = logging.getLogger(__name__)


class HttpMetricsMiddleware:
    """纯 ASGI 中间件：统计请求数与耗时。

    为什么不用 BaseHTTPMiddleware：它会包一层 anyio 任务组，
    在 SSE 长连接流式响应下可能影响逐帧透传的实时性 ——
    本项目首帧 <1s 是可演示指标，不能冒险。

    路径标签归一化：把 /documents/123/status 归为 /documents/{id}/status，
    否则每个 ID 都会产生一个时间序列（基数爆炸，Prometheus 经典踩坑）。
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        path = _normalize_path(scope.get("path", "/"))
        start = time.perf_counter()
        status_holder = {"code": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["code"] = message.get("status", 500)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - start
            status = str(status_holder["code"])
            counter_inc("rag_http_requests_total", (method, path, status))
            observe("rag_http_request_duration_seconds", elapsed, (method, path))


def _normalize_path(path: str) -> str:
    """把路径里的动态段替换成占位符，避免指标标签基数爆炸。

    /api/v1/documents/abc123/status -> /api/v1/documents/{id}/status
    """
    import re

    parts = path.split("/")
    out = []
    for i, seg in enumerate(parts):
        if not seg:
            out.append(seg)
        # 十六进制/uuid 类 id、纯数字、以及紧跟已知资源名的段落
        elif re.fullmatch(r"[0-9a-fA-F]{8,}", seg) or re.fullmatch(r"\d+", seg):
            out.append("{id}")
        else:
            out.append(seg)
    return "/".join(out)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.validate_security()
    logger.info(
        "Starting %s v%s (env=%s, auth=%s)",
        settings.app_name, __version__, settings.environment, settings.auth_enabled,
    )
    # 初始化外部资源连接池（DB / Redis 为强依赖，Qdrant 懒加载）
    db.init()
    redis_client.init()
    try:
        await db.healthcheck()
        logger.info("[DB] PostgreSQL 连接正常")
    except Exception as e:  # 探活失败不阻断启动，仅告警（便于无 DB 的环境先起服务）
        logger.warning("[DB] PostgreSQL 探活失败（表未迁移或服务未就绪？）: %s", e)
    try:
        await redis_client.healthcheck()
        logger.info("[Redis] 连接正常")
    except Exception as e:
        logger.warning("[Redis] 探活失败: %s", e)
    # ARQ 连接池（API 侧入队用）；Redis 不可用时池化命令会延迟报错，不阻断启动
    try:
        await create_arq_pool()
        logger.info("[ARQ] 入队连接池已就绪")
    except Exception as e:
        logger.warning("[ARQ] 连接池创建失败: %s", e)
    yield
    await close_arq_pool()
    await db.dispose()
    await redis_client.dispose()
    logger.info("Shutdown complete: %s", settings.app_name)


def create_app() -> FastAPI:
    setup_logging(settings.log_level, settings.log_format)

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # trace_id 中间件放在最外层（先于 CORS 注册 ⇒ 实际执行在最外层），
    # 这样即使 CORS 预检被拒也能拿到 trace_id。
    # 刻意用纯 ASGI 中间件而非 BaseHTTPMiddleware：后者包 anyio 任务组，
    # 在 SSE 流式场景下可能引入缓冲/取消语义问题（SSE 是本项目核心链路）。
    app.add_middleware(HttpMetricsMiddleware)
    app.add_middleware(PerformanceMiddleware)
    app.add_middleware(TraceIdMiddleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router, prefix="/api")

    # Prometheus 指标端点（不在 /api/v1 下，避免被 API 版本演进影响；
    # 也不参与鉴权 —— 指标本身不含业务数据，且 Prometheus 抓取不适合带 Key）
    if settings.metrics_enabled:
        @app.get("/metrics", include_in_schema=False)
        async def metrics() -> Response:
            return Response(content=render_metrics(), media_type="text/plain; version=0.0.4; charset=utf-8")

    register_exception_handlers(app)
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=settings.debug)
