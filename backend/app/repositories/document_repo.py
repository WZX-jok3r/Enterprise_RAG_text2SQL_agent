"""文档登记仓储。"""

from __future__ import annotations

from typing import List, Optional, Tuple

from sqlalchemy import select

from app.models.document import Document, DocumentStatus
from app.repositories.base import BaseRepository


class DocumentRepository(BaseRepository):
    async def get_by_hash(self, source: str, file_hash: str,
                          tenant_id: Optional[str] = None) -> Optional[Document]:
        stmt = select(Document).where(
            Document.source == source, Document.file_hash == file_hash,
            Document.tenant_id == tenant_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_id(
        self, doc_id: int, tenant_id: Optional[str]
    ) -> Optional[Document]:
        """按主键取文档；tenant_id 非空时做归属校验（跨租户视为不存在）。"""
        stmt = select(Document).where(Document.id == doc_id)
        if tenant_id is not None:
            stmt = stmt.where(Document.tenant_id == tenant_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def delete(self, doc: Document) -> None:
        """删除文档登记行（关联 ingest_tasks.document_id 由 DB 层 ON DELETE SET NULL 置空）。

        仅 flush，提交由上层 service 掌控，保持事务边界一致。
        """
        await self._session.delete(doc)
        await self.flush()

    async def list_by_tenant(self, tenant_id: Optional[str]) -> List[Document]:
        stmt = select(Document).where(Document.tenant_id == tenant_id).order_by(Document.id)
        return list((await self._session.execute(stmt)).scalars().all())

    async def upsert(
        self,
        source: str,
        file_hash: str,
        fmt: str,
        tenant_id: Optional[str],
        chunk_count: int = 0,
        status: str = DocumentStatus.READY.value,
        meta: Optional[dict] = None,
    ) -> Tuple[Document, bool]:
        """按 (source, file_hash) 建或更新。返回 (document, created?)。"""
        doc = await self.get_by_hash(source, file_hash, tenant_id)
        if doc is None:
            doc = Document(
                source=source, file_hash=file_hash, format=fmt, tenant_id=tenant_id,
                chunk_count=chunk_count, status=status, meta=meta or {},
            )
            self._session.add(doc)
            await self.flush()
            return doc, True
        doc.format = fmt
        doc.tenant_id = tenant_id
        doc.chunk_count = chunk_count
        doc.status = status
        doc.meta = meta or {}
        await self.flush()
        return doc, False
