"""Version allocation/publication serialized by a transaction-level PostgreSQL lock."""
import hashlib
from pathlib import Path

from sqlalchemy import func, select, text, update

from app.core.exceptions import AppException, NotFoundError
from app.models.document import Document
from app.repositories.base import BaseRepository


def original_name(filename: str) -> str:
    name = Path(filename.replace("\\", "/")).name
    if not name or name in {".", ".."} or len(name) > 512:
        raise AppException("Invalid original filename", status_code=400, error_code="BAD_FILENAME")
    return name


def document_key(tenant_id, filename):
    # Distinguish anonymous ownership from all literal tenant names.
    import json
    return hashlib.sha256(json.dumps([tenant_id, original_name(filename)], ensure_ascii=False).encode()).hexdigest()


class DocumentVersionRepository(BaseRepository):
    async def lock(self, key):
        lock_id = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big", signed=True)
        await self._session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_id})

    async def reserve(self, source, file_hash, fmt, tenant_id, filename):
        name = original_name(filename)
        key = document_key(tenant_id, name)
        await self.lock(key)
        latest = (await self._session.execute(select(Document).where(
            Document.document_key == key).order_by(Document.version.desc()).limit(1))).scalar_one_or_none()
        if latest and latest.file_hash == file_hash and latest.status in {"pending", "ready"}:
            return latest, False
        version = (latest.version + 1) if latest else 1
        doc = Document(source=source, file_hash=file_hash, format=fmt, tenant_id=tenant_id,
                       original_filename=name, document_key=key, version=version, status="pending")
        self._session.add(doc)
        await self.flush()
        return doc, True

    async def owned(self, doc_id, tenant_id, *, allow_deleted=False):
        # Exact ownership, including anonymous callers: never use None as a wildcard.
        doc = (await self._session.execute(select(Document).where(
            Document.id == doc_id, Document.tenant_id == tenant_id,
            Document.document_key.is_not(None)))).scalar_one_or_none()
        if doc is None or (doc.status == "deleted" and not allow_deleted):
            raise NotFoundError(f"document {doc_id} not found")
        return doc

    async def history(self, doc_id, tenant_id):
        doc = await self.owned(doc_id, tenant_id)
        return list((await self._session.execute(select(Document).where(
            Document.document_key == doc.document_key).order_by(Document.version.desc()))).scalars())

    async def publish(self, doc, *, automatic=False):
        await self.lock(doc.document_key)
        await self._session.refresh(doc)
        if doc.status != "ready":
            raise AppException("Only ready versions can be activated", status_code=409, error_code="VERSION_NOT_READY")
        # Late workers must not replace a newer completed revision. Rollback is explicit.
        if automatic:
            newest = (await self._session.execute(select(func.max(Document.version)).where(
                Document.document_key == doc.document_key, Document.status == "ready"))).scalar_one()
            if newest > doc.version:
                return False
        await self._session.execute(update(Document).where(
            Document.document_key == doc.document_key, Document.is_current.is_(True)
        ).values(is_current=False))
        await self.flush()
        doc.is_current = True
        await self.flush()
        return True
