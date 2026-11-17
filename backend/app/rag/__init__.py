"""RAG 领域：检索 -> 上下文 -> 生成 -> 来源，及流式问答编排。

`executor` 是检索与 LLM 调用的唯一出口（pipeline 与 agent 共用）；
`pipeline` 只是其中一种编排（三步固定流程 + 硬拒答）。
"""

from app.rag.executor import (
    REFUSE_NO_CONTEXT,
    AnswerStreamError,
    RagContext,
    complete_text,
    history_messages,
    retrieve,
    stream_text,
)
from app.rag.pipeline import RAGPipeline, get_pipeline

__all__ = [
    "AnswerStreamError",
    "RAGPipeline",
    "REFUSE_NO_CONTEXT",
    "RagContext",
    "complete_text",
    "get_pipeline",
    "history_messages",
    "retrieve",
    "stream_text",
]
