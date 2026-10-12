from types import SimpleNamespace

import pytest
from qdrant_client import QdrantClient, models

from app.core.config import settings
from app.vector import qdrant
from app.vector.qdrant import VectorStore


def store_with_client(client):
    store = object.__new__(VectorStore)
    store.client, store.collection_name, store.rerank_client = client, "test_versions", None
    store.embedding_client = SimpleNamespace(embed_single=lambda query: [1.0, 0.0])
    return store


def test_actual_qdrant_filter_current_only_and_tenant_intersection(monkeypatch):
    client = QdrantClient(":memory:")
    client.create_collection("test_versions", vectors_config={"dense": models.VectorParams(size=2, distance=models.Distance.COSINE)})
    points = []
    for i, (revision, tenant) in enumerate([(None, "a"), ("1", "a"), ("2", "a"), ("3", "b")], 1):
        payload = {"text": str(i), "source": "manual.md", settings.tenant_field: [tenant]}
        if revision:
            payload["document_version_id"] = revision
        points.append(models.PointStruct(id=i, vector={"dense": [1.0, 0.0]}, payload=payload))
    client.upsert("test_versions", points)
    store = store_with_client(client)
    monkeypatch.setattr(settings, "retrieval_mode", "vector")
    monkeypatch.setattr(qdrant, "current_revision_ids", lambda: ["2", "3"])
    results = store.search("q", top_k=10, filter_dict={settings.tenant_field: "a"})
    assert {r["text"] for r in results} == {"1", "3"}  # legacy plus current v2 only
    assert store.search("q", filter_dict={settings.tenant_field: "a", "document_version_id": "1"}) == []
    monkeypatch.setattr(qdrant, "current_revision_ids", lambda: ["1", "3"])
    assert {r["text"] for r in store.search("q", top_k=10, filter_dict={settings.tenant_field: "a"})} == {"1", "2"}
    client.close()


def test_hybrid_prefetch_and_outer_filter_share_version_constraint(monkeypatch):
    calls = []
    client = SimpleNamespace(query_points=lambda **kwargs: calls.append(kwargs) or SimpleNamespace(points=[]))
    store = store_with_client(client)
    monkeypatch.setattr(settings, "retrieval_mode", "hybrid")
    monkeypatch.setattr(qdrant, "current_revision_ids", lambda: ["2"])
    store.search("q", filter_dict={settings.tenant_field: "a"})
    query = calls[0]
    assert all(p.filter == query["query_filter"] for p in query["prefetch"])
    assert "document_version_id" in query["query_filter"].model_dump_json()


def test_registry_outage_never_returns_history(monkeypatch):
    store = store_with_client(SimpleNamespace(query_points=lambda **kwargs: pytest.fail("Must not query without registry")))
    def unavailable():
        raise RuntimeError("registry unavailable")
    monkeypatch.setattr(qdrant, "current_revision_ids", unavailable)
    with pytest.raises(RuntimeError, match="unavailable"):
        store.search("q")
