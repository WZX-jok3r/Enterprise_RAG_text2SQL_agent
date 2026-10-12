from types import SimpleNamespace

import pytest

from app.ingestion import indexer
from app.vector.qdrant import VectorStore


def test_staging_deletes_only_own_version_and_preserves_provenance(monkeypatch, tmp_path):
    path = tmp_path / "unique_manual.md"
    path.write_text("content", encoding="utf-8")
    metadata = {"document_version_id": "42", "document_key": "k", "document_version": 2,
                "original_filename": "manual.md"}
    chunk = SimpleNamespace(to_dict=lambda: {"text": "content", "metadata": {"source": path.name}})
    monkeypatch.setattr(indexer, "load_file", lambda p: [SimpleNamespace(to_dict=lambda: {})])
    monkeypatch.setattr(indexer, "chunk_document", lambda docs: [chunk])
    class Store:
        def delete_by_metadata(self, filters):
            self.deleted = filters
            return 1
        def upsert_chunks(self, chunks):
            self.chunks = chunks
            return len(chunks)
    store = Store()
    result = indexer.process_file(path, store, ["tenant_a"], version_context=metadata)
    assert result["status"] == "success"
    assert store.deleted == {"document_version_id": "42"}
    payload = store.chunks[0]["metadata"]
    assert payload["source"] == "manual.md" and payload["storage_source"] == path.name
    assert payload["document_version"] == 2 and payload[indexer.TENANT_FIELD] == ["tenant_a"]
    store.upsert_chunks = lambda chunks: 0
    with pytest.raises(RuntimeError, match="Incomplete"):
        indexer.process_file(path, store, ["tenant_a"], version_context=metadata)


def test_revision_point_ids_distinguish_version_position_and_full_text():
    store = object.__new__(VectorStore)
    meta = {"source": "manual.md", "document_version_id": "1", "version_chunk_index": 0}
    text = "a" * 250
    a = store._generate_id(text + "old", meta)
    assert a != store._generate_id(text + "new", meta)
    assert a != store._generate_id(text + "old", {**meta, "document_version_id": "2"})
    assert a != store._generate_id(text + "old", {**meta, "version_chunk_index": 1})


def test_sources_preserve_version_provenance():
    from app.rag.source_formatter import format_sources
    from app.schemas.chat import SourceItem
    chunks = [{"text": "content", "score": 0.9, "metadata": {"source": "manual.md",
        "format": "md", "document_version_id": "42", "document_version": 2}}]
    source = SourceItem(**format_sources(chunks)[0])
    assert source.document_version_id == "42" and source.document_version == 2


def test_legacy_scanner_cannot_republish_managed_version_files(monkeypatch, tmp_path):
    from app.ingestion import loader
    from app.core.config import settings
    monkeypatch.setattr(type(settings), "knowledge_base_dir", property(lambda self: tmp_path))
    monkeypatch.setattr(loader, "KNOWLEDGE_BASE_DIR", tmp_path)
    legacy = tmp_path / "legacy.md"
    legacy.write_text("legacy", encoding="utf-8")
    version = tmp_path / ".versions" / "unique.md"
    version.parent.mkdir()
    version.write_text("pending secret", encoding="utf-8")
    assert legacy in loader.scan_knowledge_base() and version not in loader.scan_knowledge_base()
    with pytest.raises(ValueError, match="version worker"):
        indexer.process_file(version, object())
