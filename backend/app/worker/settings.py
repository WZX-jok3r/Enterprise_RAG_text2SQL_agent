"""ARQ worker 设置：Redis 连接、任务白名单、启动/关闭钩子。

启动时用 `arq worker app.worker.settings`（module:attr 指向 WorkerSettings）。
on_startup 里初始化进程级异步引擎并把 sessionmaker / vector_store 注入 ctx，
供任务函数 `ingest_document` 使用。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from arq.connections import RedisSettings

from app.core.config import settings
from app.database import db
from app.vector.qdrant import get_vector_store
from app.worker.tasks.ingest import ingest_document

logger = logging.getLogger(__name__)

# Redis 连接参数（与后端共享同一实例，DB 0）
redis_settings: RedisSettings = RedisSettings.from_dsn(settings.redis_url)


async def startup(ctx: Dict[str, Any]) -> None:
    """worker 进程启动：初始化 DB 引擎，注入 sessionmaker 与 vector_store 到 ctx。"""
    settings.validate_security()
    db.init()
    ctx["db"] = db.sessionmaker
    ctx["vector_store"] = get_vector_store()
    logger.info("[Worker] 启动完成，已注册任务: %s", [f.__name__ for f in WorkerSettings.functions])


async def shutdown(ctx: Dict[str, Any]) -> None:
    """worker 进程关闭：释放连接池。"""
    await db.dispose()


class WorkerSettings:
    """ARQ 读取的 WorkerSettings（约定名）。"""

    redis_settings = redis_settings
    functions = [ingest_document]
    on_startup = startup
    on_shutdown = shutdown
    # 单并发保证同文件不并行重灌；超时给解析 + embedding 留足时间
    max_jobs = 1
    job_timeout = 600
    # 失败任务由 ingest_document 重新抛出触发 ARQ 重试（默认 max_tries=5）
