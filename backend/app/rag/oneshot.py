"""oneshot.py - 单轮问答（一次检索 + 一次生成）。

`POST /api/v1/query` 的唯一实现：无会话、无历史、无工具。

## 为什么它单独存在（而不是随 RAGPipeline 一起删掉）

`RAGPipeline.query()` 同时服务着两件事：单轮 `/query` 与多轮 `/chat`。
Step 3 下线 `/chat/*` 时，`/query` 仍是**对外可用**的接口，
所以把它依赖的那一小段编排原样抽出来 —— 为了删一个类而删掉一个接口，不是重构。

## 与 Agent 的关系（刻意保留的差异，不要再"统一"）

    | | `/query`（本模块） | `/agent/stream` |
    |---|---|---|
    | 上下文格式 | `prompts/rag.SYSTEM_PROMPT` 的【参考N】 | 工具观察 `[i]（来源：…）` |
    | 历史 | 无（单轮） | 有（sessions 表） |
    | 空召回 | 硬拒答 | 交模型按上下文说明 |
    | 轨迹 | 无 | route/tool_call/meta/delta/done |

检索与 LLM 出口都在 `app/rag/executor`（`retrieve` / `complete_text`），
所以两条链路的**检索口径不可能漂移** —— 差异只剩上面这些有意为之的编排差异。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.core.config import settings
from app.llm.provider import get_llm_client
from app.observability import langfuse as obs
from app.prompts.rag import SYSTEM_PROMPT
from app.rag.context_builder import build_context
from app.rag.executor import REFUSE_NO_CONTEXT, complete_text, retrieve


def answer_once(
    query: str,
    *,
    top_k: Optional[int] = None,
    filter_dict: Optional[Dict[str, Any]] = None,
    llm: Optional[Any] = None,
    retriever: Optional[Any] = None,
) -> Dict[str, Any]:
    """检索一次并生成一个答案。

    Args:
        filter_dict: **必须**先经 `security.apply_tenant_acl` 处理（调用方负责，
            与 `executor.retrieve` 的约定一致）。
        llm / retriever: 可注入（测试注入假实现；None 用进程级单例）。

    Returns:
        {"query", "answer", "sources", "retrieved_count", "hidden_count"}
        —— 字段与旧 `RAGPipeline.query()` 完全一致，`QueryResponse` 直接展开。
    """
    k = int(top_k) if top_k else settings.top_k
    with obs.trace("rag_query", input={"query": query, "top_k": k},
                   metadata={"filter": filter_dict}) as tr:
        with obs.span("retrieve", input=query) as sp:
            ctx = retrieve(query, retriever=retriever, top_k=k,
                           filter_dict=filter_dict, with_preview=True)
            sp.update(output={"retrieved_count": ctx.retrieved_count,
                              "hidden_count": ctx.dropped})

        # 空召回：确定性地拒答，不调用模型（省一次调用，也避免模型编造）
        if not ctx.has_context:
            result = {
                "query": query,
                "answer": REFUSE_NO_CONTEXT,
                "sources": [],
                "retrieved_count": 0,
                "hidden_count": ctx.dropped,
            }
            tr.update(output=result["answer"], metadata={"retrieved_count": 0})
            return result

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT.format(context=build_context(ctx.chunks))},
            {"role": "user", "content": query},
        ]
        answer, _usage = complete_text(messages, llm=llm or get_llm_client())

        result = {
            "query": query,
            "answer": answer,
            "sources": ctx.sources,
            "retrieved_count": ctx.retrieved_count,
            "hidden_count": ctx.dropped,
        }
        tr.update(output=answer, metadata={"retrieved_count": ctx.retrieved_count,
                                           "sources": [s["source"] for s in ctx.sources]})
        return result
