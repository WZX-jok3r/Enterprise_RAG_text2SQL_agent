"""
根路由聚合

统一挂载 /v1 前缀；main.py 再套 /api，最终对外路径为 /api/v1/*。
业务路由：health / query / chat / sessions / documents / agent。
"""

from fastapi import APIRouter

from app.api.v1 import agent, chat, documents, health, query, sessions, usage

api_router = APIRouter(prefix="/v1")
api_router.include_router(health.router)
api_router.include_router(query.router)
api_router.include_router(chat.router)
api_router.include_router(sessions.router)
api_router.include_router(documents.router)
# Agent 路由是**产品主入口**（Step 2 起前端只走它）：
#   /agent/stream  — 单一问答入口，意图路由（chitchat/meta/doc/data/hybrid）
#   /agent/route   — 纯函数路由调试
# /chat 与 /chat/stream 保留但已标 deprecated，作为旧客户端、评测与回滚路径；
# 它们的下线与 RAGPipeline 的删除放在 Step 3（评测口径先迁到 Agent）。
api_router.include_router(agent.router)
# 用量与成本看板（P7）：Agent 一次问答可调 1~8 次 LLM，成本必须可见
api_router.include_router(usage.router)
