"""Real PostgreSQL versioning checks; model/embedding calls are never needed."""
import asyncio
import uuid

import pytest
from sqlalchemy import delete, select

from app.core.exceptions import AppException, NotFoundError
from app.database import db
from app.models.document import Document
from app.models.ingest_task import IngestTask
from app.repositories.document_version_repo import DocumentVersionRepository, document_key

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def owner():
    try:
        await db.healthcheck()
    except Exception:
        pytest.skip("Local PostgreSQL unavailable")
    tenant = "vtest_" + uuid.uuid4().hex[:16]
    yield tenant
    async with db.sessionmaker() as s:
        ids = select(Document.id).where(Document.tenant_id.in_([tenant, tenant + "b"]))
        await s.execute(delete(IngestTask).where(IngestTask.document_id.in_(ids)))
        await s.execute(delete(Document).where(Document.tenant_id.in_([tenant, tenant + "b"])))
        await s.commit()
    await db.dispose()


async def reserve(owner, file_hash, filename="manual.md"):
    async with db.sessionmaker() as s:
        doc, created = await DocumentVersionRepository(s).reserve(
            uuid.uuid4().hex + ".md", file_hash, "md", owner, filename)
        await s.commit()
        return doc, created


async def test_versions_dedupe_and_tenant_boundary(owner):
    first, created = await reserve(owner, "a")
    duplicate, created_again = await reserve(owner, "a")
    second, _ = await reserve(owner, "b")
    foreign, _ = await reserve(owner + "b", "a")
    assert created and not created_again and duplicate.id == first.id
    assert (first.version, second.version, foreign.version) == (1, 2, 1)
    assert first.document_key != foreign.document_key
    async with db.sessionmaker() as s:
        repo = DocumentVersionRepository(s)
        assert [d.version for d in await repo.history(first.id, owner)] == [2, 1]
        for tenant in (owner + "b", None):
            with pytest.raises(NotFoundError):
                await repo.history(first.id, tenant)


async def test_concurrent_allocation_is_monotonic(owner):
    rows = await asyncio.gather(*(reserve(owner, str(i)) for i in range(6)))
    assert sorted(d.version for d, _ in rows) == list(range(1, 7))


async def test_publish_order_rollback_and_ready_guard(owner):
    first, _ = await reserve(owner, "a")
    second, _ = await reserve(owner, "b")
    async with db.sessionmaker() as s:
        repo = DocumentVersionRepository(s)
        d1, d2 = await s.get(Document, first.id), await s.get(Document, second.id)
        with pytest.raises(AppException):
            await repo.publish(d1)
        d1.status = d2.status = "ready"
        await s.flush()
        assert await repo.publish(d2, automatic=True)
        assert not await repo.publish(d1, automatic=True)
        await s.commit()
    async with db.sessionmaker() as s:
        repo = DocumentVersionRepository(s)
        d1 = await repo.owned(first.id, owner)
        assert await repo.publish(d1)
        await s.commit()
        current = list((await s.execute(select(Document).where(
            Document.document_key == first.document_key, Document.is_current.is_(True)))).scalars())
        assert [d.id for d in current] == [first.id]


async def test_database_rejects_two_current_versions(owner):
    from sqlalchemy.exc import IntegrityError
    first, _ = await reserve(owner, "a")
    second, _ = await reserve(owner, "b")
    async with db.sessionmaker() as s:
        a, b = await s.get(Document, first.id), await s.get(Document, second.id)
        a.status = b.status = "ready"
        a.is_current = b.is_current = True
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_database_rejects_missing_version_number(owner):
    from sqlalchemy.exc import IntegrityError
    async with db.sessionmaker() as s:
        s.add(Document(source="missing.md", file_hash="a", format="md", tenant_id=owner,
                       document_key=document_key(owner, "missing.md"), original_filename="missing.md"))
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_identity_separates_anonymous_and_path_components():
    assert document_key(None, "manual.md") != document_key("None", "manual.md")
    assert document_key("a", "../manual.md") == document_key("a", "manual.md")
    assert document_key("a", "folder\\manual.md") == document_key("a", "manual.md")


class Pool:
    def __init__(self):
        self.calls = []

    async def enqueue_job(self, *args, **kwargs):
        self.calls.append((args, kwargs))


async def submit_file(owner, tmp_path, text, pool):
    from app.services.ingestion_service import IngestionService
    path = tmp_path / (uuid.uuid4().hex + ".md")
    path.write_text(text, encoding="utf-8")
    svc = IngestionService(db.sessionmaker, pool)
    task_id = await svc.submit(path, tenant_id=owner, original_filename="manual.md")
    async with db.sessionmaker() as s:
        task = await s.get(IngestTask, task_id)
        return task_id, await s.get(Document, task.document_id), path


def fake_process(path, store, tenants, *, version_context):
    return {"status": "success", "chunk_count": 2, "file_hash": "fixture"}


async def test_version_worker_failure_preserves_current_and_retry(owner, tmp_path):
    from app.worker.tasks.ingest import ingest_document
    pool = Pool()
    task1, d1, p1 = await submit_file(owner, tmp_path, "old", pool)
    ctx = {"db": db.sessionmaker, "process_file": fake_process, "vector_store": object()}
    await ingest_document(ctx, str(p1), task1, [owner], d1.id)
    task2, d2, p2 = await submit_file(owner, tmp_path, "new", pool)
    def fail(*args, **kwargs):
        raise RuntimeError("partial batch failed")
    with pytest.raises(RuntimeError):
        await ingest_document({**ctx, "process_file": fail}, str(p2), task2, [owner], d2.id)
    async with db.sessionmaker() as s:
        assert (await s.get(Document, d1.id)).is_current
        assert (await s.get(Document, d2.id)).status == "failed"
    await ingest_document(ctx, str(p2), task2, [owner], d2.id)
    async with db.sessionmaker() as s:
        assert not (await s.get(Document, d1.id)).is_current
        assert (await s.get(Document, d2.id)).is_current
        assert (await s.get(IngestTask, task2)).status == "done"


async def test_reupload_idempotent_and_ready_redelivery(owner, tmp_path):
    from app.worker.tasks.ingest import ingest_document
    pool = Pool()
    t1, d1, p1 = await submit_file(owner, tmp_path, "same", pool)
    t2, d2, p2 = await submit_file(owner, tmp_path, "same", pool)
    assert (t1, d1.id) == (t2, d2.id) and not p2.exists()
    assert pool.calls[0][1]["_job_id"] == pool.calls[1][1]["_job_id"]
    ctx = {"db": db.sessionmaker, "process_file": fake_process, "vector_store": object()}
    await ingest_document(ctx, str(p1), t1, None, d1.id)
    await ingest_document(ctx, str(p1), t1, None, d1.id)
    t3, d3, p3 = await submit_file(owner, tmp_path, "same", pool)
    assert t3 == t1 and d3.id == d1.id and not p3.exists()
    assert len(pool.calls) == 2


async def test_queue_failure_is_visible_and_can_reupload(owner, tmp_path):
    class BrokenPool(Pool):
        async def enqueue_job(self, *args, **kwargs):
            raise ConnectionError("queue unavailable")
    with pytest.raises(AppException):
        await submit_file(owner, tmp_path, "new", BrokenPool())
    async with db.sessionmaker() as s:
        rows = list((await s.execute(select(Document).where(Document.tenant_id == owner))).scalars())
        assert len(rows) == 1 and rows[0].status == "failed" and not rows[0].is_current
    _, doc, _ = await submit_file(owner, tmp_path, "new", Pool())
    assert doc.version == 2


async def test_changed_file_cannot_publish(owner, tmp_path):
    from app.worker.tasks.ingest import ingest_document
    task, doc, path = await submit_file(owner, tmp_path, "immutable", Pool())
    path.write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="mismatch"):
        await ingest_document({"db": db.sessionmaker, "process_file": fake_process,
                               "vector_store": object()}, str(path), task, None, doc.id)
    async with db.sessionmaker() as s:
        assert (await s.get(Document, doc.id)).status == "failed"


async def test_activation_history_and_tombstone_cleanup(owner, tmp_path, monkeypatch):
    from app.services.ingestion_service import IngestionService
    from app.services import ingestion_service as module
    from app.worker.tasks.ingest import ingest_document
    pool = Pool()
    ctx = {"db": db.sessionmaker, "process_file": fake_process, "vector_store": object()}
    t1, d1, p1 = await submit_file(owner, tmp_path, "old", pool)
    await ingest_document(ctx, str(p1), t1, None, d1.id)
    t2, d2, p2 = await submit_file(owner, tmp_path, "new", pool)
    svc = IngestionService(db.sessionmaker, pool)
    with pytest.raises(AppException):
        await svc.activate_version(d1.id, owner)
    await ingest_document(ctx, str(p2), t2, None, d2.id)
    assert (await svc.activate_version(d1.id, owner)).is_current
    # Redelivery of the newer version cannot undo rollback.
    await ingest_document(ctx, str(p2), t2, None, d2.id)
    assert [(d.version, d.is_current) for d in await svc.version_history(d1.id, owner)] == [(2, False), (1, True)]
    calls = []
    monkeypatch.setattr(module, "_clear_version_vectors", lambda i: calls.append(i) or True)
    monkeypatch.setattr(module, "_remove_version_file", lambda source: False)
    with pytest.raises(NotFoundError):
        await svc.delete_document(d1.id, owner + "b")
    assert not calls
    await svc.delete_document(d1.id, owner)
    await svc.delete_document(d1.id, owner)  # retry cleanup is safe
    async with db.sessionmaker() as s:
        assert (await s.get(Document, d1.id)).status == "deleted"
        assert not (await s.get(Document, d1.id)).is_current
        assert (await s.get(Document, d2.id)).status == "ready"
    assert calls == [d1.id, d1.id]
    # Deleted revisions still consume version numbers; don't silently restore old content.
    _, third, _ = await submit_file(owner, tmp_path, "third", pool)
    assert third.version == 3


async def test_delete_cleanup_failure_hides_revision_and_is_retryable(owner, tmp_path, monkeypatch):
    from app.services import ingestion_service as module
    from app.services.ingestion_service import IngestionService
    from app.worker.tasks.ingest import ingest_document
    t, d, p = await submit_file(owner, tmp_path, "content", Pool())
    await ingest_document({"db": db.sessionmaker, "process_file": fake_process,
                           "vector_store": object()}, str(p), t, None, d.id)
    def fail(i):
        raise ConnectionError("Qdrant unavailable")
    monkeypatch.setattr(module, "_clear_version_vectors", fail)
    svc = IngestionService(db.sessionmaker, Pool())
    with pytest.raises(ConnectionError):
        await svc.delete_document(d.id, owner)
    async with db.sessionmaker() as s:
        got = await s.get(Document, d.id)
        assert got.status == "deleted" and not got.is_current
    monkeypatch.setattr(module, "_clear_version_vectors", lambda i: True)
    monkeypatch.setattr(module, "_remove_version_file", lambda source: False)
    assert (await svc.delete_document(d.id, owner))["vectors_cleared"]


async def test_local_postgres_qdrant_publication_and_rollback(owner, tmp_path, monkeypatch):
    from qdrant_client import QdrantClient, models
    from types import SimpleNamespace
    from app.core.config import settings
    from app.vector.qdrant import VectorStore
    from app.services.ingestion_service import IngestionService
    from app.worker.tasks.ingest import ingest_document
    client = QdrantClient(host=settings.qdrant_host, port=settings.qdrant_port, timeout=3)
    try:
        client.get_collections()
    except Exception:
        client.close()
        pytest.skip("Local Qdrant unavailable")
    collection = "version_test_" + uuid.uuid4().hex
    client.create_collection(collection, vectors_config={"dense": models.VectorParams(size=2, distance=models.Distance.COSINE)})
    store = object.__new__(VectorStore)
    store.client, store.collection_name, store.rerank_client = client, collection, None
    store.embedding_client = SimpleNamespace(embed_single=lambda query: [1.0, 0.0])
    monkeypatch.setattr(settings, "retrieval_mode", "vector")
    def stage(path, vector_store, tenants, *, version_context):
        payload = {**version_context, "source": "manual.md", "text": path.read_text(encoding="utf-8"),
                   settings.tenant_field: tenants}
        client.upsert(collection, [models.PointStruct(id=uuid.uuid4().hex,
            vector={"dense": [1.0, 0.0]}, payload=payload)], wait=True)
        return {"status": "success", "chunk_count": 1}
    pool = Pool()
    ctx = {"db": db.sessionmaker, "process_file": stage, "vector_store": store}
    try:
        t1, d1, p1 = await submit_file(owner, tmp_path, "old content", pool)
        await ingest_document(ctx, str(p1), t1, None, d1.id)
        t2, d2, p2 = await submit_file(owner, tmp_path, "new content", pool)
        stage(p2, store, [owner], version_context={"document_version_id": str(d2.id)})
        filters = {settings.tenant_field: owner}
        assert [r["text"] for r in store.search("q", top_k=10, filter_dict=filters)] == ["old content"]
        await ingest_document(ctx, str(p2), t2, None, d2.id)
        assert {r["text"] for r in store.search("q", top_k=10, filter_dict=filters)} == {"new content"}
        svc = IngestionService(db.sessionmaker, pool)
        await svc.activate_version(d1.id, owner)
        assert [r["text"] for r in store.search("q", top_k=10, filter_dict=filters)] == ["old content"]
        assert store.search("q", filter_dict={settings.tenant_field: owner + "b"}) == []
    finally:
        client.delete_collection(collection)  # Only this test's newly created collection.
        client.close()


async def test_version_api_history_rollback_and_cross_tenant_404(owner, tmp_path, monkeypatch):
    import httpx
    from app.main import app
    from app.api.deps import get_principal, get_ingestion_service
    from app.api.v1 import documents as routes
    from app.core.security import Principal
    from app.services.ingestion_service import IngestionService
    from app.worker.tasks.ingest import ingest_document
    pool = Pool()
    svc = IngestionService(db.sessionmaker, pool)
    old_overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_principal] = lambda: Principal(owner, True)
    app.dependency_overrides[get_ingestion_service] = lambda: svc
    monkeypatch.setattr(routes, "_safe_target_path", lambda name: tmp_path / (uuid.uuid4().hex + ".md"))
    ctx = {"db": db.sessionmaker, "process_file": fake_process, "vector_store": object()}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            ids = []
            for content in ("old", "new"):
                response = await client.post("/api/v1/documents", files={"file": ("manual.md", content, "text/markdown")})
                assert response.status_code == 202
                args = pool.calls[-1][0][1:]
                await ingest_document(ctx, *args)
                status = await client.get(f"/api/v1/documents/{response.json()['task_id']}/status")
                assert status.json()["version"] == len(ids) + 1
                ids.append(status.json()["document_id"])
            history = await client.get(f"/api/v1/documents/{ids[0]}/versions")
            assert history.status_code == 200 and history.json()["total"] == 2
            switched = await client.post(f"/api/v1/documents/{ids[0]}/activate")
            assert switched.status_code == 200 and switched.json()["is_current"]
            app.dependency_overrides[get_principal] = lambda: Principal(owner + "b", True)
            assert (await client.get(f"/api/v1/documents/{ids[0]}/versions")).status_code == 404
            assert (await client.post(f"/api/v1/documents/{ids[0]}/activate")).status_code == 404
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(old_overrides)


async def test_concurrent_duplicate_jobs_stage_only_once(owner, tmp_path):
    import time
    from app.worker.tasks.ingest import ingest_document
    t, d, p = await submit_file(owner, tmp_path, "content", Pool())
    calls = []
    def slow_process(*args, **kwargs):
        calls.append(1)
        time.sleep(0.05)
        return fake_process(*args, **kwargs)
    ctx = {"db": db.sessionmaker, "process_file": slow_process, "vector_store": object()}
    await asyncio.gather(*(ingest_document(ctx, str(p), t, None, d.id) for _ in range(3)))
    assert len(calls) == 1
    async with db.sessionmaker() as s:
        assert (await s.get(Document, d.id)).is_current
        assert (await s.get(IngestTask, t)).status == "done"
