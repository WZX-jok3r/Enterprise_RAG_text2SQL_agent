"""
根路由聚合

统一挂载 /v1 前缀；main.py 再套 /api，最终对外路径为 /api/v1/*。
业务路由：health / query / sessions / documents / agent / usage。
"""

from fastapi import APIRouter

from app.api.v1 import agent, documents, health, query, sessions, usage

api_router = APIRouter(prefix="/v1")
api_router.include_router(health.router)
api_router.include_router(query.router)
api_router.include_router(sessions.router)
api_router.include_router(documents.router)
# Agent 路由是**唯一的问答入口**（Step 3 起 /chat/* 与 RAGPipeline 已下线）：
#   /agent/stream  — 单一问答入口，意图路由（chitchat/meta/doc/data/hybrid）
#   /agent/route   — 纯函数路由调试
api_router.include_router(agent.router)
# 用量与成本看板（P7）：Agent 一次问答可调 1~8 次 LLM，成本必须可见
api_router.include_router(usage.router)
