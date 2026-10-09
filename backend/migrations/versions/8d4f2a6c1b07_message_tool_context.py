"""message tool_context

Revision ID: 8d4f2a6c1b07
Revises: 5b2c8d4e6f13
Create Date: 2026-10-09

为 `messages` 增加 `tool_context` 列：保存该轮 Agent 的**工具轨迹**
（执行过的 SQL + 结果行），供下一轮追问延续上下文。

## 为什么需要（实测的多轮缺陷）

原先历史只存 `role + content`（问题 + 自然语言答案），追问时模型看不到
上一轮执行过什么查询、拿到过哪些行，只能重新推导。实测（真实 LLM）：

    第 1 轮「Sales 部门有多少人？」            -> 1042
    第 2 轮「那 Engineering 呢？两个部门合起来一共多少人？」
          -> 只答出 Engineering=1010，**合计算不出来**（自称"资料未找到"）

而 1042 就在它的上下文里。

## 为什么用独立列而不是拼进 content

`content` 是给用户看的对话历史（前端直接渲染）。把 SQL 拼进去会污染显示，
也会让"历史消息"的含义变得不纯。独立列只喂给模型，前端完全无感 ——
`get_history` 仍返回 role/content，只是多带一个字段供 Agent 使用。

可空且无默认值：历史数据的该字段就是 NULL（老会话没有轨迹，行为不变）。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '8d4f2a6c1b07'
down_revision: Union[str, None] = '5b2c8d4e6f13'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'messages',
        sa.Column('tool_context', sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('messages', 'tool_context')
