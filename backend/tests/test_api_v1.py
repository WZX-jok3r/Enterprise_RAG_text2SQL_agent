"""API v1 路由门禁测试（P5）。

用 FastAPI dependency_overrides 注入假 pipeline / session / ingestion，
配合 TestClient（不进 with，故不触发 lifespan）实现完全 hermetic：
不打网络（SiliconFlow）、不依赖 PG/Redis/Qdrant。覆盖：
- /health 配置回显；/query 契约；/chat 多轮 session 续聊与历史长度；
- /chat/stream SSE 帧（meta/delta/done + 落库）；/sessions 建删与 404；
- /documents 上传 202 返回 task_id + 状态查询（并清理落盘文件）。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

from app.api.deps import (
    get_ingestion_service,
    get_pipeline,
    get_principal,
    get_session_service,
)
from app.core.config import settings
from app.core.security import Principal
from app.main import app


class FakePipeline:
    def __init__(self) -> None:
        self.last_history: List[Dict[str, str]] = []
        self.last_filter: Optional[Dict[str, Any]] = None

    def query(self, q, top_k=None, filter_dict=None, generate=True):
        self.last_filter = filter_dict
        return {
            "query": q, "answer": "答案A",
            "sources": [{"source": "s.md", "format": "md", "score": 0.9, "text_preview": "预览"}],
            "retrieved_count": 1, "hidden_count": 0,
        }

    def query_with_history(self, q, history, top_k=None, filter_dict=None):
        self.last_history = list(history)
        self.last_filter = filter_dict
        return {
            "query": q, "answer": "答案B", "sources": [],
            "retrieved_count": 0, "hidden_count": 0,
        }

    def stream_chat(self, q, history, top_k=None, filter_dict=None):
        yield {"type": "meta", "sources": [], "retrieved_count": 0, "hidden_count": 0}
        yield {"type": "delta", "text": "He"}
        yield {"type": "delta", "text": "llo"}
        yield {"type": "done", "answer": "Hello"}


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


def _use_shared(pipe: FakePipeline, svc: FakeSessions) -> None:
    """用 lambda 返回同一实例，保证多请求间 fake 状态可被测试观察。"""
    app.dependency_overrides[get_pipeline] = lambda: pipe
    app.dependency_overrides[get_session_service] = lambda: svc


def _sse_events(text: str) -> List[dict]:
    events = []
    for frame in text.split("\n\n"):
        line = next((l for l in frame.split("\n") if l.startswith("data:")), None)
        if line:
            payload = line[len("data:"):].strip()
            if payload:
                events.append(json.loads(payload))
    return events


def test_health_config_echo(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    for k in ("embedding_model", "llm_model", "collection", "retrieval_mode",
              "rerank_enabled", "default_top_k", "tenant_field", "status"):
        assert k in body
    assert body["status"] == "ok"


def test_query_contract(client):
    _use_shared(FakePipeline(), FakeSessions())
    r = client.post("/api/v1/query", json={"query": "保修政策？", "top_k": 3})
    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == "答案A"
    assert body["retrieved_count"] == 1
    assert body["sources"][0]["source"] == "s.md"


def test_chat_multi_turn_and_history(client):
    _use_shared(FakePipeline(), FakeSessions())
    r1 = client.post("/api/v1/chat", json={"message": "第一个问题"})
    assert r1.status_code == 200
    b1 = r1.json()
    sid = b1["session_id"]
    assert sid and b1["history_length"] == 2

    r2 = client.post("/api/v1/chat", json={"message": "第二个问题", "session_id": sid})
    assert r2.status_code == 200
    b2 = r2.json()
    assert b2["session_id"] == sid
    assert b2["history_length"] == 4


def test_chat_stream_sse(client):
    pipe = FakePipeline()
    svc = FakeSessions()
    _use_shared(pipe, svc)
    r = client.post("/api/v1/chat/stream", json={"message": "流式提问"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = _sse_events(r.text)
    types = [e["type"] for e in events]
    assert types[0] == "meta"
    assert "delta" in types
    done = events[-1]
    assert done["type"] == "done" and done["answer"] == "Hello"
    assert done.get("session_id")
    assert done.get("history_length") == 2
    # 会话已持久化本轮 user + assistant
    hist = asyncio.run(svc.get_history(done["session_id"], None))
    assert [m["role"] for m in hist] == ["user", "assistant"]


def test_sessions_create_then_delete(client):
    _use_shared(FakePipeline(), FakeSessions())
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
