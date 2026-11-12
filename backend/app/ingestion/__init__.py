"""
ingestion - 入库领域：解析(loader) → 分块(chunking) → 索引(indexer)

- loader.py  : 多格式解析（pdf/docx/xlsx/md/txt），迁自 src/document_loader.py
- chunking/  : 分块包（base/text/table），迁自 src/chunk_strategy.py
- indexer.py : 增量入库编排（ProcessedCache + process_file + run_ingest），迁自 src/ingest.py

对外主入口：load_file / scan_knowledge_base / load_all_documents / chunk_document / process_file / run_ingest。

导入策略（PEP 562 模块级 __getattr__）
------------------------------------
本包**不急切导入任何子模块**。此前急切导入 indexer 会连带拉入
qdrant_client / sqlalchemy / requests / jieba（向量与传输层），使任何
`import app.ingestion.*` 都要为此付费。2026-11-12 实测（各入口独立进程冷导入）：

    from app.ingestion.chunking import chunk_document   1763 ms / 1864 模块 → 221 ms / 312 模块
    from app.ingestion.loader import compute_file_hash  2022 ms / 1864 模块 → 738 ms / 1142 模块
    import app.ingestion                                   —                    → 5 ms / 84 模块
    from app.ingestion.indexer import process_file      1712 ms（不变，它确实需要向量层）

改为按名字按需导入后，`__all__` 与对外可见的名字保持不变：
    from app.ingestion import Document          → 只导入 loader
    from app.ingestion.chunking import ...      → 只导入 chunking
    import app.ingestion.indexer                → 仍会拉全量（预期）

注意：新增对外名字时必须同时补进 __all__ 与 _EXPORT_MODULES，否则访问会抛
AttributeError；解析器名（load_pdf 等）不属于本包契约，不要加进来。
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "Document", "compute_file_hash", "load_file", "scan_knowledge_base", "load_all_documents",
    "Chunk", "chunk_document", "chunk_all", "chunks_to_json",
    "ProcessedCache", "process_file", "run_ingest",
]

# 对外名字 -> 真正定义它的子模块（与 __all__ 一一对应）
_EXPORT_MODULES: dict[str, str] = {
    "Document": "app.ingestion.loader",
    "compute_file_hash": "app.ingestion.loader",
    "load_file": "app.ingestion.loader",
    "scan_knowledge_base": "app.ingestion.loader",
    "load_all_documents": "app.ingestion.loader",
    "Chunk": "app.ingestion.chunking",
    "chunk_document": "app.ingestion.chunking",
    "chunk_all": "app.ingestion.chunking",
    "chunks_to_json": "app.ingestion.chunking",
    "ProcessedCache": "app.ingestion.indexer",
    "process_file": "app.ingestion.indexer",
    "run_ingest": "app.ingestion.indexer",
}


def __getattr__(name: str) -> Any:
    """PEP 562：首次访问时才导入定义该名字的子模块，随后缓存进模块字典。

    必须抛 AttributeError（而不是返回 None），否则：
    - `from app.ingestion import loader` 这类子模块导入会失效；
    - `hasattr` / 静态检查会把不存在的名字误判为存在。
    """
    module_path = _EXPORT_MODULES.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module_path), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """让 dir()/IDE 补全能看到尚未导入的对外名字。"""
    return sorted(set(globals()) | set(__all__))
