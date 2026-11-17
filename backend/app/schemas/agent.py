"""Agent 相关的请求/响应模型（对齐前端 types.ts）。"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class AgentChatRequest(BaseModel):
    """Agent 对话请求。字段与 ChatRequest 保持一致，便于前端复用调用逻辑。"""

    message: str = Field(..., min_length=1, description="本轮用户消息")
    session_id: Optional[str] = Field(None, description="会话 ID；留空则自动新建会话")
    # 调试用：强制走某条路由，跳过规则与 LLM 决策。
    # 生产前端不传；它主要服务于测试与"为什么走了这条路"的排查。
    # 新历史值（doc/data/hybrid）与旧值（rag/sql）等价：
    # Route 的 _missing_ 会把旧值映射到新枚举，避免调用方改代码。
    force_route: Optional[str] = Field(
        None,
        description="强制路由（doc|data|hybrid|chitchat|meta；rag/sql 为兼容旧值）",
        pattern="^(doc|data|hybrid|chitchat|meta|rag|sql)$",
    )


class AgentRouteInfo(BaseModel):
    """规则路由的判定结果（/agent/route 的响应，用于调试）。"""

    route: str = Field(..., description="chitchat | meta | doc | data | hybrid")
    reason: str
    matched_aggregation: list[str] = Field(default_factory=list)
    matched_column: list[str] = Field(default_factory=list)
    matched_doc_word: list[str] = Field(default_factory=list)
