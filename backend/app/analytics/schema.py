"""schema.py - Schema 语义层：把元数据渲染成「给 LLM 看的数据字典」。

为什么单独一层，而不是每次查询都去 introspect 业务库：
1. **速度**：渲染结果可缓存到 Redis，避免每次提问都读一遍系统目录；
2. **可控**：出问题改元数据即可（补中文别名、清空敏感枚举值），不用改代码；
3. **权限**：`is_enabled=False` 的表直接不进 prompt —— 这是"数据权限"，比"表权限"细；
4. **它是 Schema Linking 的输入**：主流 Text2SQL 方案（Vanna / DB-GPT / CHASE-SQL）
   的做法高度收敛 —— 都是「检索 schema 描述 + few-shot 示例 → 生成 → 执行反馈修正」。
   所以本模块产出的文本质量，直接决定生成 SQL 的准确率。

渲染格式（紧凑但信息完整，枚举值内联）：

    ### 表 employees —— 员工表：全公司员工花名册，含部门、薪资与入职日期（10000 行）
    - id INTEGER [主键]        员工工号
    - department VARCHAR(64)   所属部门；取值：Sales/Support/HR/... （共 10 类）
    - salary INTEGER           年薪（美元）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.analytics import AnalyticsColumn, AnalyticsTable

logger = logging.getLogger(__name__)

# 单个枚举列最多内联多少个取值（防止超长枚举把 prompt 撑爆）
MAX_ENUM_INLINE = 20
# 渲染结果总长度上限（防御性：表特别多时截断，避免 prompt 超长）
MAX_SCHEMA_CHARS = 6000


@dataclass
class TableSchema:
    """一张表的语义描述（渲染的中间结构，便于单测）。"""

    table_name: str
    display_name: str
    description: str
    row_count: int
    columns: List[Dict[str, object]]      # {name,type,description,enum_values,is_primary}

    def render(self) -> str:
        head = f"### 表 {self.table_name} —— {self.display_name}"
        if self.description:
            head += f"：{self.description}"
        if self.row_count:
            head += f"（约 {self.row_count} 行）"

        lines = [head]
        for c in self.columns:
            name = str(c["name"])
            ctype = str(c["type"])
            desc = str(c.get("description") or "")
            # 主键标记：帮助模型理解唯一标识列
            pk = " [主键]" if c.get("is_primary") else ""
            # 列名与类型对齐，便于模型读
            line = f"- {name} {ctype}{pk}"
            if desc:
                line += f"    {desc}"
            lines.append(line)
        return "\n".join(lines)


def filter_sensitive_columns(schemas: Sequence[TableSchema], redact) -> List[TableSchema]:
    """Copy schema descriptions so restricted requests cannot mutate shared data."""
    from dataclasses import replace
    denied = {entry.rsplit('.', 1)[-1].lower() for entry in (redact or ())}
    return [replace(s, columns=[dict(c) for c in s.columns
                               if str(c['name']).lower() not in denied]) for s in schemas]


def load_table_schemas(
    session: Session,
    tenant_id: Optional[str] = None,
    only_enabled: bool = True,
) -> List[TableSchema]:
    """从元数据表读出可用的表结构。

    Args:
        tenant_id: 只返回该租户可见的表（NULL 或匹配）
        only_enabled: 只返回 is_enabled=True 的表（LLM 可见性白名单）
    """
    stmt = select(AnalyticsTable)
    if only_enabled:
        stmt = stmt.where(AnalyticsTable.is_enabled.is_(True))
    if tenant_id is not None:
        # 共享表（tenant_id IS NULL）+ 本租户表
        stmt = stmt.where(
            (AnalyticsTable.tenant_id.is_(None)) | (AnalyticsTable.tenant_id == tenant_id)
        )
    tables = list(session.execute(stmt.order_by(AnalyticsTable.table_name)).scalars())

    out: List[TableSchema] = []
    for t in tables:
        cols: List[Dict[str, object]] = []
        for c in t.columns:  # relationship 已 order_by ordinal，lazy="selectin"
            cols.append({
                "name": c.column_name,
                "type": c.data_type,
                "description": c.description,
                "enum_values": list(c.enum_values) if c.enum_values else [],
                "is_primary": c.is_primary,
            })
        out.append(TableSchema(
            table_name=t.table_name,
            display_name=t.display_name,
            description=t.description or "",
            row_count=t.row_count,
            columns=cols,
        ))
    return out


def render_schema(schemas: Sequence[TableSchema], max_chars: int = MAX_SCHEMA_CHARS) -> str:
    """把表结构渲染成 prompt 里的「数据字典」文本。"""
    if not schemas:
        return "（当前没有可查询的数据表）"

    parts: List[str] = []
    for s in schemas:
        parts.append(s.render())

    text = "\n\n".join(parts)
    if len(text) > max_chars:
        # 截断时明确告知，避免模型以为 schema 就这么少
        text = text[:max_chars] + "\n\n（schema 过长已截断，如需其它表请用 list_data_tables 工具查询）"
    return text


def get_schema_text(session: Session, tenant_id: Optional[str] = None) -> str:
    """便捷入口：读元数据 + 渲染，一步到位。"""
    return render_schema(load_table_schemas(session, tenant_id=tenant_id))


# ==================== Schema Linking：裁剪相关表 ====================

def _tokenize(text: str) -> List[str]:
    """极轻量分词：中文按字，英文/数字按词。仅用于表相关性打分，不追求精度。"""
    import re

    text = text.lower()
    latin = re.findall(r"[a-z0-9_]+", text)
    cjk = re.findall(r"[\u4e00-\u9fff]", text)
    return latin + cjk


def rank_tables(question: str, schemas: Sequence[TableSchema]) -> List[tuple[float, TableSchema]]:
    """按问题与表的词面相关性打分（Schema Linking 的第一步）。

    打分信号（按权重）：
      - 表 display_name 命中（如「员工表」）：权重最高 —— 中文问法主要靠它
      - 列 description 命中（如「薪资」「部门」）
      - 表名/列名英文命中（如 "salary"）
      - 枚举值命中（如「Sales」）—— 命中枚举说明问题确实指向这张表的数据

    返回按分数降序；分数为 0 的表仍保留（调用方可决定是否丢弃）。
    """
    q_tokens = set(_tokenize(question))
    if not q_tokens:
        return [(0.0, s) for s in schemas]

    scored: List[tuple[float, TableSchema]] = []
    for s in schemas:
        score = 0.0
        # 表显示名：按字符命中
        for ch in set(_tokenize(s.display_name)):
            if ch in q_tokens:
                score += 3.0
        # 表英文名
        if s.table_name.lower() in question.lower():
            score += 4.0
        for c in s.columns:
            # 列描述（中文语义，权重高）
            for ch in set(_tokenize(str(c.get("description") or ""))):
                if ch in q_tokens:
                    score += 1.0
            # 列英文名
            name = str(c["name"]).lower()
            if name and name in question.lower():
                score += 2.0
            # 枚举值命中（强信号：说明问题在问这张表的具体取值）
            for ev in (c.get("enum_values") or [])[:MAX_ENUM_INLINE]:
                ev_s = str(ev)
                if ev_s and ev_s.lower() in question.lower():
                    score += 2.5
        scored.append((score, s))

    scored.sort(key=lambda x: -x[0])
    return scored


def select_relevant_schema_text(
    question: str,
    schemas: Sequence[TableSchema],
    max_tables: int = 4,
    always_include_zero_score: bool = True,
) -> str:
    """按问题裁剪出最相关的 max_tables 张表并渲染。

    为什么需要裁剪：表多了 prompt 会膨胀、模型还会误选无关表。
    本轮演示只有 4 张表（全塞得下），但接口先按"可扩展"设计 ——
    真实企业库几十上百张表时，这个函数就是必需的。
    """
    if not schemas:
        return "（当前没有可查询的数据表）"

    ranked = rank_tables(question, schemas)
    picked = [s for _, s in ranked[:max_tables]]

    # 若所有表都是 0 分（问题与任何表都无词面关联），全给反而有害；
    # 但也可能确实是跨表统计，所以默认仍保留（由 LLM 用 list_data_tables 兜底）
    if always_include_zero_score and not any(sc > 0 for sc, _ in ranked):
        logger.info("[schema] 问题与所有表均无词面关联，回退为全量 schema")

    return render_schema(picked)
