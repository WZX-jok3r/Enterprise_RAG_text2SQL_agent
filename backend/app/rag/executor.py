"""executor.py - RAG 执行器：检索上下文 + LLM 调用的**唯一出口**。

## 为什么要有这一层（Step 1 的动机）

同一件事此前写了两遍：
    - `rag/pipeline.py`  → `_retrieve()` + 内联 `llm.complete/stream`
    - `agent/tools.py`   → `kb_search()` 自己调 `retriever.search` + `format_sources`
    - `agent/loop.py`    → `_compose_answer()` 自己调 `chat_with_tools(tools=None)`

后果不是"代码丑"，而是**行为会各自漂移**：历史注入只写在一条路上（P0 缺陷）、
来源列表两套拼法、流式只在一条路上（agent 的最终答案被憋成一次性 delta）。

## 职责边界（刻意只做这四件事）

搬进来（真正重复的）：
    1. `retrieve()`          唯一的 `Retriever.search` 调用点，统一 sources 与 dropped
    2. `history_messages()`  历史 → messages 的唯一渲染（含上一轮工具轨迹注记）
    3. `stream_text()`       流式生成的唯一适配层（两种客户端契约 → 一种迭代）
    4. `complete_text()`     非流式生成的唯一适配层

**不搬**（有意保留差异，动它们会改变答案质量，属 Step 2 的 A/B 项）：
    - 上下文文本格式：pipeline 的 `【参考N】来源: …` 与 agent 的 `[i]（来源：…）`
    - prompt 文本（`prompts/rag.SYSTEM_PROMPT` / `prompts/agent.*`）
    - 空召回策略：pipeline 硬拒答 / agent 交给模型（用调用方分支表达，不在这里 if）
    - 编排：pipeline 的三步、agent 的三门与有界循环

## 依赖方向

`rag` **不得** import `agent`（否则两个领域互相咬住）。因此历史渲染放在这里，
由 agent 反向调用；测试注入的假 LLM/假 Retriever 走参数，不读全局单例。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple

from app.core.config import settings
from app.rag.source_formatter import format_sources

logger = logging.getLogger(__name__)

# 无召回时的确定性拒答文本。
# 放在这里而不是散落在 pipeline：它是"对外文案"，只能有一份。
REFUSE_NO_CONTEXT = "根据现有资料，未能找到相关信息。"

# 上一轮工具轨迹注记的固定措辞（进 prompt 的文本，改动会让 P0 的回归失效）。
TOOL_CONTEXT_NOTE = "[上一轮工具执行记录，供你延续上下文]"


@dataclass(frozen=True)
class RagContext:
    """一次检索的完整产物。

    为什么把 chunks / sources / dropped 收进一个值对象：
        它们此前以两种形状流动（pipeline 的 tuple、kb_search 的 payload dict），
        将来加"去重条数""召回耗时"时必然只改到一边。
    """

    query: str
    chunks: List[Dict[str, Any]] = field(default_factory=list)
    sources: List[Dict[str, Any]] = field(default_factory=list)
    dropped: int = 0

    @property
    def has_context(self) -> bool:
        """是否有可用上下文 —— 空召回判定的唯一出口。"""
        return bool(self.chunks)

    @property
    def retrieved_count(self) -> int:
        return len(self.chunks)


_retriever: Optional[Any] = None


def get_retriever() -> Any:
    """进程级 Retriever 单例（首次调用才构造，避免 import 时就连 Qdrant）。"""
    global _retriever
    if _retriever is None:
        from app.rag.retriever import Retriever

        _retriever = Retriever()
    return _retriever


def retrieve(
    query: str,
    *,
    retriever: Optional[Any] = None,
    top_k: Optional[int] = None,
    filter_dict: Optional[Dict[str, Any]] = None,
    with_preview: bool = False,
) -> RagContext:
    """检索并组装 RagContext。

    Args:
        retriever: 可注入（pipeline 持有自己的实例；测试注入假实现）。None 用单例。
        with_preview: 是否在 sources 里带正文预览（单轮 `query()` 需要，其余不需要）。

    ⚠️ `filter_dict` 必须由调用方**先**经 `security.apply_tenant_acl` 处理。
    这里不做租户判定 —— 那一层只有一个入口（见 core/security.py），
    在这里再写一遍就是再造一个可能漏传的地方。
    """
    r = retriever if retriever is not None else get_retriever()
    k = int(top_k) if top_k else settings.top_k
    chunks, dropped = r.search(query, top_k=k, filter_dict=filter_dict)
    return RagContext(
        query=query,
        chunks=chunks,
        sources=format_sources(chunks, with_preview=with_preview),
        dropped=dropped,
    )


def history_messages(
    history: Optional[List[Dict[str, Any]]],
    *,
    include_tool_context: bool = True,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """把会话历史渲染成 LLM messages。

    Args:
        include_tool_context: 是否把上一轮的工具轨迹拼进 assistant 消息。
            默认 True —— 这正是 P0 的修复：此前只有 agent 的 LLM 循环会注入，
            强制路由与经典 RAG 都看不到，于是"记不记得上文"取决于走了哪条路。
        limit: 只取最近 N 条；None 表示不截断（pipeline 的历史已由 SessionService 限长）。

    两个细节都不能改：
        1. 只输出 `{role, content}` —— `SessionService.get_history` 返回的
           `tool_context` 是**给模型看的**，不是 OpenAI messages 的合法字段，
           直接 extend 会把脏字段发给上游（此前 pipeline 就是这样）。
        2. 注记措辞而不是伪造 `role=tool`：伪造需要配对的 tool_call_id，
           而会话存储里没有完整的那一串。
    """
    items = list(history or [])
    if limit is not None and limit > 0:
        items = items[-limit:]

    out: List[Dict[str, Any]] = []
    for h in items:
        role = h.get("role", "user")
        content = h.get("content", "")
        if include_tool_context and role == "assistant" and h.get("tool_context"):
            content = f"{content}\n\n{TOOL_CONTEXT_NOTE}\n{h['tool_context']}"
        out.append({"role": role, "content": content})
    return out


class AnswerStreamError(RuntimeError):
    """流式生成失败。

    为什么把"错误"统一成异常：两种客户端表达失败的方式不同 ——
    `provider.LLMClient.stream` 抛 `requests.RequestException`，
    `tool_client.ToolLLMClient.stream_chat` 则 yield 一个 error 事件后结束。
    调用方要写"回退策略"就必须只有一种语义，否则两处回退逻辑会各写一遍。
    """


def stream_text(
    messages: List[Dict[str, Any]],
    *,
    llm: Any,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Iterator[str]:
    """流式生成，逐片产出文本。

    支持三种客户端契约（**优先流式，不能流就整段**）：
        - `LLMClient.stream(messages, ...)`          -> 直接迭代文本
        - `ToolLLMClient.stream_chat(messages, ...)` -> 事件字典，取 delta.text
        - 只有 `chat_with_tools(...)`                -> 一次性产出 `content`

    第三条不是"测试便利"，而是真实的降级路径：注入的客户端若不支持流式，
    行为必须仍然正确（宁可慢，不可没答案）。它也让 loop 的单测替身
    只需实现 `chat_with_tools` 一个方法 —— 与改造前完全一样的替身仍然可用。

    Args:
        extra: 仅透传给 `stream_chat` 的附加参数（记账归因：scene/actor 等）。

    ⚠️ 失败**必须抛** `AnswerStreamError`（或原样上抛 requests 异常）：
        "已产出多少片"决定调用方能否安全回退（见 AgentLoop._llm_pieces）。
    """
    kwargs = dict(extra or {})

    if hasattr(llm, "stream_chat"):
        for item in llm.stream_chat(
            messages, tools=None, temperature=temperature, max_tokens=max_tokens, **kwargs
        ):
            kind = item.get("type")
            if kind == "delta":
                text = item.get("text") or ""
                if text:
                    yield text
            elif kind == "error":
                raise AnswerStreamError(item.get("message") or "流式生成失败")
        return

    if hasattr(llm, "stream"):
        for piece in llm.stream(messages, temperature=temperature, max_tokens=max_tokens):
            if piece:
                yield piece
        return

    if not hasattr(llm, "chat_with_tools"):
        raise TypeError(f"LLM 客户端不支持生成文本: {type(llm).__name__}")
    # 只传非 None 的可选参数：真实客户端允许 None，但"只实现必需参数"的
    # 替身/适配器（既有单测里就有）会因多余的关键字参数直接 TypeError。
    # 省略与传 None 在语义上等价（都用客户端默认值），因此这样收窄是安全的。
    call_kwargs: Dict[str, Any] = {"tools": None}
    if temperature is not None:
        call_kwargs["temperature"] = temperature
    if max_tokens is not None:
        call_kwargs["max_tokens"] = max_tokens
    chat = llm.chat_with_tools(messages, **call_kwargs)
    content = getattr(chat, "content", "") or ""
    if content:
        yield content


def complete_text(
    messages: List[Dict[str, Any]],
    *,
    llm: Any,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> Tuple[str, Dict[str, Any]]:
    """非流式生成，返回 (答案, usage)。

    同样兼容两种客户端：
        - `LLMClient.complete(...)`            -> (answer, usage)
        - `ToolLLMClient.chat_with_tools(...)` -> ToolChatResult
    """
    if hasattr(llm, "complete"):
        return llm.complete(messages, temperature=temperature, max_tokens=max_tokens)

    chat = llm.chat_with_tools(
        messages, tools=None, temperature=temperature, max_tokens=max_tokens
    )
    return getattr(chat, "content", "") or "", getattr(chat, "usage", {}) or {}
