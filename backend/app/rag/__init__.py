"""RAG 领域：检索 -> 上下文 -> 生成 -> 来源。

三个出口，职责互不重叠：
    `executor`  检索与 LLM 调用的**唯一**入口（agent / oneshot 共用）
    `oneshot`   单轮"检索 + 生成"的编排（POST /api/v1/query）
    `agent`    见 app/agent（多轮、意图路由、工具循环）—— 不在本包内

Step 3 起 `pipeline.py`（RAGPipeline）已删除：多轮问答由 `/agent/stream` 承担，
单轮由 `oneshot.answer_once` 承担，两者共用 `executor`。

⚠️ 这里**刻意不 import `app.rag.retriever` / `oneshot`**：
它们会连带 `app.vector.qdrant`（qdrant_client 是重依赖）。而 `agent.loop`
只需要 `executor`，不应为了一个 `stream_text` 在 import 阶段就把向量库客户端拉起来。
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
