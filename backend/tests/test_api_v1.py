"""API v1 路由门禁测试（P5）。

用 FastAPI dependency_overrides 注入假 answerer / session / ingestion，
配合 TestClient（不进 with，故不触发 lifespan）实现完全 hermetic：
不打网络（SiliconFlow）、不依赖 PG/Redis/Qdrant。覆盖：
- /health 配置回显；/query 契约（Step 3 起编排在 app/rag/oneshot）；
- /chat 与 /chat/stream 已下线（404，且不在 openapi）；
- /sessions 建删与 404；/documents 上传 202 返回 task_id + 状态查询（并清理落盘文件）。
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

from app.api.deps import (
    get_ingestion_service,
    get_oneshot_answerer,
    get_principal,
    get_session_service,
)
from app.core.config import settings
from app.core.security import Principal
from app.main import app


class FakeAnswerOnce:
    """`/query` 的编排替身（形状与 app.rag.oneshot.answer_once 一致）。"""

    def __init__(self) -> None:
        self.last_filter: Optional[Dict[str, Any]] = None
        self.last_top_k: Optional[int] = None
        self.calls = 0

    def __call__(self, q, top_k=None, filter_dict=None):
        self.calls += 1
        self.last_filter = filter_dict
        self.last_top_k = top_k
        return {
            "query": q, "answer": "答案A",
            "sources": [{"source": "s.md", "format": "md", "score": 0.9, "text_preview": "预览"}],
            "retrieved_count": 1, "hidden_count": 0,
        }


class FakeSessions:
    def __init__(self) -> None:
        self._store: Dict[str, Dict[str, Any]] = {}

    async def create(self, tenant_id):
        sid = uuid.uuid4().hex
        self._store[sid] = {"tenant": tenant_id, "history": []}
        return sid

    async def exists(self, sid, tenant_id):
        s = self._store.get(sid)
        return bool(s and s["tenant"] == tenant_id)

    async def get_history(self, sid, tenant_id):
        s = self._store.get(sid)
        if not s or s["tenant"] != tenant_id:
            return []
        return [dict(m) for m in s["history"]]

    async def append(self, sid, tenant_id, message):
        s = self._store.get(sid)
        if not s or s["tenant"] != tenant_id:
            return
        s["history"].append(dict(message))

    async def delete(self, sid, tenant_id):
        s = self._store.get(sid)
        if s and s["tenant"] == tenant_id:
            del self._store[sid]
            return True
        return False


class FakeIngestion:
    def __init__(self) -> None:
        self.submitted: List[Any] = []

    def queue_ready(self) -> bool:
        return True

    async def submit(self, file_path, tenant_id=None, tenant_ids=None, original_filename=None):
        self.original_filename = original_filename
        self.submitted.append((str(file_path), tenant_id, tenant_ids))
        return "task_test_123"

    async def get_status(self, task_id, tenant_id=None):
        """签名必须跟着真实服务走。

        ⚠️ 这里此前是 `get_status(self, task_id)`，加了租户归属校验后立刻报
        "takes 2 positional arguments but 3 were given"。
        按 L-014 的规则，替身**不要用 **kwargs 吞参数** ——
        吞掉会让"忘了传租户"这类缺陷静默通过，
        而它恰恰就是本项目出过的跨租户泄露形态。
        保持显式签名，让签名漂移在测试期就炸出来。
        """
        self.last_status_query = (task_id, tenant_id)
        return {
            "task_id": task_id, "document_id": None,
            "status": "queued", "progress": 0, "error": None,
        }


@pytest.fixture()
def client():
    """匿名主体 + 全依赖覆盖，测试后清空；不触发 lifespan（无需 infra）。"""
    app.dependency_overrides[get_principal] = lambda: Principal(tenant_id=None, authenticated=False)
    yield TestClient(app)
    app.dependency_overrides.clear()


def _use_shared(answer: FakeAnswerOnce, svc: FakeSessions) -> None:
    """用 lambda 返回同一实例，保证多请求间 fake 状态可被测试观察。"""
    app.dependency_overrides[get_oneshot_answerer] = lambda: answer
    app.dependency_overrides[get_session_service] = lambda: svc


def test_health_config_echo(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    for k in ("embedding_model", "llm_model", "collection", "retrieval_mode",
              "rerank_enabled", "default_top_k", "tenant_field", "status"):
        assert k in body
    assert body["status"] == "ok"


def test_query_contract(client):
    answer = FakeAnswerOnce()
    _use_shared(answer, FakeSessions())
    r = client.post("/api/v1/query", json={"query": "保修政策？", "top_k": 3})
    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == "答案A"
    assert body["retrieved_count"] == 1
    assert body["sources"][0]["source"] == "s.md"
    assert answer.last_top_k == 3, "top_k 没有传到编排层"


def test_chat_routes_are_gone(client):
    """Step 3：/chat 与 /chat/stream 已下线，能力由 /agent/stream 承担。"""
    _use_shared(FakeAnswerOnce(), FakeSessions())
    assert client.post("/api/v1/chat", json={"message": "x"}).status_code == 404
    assert client.post("/api/v1/chat/stream", json={"message": "x"}).status_code == 404
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/v1/chat" not in paths and "/api/v1/chat/stream" not in paths
    assert "/api/v1/agent/stream" in paths


def test_sessions_create_then_delete(client):
    _use_shared(FakeAnswerOnce(), FakeSessions())
    r = client.post("/api/v1/sessions")
    assert r.status_code == 200
    sid = r.json()["session_id"]

    assert client.delete(f"/api/v1/sessions/{sid}").status_code == 200
    # 再删：租户下已不存在 -> 404
    assert client.delete(f"/api/v1/sessions/{sid}").status_code == 404


def test_documents_upload_returns_202_and_status(client):
    fake = FakeIngestion()
    app.dependency_overrides[get_ingestion_service] = lambda: fake
    written_name = None
    try:
        r = client.post(
            "/api/v1/documents",
            files={"file": ("上传笔记.md", "# 标题\n正文内容", "text/markdown")},
        )
        assert r.status_code == 202
        body = r.json()
        assert body["task_id"] == "task_test_123"
        assert body["status"] == "queued"
        assert body["filename"].endswith("上传笔记.md")
        written_name = body["filename"]
        # tenant 匿名 -> tenant_ids None
        assert fake.submitted[0][2] is None

        s = client.get(f"/api/v1/documents/{body['task_id']}/status")
        assert s.status_code == 200
        assert s.json()["status"] == "queued"
    finally:
        if written_name:
            (settings.knowledge_base_dir / ".versions" / written_name).unlink(missing_ok=True)


def test_documents_rejects_unsupported_type(client):
    fake = FakeIngestion()
    app.dependency_overrides[get_ingestion_service] = lambda: fake
    r = client.post(
        "/api/v1/documents",
        files={"file": ("evil.exe", b"MZ\x90\x00", "application/octet-stream")},
    )
    assert r.status_code == 415
