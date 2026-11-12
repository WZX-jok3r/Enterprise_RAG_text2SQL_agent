"""seed.py - 装载 xlsx 业务表并做真值自检（运维 CLI）。

ETL 引擎（读取 sheet、类型推断、建表、COPY、写元数据）在
`app/analytics/seed.py`；本模块只保留命令行入口、默认文件清单与真值断言，
供人工与 CI 调用，**不被 api/worker 运行态 import**。

用法：
    python -m app.ops.seed --all
    python -m app.ops.seed --file knowledge_base/cost_data.xlsx
    python -m app.ops.seed --all --verify     # 装载后跑真值断言
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any, List

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.analytics import ddl
from app.analytics.seed import LoadResult, load_file
from app.core.config import settings

# 默认要装载的文件（相对 knowledge_base）
DEFAULT_FILES = [
    "xlsx-sample-large-10000-rows.xlsx",
    "xlsx-sample-multiple-sheets.xlsx",
    "cost_data.xlsx",
]


# ==================== 真值自检 ====================

# 用 pandas/openpyxl 直读原始文件算出的真值（见改造方案第三部分）。
#
# ⚠️ cost_data.xlsx 的真实结构（实测，2026-10-08）：
#     [0] 标题行  「橱柜项目成本明细表」（8 列只有 1 列有值）-> 由表头探测跳过
#     [1] 表头行  「项目/品牌型号/.../备注」
#     [2..6] 数据 5 行
#     [7] 尾部汇总脚注「汇总：铰链(28×20)、颗粒板...」（首列有值 => openpyxl 视为数据行）
#   => 装载后 **6 行**（含 [7] 这行脚注），不是改造方案文档里估的 7 行。
#   业务列 = 7 列（col_1..col_7）。
TRUTH_CHECKS: List[tuple[str, str, Any]] = [
    ("employees", "SELECT count(*) FROM employees", 10000),
    ("employees", "SELECT count(*) FROM employees WHERE department='Sales'", 1042),
    ("employees", "SELECT count(*) FROM employees WHERE department='Engineering'", 1010),
    ("employees", "SELECT count(*) FROM employees WHERE hire_date >= '2026-01-01'", 192),
    ("employees", "SELECT department FROM employees GROUP BY department ORDER BY count(*) DESC LIMIT 1", "Sales"),
    ("employees", "SELECT department FROM employees GROUP BY department ORDER BY count(*) ASC LIMIT 1", "Finance"),
    # 这两条是 30 倍错误案例的直接回归断言（改造方案第三部分）
    ("employees",
     "SELECT count(*) FROM employees WHERE department='Sales'", 1042),
    ("sales", "SELECT count(*) FROM sales", 20),
    ("expenses", "SELECT count(*) FROM expenses", 15),
    ("cost_data", "SELECT count(*) FROM cost_data", 6),
    # cost_data 必须建出 8 业务列（不是 1 列）—— 表头陷阱的回归断言。
    # 物理列 = 8 业务列 + tenant_id + is_shared = 10。
    # ⚠️ 加了 is_shared 后这条从 9 变 10（实测：漏改会让真值自检整体失败，
    #    且失败信息只出现在 problems 列表里，CLI 汇总行不显示 —— 见 L-020）。
    ("cost_data",
     "SELECT count(*) FROM information_schema.columns "
     "WHERE table_schema='public' AND table_name='cost_data'", 10),
    # 中文列名表必须能按数值列排序（验证中文表头没有破坏列语义）
    ("cost_data",
     "SELECT col_1 FROM cost_data WHERE col_4 IS NOT NULL ORDER BY col_4 DESC LIMIT 1", "PET肤感门板"),
    # 多租户：演示语料必须标记为共享，否则 fail-closed 策略下所有租户都读不到
    # （这是"改造完演示突然全空"的直接回归断言）
    ("employees", "SELECT count(*) FROM employees WHERE is_shared IS TRUE", 10000),
    ("cost_data", "SELECT count(*) FROM cost_data WHERE is_shared IS TRUE", 6),
]


def verify_truth(engine: Engine) -> tuple[bool, List[str]]:
    """装载后用 SQL 实测真值。这是"SQL 通道真的能答对统计题"的第一道证明。"""
    problems: List[str] = []
    with engine.connect() as conn:
        for table, sql, expected in TRUTH_CHECKS:
            # 表不存在时给明确提示，而不是抛异常
            if not ddl.table_exists(engine, table):
                problems.append(f"[{table}] 表不存在")
                continue
            try:
                got = conn.execute(text(sql)).scalar()
            except Exception as e:  # noqa: BLE001
                problems.append(f"[{table}] 查询失败: {str(e)[:120]}")
                continue
            if got != expected:
                problems.append(f"[{table}] {sql[:70]}... 期望 {expected!r} 得到 {got!r}")
    return (len(problems) == 0), problems


# ==================== CLI ====================

def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="从 xlsx 装载 Analytics 业务表（ETL）")
    parser.add_argument("--all", action="store_true", help="装载默认的全部 xlsx")
    parser.add_argument("--file", action="append", default=[], help="指定文件（可多次）")
    parser.add_argument(
        "--tenant", type=str, default="",
        help="数据归属租户；留空表示无归属（仅当 --shared 时才会被读到）",
    )
    parser.add_argument(
        "--shared", action="store_true",
        help="标记为全公司共享语料（所有租户可见）。默认语料（--all）即共享，"
             "因为它们是 demo 知识库、不属于任何单一租户",
    )
    parser.add_argument("--verify", action="store_true", help="装载后跑真值断言")
    args = parser.parse_args()

    if not args.all and not args.file:
        parser.error("需指定 --all 或 --file")

    targets: List[Path] = []
    if args.all:
        targets += [settings.knowledge_base_dir / f for f in DEFAULT_FILES]
    for f in args.file:
        p = Path(f)
        targets.append(p if p.is_absolute() else (settings.knowledge_base_dir / p.name))

    missing = [p for p in targets if not p.exists()]
    if missing:
        for p in missing:
            print(f"[seed] 文件不存在: {p}", file=sys.stderr)
        return 1

    analytics_engine = create_engine(settings.sync_analytics_url)
    rag_engine = create_engine(settings.sync_postgres_url)

    all_results: List[LoadResult] = []
    try:
        # `--all` 装载的是 demo 知识库语料 —— 它们是**全公司共享**的，
        # 不属于任何单一租户，因此默认标记为 shared（否则 fail-closed 策略下
        # 谁都读不到，演示直接崩）。用 --tenant 显式指定则改为租户私有。
        shared = args.shared or (args.all and not args.tenant)
        for p in targets:
            all_results += load_file(
                analytics_engine, rag_engine, p,
                tenant_id=args.tenant, is_shared=shared,
            )
        print(f"（归属租户={args.tenant or '无'}，共享={shared}）")

        print()
        print("=" * 78)
        print(f"装载完成：{len(all_results)} 张表")
        print("=" * 78)
        print(f"{'表名':<14}{'来源':<38}{'sheet':<12}{'表头行':>6}{'列':>4}{'行数':>8}")
        print("-" * 78)
        for r in all_results:
            print(f"{r.table_name:<14}{r.source:<38}{r.sheet_name:<12}"
                  f"{r.header_row_index:>6}{r.column_count:>4}{r.row_count:>8}")

        if args.verify:
            ok, problems = verify_truth(analytics_engine)
            print()
            print("=" * 78)
            print(f"真值自检: {'全部通过 ✅' if ok else '失败 ❌'}")
            print("=" * 78)
            for pr in problems:
                print(f"  - {pr}")
            return 0 if ok else 1
    finally:
        analytics_engine.dispose()
        rag_engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
