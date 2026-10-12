"""
qdrant.py - Qdrant 向量库连接、写入、检索、混合检索逻辑

从 src/vector_store.py 拆出（Embedding/Rerank 客户端见 vector/embedding.py）。职责:
1. 连接 Qdrant（本地 Docker）
2. 创建/管理 collection（named vectors: dense + bm25 稀疏）
3. 写入向量（带 metadata）
4. 检索向量（混合检索 dense+BM25+RRF）
5. 支持按 metadata 删除向量（文件更新时清理旧版本）
6. Rerank 精排（开关控制，失败自动降级）
7. 低相关过滤（仅对 rerank 绝对分生效，见 _filter_low_relevance）
"""

import hashlib
import logging
import re
from typing import Any, Dict, List, Optional

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    Document,
    FieldCondition,
    Filter,
    Fusion,
    FusionQuery,
    MatchValue,
    MatchAny,
    IsEmptyCondition,
    PayloadField,
    Modifier,
    PayloadSchemaType,
    PointStruct,
    Prefetch,
    SparseVectorParams,
    VectorParams,
)

from app.core.config import settings
from app.vector.embedding import EmbeddingClient, RerankClient
from app.vector.version_visibility import current_revision_ids

logger = logging.getLogger(__name__)

# ==================== BM25 稀疏通道：中文预分词 ====================
# 可选依赖：jieba（纯 Python，用于中文分词）；未安装时自动降级为原文投喂 BM25
try:
    import jieba
    _JIEBA_AVAILABLE = True
except ImportError:
    _JIEBA_AVAILABLE = False

if settings.hybrid_tokenize and not _JIEBA_AVAILABLE:
    logger.warning("[Tokenize] HYBRID_TOKENIZE 已开启但 jieba 未安装，BM25 通道将使用原文（中文匹配能力受限）")

# 英数串保护正则：型号/SKU/金额/页码等 token 整体保留，避免被 jieba 拆碎
_ASCII_RUN_RE = re.compile(r"[A-Za-z0-9]+(?:[.\-_/][A-Za-z0-9]+)*")


def tokenize_for_bm25(text: str) -> str:
    """
    为 BM25 稀疏通道做预分词（入库与查询必须对称调用）：
    - 先把 Unicode 破折号（U+2011 非断行连字符/en-dash/em-dash）归一为 ASCII '-'：
      否则 "docx‑sample‑..." 这类题面会被拆成 docx/sample 碎 token，与文档名整 token 失配
    - 再保护英数串（PS-12V-1.5A、BHG-24667 等）整体成 token
    - 其余中文段用 jieba 切词
    - jieba 未安装或开关关闭时直接返回归一化后的原文
    """
    # 破折号归一：与评测器 normalize_source_name 同口径，对称作用于入库/查询两侧
    text = text.replace("\u2011", "-").replace("\u2013", "-").replace("\u2014", "-")
    if not _JIEBA_AVAILABLE or not settings.hybrid_tokenize:
        return text

    tokens: List[str] = []
    pos = 0
    for m in _ASCII_RUN_RE.finditer(text):
        seg = text[pos:m.start()]
        if seg.strip():
            tokens.extend(t for t in jieba.cut(seg) if t.strip())
        tokens.append(m.group(0))
        pos = m.end()
    seg = text[pos:]
    if seg.strip():
        tokens.extend(t for t in jieba.cut(seg) if t.strip())
    return " ".join(tokens)


def _filter_low_relevance(results: List[Dict[str, Any]], score_kind: str):
    """
    低相关结果过滤（对已按分数降序排列的 results 生效），返回 (kept, dropped_count)。

    量纲安全（H）：
    - score_kind == "rerank": score 是交叉编码器的绝对相关分（约 [0,1]），才启用下面的阈值。
    - 其它（"rrf"/"vector"）：分数不具备跨 query 的绝对含义（RRF 融合分量级仅 ~0.016），
      套用绝对阈值会把全部结果误杀，因此直接原样返回、不做过滤。

    阈值策略（仅 rerank）：
    1) score_abs_min <= 0 视为关闭过滤；
    2) 最高分 < score_abs_min：整体无相关。score_drop_all_below=True 则全丢（触发拒答），
       否则仅保底保留最高分 score_min_keep 条；
    3) 存在强锤点（最高分 >= score_confident）：启用相对断层，阈值 = max(score_abs_min, 最高分*score_rel_ratio)；
    4) 只有中等分（无强锤点）：保守只用绝对下限 score_abs_min，不对中等分簇施断层（避免误杀最优项）；
    5) 过滤后为空则保底保留最高分 score_min_keep 条。
    """
    if not results or score_kind != "rerank" or settings.score_abs_min <= 0:
        return results, 0

    max_score = results[0].get("score", 0.0)

    # 2) 整体无相关：最高分也低于绝对下限
    if max_score < settings.score_abs_min:
        if settings.score_drop_all_below:
            return [], len(results)
        keep = results[:max(1, settings.score_min_keep)]
        return keep, len(results) - len(keep)

    # 3)/4) 根据是否有强锤点选择断层阈值 or 仅绝对下限
    if max_score >= settings.score_confident:
        threshold = max(settings.score_abs_min, max_score * settings.score_rel_ratio)
    else:
        threshold = settings.score_abs_min

    kept = [r for r in results if r.get("score", 0.0) >= threshold]

    # 5) 保底：即使全部阈值之上无命中，也保留最高分 N 条
    if not kept:
        kept = results[:max(1, settings.score_min_keep)]

    return kept, len(results) - len(kept)


class VectorStore:
    """Qdrant 向量库封装"""

    def __init__(self):
        self.client = QdrantClient(host=settings.qdrant_host, port=settings.qdrant_port)
        self.collection_name = settings.qdrant_collection_name
        self.embedding_client = EmbeddingClient()
        self.vector_dim = settings.vector_dim
        # Rerank 精排客户端（rerank_enabled 控制；精排 API 异常时检索路径自动降级）
        self.rerank_client = RerankClient() if settings.rerank_enabled else None
        if self.rerank_client:
            logger.info(f"[Rerank] 精排已启用: {settings.rerank_model}（候选 {settings.rerank_candidates} 条）")

        # 确保 collection 存在
        self._ensure_collection()

    def _ensure_collection(self):
        """检查并（按需）创建 collection：named vectors = dense + bm25(sparse)"""
        collections = self.client.get_collections().collections
        collection_names = [c.name for c in collections]

        if self.collection_name not in collection_names:
            logger.info(f"[Qdrant] 创建 collection: {self.collection_name} (named vectors: dense + bm25)")
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config={
                    "dense": VectorParams(size=self.vector_dim, distance=Distance.COSINE),
                },
                sparse_vectors_config={
                    "bm25": SparseVectorParams(modifier=Modifier.IDF),
                },
            )
        else:
            # 结构校验：v2 要求名为 "dense" 的稠密向量；旧版单无名向量结构不兼容
            info = self.client.get_collection(self.collection_name)
            vectors = info.config.params.vectors
            if not isinstance(vectors, dict) or "dense" not in vectors:
                raise RuntimeError(
                    f"collection '{self.collection_name}' 为旧版单向量结构，与混合检索（named vectors）不兼容。"
                    "请使用新的 collection 名，或执行 python -m app.ingestion.indexer --reset 重建。"
                )
            logger.info(f"[Qdrant] collection 已存在: {self.collection_name}")

        # 为多租户隔离字段建 keyword 索引，避免按 tenant_id 过滤时逐点扫描（幂等，失败仅告警）
        self._ensure_tenant_index()
        self.client.create_payload_index(collection_name=self.collection_name,
            field_name="document_version_id", field_schema=PayloadSchemaType.KEYWORD)

    def _ensure_tenant_index(self):
        """为租户字段创建 payload 索引，保障 tenant_id 过滤性能"""
        try:
            self.client.create_payload_index(
                collection_name=self.collection_name,
                field_name=settings.tenant_field,
                field_schema=PayloadSchemaType.KEYWORD,
            )
            logger.info(f"[Qdrant] 已确保租户字段 payload 索引: {settings.tenant_field}")
        except Exception as e:
            # 旧版 client 方法名差异或已存在时，不阻断启动
            logger.warning(f"[Qdrant] 创建租户字段索引跳过/失败（不影响主流程）: {e}")

    def delete_collection(self):
        """删除 collection（用于重置）"""
        logger.warning(f"[Qdrant] 删除 collection: {self.collection_name}")
        self.client.delete_collection(collection_name=self.collection_name)

    def delete_by_metadata(self, filter_dict: Dict[str, Any]) -> int:
        """
        根据 metadata 字段精确匹配删除向量
        用于增量更新时清理同一文件的旧版本向量

        Args:
            filter_dict: 要匹配的 metadata 键值对，如 {"source": "contract_snippet.md"}

        Returns:
            1 表示删除请求已成功提交，0 表示无匹配条件未执行删除
            注意: Qdrant delete API 不返回实际删除数量，仅返回操作确认
        """
        if not filter_dict:
            return 0

        # 构建 must 条件列表（所有条件都要满足）
        must_conditions = [
            FieldCondition(
                key=key,
                match=MatchValue(value=value)
            )
            for key, value in filter_dict.items()
        ]

        try:
            result = self.client.delete(
                collection_name=self.collection_name,
                points_selector=Filter(must=must_conditions),
                wait=True,  # 同步等待删除完成，保证后续 upsert 不会与删除并发冲突
            )
            # UpdateResult 只有 status 和 operation_id，没有 points_count
            # status == "completed" 即表示删除操作已成功执行
            if result.status == "completed":
                logger.info(f"[VectorStore] 按 metadata 删除请求已执行: {filter_dict}")
                return 1
            else:
                logger.warning(f"[VectorStore] 删除操作未完成，status={result.status}")
                return 0
        except Exception as e:
            logger.error(f"[VectorStore] delete_by_metadata 失败: {e}")
            raise

    def upsert_chunks(self, chunks: List[Dict[str, Any]], batch_size: int = 50) -> int:
        """
        将 chunks 写入 Qdrant
        - 先获取 embedding
        - 再批量写入
        返回写入的 chunk 数量
        """
        if not chunks:
            logger.warning("[Upsert] 无 chunk 需要写入")
            return 0

        total_inserted = 0

        # 分批处理（避免 API 超时或内存溢出）
        for i in range(0, len(chunks), batch_size):
            batch = chunks[i:i + batch_size]
            texts = [c["text"] for c in batch]

            # 获取 embedding
            embeddings = self.embedding_client.embed(texts)
            if len(embeddings) != len(batch):
                raise RuntimeError("Embedding response count does not match chunk count")

            # 构建 PointStruct（双通道：dense 向量 + bm25 稀疏向量）
            points = []
            for chunk, vector in zip(batch, embeddings):
                point_id = self._generate_id(chunk["text"], chunk["metadata"])
                points.append(
                    PointStruct(
                        id=point_id,
                        vector={
                            "dense": vector,
                            "bm25": Document(
                                text=tokenize_for_bm25(chunk["text"]),
                                model="qdrant/bm25",
                            ),
                        },
                        payload={
                            "text": chunk["text"],
                            **chunk["metadata"],  # 展开所有 metadata
                        },
                    )
                )

            # 写入 Qdrant
            self.client.upsert(
                collection_name=self.collection_name,
                points=points,
                wait=True,
            )

            total_inserted += len(points)
            logger.info(f"[Upsert] 批次 {i // batch_size + 1}: 写入 {len(points)} 个 chunk")

        logger.info(f"[Upsert] 总计写入 {total_inserted} 个 chunk")
        return total_inserted

    def search(self, query: str, top_k: Optional[int] = None, filter_dict: Optional[Dict] = None,
               with_stats: bool = False):
        """
        检索（默认混合检索 + 可选精排）
        - retrieval_mode=hybrid: dense + BM25 双通道召回，RRF 融合（推荐）
        - retrieval_mode=vector: 仅 dense 向量检索（旧行为，做对照用）
        - rerank_enabled=true: 先召回 rerank_candidates 条候选，再交叉编码器精排取 top_k
        返回: List[{"text": str, "score": float, "score_kind": str, "metadata": dict}]
        with_stats=True 时额外返回统计: (results, {"dropped": int, "score_kind": str})，
        dropped 为本次被低相关过滤掉的条数（供前端"已隐藏 N 条"徽标）。默认 False 保持旧行为。
        """
        if top_k is None:
            top_k = settings.top_k

        # 构建过滤条件
        version_conditions = [IsEmptyCondition(is_empty=PayloadField(key="document_version_id"))]
        active_ids = current_revision_ids()
        if active_ids:
            version_conditions.append(FieldCondition(key="document_version_id", match=MatchAny(any=active_ids)))
        query_filter = Filter(must=[Filter(should=version_conditions)])
        if filter_dict:
            conditions = []
            for key, value in filter_dict.items():
                conditions.append(
                    FieldCondition(key=key, match=MatchValue(value=value))
                )
            if conditions:
                query_filter.must.extend(conditions)

        # Resolve publication before sending the question to the embedding provider.
        query_vector = self.embedding_client.embed_single(query)

        # 候选召回条数：开启精排时多召回候选供重排，否则直接取 top_k
        limit = max(top_k, settings.rerank_candidates) if self.rerank_client else top_k

        if settings.retrieval_mode == "hybrid":
            results = self._hybrid_query(query, query_vector, limit, query_filter)
        else:
            results = self.client.query_points(
                collection_name=self.collection_name,
                query=query_vector,
                using="dense",
                limit=limit,
                query_filter=query_filter,
                with_payload=True,
            )

        points = results.points

        def _ret(res, dropped, kind):
            # 统一出口：with_stats=True 时附带统计，否则只返回列表（旧行为）
            return (res, {"dropped": dropped, "score_kind": kind}) if with_stats else res

        # Rerank 精排：候选按 (query, doc) 相关性重排后取 top_k；API 异常自动降级为原始排序
        if self.rerank_client and len(points) > 1:
            reranked = self._rerank_points(query, points, top_k)
            if reranked is not None:
                formatted = [
                    {
                        "text": p.payload.get("text", ""),
                        "score": score,
                        "score_kind": "rerank",
                        "metadata": {k: v for k, v in p.payload.items() if k != "text"},
                    }
                    for p, score in reranked
                ]
                # 低相关过滤：rerank 分为绝对相关量纲，可安全施加阈值
                formatted, dropped = _filter_low_relevance(formatted, "rerank")
                extra = f"，已过滤低相关 {dropped} 条" if dropped else ""
                logger.info(f"[Search] ({settings.retrieval_mode}+rerank) 查询 \"{query[:30]}...\" 返回 {len(formatted)} 条结果{extra}")
                return _ret(formatted, dropped, "rerank")

        # 未开启精排 / 精排降级：按召回原始顺序取前 top_k 条格式化
        # 此处 score 为 RRF 融合分或原始向量分，量纲不可跨 query 比较，只截断不施加阈值
        raw_kind = "rrf" if settings.retrieval_mode == "hybrid" else "vector"
        formatted = []
        for r in points[:top_k]:
            formatted.append({
                "text": r.payload.get("text", ""),
                "score": r.score,
                "score_kind": raw_kind,
                "metadata": {k: v for k, v in r.payload.items() if k != "text"},
            })
        formatted, dropped = _filter_low_relevance(formatted, raw_kind)  # 非 rerank 量纲：原样返回，dropped=0
        logger.info(f"[Search] ({settings.retrieval_mode}) 查询 \"{query[:30]}...\" 返回 {len(formatted)} 条结果")
        return _ret(formatted, dropped, raw_kind)

    def _hybrid_query(self, query: str, query_vector: List[float], top_k: int,
                      query_filter: Optional[Filter]):
        """
        混合检索核心：dense 与 BM25 各取 prefetch_k 候选，RRF 排名融合后取 top_k。
        - filter 同时挂在各通道与顶层，保证子查询各自受限且结果不含被过滤项
        - BM25 通道查询文本经 tokenize_for_bm25 切分（与入库对称）
        """
        return self.client.query_points(
            collection_name=self.collection_name,
            prefetch=[
                Prefetch(
                    query=query_vector,
                    using="dense",
                    limit=settings.prefetch_k,
                    filter=query_filter,
                ),
                Prefetch(
                    query=Document(text=tokenize_for_bm25(query), model="qdrant/bm25"),
                    using="bm25",
                    limit=settings.prefetch_k,
                    filter=query_filter,
                ),
            ],
            query=FusionQuery(fusion=Fusion.RRF),
            limit=top_k,
            query_filter=query_filter,
            with_payload=True,
        )

    def _rerank_points(self, query: str, points: List[Any], top_k: int):
        """
        精排候选：调用 Rerank API 按相关性重排，返回 [(point, relevance_score), ...]。
        API 异常时返回 None，由 search 降级为原始排序（保证检索可用性）
        """
        documents = [p.payload.get("text", "") for p in points]
        try:
            ranked = self.rerank_client.rerank(query, documents, top_n=top_k)
        except Exception as e:
            logger.warning(f"[Rerank] 精排失败，降级为原始排序: {e}")
            return None
        return [(points[item["index"]], item["relevance_score"]) for item in ranked]

    def count(self) -> int:
        """获取 collection 中的向量数量"""
        return self.client.count(collection_name=self.collection_name).count

    def _generate_id(self, text: str, metadata: Dict[str, Any]) -> str:
        """
        生成唯一 point ID
        基于 text + source 的 hash，确保同一内容重复写入时覆盖旧数据
        """
        source = metadata.get("source", "")
        if metadata.get("document_version_id") is not None:
            unique_str = f"{metadata['document_version_id']}:{metadata['version_chunk_index']}:{text}"
            return hashlib.md5(unique_str.encode("utf-8")).hexdigest()
        unique_str = f"{source}:{text[:200]}"  # 取前200字符避免过长
        return hashlib.md5(unique_str.encode("utf-8")).hexdigest()


# ==================== 便捷函数 ====================

def get_vector_store() -> VectorStore:
    """获取 VectorStore 实例（单例模式）"""
    return VectorStore()
