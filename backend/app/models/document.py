"""文档登记表（入库的元数据事实来源，替代 processed_cache/cache.json 的角色）。

一个已入库文件对应一行：以 (source, file_hash) 定位，支持增量判断与删除清理。
ChunkMeta 明细仍存于 Qdrant（向量+payload），此处只登记文档级信息，避免双写大对象。
"""

from __future__ import annotations

from enum import Enum as PyEnum
from typing import Optional

from sqlalchemy import Boolean, CheckConstraint, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class DocumentStatus(str, PyEnum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"
    DELETED = "deleted"


class Document(Base, TimestampMixin):
    """知识库文档登记。"""

    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("document_key", "version", name="uq_documents_key_version"),
        CheckConstraint("(document_key IS NULL AND version IS NULL) OR "
                        "(document_key IS NOT NULL AND version IS NOT NULL AND version > 0 AND original_filename IS NOT NULL)",
                        name="version_identity"),
        CheckConstraint("NOT is_current OR (document_key IS NOT NULL AND status = 'ready')",
                        name="current_ready"),
        Index("uq_documents_current_key", "document_key", unique=True,
              postgresql_where=text("is_current")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # NULL identifies pre-versioning records, retained for backward compatibility.
    document_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    original_filename: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # 文件名 / 相对路径（与 Qdrant payload 的 source 对齐）
    source: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tenant_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    format: Mapped[str] = mapped_column(String(16), nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DocumentStatus.PENDING.value,
        server_default=DocumentStatus.PENDING.value, index=True,
    )
    # 解析统计等轻量元信息（页数/行数/表格数等）
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Document id={self.id} source={self.source!r} status={self.status!r}>"
