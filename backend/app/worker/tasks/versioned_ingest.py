"""Stage an immutable revision, then publish it in the PostgreSQL transaction."""
import asyncio
from pathlib import Path

from sqlalchemy import select

from app.ingestion.indexer import process_file
from app.ingestion.loader import compute_file_hash
from app.models.document import Document
from app.repositories.document_version_repo import DocumentVersionRepository
from app.repositories.ingest_task_repo import IngestTaskRepository
from app.vector.qdrant import get_vector_store


async def ingest_version(ctx, file_path, task_id, version_id):
    sm = ctx["db"]
    async with sm() as s:
        task = await IngestTaskRepository(s).get(task_id)
        doc = await s.get(Document, version_id)
        if not task or not doc or not doc.document_key or task.document_id != doc.id or task.tenant_id != doc.tenant_id:
            raise ValueError("Version task binding is invalid")
        if doc.status == "deleted":
            raise ValueError("Document version was deleted")
        if doc.status == "ready":
            return doc.meta  # A redelivered job must never undo an explicit rollback.
        await IngestTaskRepository(s).update_status(task_id, "running", progress=10)
        await s.commit()
    try:
        async with sm() as s:
            doc = await s.get(Document, version_id)
            # Use one lock order for ingestion, activation and deletion. Serializing this
            # document family also prevents a duplicate job from clearing published points.
            await DocumentVersionRepository(s).lock(doc.document_key)
            doc = (await s.execute(select(Document).where(Document.id == version_id).with_for_update()
                                   .execution_options(populate_existing=True))).scalar_one()
            if doc.status == "deleted":
                raise ValueError("Document version was deleted")
            if doc.status == "ready":
                await IngestTaskRepository(s).update_status(task_id, "done", progress=100, error="")
                await s.commit()
                return doc.meta
            path = Path(file_path)
            if path.name != doc.source or await asyncio.to_thread(compute_file_hash, path) != doc.file_hash:
                raise ValueError("Immutable version file mismatch")
            store = ctx.get("vector_store")
            if store is None:
                store = await asyncio.to_thread(get_vector_store)
            version_context = {"document_version_id": str(doc.id), "document_key": doc.document_key,
                               "document_version": doc.version, "original_filename": doc.original_filename}
            record = await asyncio.to_thread(ctx.get("process_file", process_file), path, store,
                [doc.tenant_id] if doc.tenant_id else None, version_context=version_context)
            if record.get("status") != "success" or record.get("chunk_count", 0) <= 0:
                raise ValueError("Version parsing produced no indexable content")
            if await asyncio.to_thread(compute_file_hash, path) != doc.file_hash:
                raise ValueError("Immutable version file changed during ingestion")
            record["analytics_sync"] = {"attempted": False, "reason": "Versioned ingestion uses RAG; SQL version publication is not supported"}
            doc.status, doc.chunk_count, doc.meta = "ready", record["chunk_count"], record
            await s.flush()
            await DocumentVersionRepository(s).publish(doc, automatic=True)
            await IngestTaskRepository(s).update_status(task_id, "done", progress=100, error="")
            await s.commit()
            return record
    except Exception as exc:
        async with sm() as s:
            doc = (await s.execute(select(Document).where(Document.id == version_id).with_for_update())).scalar_one_or_none()
            if doc and doc.status not in {"ready", "deleted"}:
                doc.status = "failed"
                await IngestTaskRepository(s).update_status(task_id, "failed", error=str(exc)[:1000])
            await s.commit()
        raise
