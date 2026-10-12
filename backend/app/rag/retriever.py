"""
retriever.py - 检索读路径

薄封装 VectorStore.search：把 (results, stats) 收敛成 (chunks, dropped)，
供 pipeline 使用。检索的混合召回 / rerank / 低相关过滤全部在 vector 层完成，
本模块不重复实现（忠实对应 src/rag_pipeline.py 的 _retrieve）。
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
from app.core.performance import timed_stage
from app.vector.qdrant import VectorStore, get_vector_store

logger = logging.getLogger(__name__)


class Retriever:
    """从知识库召回相关 chunk（含低相关过滤统计）。"""

    def __init__(self, vector_store: Optional[VectorStore] = None):
        self.vector_store = vector_store or get_vector_store()

    @timed_stage("retrieval")
    def search(self, query: str, top_k: Optional[int] = None,
               filter_dict: Optional[Dict[str, Any]] = None) -> Tuple[List[Dict[str, Any]], int]:
        """检索相关 chunk。返回 (chunks, dropped)，dropped 为被低相关过滤掉的条数。"""
        if top_k is None:
            top_k = settings.top_k
        logger.info(f"[Retrieve] 查询: {query}")
        results, stats = self.vector_store.search(
            query, top_k=top_k, filter_dict=filter_dict, with_stats=True)
        dropped = stats.get("dropped", 0)
        logger.info(f"[Retrieve] 召回 {len(results)} 条结果" + (f"（已过滤低相关 {dropped} 条）" if dropped else ""))
        return results, dropped
