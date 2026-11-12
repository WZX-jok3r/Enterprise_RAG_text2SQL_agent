"""loader 公共接口（兼容面）守卫。

这套断言把"重构不得破坏的契约"写死，供 loader 拆分/搬移时逐阶段验收：

1. 五个公共名必须从 `app.ingestion.loader` 可取（4 处生产调用方依赖）；
2. `app.ingestion` 包级再导出与 `__all__` 必须保持 12 项（文档化的对外入口）；
3. `load_file` / `scan_knowledge_base` / `load_all_documents` 的 `__globals__`
   必须仍是 loader 模块字典 —— 这是 `test_versioned_indexing.py` 直接
   monkeypatch `loader.KNOWLEDGE_BASE_DIR` 的前提，也是"配置量必须与读取它的
   函数同处一个模块字典"的硬约束；
4. 配置快照必须与**读取它的函数**同处一个模块字典（详见 CONFIG_SNAPSHOTS 与
   MOVED_CONFIG_SNAPSHOTS：拆包后两者不再是同一批名字）；
5. `Document` 的字段与序列化契约不变（indexer 依赖 `doc.to_dict()`）；
6. 能力标志（HAS_*）允许退化为兼容再导出，但不得整体消失。

注解：这些断言在"loader.py → loader/ 包 + 延迟导入"重构中必须全程为绿。
若某条变红，说明契约被破坏了，而不是"测试过时"。
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

import app.ingestion as ingestion
from app.ingestion import loader

# 4 处生产导入方实际用到的名字（见 app/ingestion/indexer.py、
# app/services/ingestion_service.py、app/worker/tasks/versioned_ingest.py）
REQUIRED_FROM_LOADER = {
    "Document",
    "compute_file_hash",
    "load_file",
    "scan_knowledge_base",
    "load_all_documents",
}

# app/ingestion/__init__.py 文档化的 12 项对外入口
EXPECTED_PACKAGE_ALL = {
    "Document", "compute_file_hash", "load_file", "scan_knowledge_base",
    "load_all_documents",
    "Chunk", "chunk_document", "chunk_all", "chunks_to_json",
    "ProcessedCache", "process_file", "run_ingest",
}

# 仍留在门面、由门面函数读取的配置快照：必须与读取它的函数同处 loader 模块字典
CONFIG_SNAPSHOTS = (
    "KNOWLEDGE_BASE_DIR",
    "SUPPORTED_EXTENSIONS",
    "PDF_SUBDIR",
)

# 随解析域一起搬走的配置快照：读取它的函数已不在门面，
# 因此"配置量与读取者同模块"这条约束要在**拥有者模块**内验证。
# 注意：patch 门面的同名别名不会影响解析行为（函数读的是自己模块的 __globals__）。
MOVED_CONFIG_SNAPSHOTS = {
    "USE_MARKER_FOR_PDF": "app.ingestion.loader.pdf_loader",
}

# 能力标志：允许只做兼容再导出（全仓零消费方），但不得整体消失
EXPECTED_CAPABILITY_FLAGS = (
    "HAS_PYMUPDF",
    "HAS_PDFPLUMBER",
    "HAS_DOCX",
    "HAS_OPENPYXL",
    "HAS_PANDAS",
    "HAS_TESSERACT",
    "HAS_PIL",
)

FUNCTIONS_READING_GLOBALS = (
    "load_file",
    "scan_knowledge_base",
    "load_all_documents",
)


@pytest.mark.parametrize("name", sorted(REQUIRED_FROM_LOADER))
def test_loader_exposes_required_name(name: str) -> None:
    assert hasattr(loader, name), f"loader 缺少公共名: {name}"


def test_loader_public_names_are_usable() -> None:
    """取到的名字必须是可直接调用的对象，而不只是占位属性。"""
    assert isinstance(loader.Document, type)
    for name in ("compute_file_hash", "load_file", "scan_knowledge_base", "load_all_documents"):
        assert callable(getattr(loader, name)), f"{name} 不可调用"


@pytest.mark.parametrize("name", sorted(EXPECTED_PACKAGE_ALL))
def test_package_reexports_documented_name(name: str) -> None:
    """`app.ingestion.<name>` 形式必须可用（重构为 PEP 562 懒表后也要成立）。"""
    assert hasattr(ingestion, name), f"app.ingestion 缺少对外入口: {name}"


def test_package_all_is_unchanged() -> None:
    assert set(ingestion.__all__) == EXPECTED_PACKAGE_ALL
    assert len(ingestion.__all__) == len(EXPECTED_PACKAGE_ALL), "__all__ 有重复项"


@pytest.mark.parametrize("name", FUNCTIONS_READING_GLOBALS)
def test_function_globals_are_loader_dict(name: str) -> None:
    """函数必须物理定义在 loader 模块内（否则 monkeypatch 配置量失效）。"""
    func = getattr(loader, name)
    assert func.__globals__ is vars(loader), (
        f"{name}.__globals__ 不是 loader 模块字典 —— 配置量的 monkeypatch 会静默失效"
    )


@pytest.mark.parametrize("name", CONFIG_SNAPSHOTS)
def test_config_snapshot_is_real_attribute(name: str) -> None:
    """留在门面的配置量必须真实存在于模块字典（monkeypatch 的 raising=True 前提）。"""
    assert name in vars(loader), (
        f"{name} 不在 loader.__dict__ 中 —— monkeypatch.setattr 默认会因缺少属性而报错"
    )


@pytest.mark.parametrize("name,module_path", sorted(MOVED_CONFIG_SNAPSHOTS.items()))
def test_moved_config_snapshot_is_colocated_with_reader(name: str, module_path: str) -> None:
    """搬走的配置量必须住在读取它的函数所在模块，门面只是别名。"""
    owner = importlib.import_module(module_path)

    assert name in vars(owner), f"{module_path} 缺少配置量 {name}（monkeypatch 会报错）"
    assert getattr(loader, name) is getattr(owner, name), "门面别名与拥有者不是同一个对象"


@pytest.mark.parametrize("name", EXPECTED_CAPABILITY_FLAGS)
def test_capability_flag_still_queryable(name: str) -> None:
    """能力标志可以只剩兼容再导出，但外部查询不能因为拆包而断掉。"""
    value = getattr(loader, name, None)
    assert isinstance(value, bool), f"loader.{name} 应为 bool，实际 {value!r}"


def test_knowledge_base_dir_patch_takes_effect(monkeypatch, tmp_path: Path) -> None:
    """patch 配置量后，扫描函数必须立刻读到新值（与 test_versioned_indexing 同一契约）。"""
    (tmp_path / "patched_only.md").write_text("patched", encoding="utf-8")
    (tmp_path / "pdf_examples").mkdir()

    monkeypatch.setattr(loader, "KNOWLEDGE_BASE_DIR", tmp_path)
    found = {path.name for path in loader.scan_knowledge_base()}

    assert found == {"patched_only.md"}, f"patch 未生效，实际扫到: {sorted(found)}"


def test_load_file_reads_patched_extensions(monkeypatch, tmp_path: Path) -> None:
    """`load_file` 读的是模块字典里的 SUPPORTED_EXTENSIONS（同一 __globals__ 契约）。

    只断言"模块级配置的 patch 立即生效"，不依赖 FORMAT_LOADER_MAP 的形态：
    它的值类型（真实函数 → 模块路径元组）本身就是重构对象。
    """
    target = tmp_path / "sample.txt"
    target.write_text("hello", encoding="utf-8")

    assert [doc.text for doc in loader.load_file(target)] == ["hello"]

    monkeypatch.setattr(loader, "SUPPORTED_EXTENSIONS", set())
    assert loader.load_file(target) == [], "收缩 SUPPORTED_EXTENSIONS 后 load_file 未被影响"


def test_document_schema_is_stable() -> None:
    """Document 的字段与序列化形状是 indexer / chunking 的输入契约。"""
    doc = loader.Document(text="hello", metadata={"format": "md"})
    assert set(doc.to_dict()) == {"text", "metadata"}
    roundtrip = loader.Document.from_dict(doc.to_dict())
    assert (roundtrip.text, roundtrip.metadata) == ("hello", {"format": "md"})
    assert loader.Document.from_dict({"text": "x"}).metadata == {}


def test_compute_file_hash_is_md5_of_content(tmp_path: Path) -> None:
    """哈希口径被增量入库依赖（processed_cache 与版本判重都按它比对）。"""
    import hashlib

    target = tmp_path / "sample.txt"
    target.write_bytes(b"loader-golden")
    assert loader.compute_file_hash(target) == hashlib.md5(b"loader-golden").hexdigest()
