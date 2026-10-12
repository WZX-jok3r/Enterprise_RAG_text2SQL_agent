"""文档入库相关响应模型。"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class UploadResponse(BaseModel):
    task_id: str
    filename: str
    status: str = Field("queued", description="任务初始状态")
    message: str = "文件已接收，正在后台入库"


class TaskStatusResponse(BaseModel):
    task_id: str
    document_id: Optional[int] = None
    status: str
    progress: int
    error: Optional[str] = None
    document_key: Optional[str] = None
    version: Optional[int] = None
    original_filename: Optional[str] = None
    is_current: bool = False


class DocumentItem(BaseModel):
    id: int
    source: str
    format: str
    status: str
    chunk_count: int
    document_key: Optional[str] = None
    version: Optional[int] = None
    original_filename: Optional[str] = None
    is_current: bool = False


class DocumentListResponse(BaseModel):
    documents: List[DocumentItem]
    total: int


class DeleteDocumentResponse(BaseModel):
    deleted_id: int
    source: str
    vectors_cleared: bool = Field(..., description="是否已向 Qdrant 提交该文件的向量删除")
    file_removed: bool = Field(..., description="是否删除了知识库中的物理副本")
    message: str = "文档已删除（向量、物理副本与登记记录均已清理）"
