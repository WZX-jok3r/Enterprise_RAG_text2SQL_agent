"""
pipeline.py - RAG 完整链路编排

忠实迁移自 src/rag_pipeline.py 的 RAGPipeline，保留三条对外链路的完整行为：
- query():               检索 -> (可选)生成 -> sources(with_preview=True)；支持 generate=False 仅检索（评测快速模式）
- query_with_history():  多轮问答，无召回时用 "无相关参考资料" 兜底 context（不拒答）
- stream_chat():         流式多轮，依次 yield meta -> delta* -> done/error

编排职责-only：检索交给 Retriever（底层 vector 完成混合召回/rerank/低分过滤），
上下文/来源交给 context_builder / source_formatter，LLM 调用交给 llm.LLMClient，
埋点交给 observability。结果字典的键（query/answer/sources/retrieved_count/hidden_count）
与流式事件类型（meta/delta/done/error）保持与 src 完全一致，确保前端与评测无感。
"""

import logging
from typing import Any, Dict, Iterator, List, Optional

import requests

from app.core.config import settings
from app.llm.provider import LLMClient, get_llm_client
from app.observability import langfuse as obs
from app.rag.context_builder import build_context
from app.prompts.rag import SYSTEM_PROMPT
from app.rag.retriever import Retriever
from app.rag.source_formatter import format_sources

logger = logging.getLogger(__name__)

_REFUSE_NO_CONTEXT = "根据现有资料，未能找到相关信息。"


class RAGPipeline:
    """RAG 问答管道。"""

    SYSTEM_PROMPT = SYSTEM_PROMPT

    def __init__(self, retriever: Optional[Retriever] = None,
                 llm: Optional[LLMClient] = None):
        self.retriever = retriever or Retriever()
        self.llm = llm or get_llm_client()
        self.model = self.llm.model

    def _retrieve(self, query: str, top_k: Optional[int] = None,
                  filter_dict: Optional[Dict] = None):
        """检索相关 chunk。返回 (chunks, dropped)。"""
        return self.retriever.search(query, top_k=top_k, filter_dict=filter_dict)

    def query(self, query: str, top_k: Optional[int] = None, filter_dict: Optional[Dict] = None,
              generate: bool = True) -> Dict[str, Any]:
        """
        执行完整 RAG 查询。

        Args:
            generate: True=检索+LLM 生成；False=仅检索（评测快速模式，跳过 LLM 调用）

        Returns:
            {"query", "answer", "sources", "retrieved_count", "hidden_count"}
        """
        if top_k is None:
            top_k = settings.top_k
        # 1. 检索
        with obs.trace("rag_query", input={"query": query, "top_k": top_k, "generate": generate},
                        metadata={"filter": filter_dict}) as tr:
            with obs.span("retrieve", input=query) as sp:
                chunks, dropped = self._retrieve(query, top_k=top_k, filter_dict=filter_dict)
                sp.update(output={"retrieved_count": len(chunks), "hidden_count": dropped})
            if not chunks:
                result = {
                    "query": query,
                    "answer": _REFUSE_NO_CONTEXT,
                    "sources": [],
                    "retrieved_count": 0,
                    "hidden_count": dropped,
                }
                tr.update(output=result["answer"], metadata={"retrieved_count": 0})
                return result

            # 2~4. 组装 context + 调用 LLM（retrieval-only 模式跳过生成环节）
            if generate:
                context = build_context(chunks)
                system_prompt = self.SYSTEM_PROMPT.format(context=context)
                logger.info(f"[LLM] 调用 {self.model} 生成回答...")
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": query},
                ]
                answer, _usage = self.llm.complete(messages)
            else:
                answer = "(retrieval-only：已跳过 LLM 生成)"

            # 5. 格式化 sources
            sources = format_sources(chunks, with_preview=True)

            result = {
                "query": query,
                "answer": answer,
                "sources": sources,
                "retrieved_count": len(chunks),
                "hidden_count": dropped,
            }
            tr.update(output=answer, metadata={"retrieved_count": len(chunks),
                                                "sources": [s["source"] for s in sources]})
            return result

    def query_with_history(self, query: str, history: List[Dict[str, str]],
                           top_k: Optional[int] = None, filter_dict: Optional[Dict] = None) -> Dict[str, Any]:
        """
        带历史对话的 RAG 查询。
        history: [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}]
        filter_dict: 服务端强制的元数据过滤（如多租户 tenant_id 隔离）
        """
        if top_k is None:
            top_k = settings.top_k
        # 1. 检索（基于当前 query，叠加服务端强制过滤）
        with obs.trace("rag_chat", input={"query": query, "top_k": top_k, "history_len": len(history)},
                        metadata={"filter": filter_dict}) as tr:
            with obs.span("retrieve", input=query) as sp:
                chunks, dropped = self._retrieve(query, top_k=top_k, filter_dict=filter_dict)
                sp.update(output={"retrieved_count": len(chunks), "hidden_count": dropped})
            context = build_context(chunks) if chunks else "无相关参考资料"

            # 2. 组装 messages
            messages = [{"role": "system", "content": self.SYSTEM_PROMPT.format(context=context)}]
            messages.extend(history)
            messages.append({"role": "user", "content": query})

            # 3. 调用 LLM（统一入口，内部带 generation 埋点与 usage 捕获）
            answer, _usage = self.llm.complete(messages)

            sources = format_sources(chunks, with_preview=False)

            result = {
                "query": query,
                "answer": answer,
                "sources": sources,
                "retrieved_count": len(chunks),
                "hidden_count": dropped,
            }
            tr.update(output=answer, metadata={"retrieved_count": len(chunks),
                                                "sources": [s["source"] for s in sources]})
            return result

    def stream_chat(self, query: str, history: List[Dict[str, str]],
                    top_k: Optional[int] = None, filter_dict: Optional[Dict] = None) -> Iterator[Dict[str, Any]]:
        """流式多轮问答：依次 yield meta -> delta(多个) -> done/error 事件字典。
        检索、低相关过滤与来源组装与非流式 query_with_history 一致，不改动。"""
        if top_k is None:
            top_k = settings.top_k
        with obs.trace("rag_chat_stream", input={"query": query, "top_k": top_k, "history_len": len(history)},
                        metadata={"filter": filter_dict}) as tr:
            with obs.span("retrieve", input=query) as sp:
                chunks, dropped = self._retrieve(query, top_k=top_k, filter_dict=filter_dict)
                sp.update(output={"retrieved_count": len(chunks), "hidden_count": dropped})

            sources = format_sources(chunks, with_preview=False)
            yield {"type": "meta", "sources": sources,
                   "retrieved_count": len(chunks), "hidden_count": dropped}

            if not chunks:
                refuse = _REFUSE_NO_CONTEXT
                yield {"type": "delta", "text": refuse}
                yield {"type": "done", "answer": refuse}
                tr.update(output=refuse, metadata={"retrieved_count": 0})
                return

            context = build_context(chunks)
            messages = [{"role": "system", "content": self.SYSTEM_PROMPT.format(context=context)}]
            messages.extend(history)
            messages.append({"role": "user", "content": query})

            parts: List[str] = []
            try:
                for delta in self.llm.stream(messages):
                    parts.append(delta)
                    yield {"type": "delta", "text": delta}
                answer = "".join(parts).strip()
                tr.update(output=answer, metadata={"retrieved_count": len(chunks),
                                                   "sources": [s["source"] for s in sources]})
                yield {"type": "done", "answer": answer}
            except requests.exceptions.RequestException as e:
                yield {"type": "error", "message": f"抱歉，调用模型时出错: {str(e)}"}


# ==================== 便捷函数 ====================

def get_pipeline() -> RAGPipeline:
    """获取 RAGPipeline 实例。"""
    return RAGPipeline()
