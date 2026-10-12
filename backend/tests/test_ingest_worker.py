"""异步入库门禁测试（P4）。

直打真实 PostgreSQL（docker compose 已起）；无 PG 则整模块 skip。
ARQ worker 进程不在此拉起：用「假连接池」验证 submit 的入队语义（秒返回 task_id），
用「直接 await 任务函数 + 注入 fake process_file / vector_store」验证状态机，
从而无需真连 Qdrant / SiliconFlow，也能覆盖四条门禁：
1. 大文件上传立即返回（submit 只建记录 + 入队，不落重活）。
2. 后台完成入库（任务函数把 queued→running→done，并登记 Document ready）。
3. 失败可重试（处理抛错时置 failed、记录 error，并重新抛出以触发 ARQ 重试）。
4. 状态可查（get_status 返回 PG 中的真实状态）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from sqlalchemy import delete

from app.database import db
from app.models.document import Document
from app.models.ingest_task import IngestTask, TaskStatus
from app.repositories.document_repo import DocumentRepository
from app.repositories.ingest_task_repo import IngestTaskRepository
from app.services.ingestion_service import INGEST_JOB, IngestionService
from app.worker.tasks.ingest import ingest_document

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _ensure_pg():
    try:
        await db.healthcheck()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"PostgreSQL 未就绪，跳过入库门禁测试: {e}")
    await db.create_all()
    yield
    await db.dispose()


class FakeArqPool:
    """记录入队调用的假 ARQ 池（不连真 Redis）。"""

    def __init__(self) -> None:
        self.calls: List[Any] = []

    async def enqueue_job(self, name: str, *args: Any, **kwargs: Any):
        self.calls.append((name, args, kwargs))
        return object()


async def _purge(task_id: str, source: Optional[str] = None) -> None:
    async with db.sessionmaker() as s:
        task = await s.get(IngestTask, task_id)
        if task is not None:
            await s.delete(task)
        if source is not None:
            await s.execute(delete(Document).where(Document.source == source))
        await s.commit()


async def test_submit_returns_task_id_and_enqueues(tmp_path: Path):
    """大文件上传应立即返回 task_id：submit 只建记录 + 入队，绝不在请求内做解析/向量化。"""
    f = tmp_path / "big.md"
    f.write_text("# 大文件\n内容" * 100, encoding="utf-8")
    pool = FakeArqPool()
    svc = IngestionService(db.sessionmaker, pool)

    task_id = await svc.submit(f, tenant_id="t_submit")
    try:
        assert isinstance(task_id, str) and task_id
        # 恰好一条入队，job 名与参数正确
        assert len(pool.calls) == 1
        name, args, _ = pool.calls[0]
        assert name == INGEST_JOB
        assert args[0] == str(f) and args[1] == task_id
        assert args[2] == ["t_submit"]
        # 状态可查：刚提交应为 queued
        status = await svc.get_status(task_id)
        assert status["status"] == TaskStatus.QUEUED.value
        assert status["task_id"] == task_id
    finally:
        await _purge(task_id, source=f.name)


async def test_job_success_transitions_to_done():
    """后台完成入库：queued→running→done，并登记 Document 为 ready。"""
    recorded: Dict[str, Any] = {
        "file_hash": "hash_ok_1",
        "status": "success",
        "chunk_count": 7,
        "inserted_count": 7,
        "format": "md",
        "processing_time": 0.1,
    }

    def fake_process(path: Path, vs: Any, tenant_ids: Optional[List[str]]):
        return recorded

    async with db.sessionmaker() as s:
        task = await IngestTaskRepository(s).create(tenant_id="t_ok", document_id=None)
        await s.commit()
        task_id = task.id

    ctx = {"db": db.sessionmaker, "process_file": fake_process, "vector_store": object()}
    result = await ingest_document(ctx, "kb/产品手册.md", task_id, ["t_ok"])
    try:
        assert result["status"] == "success"
        async with db.sessionmaker() as s:
            got = await IngestTaskRepository(s).get(task_id)
            assert got is not None
            assert got.status == TaskStatus.DONE.value
            assert got.progress == 100
            doc = await DocumentRepository(s).get_by_hash("产品手册.md", "hash_ok_1", "t_ok")
            assert doc is not None and doc.status == "ready" and doc.chunk_count == 7
    finally:
        await _purge(task_id, source="产品手册.md")


async def test_job_failure_records_error_and_raises():
    """失败可重试：处理抛错时任务置 failed、记录 error，并重新抛出以触发 ARQ 重试。"""

    def boom(path: Path, vs: Any, tenant_ids: Optional[List[str]]):
        raise RuntimeError("embedding 上游 500")

    async with db.sessionmaker() as s:
        task = await IngestTaskRepository(s).create(tenant_id="t_bad", document_id=None)
        await s.commit()
        task_id = task.id

    ctx = {"db": db.sessionmaker, "process_file": boom, "vector_store": object()}
    with pytest.raises(RuntimeError):
        await ingest_document(ctx, "kb/broken.pdf", task_id, None)

    try:
        async with db.sessionmaker() as s:
            got = await IngestTaskRepository(s).get(task_id)
            assert got is not None
            assert got.status == TaskStatus.FAILED.value
            assert "embedding" in (got.error or "")
    finally:
        await _purge(task_id)


async def test_get_status_unknown_task_raises():
    from app.core.exceptions import NotFoundError

    svc = IngestionService(db.sessionmaker, FakeArqPool())
    with pytest.raises(NotFoundError):
        await svc.get_status("00000000000000000000000000000000")


async def test_delete_document_removes_record_and_cleanup(monkeypatch):
    """删除文档：先清向量/物理副本，再删 PG 行；登记行确认消失。"""
    from app.services import ingestion_service as isvc

    calls: List[Any] = []
    monkeypatch.setattr(isvc, "_clear_source_vectors", lambda src, tenant: calls.append(("vec", src, tenant)) or True)
    monkeypatch.setattr(isvc, "_remove_physical_file", lambda src: calls.append(("file", src)) or False)

    async with db.sessionmaker() as s:
        doc, _ = await DocumentRepository(s).upsert(
            source="to_delete.md", file_hash="hash_del_1", fmt="md",
            tenant_id="t_del", status="ready", chunk_count=3,
        )
        await s.commit()
        doc_id = doc.id

    svc = IngestionService(db.sessionmaker, FakeArqPool())
    result = await svc.delete_document(doc_id, "t_del")

    assert result["deleted_id"] == doc_id and result["source"] == "to_delete.md"
    assert result["vectors_cleared"] is True
    # 向量与物理副本均按 source 被清理
    assert ("vec", "to_delete.md", "t_del") in calls and ("file", "to_delete.md") in calls
    async with db.sessionmaker() as s:
        assert await DocumentRepository(s).get_by_id(doc_id, None) is None


async def test_delete_document_wrong_tenant_not_found(monkeypatch):
    """跨租户删除视为不存在（NotFoundError），且不动原登记行。"""
    from app.core.exceptions import NotFoundError
    from app.services import ingestion_service as isvc

    monkeypatch.setattr(isvc, "_clear_source_vectors", lambda src, tenant: True)
    monkeypatch.setattr(isvc, "_remove_physical_file", lambda src: False)

    async with db.sessionmaker() as s:
        doc, _ = await DocumentRepository(s).upsert(
            source="owned_by_a.md", file_hash="hash_own_a", fmt="md",
            tenant_id="t_a_only", status="ready", chunk_count=1,
        )
        await s.commit()
        doc_id = doc.id

    svc = IngestionService(db.sessionmaker, FakeArqPool())
    try:
        with pytest.raises(NotFoundError):
            await svc.delete_document(doc_id, "t_b_other")
        # 归属校验失败不应误删
        async with db.sessionmaker() as s:
            assert await DocumentRepository(s).get_by_id(doc_id, "t_a_only") is not None
    finally:
        async with db.sessionmaker() as s:
            await s.execute(delete(Document).where(Document.id == doc_id))
            await s.commit()
