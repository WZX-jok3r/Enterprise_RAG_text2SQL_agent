"""会话服务：把 src/api.py 的内存 SessionStore 迁移为 PostgreSQL(事实来源) + Redis(热缓存)。

对外语义与内存实现一一对应，P5 接入 API 时行为无感：
    create / exists / get_history / append / delete
差异仅在于：
- 历史事实来源是 PG 的 messages 表；Redis 只是缓存，miss 时从 PG 回源并 reheat（会话因此可跨重启保留）。
- 所有读写都带 tenant_id 的 ACL 判定：非本租户的 session 视为不存在（隔离）。
- MAX_HISTORY_MESSAGES / SESSION_TTL_SECONDS 沿用 settings，Redis LTRIM/EXPIRE 落到位。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.cache.redis import SessionCache
from app.core.config import settings
from app.repositories.session_repo import SessionRepository

logger = logging.getLogger(__name__)


class SessionService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        cache: Optional[SessionCache] = None,
        max_history: Optional[int] = None,
        ttl: Optional[int] = None,
    ) -> None:
        self._sm = sessionmaker
        self._cache = cache or SessionCache()
        self._max_history = max_history if max_history is not None else settings.max_history_messages
        self._ttl = ttl if ttl is not None else settings.session_ttl_seconds

    async def create(self, tenant_id: Optional[str]) -> str:
        session_id = uuid.uuid4().hex
        async with self._sm() as s:
            repo = SessionRepository(s)
            await repo.create(session_id, tenant_id)
            await s.commit()
        logger.info("[Session] 新建会话 %s (tenant=%s)", session_id, tenant_id)
        return session_id

    async def exists(self, session_id: str, tenant_id: Optional[str]) -> bool:
        async with self._sm() as s:
            return await SessionRepository(s).exists(session_id, tenant_id)

    async def get_history(self, session_id: str, tenant_id: Optional[str]) -> List[Dict[str, Any]]:
        """返回 [{role, content}, ...]（时间正序，最多 max_history 条）。非本租户返回 []。"""
        if not await self.exists(session_id, tenant_id):
            return []
        cached = await self._cache.get(session_id)
        if cached is not None:
            await self._cache.touch(session_id, self._ttl)
            return cached
        # miss：从 PG 回源并 reheat
        async with self._sm() as s:
            msgs = await SessionRepository(s).get_recent_messages(session_id, self._max_history)
        # tool_context 一并带出：它是给**模型**看的工具轨迹，
        # 不参与前端渲染（前端只读 role/content），但多轮追问必须靠它。
        history = [
            {"role": m.role, "content": m.content, "tool_context": m.tool_context}
            for m in msgs
        ]
        await self._cache.reheat(session_id, history, self._max_history, self._ttl)
        return history

    async def append(self, session_id: str, tenant_id: Optional[str], message: Dict[str, str]) -> None:
        """追加一条干净消息（role/content，可带 tool_context）。

        `tool_context` 是**给模型看的**工具轨迹（上一轮执行过的 SQL 与结果行），
        与 content 分开存：content 供前端渲染，tool_context 只喂模型
        —— 混进 content 会污染对话显示。

        会话不存在或租户不符则静默跳过（对齐内存实现）。

        并发安全：整段「取 seq -> 插入 -> 续期」在**同一事务**内并持有
        会话级 advisory lock，避免同一 session 的并发追加算出重复 seq 撞唯一约束
        （见 SessionRepository.lock_session 的说明）。
        """
        if not await self.exists(session_id, tenant_id):
            return
        async with self._sm() as s:
            repo = SessionRepository(s)
            await repo.lock_session(session_id)
            seq = await repo.next_seq(session_id)
            await repo.add_message(
                session_id, message["role"], message["content"], seq,
                tool_context=message.get("tool_context"),
            )
            await repo.touch(session_id)
            await s.commit()
        await self._cache.append(session_id, message, self._max_history, self._ttl)

    async def delete(self, session_id: str, tenant_id: Optional[str]) -> bool:
        async with self._sm() as s:
            repo = SessionRepository(s)
            removed = await repo.delete(session_id, tenant_id)
            await s.commit()
        if removed:
            await self._cache.delete(session_id)
        return removed
