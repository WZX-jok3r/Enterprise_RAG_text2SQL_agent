"""入库编排服务：接收文件→落 PG 任务/文档记录→入队 ARQ→立即返回 task_id。

设计要点：
- submit 只做「建记录 + 入队」，解析/向量化等重活交给 worker 后台执行，故大文件也能秒返回。
- 状态真相在 PG（IngestTask/Document），Redis 仅作队列；客户端凭 task_id 轮询 get_status。
- arq 连接池经依赖注入，便于用假池单测（无需真起 worker）。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.exceptions import AppException, NotFoundError
from app.ingestion.loader import compute_file_hash
from app.models.document import Document, DocumentStatus
from app.repositories.document_repo import DocumentRepository
from app.repositories.ingest_task_repo import IngestTaskRepository
from app.repositories.document_version_repo import DocumentVersionRepository
from app.models.ingest_task import IngestTask

logger = logging.getLogger(__name__)

# ARQ 任务名（与 worker/tasks/ingest.py 的函数名一致）
INGEST_JOB = "ingest_document"


def _clear_source_vectors(source: str, tenant_id: Optional[str]) -> bool:
    """删除该 source 在 Qdrant 中的全部向量点（阻塞 I/O，供线程池调用）。

    同时约束来源与登记租户，防止同名历史记录导致跨租户清理。
    """
    from app.vector.qdrant import get_vector_store

    try:
        filters = {"source": source}
        if tenant_id:
            filters[settings.tenant_field] = tenant_id
        return bool(get_vector_store().delete_by_metadata(filters))
    except Exception as e:  # noqa: BLE001
        logger.error("[Ingest] 删除向量失败 source=%s: %s", source, e)
        raise


def _remove_physical_file(source: str) -> bool:
    """删除知识库根目录下的物理副本（取 basename 防目录穿越）；不存在返回 False。"""
    path = settings.knowledge_base_dir / Path(source).name
    if path.is_file():
        path.unlink()
        return True
    return False


def _clear_version_vectors(version_id: int) -> bool:
    from app.vector.qdrant import get_vector_store
    if not get_vector_store().delete_by_metadata({"document_version_id": str(version_id)}):
        raise RuntimeError("Version vector deletion was not acknowledged")
    return True


def _remove_version_file(source: str) -> bool:
    path = settings.knowledge_base_dir / ".versions" / Path(source).name
    if path.is_file():
        path.unlink()
        return True
    return False


class IngestionService:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], arq_pool: Any) -> None:
        self._sm = sessionmaker
        self._pool = arq_pool

    def queue_ready(self) -> bool:
        """ARQ 入队连接池是否就绪（未起 infra 时为 None）。"""
        return self._pool is not None

    async def submit(
        self,
        file_path: str | Path,
        tenant_id: Optional[str] = None,
        tenant_ids: Optional[List[str]] = None,
        original_filename: Optional[str] = None,
    ) -> str:
        """建 Document(pending)+IngestTask(queued)，入队并立即返回 task_id。"""
        path = Path(file_path)
        # 计算 hash 属文件 I/O，放线程池避免阻塞事件循环（大文件也很快，只读不解析）
        file_hash = await asyncio.to_thread(compute_file_hash, path)
        fmt = path.suffix.lower().lstrip(".")
        if original_filename is not None:
            return await self._submit_version(path, file_hash, fmt, tenant_id, original_filename)

        async with self._sm() as s:
            doc, _created = await DocumentRepository(s).upsert(
                source=path.name,
                file_hash=file_hash,
                fmt=fmt,
                tenant_id=tenant_id,
                status=DocumentStatus.PENDING.value,
            )
            task = await IngestTaskRepository(s).create(
                tenant_id=tenant_id, document_id=doc.id
            )
            await s.commit()
            task_id = task.id

        # 入队（_job_start 默认立即可被 worker 领取）
        await self._pool.enqueue_job(
            INGEST_JOB, str(path), task_id, tenant_ids or ([tenant_id] if tenant_id else None)
        )
        logger.info("[Ingest] 已入队 task_id=%s file=%s", task_id, path.name)
        return task_id

    async def _submit_version(self, path, file_hash, fmt, tenant_id, filename):
        async with self._sm() as s:
            doc, created = await DocumentVersionRepository(s).reserve(
                path.name, file_hash, fmt, tenant_id, filename)
            task = None
            if not created:
                task = (await s.execute(select(IngestTask).where(
                    IngestTask.document_id == doc.id).order_by(IngestTask.created_at.desc()).limit(1))).scalar_one_or_none()
            if task is None:
                task = await IngestTaskRepository(s).create(tenant_id, doc.id)
                if doc.status == "ready":
                    task.status, task.progress = "done", 100
            await s.commit()
            task_id, doc_id, source, doc_status = task.id, doc.id, doc.source, doc.status
        if source != path.name:
            # An identical upload reuses the immutable first copy.
            await asyncio.to_thread(path.unlink, missing_ok=True)
        if doc_status == "ready":
            return task_id
        try:
            await self._pool.enqueue_job(
                INGEST_JOB, str(path.parent / source), task_id,
                [tenant_id] if tenant_id else None, doc_id, _job_id="ingest:" + task_id)
            # None means ARQ already has this stable job ID, also an idempotent success.
        except Exception as exc:
            async with self._sm() as s:
                doc = await s.get(Document, doc_id)
                if doc:
                    await DocumentVersionRepository(s).lock(doc.document_key)
                    await s.refresh(doc)
                if doc and doc.status not in {"ready", "deleted"}:
                    doc.status = "failed"
                    await IngestTaskRepository(s).update_status(task_id, "failed", error="Queue submission failed")
                await s.commit()
            raise AppException("入库任务入队失败，请重试上传", status_code=503,
                               error_code="QUEUE_SUBMISSION_FAILED") from exc
        return task_id

    async def get_status(
        self, task_id: str, tenant_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """查询任务状态（供前端轮询进度）。不存在抛 NotFoundError。

        `tenant_id` 非空时**同时做归属校验**：跨租户一律视为"不存在"（404），
        避免已认证用户枚举/窥探他人任务（返回体含 document_id 与 error，
        而 error 里可能带文件名/表名）。

        ⚠️ 这个缺口与 Agent 检索那次泄露同源：**同一份代码里
        `/documents` 列表与删除都做了租户校验，只有状态查询漏了**。
        修"某一处"没用，所以顺手把 ingest 任务的状态查询也纳入统一口径。
        """
        async with self._sm() as s:
            task = await IngestTaskRepository(s).get(task_id, tenant_id)
            if task is None:
                raise NotFoundError(f"ingest task {task_id} not found")
            return {
                "task_id": task.id,
                "document_id": task.document_id,
                "status": task.status,
                "progress": task.progress,
                "error": task.error,
                **(await self._version_status(s, task.document_id)),
            }

    async def _version_status(self, session, doc_id):
        doc = await session.get(Document, doc_id) if doc_id else None
        if not doc or not doc.document_key:
            return {}
        return {"document_key": doc.document_key, "version": doc.version,
                "original_filename": doc.original_filename, "is_current": doc.is_current}

    async def version_history(self, document_id, tenant_id):
        async with self._sm() as s:
            return await DocumentVersionRepository(s).history(document_id, tenant_id)

    async def activate_version(self, document_id, tenant_id):
        async with self._sm() as s:
            repo = DocumentVersionRepository(s)
            doc = await repo.owned(document_id, tenant_id)
            await repo.lock(doc.document_key)
            unfinished = (await s.execute(select(Document.id).where(
                Document.document_key == doc.document_key, Document.status.in_(["pending", "failed"])
            ).limit(1))).first()
            if unfinished:
                raise AppException("请先完成或删除未完成版本，再切换当前版本", status_code=409,
                                   error_code="VERSION_INGEST_UNFINISHED")
            await repo.publish(doc)
            await s.commit()
            return doc

    async def _delete_version(self, document_id, tenant_id):
        async with self._sm() as s:
            repo = DocumentVersionRepository(s)
            doc = await repo.owned(document_id, tenant_id, allow_deleted=True)
            # Same lock order as the worker: logical document, then revision row.
            await repo.lock(doc.document_key)
            await s.refresh(doc, with_for_update=True)
            doc.is_current, doc.status = False, "deleted"
            source = doc.source
            await s.execute(update(IngestTask).where(IngestTask.document_id == doc.id,
                IngestTask.status.in_(["queued", "running"])).values(
                    status="failed", error="Document version deleted"))
            await s.commit()  # Hide immediately; external cleanup is retryable.
        cleared = await asyncio.to_thread(_clear_version_vectors, document_id)
        removed = await asyncio.to_thread(_remove_version_file, source)
        return {"deleted_id": document_id, "source": source,
                "vectors_cleared": cleared, "file_removed": removed}

    async def delete_document(
        self, document_id: int, tenant_id: Optional[str]
    ) -> Dict[str, Any]:
        """删除已登记文档：定位→清向量→删物理副本→删 PG 行。

        顺序考量：先清外部存储（向量/文件），全部成功后才删登记行；任一环节
        失败则保留登记行以便重试（避免“文件还在、记录已没”的不可追溯孤儿）。
        关联 ingest_tasks 行由外键 ON DELETE SET NULL 自动置空，作为历史保留。
        tenant_id 非空时做归属校验，跨租户视为不存在（NotFoundError）。
        """
        async with self._sm() as s:
            doc = await DocumentRepository(s).get_by_id(document_id, tenant_id)
            if doc is None:
                raise NotFoundError(f"document {document_id} not found")
            if doc.document_key:
                return await self._delete_version(document_id, tenant_id)
            source = doc.source
            owner = doc.tenant_id
            # Legacy records may share a source. Never unlink a physical copy
            # still referenced by another document/tenant.
            other = (await s.execute(select(Document.id).where(
                Document.source == source, Document.id != document_id
            ).limit(1))).first()

        # 外部清理（阻塞 I/O 放线程池）；失败向上抛，保留登记行可重试
        vectors_cleared = await asyncio.to_thread(_clear_source_vectors, source, owner)
        file_removed = False if other else await asyncio.to_thread(_remove_physical_file, source)

        # 外部已无残留，最后删登记行
        async with self._sm() as s:
            repo = DocumentRepository(s)
            doc = await repo.get_by_id(document_id, tenant_id)
            if doc is not None:
                await repo.delete(doc)
                await s.commit()

        logger.info(
            "[Ingest] 删除文档 id=%s source=%s vectors_cleared=%s file_removed=%s",
            document_id, source, vectors_cleared, file_removed,
        )
        return {
            "deleted_id": document_id,
            "source": source,
            "vectors_cleared": vectors_cleared,
            "file_removed": file_removed,
        }
