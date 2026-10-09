"""会话与消息仓储（含多租户 ACL 判定）。"""

from __future__ import annotations

from typing import List, Optional

from sqlalchemy import func, select

from app.models.chat import ChatSession, Message
from app.repositories.base import BaseRepository


class SessionRepository(BaseRepository):
    # ---------- 会话 ----------
    async def create(self, session_id: str, tenant_id: Optional[str]) -> ChatSession:
        sess = ChatSession(id=session_id, tenant_id=tenant_id)
        self._session.add(sess)
        await self.flush()
        return sess

    async def get(self, session_id: str) -> Optional[ChatSession]:
        return await self._session.get(ChatSession, session_id)

    async def exists(self, session_id: str, tenant_id: Optional[str]) -> bool:
        """ACL：会话必须属于同一租户（严格相等）。鉴权关闭时双方均为 None，正常匹配。"""
        stmt = select(ChatSession.id).where(
            ChatSession.id == session_id,
            ChatSession.tenant_id == tenant_id,
        )
        return (await self._session.execute(stmt)).first() is not None

    async def touch(self, session_id: str) -> None:
        """刷新 last_active_at（读取/写入即续期）。"""
        await self._session.execute(
            ChatSession.__table__.update()
            .where(ChatSession.id == session_id)
            .values(last_active_at=func.now())
        )

    async def delete(self, session_id: str, tenant_id: Optional[str]) -> bool:
        """按 (id, tenant) 精确删除，命中返回 True。CASCADE 连带删消息。"""
        sess = await self.get(session_id)
        if sess is None or sess.tenant_id != tenant_id:
            return False
        await self._session.delete(sess)
        await self.flush()
        return True

    # ---------- 消息 ----------
    async def lock_session(self, session_id: str) -> None:
        """对同一会话加**事务级 advisory lock**，串行化并发追加。

        为什么需要它（真实竞态）：
            `append` 的写入序列是「SELECT max(seq) -> INSERT seq+1」——
            典型的 read-modify-write。同一 session 两个并发请求（例如双标签页、
            或 Agent 的多步写入）会算出**同一个 seq**，撞
            `unique(session_id, seq)` 约束，后到的那个直接报错。
            这也是改造方案「不足」清单里列出的既有技术债（B5）。

        为什么用 advisory lock 而不是改 schema：
            - `SELECT ... FOR UPDATE` 对「尚无消息的新会话」无效（没有行可锁）；
            - 加序列表/DB 序列需要迁移，且要处理历史数据回填；
            - advisory lock 是**零迁移**方案：pg_advisory_xact_lock 在事务结束时
              自动释放（不会泄漏），无需 try/finally。
            - 锁粒度是 session_id 哈希 ⇒ **不同会话完全并行**，不损吞吐。

        注意：`hashtext` 是 PG 内置函数，把字符串稳定映射成 int4。
            偶发哈希碰撞只会让两个不同会话短暂串行（正确性不受影响）。
        """
        await self._session.execute(
            select(func.pg_advisory_xact_lock(func.hashtext(session_id)))
        )

    async def next_seq(self, session_id: str) -> int:
        stmt = select(func.coalesce(func.max(Message.seq), 0)).where(
            Message.session_id == session_id
        )
        current = (await self._session.execute(stmt)).scalar_one()
        return int(current) + 1

    async def add_message(
        self, session_id: str, role: str, content: str, seq: int,
        tool_context: Optional[str] = None,
    ) -> Message:
        msg = Message(session_id=session_id, role=role, content=content,
                      seq=seq, tool_context=tool_context)
        self._session.add(msg)
        await self.flush()
        return msg

    async def get_recent_messages(self, session_id: str, limit: int) -> List[Message]:
        """取最近 limit 条并按 seq 升序返回（用于回源重建历史）。"""
        stmt = (
            select(Message)
            .where(Message.session_id == session_id)
            .order_by(Message.seq.desc())
            .limit(limit)
        )
        rows = list((await self._session.execute(stmt)).scalars().all())
        rows.reverse()  # 降序取最近 N 条后反转为时间正序
        return rows
