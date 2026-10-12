"""
ingest.py - 入口脚本：扫描知识库，增量入库

核心逻辑:
1. 扫描 knowledge_base 目录，收集所有文件
2. 对比 processed_cache，判断文件是否已处理/是否变更
3. 新增/变更的文件：清理旧向量 → 解析 → 分块 → Embedding → 写入 Qdrant
4. 更新 processed_cache 记录（增量保存）

用法:
    python src/ingest.py              # 全量扫描并入库
    python src/ingest.py --force      # 强制重新处理所有文件（忽略缓存）
    python src/ingest.py --reset      # 清空 Qdrant collection 和缓存，重新全量入库
"""

import json
import time
import argparse
import logging
import fnmatch
from pathlib import Path
from typing import Dict, List, Any, Optional

from app.core.config import settings
from app.ingestion.loader import (
    load_file,
    compute_file_hash,
    scan_knowledge_base,
    Document,
)
from app.ingestion.chunking import chunk_document, Chunk, chunks_to_json
from app.vector.qdrant import get_vector_store, VectorStore

# 配置兼容层：沿用原 UPPER_CASE 常量名
KNOWLEDGE_BASE_DIR = settings.knowledge_base_dir
PROCESSED_CACHE_FILE = settings.processed_cache_file
SUPPORTED_EXTENSIONS = settings.supported_extensions
PDF_SUBDIR = settings.pdf_subdir
TENANT_FIELD = settings.tenant_field

logger = logging.getLogger(__name__)


class ProcessedCache:
    """processed_cache 管理器"""

    def __init__(self, cache_file: Path):
        self.cache_file = cache_file
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.data = self._load()
        self._dirty = False  # 标记是否有未保存的变更

    def _load(self) -> Dict[str, Any]:
        """加载缓存文件"""
        if self.cache_file.exists():
            try:
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except json.JSONDecodeError:
                logger.warning("[Cache] 缓存文件损坏，将重建")
                return {}
        return {}

    def save(self, force: bool = False):
        """保存缓存到文件（仅在数据变更时写入）"""
        if not self._dirty and not force:
            return
        with open(self.cache_file, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        self._dirty = False
        logger.info(f"[Cache] 缓存已保存: {self.cache_file}")

    def get_file_record(self, rel_path: str) -> Dict[str, Any]:
        """获取单个文件的缓存记录"""
        return self.data.get(rel_path, {})

    def set_file_record(self, rel_path: str, record: Dict[str, Any]):
        """设置单个文件的缓存记录"""
        self.data[rel_path] = record
        self._dirty = True

    def is_file_unchanged(self, rel_path: str, file_hash: str) -> bool:
        """判断文件是否未变更"""
        record = self.get_file_record(rel_path)
        return record.get("file_hash") == file_hash and record.get("status") == "success"

    def clear(self):
        """清空缓存"""
        self.data = {}
        self._dirty = True
        logger.info("[Cache] 缓存已清空")


def _get_rel_path(file_path: Path) -> str:
    """获取相对于知识库根目录的路径，作为跨环境稳定的 cache key 和 metadata source"""
    try:
        return str(file_path.relative_to(KNOWLEDGE_BASE_DIR))
    except ValueError:
        # 兜底：如果文件不在 KNOWLEDGE_BASE_DIR 下，使用文件名
        return file_path.name


def _normalize_tenants(raw: Optional[List[str]]) -> Optional[List[str]]:
    """
    将 --tenant 入参归一为去重的租户列表。
    支持两种写法：--tenant a,b 与 --tenant a --tenant b（可混用）。
    返回 None 表示不启用租户隔离（不写字段）；空列表也视为 None。
    """
    if not raw:
        return None
    result: List[str] = []
    for part in raw:
        for token in str(part).split(","):
            t = token.strip()
            if t and t not in result:
                result.append(t)
    return result or None


def _select_files(file_paths: List[Path], patterns: Optional[List[str]]) -> Optional[List[Path]]:
    """
    按 --files 传入的模式筛选文件（支持逗号分隔与多次传入）。
    每个模式可是：文件名(product_spec.txt)、相对路径(sub/a.md)或通配符(*.md)。
    无模式时返回 None（表示不筛选、处理全部）；有模式但无命中时返回空列表。
    """
    if not patterns:
        return None
    pats: List[str] = []
    for part in patterns:
        for token in str(part).split(","):
            t = token.strip()
            if t:
                pats.append(t)
    if not pats:
        return None
    selected: List[Path] = []
    for fp in file_paths:
        rel = _get_rel_path(fp)
        name = fp.name
        if any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(rel, p) or rel == p or name == p for p in pats):
            selected.append(fp)
    return selected


def process_file(file_path: Path, vector_store: VectorStore, tenant_ids: Optional[List[str]] = None,
                 *, version_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    处理单个文件：清理旧向量 → 解析 → 分块 → 向量化入库
    返回处理记录，用于更新 cache

    tenant_ids: 多租户 ACL 列表。为空时不写入租户字段（保持旧行为）；
    非空时把整个列表写入每个 chunk 的 tenant 字段（一份数据多方可见），
    旧向量按 source 整体清理（一份文件=一组向量，重新入库即刷新其 ACL）。
    """
    rel_path = _get_rel_path(file_path)
    if version_context is None and file_path.resolve().is_relative_to(
            (settings.knowledge_base_dir / ".versions").resolve()):
        raise ValueError("Managed version files must be ingested through the version worker")
    start_time = time.time()
    logger.info(f"[Process] 开始处理: {file_path.name}")

    # ✅ P0: 无论新增/变更/空文件，先清理该文件的所有旧向量，保证数据一致性
    #   ACL 模型下一个文件是一份数据（一组向量），按 source 整体清理即可
    #   注：各 loader 写入向量 payload 的 source 均为文件名（file_path.name），
    #   而 cache/rel_path 是相对路径；删除必须按“存储时写入的值”精确匹配，
    #   故此处用 file_path.name 而非 rel_path（否则 MatchValue 命中 0 条、清理变空操作，
    #   导致文件变更后重灌时旧向量残留、新旧块并存）。前提是知识库内文件名唯一。
    # Version retries clear only their staged revision, never the currently published one.
    cleanup = ({"document_version_id": version_context["document_version_id"]}
               if version_context else {"source": file_path.name})
    deleted = vector_store.delete_by_metadata(cleanup)
    if deleted > 0:
        logger.info(f"[Ingest] 已清理 {file_path.name} 的旧向量")

    # 1. 解析
    documents = load_file(file_path)
    if not documents:
        logger.warning(f"[Process] 文件解析结果为空: {file_path.name}")
        return {
            "file_hash": compute_file_hash(file_path),
            "status": "empty",
            "chunk_count": 0,
            "processing_time": round(time.time() - start_time, 2),
        }

    logger.info(f"[Process] {file_path.name}: 解析得到 {len(documents)} 个文档块")

    # 2. 按文件分组（转成 dict 格式供 chunk_strategy 使用）
    doc_dicts = [doc.to_dict() for doc in documents]

    # 3. 分块
    chunks = chunk_document(doc_dicts)
    logger.info(f"[Process] {file_path.name}: 切分为 {len(chunks)} 个 chunk")

    # 4. 向量化入库（内部应支持批量 embedding + 批量 upsert）
    chunk_dicts = [chunk.to_dict() for chunk in chunks]
    display_name = version_context["original_filename"] if version_context else file_path.name
    if version_context:
        for i, cd in enumerate(chunk_dicts):
            cd.setdefault("metadata", {}).update(version_context)
            cd["metadata"].update(source=display_name, storage_source=file_path.name, version_chunk_index=i)
    # 检索失败 A 类修复：把文档名注入块文本头部。BM25 索引与 rerank 输入都只看块正文，
    #   此前文件名不在任何可检索文本里，导致“docx-sample-with-table.docx 的表格标题是什么”
    #   这类点名题无法用强词面信号区分内容近同质的兄弟文档（如 pdf 版同名表格）。
    #   注意：注入后块文本变化 → 点 ID（text+source 派生）全变，需全量重灌才有效果；
    #   可用 INDEX_FILENAME_PREFIX=false 回滚（同样需重灌）。
    if settings.index_filename_prefix and file_path.name:
        filename_prefix = f"【文档: {display_name}】\n"
        for cd in chunk_dicts:
            if cd.get("text"):
                cd["text"] = filename_prefix + cd["text"]
    # 多租户 ACL：把租户列表整体写入每个 chunk 的 tenant 字段（为空则不写入，保持旧行为）
    #   Qdrant keyword 字段支持多值，查询 MatchValue(单个租户) 会命中"数组包含该值"的点
    if tenant_ids:
        for cd in chunk_dicts:
            cd.setdefault("metadata", {})[TENANT_FIELD] = list(tenant_ids)
    inserted = vector_store.upsert_chunks(chunk_dicts)
    if version_context and (not chunk_dicts or inserted != len(chunk_dicts)):
        raise RuntimeError("Incomplete version vector write")
    logger.info(f"[Process] {file_path.name}: 已写入 {inserted} 条向量")

    # 5. 返回处理记录
    return {
        "file_hash": compute_file_hash(file_path),
        "status": "success",
        "chunk_count": len(chunks),
        "inserted_count": inserted,
        "format": file_path.suffix.lower().lstrip("."),
        "processing_time": round(time.time() - start_time, 2),
    }


def run_ingest(force: bool = False, reset: bool = False, tenant_ids: Optional[List[str]] = None,
               include: Optional[List[str]] = None):
    """
    执行入库流程

    Args:
        force: 强制重新处理所有文件（忽略缓存）
        reset: 清空 Qdrant 和缓存，重新全量入库
        tenant_ids: 多租户 ACL 列表，写入每个 chunk 的 tenant 字段；为空则不启用隔离
        include: --files 文件筛选模式（文件名/相对路径/通配符）；为空则处理全部
    """
    # 初始化
    cache = ProcessedCache(PROCESSED_CACHE_FILE)
    vector_store = get_vector_store()

    # 处理 reset（带交互确认）
    if reset:
        confirm = input("⚠️  确认清空 Qdrant 和缓存？此操作不可逆 (y/N): ")
        if confirm.lower() != "y":
            logger.info("[Ingest] 已取消重置操作")
            return
        logger.warning("[Ingest] 执行重置操作...")
        vector_store.delete_collection()
        vector_store._ensure_collection()  # 重新创建
        cache.clear()
        cache.save(force=True)
        force = True  # reset 后强制全量处理

    # 扫描文件
    file_paths = scan_knowledge_base()
    if not file_paths:
        logger.error("[Ingest] 知识库目录为空或未找到支持的文件")
        return

    logger.info(f"[Ingest] 扫描到 {len(file_paths)} 个文件")

    # 文件筛选（--files）：只处理命中的文件，便于对不同文件打不同租户标签
    #   指定了筛选时，选中文件一律强制重处理（改标签本就需要重灌，不受缓存跳过影响）
    selected = _select_files(file_paths, include)
    force_selected = False
    if include:
        if not selected:
            logger.error(f"[Ingest] --files 未匹配到任何文件，请检查名称/路径/通配符: {include}")
            return
        force_selected = True
        logger.info(f"[Ingest] 文件筛选命中 {len(selected)} 个（共扫描 {len(file_paths)} 个），将强制重处理这 {len(selected)} 个")
        file_paths = selected

    # 统计
    stats = {
        "total_files": len(file_paths),
        "processed": 0,
        "skipped": 0,
        "failed": 0,
        "empty": 0,
        "total_chunks": 0,
        "total_processing_time": 0.0,
    }

    # 逐个处理
    for fp in file_paths:
        rel_path = _get_rel_path(fp)
        file_hash = compute_file_hash(fp)

        # 判断是否跳过（仅当缓存状态为 success 且 hash 一致时跳过）
        #   force_selected: 由 --files 显式选中的文件不跳过，确保重新打标签生效
        if not force and not force_selected and cache.is_file_unchanged(rel_path, file_hash):
            logger.info(f"[Ingest] 跳过未变更文件: {fp.name}")
            stats["skipped"] += 1
            continue

        # 处理文件
        try:
            record = process_file(fp, vector_store, tenant_ids=tenant_ids)
            cache.set_file_record(rel_path, record)
            # ✅ P1: 每处理完一个文件立即保存缓存，防止中途崩溃导致重跑
            cache.save()

            stats["total_processing_time"] += record.get("processing_time", 0)

            if record["status"] == "empty":
                stats["empty"] += 1
            elif record["status"] == "success":
                stats["processed"] += 1
                stats["total_chunks"] += record.get("chunk_count", 0)
            else:
                stats["failed"] += 1

        except Exception as e:
            logger.error(f"[Ingest] 处理失败 {fp.name}: {e}", exc_info=True)
            cache.set_file_record(rel_path, {
                "file_hash": file_hash,
                "status": "failed",
                "error": str(e),
            })
            cache.save()
            stats["failed"] += 1

    # 最终统计
    total_vectors = vector_store.count()
    logger.info("=" * 50)
    logger.info("[Ingest] 入库完成!")
    logger.info(f"  总文件: {stats['total_files']}")
    logger.info(f"  成功处理: {stats['processed']}")
    logger.info(f"  空文件: {stats['empty']}")
    logger.info(f"  已跳过: {stats['skipped']}")
    logger.info(f"  失败: {stats['failed']}")
    logger.info(f"  总 chunk 数: {stats['total_chunks']}")
    logger.info(f"  总耗时: {round(stats['total_processing_time'], 2)}s")
    logger.info(f"  Qdrant 向量总数: {total_vectors}")
    logger.info("=" * 50)


def main():
    parser = argparse.ArgumentParser(description="RAG 知识库增量入库脚本")
    parser.add_argument("--force", action="store_true", help="强制重新处理所有文件")
    parser.add_argument("--reset", action="store_true", help="清空 Qdrant 和缓存，重新全量入库")
    parser.add_argument("--tenant", type=str, action="append", default=None,
                        help="多租户 ACL（可重复或逗号分隔）：--tenant a,b 或 --tenant a --tenant b；"
                             "写入每个 chunk 的租户列表并按包含关系隔离检索；不传则不启用租户隔离")
    parser.add_argument("--files", type=str, action="append", default=None,
                        help="只处理命中的文件（可重复或逗号分隔）：支持文件名、相对路径或通配符如 *.md；"
                             "用于对不同文件选择性打不同租户。选中文件会强制重处理，其余文件与缓存不动")
    args = parser.parse_args()

    tenant_ids = _normalize_tenants(args.tenant)
    if tenant_ids:
        logger.info(f"[Ingest] 多租户 ACL 已启用，本批数据归属租户: {tenant_ids}")
    run_ingest(force=args.force, reset=args.reset, tenant_ids=tenant_ids, include=args.files)


if __name__ == "__main__":
    main()
