"""
loader.text - 纯文本类解析（.txt / .md）

只依赖标准库 + base，不触发任何二进制格式解析库：
    from app.ingestion.loader.text import load_txt
不应加载 pymupdf / pandas / openpyxl / python-docx。

搬迁说明：两个函数与迁移前逐字一致，未做任何行为改写；它们读取的全局名
（Document / logger）通过 `.base` 导入，与拆包前同属一个模块字典的
monkeypatch 语义不再适用（此前解析层也没有针对这两个函数的测试钩子）。
"""

from pathlib import Path
from typing import List

from .base import Document, logger


def load_txt(file_path: Path) -> List[Document]:
    """
    加载 .txt 文件
    自动检测文件编码（utf-8 / gbk / latin-1）
    """
    logger.info(f"[TXT] 解析: {file_path.name}")
    documents = []
    try:
        # 尝试多种编码读取
        content = ""
        detected_encoding = "utf-8"
        for encoding in ["utf-8", "utf-8-sig", "gbk", "gb2312", "latin-1"]:
            try:
                with open(file_path, "r", encoding=encoding) as f:
                    content = f.read()
                detected_encoding = encoding
                break
            except (UnicodeDecodeError, UnicodeError):
                continue
        if content.strip():
            documents.append(
                Document(
                    text=content.strip(),
                    metadata={
                        "source": file_path.name,
                        "full_path": str(file_path),
                        "format": "txt",
                        "type": "text",
                        "encoding": detected_encoding,
                    },
                )
            )
        else:
            logger.warning(f"[TXT] 文件内容为空: {file_path.name}")
        return documents
    except Exception as e:
        logger.error(f"[TXT] 解析失败 {file_path.name}: {e}")
        return []


def load_md(file_path: Path) -> List[Document]:
    """
    加载 .md 文件，保留原始 markdown 文本
    （后续 chunk_strategy 按标题切分）
    """
    logger.info(f"[MD] 解析: {file_path.name}")
    documents = []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read()
        if not text.strip():
            logger.warning(f"[MD] 文件为空: {file_path.name}")
            return []
        return [
            Document(
                text=text,
                metadata={
                    "source": file_path.name,
                    "full_path": str(file_path),
                    "format": "md",
                    "type": "markdown",
                },
            )
        ]
    except Exception as e:
        logger.error(f"[MD] 解析失败 {file_path.name}: {e}")
        return []
