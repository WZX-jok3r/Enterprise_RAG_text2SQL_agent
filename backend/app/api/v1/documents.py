"""文档入库路由（P5）。

- POST /api/v1/documents          multipart 上传 → 落盘知识库 → 入队 ARQ → 202 返回 task_id
- GET  /api/v1/documents/{id}/status  轮询任务状态（读 PG 真相）
- GET  /api/v1/documents         列出当前租户已登记文档

上传只负责「落盘 + 入队」，解析/向量化由 worker 后台执行，故大文件也秒返回。
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_db, get_ingestion_service, get_principal
from app.core.config import settings
from app.core.exceptions import AppException
from app.core.security import Principal
from app.repositories.document_repo import DocumentRepository
from app.schemas.documents import (
    DeleteDocumentResponse,
    DocumentItem,
    DocumentListResponse,
    TaskStatusResponse,
    UploadResponse,
)
from app.services.ingestion_service import IngestionService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["documents"])

_ALLOWED_SUFFIXES = settings.supported_extensions


def _safe_target_path(filename: str) -> Path:
    """生成落到知识库根目录下的安全唯一路径（防目录穿越 / 重名覆盖）。"""
    name = Path(filename).name  # 去除任何路径成分
    if not name:
        raise AppException("文件名非法", status_code=400, error_code="BAD_FILENAME")
    suffix = Path(name).suffix.lower()
    if suffix not in _ALLOWED_SUFFIXES:
        raise AppException(
            f"不支持的文件类型: {suffix or '未知'}（支持 {sorted(_ALLOWED_SUFFIXES)}）",
            status_code=415, error_code="UNSUPPORTED_TYPE",
        )
    unique = f"{uuid.uuid4().hex}_{name}"
    # Keep immutable API revisions outside the legacy CLI scanner's root glob.
    kb = settings.knowledge_base_dir / ".versions"
    kb.mkdir(parents=True, exist_ok=True)
    return kb / unique


@router.post(
    "/documents",
    response_model=UploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="上传文档并异步入库",
)
async def upload_document(
    file: UploadFile = File(...),
    principal: Principal = Depends(get_principal),
    svc: IngestionService = Depends(get_ingestion_service),
) -> UploadResponse:
    if not svc.queue_ready():
        raise AppException("入库队列不可用（Redis/ARQ 未就绪）", status_code=503, error_code="QUEUE_UNAVAILABLE")

    data = await file.read()
    if not data:
        raise AppException("空文件", status_code=400, error_code="EMPTY_FILE")
    dest = _safe_target_path(file.filename or "upload")
    await run_in_threadpool(dest.write_bytes, data)

    tenant_ids = [principal.tenant_id] if principal.tenant_id else None
    task_id = await svc.submit(dest, tenant_id=principal.tenant_id, tenant_ids=tenant_ids,
                               original_filename=file.filename or "upload")
    logger.info("[API] 上传入队 %s -> task %s (tenant=%s)", dest.name, task_id, principal.tenant_id)
    return UploadResponse(task_id=task_id, filename=dest.name, status="queued")


@router.get(
    "/documents/{task_id}/status",
    response_model=TaskStatusResponse,
    summary="查询入库任务状态",
)
async def get_ingest_status(
    task_id: str,
    principal: Principal = Depends(get_principal),
    svc: IngestionService = Depends(get_ingestion_service),
) -> TaskStatusResponse:
    # 传 principal.tenant_id 做归属校验：跨租户任务一律 404（不区分"不存在"与"无权限"）
    return TaskStatusResponse(**await svc.get_status(task_id, principal.tenant_id))


@router.get(
    "/documents",
    response_model=DocumentListResponse,
    summary="列出当前租户已登记文档",
)
async def list_documents(
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_db),
) -> DocumentListResponse:
    docs = await DocumentRepository(session).list_by_tenant(principal.tenant_id)
    items = [
        _document_item(d)
        for d in docs
    ]
    return DocumentListResponse(documents=items, total=len(items))


def _document_item(d):
    return DocumentItem(id=d.id, source=d.source, format=d.format, status=d.status,
        chunk_count=d.chunk_count, document_key=d.document_key, version=d.version,
        original_filename=d.original_filename, is_current=d.is_current)


@router.get("/documents/{document_id}/versions", response_model=DocumentListResponse,
            summary="查看文档版本历史")
async def document_versions(document_id: int, principal: Principal = Depends(get_principal),
                            svc: IngestionService = Depends(get_ingestion_service)):
    docs = await svc.version_history(document_id, principal.tenant_id)
    return DocumentListResponse(documents=[_document_item(d) for d in docs], total=len(docs))


@router.post("/documents/{document_id}/activate", response_model=DocumentItem,
             summary="将已就绪版本设为当前版本（回滚）")
async def activate_document_version(document_id: int, principal: Principal = Depends(get_principal),
                                    svc: IngestionService = Depends(get_ingestion_service)):
    return _document_item(await svc.activate_version(document_id, principal.tenant_id))


@router.delete(
    "/documents/{document_id}",
    response_model=DeleteDocumentResponse,
    summary="删除文档（向量 + 物理副本 + 登记记录）",
)
async def delete_document(
    document_id: int,
    principal: Principal = Depends(get_principal),
    svc: IngestionService = Depends(get_ingestion_service),
) -> DeleteDocumentResponse:
    """按 document_id 删除：清 Qdrant 向量 → 删知识库物理副本 → 删 PG 登记行。

    带租户 ACL：只能删本租户文档，跨租户/不存在均回 404。
    """
    return DeleteDocumentResponse(**await svc.delete_document(document_id, principal.tenant_id))
