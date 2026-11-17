"""RAG 领域：检索 -> 上下文 -> 生成 -> 来源，及流式问答编排。

`executor` 是检索与 LLM 调用的唯一出口（pipeline 与 agent 共用）；
`pipeline` 只是其中一种编排（三步固定流程 + 硬拒答）。

⚠️ 这里**刻意不 import `app.rag.pipeline`**：它会连带 `app.rag.retriever`
-> `app.vector.qdrant`（qdrant_client 是重依赖）。而 `agent.loop` 只需要
`executor`，不应为了一个 `stream_text` 在 import 阶段就把向量库客户端拉起来。
需要 RAGPipeline 的地方一律 `from app.rag.pipeline import ...`（既有调用方都是这样）。
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

__all__ = [
    "AnswerStreamError",
    "REFUSE_NO_CONTEXT",
    "RagContext",
    "complete_text",
    "history_messages",
    "retrieve",
    "stream_text",
]
