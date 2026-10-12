"""
source_formatter.py - 把检索 chunk 组装成对外 sources 列表

忠实迁移自 src/rag_pipeline.py 的 _format_sources（query / query_with_history / stream 共用）。
with_preview=True 时附带正文预览（截断 200 字）。score 统一 round 到 4 位。
"""

from pathlib import Path
from typing import Any, Dict, List


def format_sources(chunks: List[Dict[str, Any]], with_preview: bool = False) -> List[Dict[str, Any]]:
    """将检索 chunks 组装成 sources 列表。"""
    sources = []
    for chunk in chunks:
        meta = chunk["metadata"]
        source_name = Path(meta.get("source", "")).name
        item = {
            "source": source_name,
            "format": meta.get("format", "unknown"),
            "score": round(chunk["score"], 4),
        }
        if with_preview:
            text = chunk["text"]
            item["text_preview"] = text[:200] + "..." if len(text) > 200 else text
        if meta.get("document_version_id") is not None:
            item["document_version_id"] = meta["document_version_id"]
            item["document_version"] = meta["document_version"]
        sources.append(item)
    return sources
