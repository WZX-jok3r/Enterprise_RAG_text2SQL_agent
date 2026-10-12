"""
embedding.py - 模型网关（SiliconFlow Embedding + Rerank）

从 src/vector_store.py 拆出，职责单一：
- EmbeddingClient: 调 /embeddings 批量取稠密向量（BAAI/bge-m3）
- RerankClient:    调 /rerank 对候选做交叉编码器精排

两者共用 SiliconFlow 的 Key / Base URL，均带 Langfuse 埋点（obs.span）。
"""

import logging
from typing import Any, Dict, List

import requests

from app.core.config import settings
from app.core.retry import call_with_retry
from app.core.performance import measured_post
from app.observability import langfuse as obs

logger = logging.getLogger(__name__)


class EmbeddingClient:
    """SiliconFlow Embedding API 客户端"""

    def __init__(self):
        self.api_key = settings.siliconflow_api_key
        self.base_url = settings.siliconflow_base_url.rstrip("/")
        self.model = settings.embedding_model

        if not self.api_key:
            raise ValueError("SILICONFLOW_API_KEY 未配置，请在 .env 中设置")

    def embed(self, texts: List[str]) -> List[List[float]]:
        """
        批量获取文本的 embedding 向量
        SiliconFlow embedding API 支持批量，但建议单次不超过 100 条
        """
        if not texts:
            return []

        url = f"{self.base_url}/embeddings"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        # SiliconFlow embedding API 格式
        payload = {
            "model": self.model,
            "input": texts,
            "encoding_format": "float",
        }

        with obs.span("embedding", input={"count": len(texts), "model": self.model}) as sp:
            try:
                # 带重试：实测上游网关偶发 504，而无重试会让一次抖动
                # 直接把整个问答打成失败（见 docs/经验教训.md L-013）
                def _do_post():
                    r = measured_post("embedding", self.model, url, headers=headers, json=payload, timeout=60)
                    r.raise_for_status()
                    return r

                response = call_with_retry(_do_post, attempts=3, label="embedding")
                data = response.json()

                # 解析返回结果
                embeddings = []
                for item in data["data"]:
                    embeddings.append(item["embedding"])

                usage = obs.extract_usage(data)
                sp.update(output={"count": len(embeddings)},
                          metadata={"dim": len(embeddings[0]) if embeddings else 0, "usage": usage})
                logger.info(f"[Embedding] 成功获取 {len(embeddings)} 个向量，维度={len(embeddings[0]) if embeddings else 0}")
                return embeddings

            except requests.exceptions.RequestException as e:
                logger.error(f"[Embedding] API 请求失败: {e}")
                sp.update(level="ERROR", status_message=str(e))
                raise
            except (KeyError, IndexError) as e:
                logger.error(f"[Embedding] 响应解析失败: {e}, 响应内容: {response.text[:500]}")
                sp.update(level="ERROR", status_message=str(e))
                raise

    def embed_single(self, text: str) -> List[float]:
        """获取单条文本的 embedding"""
        results = self.embed([text])
        return results[0] if results else []


class RerankClient:
    """SiliconFlow Rerank API 客户端（交叉编码器精排，与 Embedding 共用 Key/Base URL）"""

    def __init__(self):
        self.api_key = settings.siliconflow_api_key
        self.base_url = settings.siliconflow_base_url.rstrip("/")
        self.model = settings.rerank_model

        if not self.api_key:
            raise ValueError("SILICONFLOW_API_KEY 未配置，请在 .env 中设置")

    def rerank(self, query: str, documents: List[str], top_n: int) -> List[Dict[str, Any]]:
        """
        对候选文档按 (query, document) 相关性精排
        返回: [{"index": 原文在 documents 中的下标, "relevance_score": float}]（按相关度降序）
        异常抛给调用方做降级处理
        """
        if not documents:
            return []

        url = f"{self.base_url}/rerank"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "query": query,
            "documents": documents,
            "top_n": top_n,
            "return_documents": False,
        }

        with obs.span("rerank", input={"query": query, "candidates": len(documents), "model": self.model}) as sp:
            # 带重试：rerank 失败虽会降级为原始排序（不阻断），
            # 但降级会损失精排质量，因此也值得重试几次。
            def _do_post():
                r = measured_post("rerank", self.model, url, headers=headers, json=payload, timeout=30)
                r.raise_for_status()
                return r

            response = call_with_retry(_do_post, attempts=2, label="rerank")
            data = response.json()

            ranked = [
                {"index": item["index"], "relevance_score": item["relevance_score"]}
                for item in data["results"]
            ]
            sp.update(output={"returned": len(ranked)})
            logger.info(f"[Rerank] 候选 {len(documents)} 条精排完成，返回前 {len(ranked)} 条")
            return ranked
