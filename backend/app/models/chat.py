"""会话与消息模型（多轮对话持久化，替代 src/api.py 的内存 SessionStore）。

- ChatSession.id 沿用前端的 session_id（uuid4 hex，32 字符），保证前端零改动。
- tenant_id 冗余存储调用方租户标识；鉴权关闭时为 None。ACL 隔离即按此字段判定。
- Message 是历史的事实来源（source of truth）；Redis 仅作热缓存，丢失可从 PG 重建。
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class ChatSession(Base, TimestampMixin):
    """一次多轮会话。"""

    __tablename__ = "sessions"

    # uuid4().hex，与前端 session_id 同形
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    # 最近活跃时间：用于 TTL 惰性回收与“会话跨重启保留”验证
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    messages: Mapped[List["Message"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="Message.seq",
        lazy="selectin",
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ChatSession id={self.id!r} tenant={self.tenant_id!r}>"


class Message(Base):
    """会话中的一条消息（仅存干净的 role/content，不含参考资料）。"""

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("session_id", "seq", name="uq_messages_session_id_seq"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)      # user | assistant
    content: Mapped[str] = mapped_column(Text, nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)          # 会话内单调序号，稳定排序
    # 该轮 Agent 的**工具轨迹**（紧凑文本：执行过的 SQL + 结果行）。
    #
    # ## 为什么需要它（实测的多轮缺陷，见 docs/经验教训.md L-022）
    #
    # 原先历史只存 `role + content`（问题 + 自然语言答案），
    # 追问时模型**看不到上一轮执行过什么查询、拿到过哪些行**，只能重新推导。
    # 实测（真实 LLM）：
    #   第 1 轮「Sales 部门有多少人？」           -> 1042
    #   第 2 轮「那 Engineering 呢？两个部门合起来一共多少人？」
    #          -> 只答出 Engineering=1010，**合计算不出来**（自称"资料未找到"）
    # 而这两步所需的全部信息（1042 + 1010）本该唾手可得。
    #
    # 刻意**不拼进 content**：content 是给用户看的历史，
    # 混入 SQL 会污染对话显示与前端渲染。用独立列，只喂给模型。
    tool_context: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    session: Mapped["ChatSession"] = relationship(back_populates="messages")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Message session={self.session_id!r} seq={self.seq} role={self.role!r}>"
