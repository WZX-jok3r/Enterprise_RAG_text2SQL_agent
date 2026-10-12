"""问答 / 会话相关的请求与响应模型（对齐前端 types.ts）。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class SourceItem(BaseModel):
    source: str
    format: str
    score: float
    text_preview: Optional[str] = None
    document_version_id: Optional[str] = None
    document_version: Optional[int] = None


class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1, description="用户问题")
    top_k: Optional[int] = Field(None, ge=1, le=20, description="召回数量，默认取配置 TOP_K")
    filter_dict: Optional[Dict[str, Any]] = Field(None, description="可选的元数据过滤条件")


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="本轮用户消息")
    session_id: Optional[str] = Field(None, description="会话 ID；留空则自动新建会话")
    top_k: Optional[int] = Field(None, ge=1, le=20, description="召回数量，默认取配置 TOP_K")


class QueryResponse(BaseModel):
    query: str
    answer: str
    sources: List[SourceItem]
    retrieved_count: int
    hidden_count: int = Field(0, description="本次被低相关过滤掉、未展示的条数")


class ChatResponse(BaseModel):
    session_id: str
    answer: str
    sources: List[SourceItem]
    retrieved_count: int
    history_length: int = Field(..., description="该会话当前保存的历史消息条数")
    hidden_count: int = Field(0, description="本次被低相关过滤掉、未展示的条数")


class SessionCreated(BaseModel):
    session_id: str
