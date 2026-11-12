"""
loader.base - 解析层的基础件（零重依赖）

只依赖标准库，供各格式子模块共用：
- Document           : 统一文档块数据结构（indexer / chunking 的输入契约）
- compute_file_hash  : 文件 MD5，增量入库与版本判重依赖此口径
- _data_to_markdown_table : 二维列表转 Markdown 表格（PDF/DOCX/XLSX 三条链路共用）
- logger             : 全解析层共享的 logger

这个模块刻意不导入 pymupdf / pandas / openpyxl / python-docx 等任何解析库，
也不导入本包的其他子模块，保证 `from .base import Document` 不会触发连锁导入。

logger 名字固定为 "app.ingestion.loader"（不是 __name__）：
`app/core/logging.py` 的 formatter 使用 `%(name)s`，logger 名本身就是日志维度。
拆包后若沿用 `logging.getLogger(__name__)`，名字会漂移成
"app.ingestion.loader.base"，下游按名字过滤的日志会静默失效。

注意：Document 物理定义在本模块，`Document.__module__` 为
"app.ingestion.loader.base"。对象身份与 `from app.ingestion.loader import Document`
取得的完全一致；仅依赖 pickle 的持久化数据在以模块路径反查时才需要留意。
"""

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

# 固定 logger 名，保持与拆包前一致（见模块文档）
logger = logging.getLogger("app.ingestion.loader")


@dataclass
class Document:
    """统一文档块数据结构"""
    text: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"text": self.text, "metadata": self.metadata}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Document":
        return cls(text=data["text"], metadata=data.get("metadata", {}))


def compute_file_hash(file_path: Path) -> str:
    """计算文件 MD5 hash，用于增量更新判断"""
    hash_md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()


def _data_to_markdown_table(data: List[List]) -> str:
    """将二维列表转为 Markdown 表格格式"""
    if not data:
        return ""
    max_cols = max(len(row) for row in data)
    for row in data:
        while len(row) < max_cols:
            row.append("")
    lines = []
    header = " | ".join(str(cell).strip() for cell in data[0])
    lines.append(f"| {header} |")
    sep = "| " + " | ".join("---" for _ in data[0]) + " |"
    lines.append(sep)
    for row in data[1:]:
        line = "| " + " | ".join(str(cell).strip() for cell in row) + " |"
        lines.append(line)
    return "\n".join(lines)
