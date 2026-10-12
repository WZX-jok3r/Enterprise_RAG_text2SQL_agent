"""redact.py - 结果集脱敏补充层。

SQL 权限边界现由 executor 的敏感引用预检与独立数据库账号的列级授权建立。
下面描述的是保留的结果集脱敏策略及其局限，不能单独用于访问控制。

## 为什么在"结果集"上脱敏，而不是拒绝查询或改写 SQL

三种可选做法及其取舍：

| 做法 | 问题 |
|---|---|
| 拒绝含敏感列的查询 | LLM 常写 `SELECT *`，会导致大量正常问题被拒 —— 可用性崩 |
| 在 guard 里改写 SQL 去掉敏感列 | 需要精确的列级 AST 改写（`SELECT *` 展开、别名、CTE、JOIN…），复杂且易漏 |
| **在结果集上按列名脱敏** | ✅ 实现简单、**不依赖 LLM 写出什么 SQL**，`SELECT *` 也照样拦住 |

第三种是**默认拒绝式**的：无论模型生成什么语句，只要结果里出现敏感列名就被替换值。
它不追求"不查"，只保证"查了也看不到" —— 这正是纵深防御的思路：
不赌上游不出错，而是保证即使出错也没有信息泄露。

## 局限（必须诚实说明，避免误以为它是万能）

1. **靠列名匹配**：若模型给敏感列起别名（`SELECT salary AS s`），列名不同则匹配不到。
   → 因此这一层**只是数据层兜底**，上游仍应配合：guard 的 AST 检查、
   以及在 schema 描述里对非特权角色**不展示**敏感列的语义。
   （真正彻底的方案是数据库列级权限 `GRANT SELECT(col)`，属后续演进。）
2. 仅处理结果集，不影响聚合值（如 `AVG(salary)` 的列名未必叫 salary）。

这两条已写入 docs/经验教训.md，避免把"看起来有了脱敏"当成"数据安全已完成"。
"""

from __future__ import annotations

import logging
from typing import Any, FrozenSet, List, Sequence

logger = logging.getLogger(__name__)

# 脱敏后的占位值（保持可读，让用户知道"这里有值但你看不到"）
MASK = "***"


def _is_sensitive(column_name: str, table_hint: str, redact: FrozenSet[str]) -> bool:
    """列是否敏感。

    匹配两种形式：
      - "表.列"（配置里的精确形式，如 employees.salary）
      - 裸列名（如 salary）—— 因为结果集里通常只有列名、没有表名，
        若不支持裸列名匹配，绝大多数查询都拦不住。
    """
    if not redact:
        return False
    col = str(column_name).strip().lower()
    if f"{table_hint}.{col}" in redact:
        return True
    # 裸列名匹配：redact 里任一条的"列部分"等于该列名
    return any(entry.split(".", 1)[1] == col for entry in redact if "." in entry)


def redact_rows(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    redact: FrozenSet[str],
    table_hint: str = "",
) -> tuple[List[List[Any]], List[str]]:
    """对结果集按列名脱敏。

    Args:
        columns: 列名列表（来自 cursor.keys()）
        rows: 行数据
        redact: 需要脱敏的列集合（"表.列" 小写）；空集则**零成本直接返回**
        table_hint: 表名提示（当前单表查询场景下通常为空）

    Returns:
        (脱敏后的行, 被脱敏的列名列表)
    """
    if not redact or not columns:
        return [list(r) for r in rows], []

    sensitive_idx = [
        i for i, c in enumerate(columns) if _is_sensitive(c, table_hint, redact)
    ]
    if not sensitive_idx:
        return [list(r) for r in rows], []

    masked_cols = [columns[i] for i in sensitive_idx]
    out: List[List[Any]] = []
    for row in rows:
        new_row = list(row)
        for i in sensitive_idx:
            if i < len(new_row) and new_row[i] is not None:
                new_row[i] = MASK
        out.append(new_row)

    logger.info("[redact] 已脱敏列: %s（%d 行）", masked_cols, len(out))
    return out, masked_cols


def redact_observation(observation: str, masked_columns: Sequence[str]) -> str:
    """给 LLM 的观察文本追加脱敏说明。

    必须显式告知模型"这些列被脱敏了"，否则它会看到 `***` 而困惑，
    甚至编造一个数值来解释 —— 那是我们要消灭的静默错误。
    """
    if not masked_columns:
        return observation
    cols = "、".join(masked_columns)
    return (
        f"{observation}\n\n"
        f"⚠️ 说明：列 [{cols}] 属于敏感数据，当前账号权限不足已做脱敏（显示为 {MASK}）。"
        f"**不要猜测或编造这些列的具体数值**；如果用户问的就是这些数据，"
        f"请直接说明需要更高权限。"
    )
