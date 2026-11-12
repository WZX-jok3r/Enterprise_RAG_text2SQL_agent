"""events.py - Agent 的 SSE 事件契约（**扩帧不改帧**，向后兼容）。

⚠️ 本项目最不能破坏的契约之一（见改造方案 1.7 契约清单）：
    前端 `stores/chat.ts` 按 `type` 分支处理事件，其中
        meta   -> 渲染来源列表 + 「已隐藏 N 条」徽标
        delta  -> 逐字追加
        done   -> 定稿并写回 session_id
        error  -> 显示错误
    这四类事件的**字段集合与语义必须完全不变**，否则前端与评测同时崩。

扩帧策略（关键）：
    - 原 4 类事件**原样保留**，一个字段都不改；
    - 新增 6 类（route / tool_call / tool_result / sql / clarify / degraded）；
    - 前端对**未知 type 走 default 忽略分支** -> **旧前端 + 新后端不会崩**。
    验收方式：改造前后用同一问题 diff SSE 原始帧序列，`meta/delta/done` 必须逐字节一致。

⚠️ 降级路径必须补发标准 meta 帧（见 5.5.3）：
    若 Agent 降级到 RAG 时只是"调了 kb_search 然后生成答案"，前端拿不到 meta 帧，
    来源列表与「已隐藏 N 条」徽标会**永久空着** —— 用户会以为功能退化了。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# ==================== 既有事件（字段冻结，禁止改动）====================

def ev_meta(sources: List[Dict[str, Any]], retrieved_count: int, hidden_count: int) -> Dict[str, Any]:
    """既有事件：检索完成元信息。字段与改造前完全一致。"""
    return {
        "type": "meta",
        "sources": sources,
        "retrieved_count": retrieved_count,
        "hidden_count": hidden_count,
    }


def ev_delta(text: str) -> Dict[str, Any]:
    return {"type": "delta", "text": text}


def ev_done(answer: str, **extra: Any) -> Dict[str, Any]:
    ev: Dict[str, Any] = {"type": "done", "answer": answer}
    ev.update(extra)
    return ev


def ev_error(message: str) -> Dict[str, Any]:
    return {"type": "error", "message": message}


# ==================== 新增事件（Agent 轨迹）====================

def ev_route(target: str, reason: str, **extra: Any) -> Dict[str, Any]:
    """路由决策。target: rag | sql | multi | clarify"""
    ev = {"type": "route", "target": target, "reason": reason}
    ev.update(extra)
    return ev


def ev_tool_call(call_id: str, name: str, args: Dict[str, Any], purpose: str = "") -> Dict[str, Any]:
    """工具调用开始（前端显示"正在查询数据库…"）。"""
    return {"type": "tool_call", "id": call_id, "name": name, "args": args, "purpose": purpose}


def ev_tool_result(call_id: str, ok: bool, summary: str, **extra: Any) -> Dict[str, Any]:
    ev = {"type": "tool_result", "id": call_id, "ok": ok, "summary": summary}
    ev.update(extra)
    return ev


def ev_sql(sql: str, row_count: int, elapsed_ms: int, retries: int = 0,
           truncated: bool = False) -> Dict[str, Any]:
    """展示实际执行的 SQL 与结果规模 —— **可解释性的核心**。

    truncated 必须透传给前端：结果被截断时用户必须知道，否则会把
    "前 N 行"当成全部数据（这正是本项目要消灭的静默错误）。
    """
    return {
        "type": "sql",
        "sql": sql,
        "row_count": row_count,
        "elapsed_ms": elapsed_ms,
        "retries": retries,
        "truncated": truncated,
    }


def ev_clarify(question: str) -> Dict[str, Any]:
    """口径澄清：需要用户补充说明后才继续。"""
    return {"type": "clarify", "question": question}


def ev_degraded(reason: str, **extra: Any) -> Dict[str, Any]:
    """降级到纯 RAG（超过步数/超时/工具失败）。"""
    ev = {"type": "degraded", "reason": reason}
    ev.update(extra)
    return ev


def ev_rewrite(original: str, rewritten: str, reason: str) -> Dict[str, Any]:
    """追问改写（仅强制 SQL / RAG 路径，见 agent/rewrite.py）。

    为什么要发这个事件：改写会**改变实际检索词**，用户看到"那 Engineering 呢"
    却拿到 Engineering 的结果是正常的，但排查时必须能还原"到底拿什么去查的"。
    只在改写真正生效时发送 —— 跳过/失败属于内部决策，不该刷屏。
    """
    return {"type": "rewrite", "original": original, "rewritten": rewritten,
            "reason": reason}


# ==================== 契约自检 ====================

# 既有事件的必需字段（改动即破坏前端，测试会断言）
FROZEN_META_FIELDS = {"type", "sources", "retrieved_count", "hidden_count"}
FROZEN_DELTA_FIELDS = {"type", "text"}
FROZEN_DONE_FIELDS = {"type", "answer"}
FROZEN_ERROR_FIELDS = {"type", "message"}

# 新增事件类型（前端可选择性渲染）
AGENT_EVENT_TYPES = {"route", "tool_call", "tool_result", "sql", "clarify", "degraded",
                     "rewrite"}
