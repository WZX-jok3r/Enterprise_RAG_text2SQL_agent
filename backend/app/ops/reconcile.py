"""reconcile.py - PG 登记表与 Qdrant 向量库的对账（P7）。

## 为什么需要对账

向量库与关系库是两个独立存储，"删除文档"是**三段操作**：
    清 Qdrant 向量 -> 删磁盘文件 -> 删 PG 登记行
任一段失败都会留下不一致（本项目刻意保留登记行以便重试，因此**失败后会真的不一致**）：
    - PG 有记录、Qdrant 无向量  -> 检索永远召回不到该文档（"登记了但查不到"）
    - Qdrant 有向量、PG 无记录  -> 删不掉的幽灵数据（"查得到但管不了"）
    - 磁盘无文件、PG 有记录    -> 重灌会失败

日常运行没问题，但**长期半途失败无自动修复** —— 这是改造方案「不足」清单 C5。
对账任务把这些不一致**找出来并报告**，可选自动修复。

## 设计原则

1. **默认只报告、不修改**（`--fix` 才动手）。
   对账工具最大的风险是"自动修错" —— 先看清再动手。
2. **只处理能明确判定的不一致**。例如"PG 有记录但 Qdrant 无向量"可安全清理登记行；
   而"Qdrant 有向量但 PG 无记录"需要**人工确认**是否真该删（可能是正在入库的中间态）。
3. 输出结构化结果，可被监控采集（孤儿数 > 0 应告警）。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

from sqlalchemy import create_engine, text

from app.core.config import settings

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

logger = logging.getLogger(__name__)


@dataclass
class ReconcileReport:
    """对账结果。

    ⚠️ 关于 "orphan_in_qdrant" 的一个重要区分（实测得出）：

    本项目有**两套入库路径**，它们的登记方式不同：
      A. API 上传（POST /documents）—— 写 PG `documents` 表 + ARQ worker 入库
      B. 批量 CLI（python -m app.ingestion.indexer）—— 只写 `processed_cache/cache.json`
         （历史设计），**不写 documents 表**

    因此"Qdrant 有向量但 documents 表无登记"在**绝大多数情况下是正常现象**：
    那些语料是走 B 路径灌进去的（本项目 18 个 source 里有 17 个属于此类）。

    这个区分很关键，否则对账会天天报 17 条"不一致"，变成噪音而被忽略 ——
    **一个天天误报的对账工具等于没有对账**。

    所以拆成两类：
      - `unregistered_on_disk`：向量在、登记无、**但文件在 knowledge_base**
        ⇒ 大概率是 B 路径灌的，属"已知状态"，不计为异常。
      - `orphan_in_qdrant`：向量在、登记无、**且文件已不在磁盘**
        ⇒ 真正的幽灵数据（删了文件但向量残留），需要处理。
    """

    pg_documents: int = 0
    qdrant_sources: int = 0
    # PG 有登记、Qdrant 无向量（检索不到）
    missing_in_qdrant: List[str] = field(default_factory=list)
    # Qdrant 有向量、PG 无登记、且磁盘也无文件（真幽灵数据）
    orphan_in_qdrant: List[str] = field(default_factory=list)
    # Qdrant 有向量、PG 无登记，但磁盘有文件（走批量 CLI 入库，属已知状态）
    unregistered_on_disk: List[str] = field(default_factory=list)
    # PG 有登记、磁盘无文件（重灌会失败）
    missing_on_disk: List[str] = field(default_factory=list)
    # 租户维度：Qdrant payload 里多值 tenant 的样本（便于核对 ACL 是否生效）
    qdrant_tenants: Dict[str, int] = field(default_factory=dict)
    fixed: List[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        """只有真正需要处理的不一致才算"不干净"。

        `unregistered_on_disk` 属已知状态（批量 CLI 入库），不计入。
        """
        return not (self.missing_in_qdrant or self.orphan_in_qdrant
                    or self.missing_on_disk)

    def to_dict(self) -> Dict:
        return {
            "pg_documents": self.pg_documents,
            "qdrant_sources": self.qdrant_sources,
            "missing_in_qdrant": self.missing_in_qdrant,
            "orphan_in_qdrant": self.orphan_in_qdrant,
            "unregistered_on_disk": self.unregistered_on_disk,
            "missing_on_disk": self.missing_on_disk,
            "qdrant_tenants": self.qdrant_tenants,
            "fixed": self.fixed,
            "clean": self.is_clean,
        }


def pg_sources(sync_engine) -> Set[str]:
    """PG documents 表里登记的全部 source。"""
    with sync_engine.connect() as c:
        rows = c.execute(text("SELECT DISTINCT source FROM documents")).fetchall()
    return {r[0] for r in rows if r[0]}


def qdrant_sources_and_tenants() -> tuple[Set[str], Dict[str, int]]:
    """扫描 Qdrant 全部点的 payload，返回 (source 集合, 租户计数)。

    用 scroll 分页扫描，避免一次性拉全量（点数可能很大）。
    """
    from qdrant_client import QdrantClient

    client = QdrantClient(host=settings.qdrant_host, port=settings.qdrant_port)
    collection = settings.qdrant_collection_name

    sources: Set[str] = set()
    tenants: Dict[str, int] = {}
    offset = None
    total = 0

    while True:
        points, offset = client.scroll(
            collection_name=collection,
            limit=512,
            offset=offset,
            with_payload=["source", settings.tenant_field],
            with_vectors=False,
        )
        if not points:
            break
        for p in points:
            payload = p.payload or {}
            src = payload.get("source")
            if src:
                sources.add(src)
            tid = payload.get(settings.tenant_field)
            if isinstance(tid, list):
                for t in tid:
                    tenants[str(t)] = tenants.get(str(t), 0) + 1
            elif tid is not None:
                tenants[str(tid)] = tenants.get(str(tid), 0) + 1
            total += 1
        if offset is None:
            break
        # 防御：极端情况下 offset 不推进会导致死循环
        if total > 1_000_000:
            logger.warning("[reconcile] 点数超过 100 万，提前停止扫描")
            break

    return sources, tenants


def reconcile(fix: bool = False) -> ReconcileReport:
    """执行对账。fix=True 时对**可安全判定**的不一致做修复。"""
    report = ReconcileReport()
    sync_engine = create_engine(settings.sync_postgres_url)

    try:
        pg = pg_sources(sync_engine)
        report.pg_documents = len(pg)

        try:
            qd, tenants = qdrant_sources_and_tenants()
        except Exception as e:  # noqa: BLE001
            logger.error("[reconcile] 无法扫描 Qdrant: %s", e)
            raise

        report.qdrant_sources = len(qd)
        report.qdrant_tenants = dict(sorted(tenants.items(), key=lambda x: -x[1])[:20])

        # 磁盘文件名集合（Qdrant 的 source 是文件名；物理副本可能带 uuid 前缀）
        # ⚠️ 必须用 rglob 递归：PDF 语料放在 knowledge_base/pdf_examples/ 子目录下，
        #    用 glob("*") 只扫根目录会把那 5 个 PDF 误判成"磁盘已无文件"的幽灵数据。
        #    （实测踩到：初版用 glob("*") 报了 5 个假幽灵。）
        kb = settings.knowledge_base_dir
        disk_names = {p.name for p in kb.rglob("*") if p.is_file()}

        def on_disk(src: str) -> bool:
            name = Path(src).name
            return name in disk_names or any(n.endswith("_" + name) for n in disk_names)

        # ---- 三类不一致 ----
        report.missing_in_qdrant = sorted(pg - qd)

        # 关键区分：向量在、登记无 —— 文件还在磁盘 vs 文件已不在
        #   （前者大概率是批量 CLI 入库，属已知状态；后者才是真幽灵数据）
        unregistered = qd - pg
        report.orphan_in_qdrant = sorted(s for s in unregistered if not on_disk(s))
        report.unregistered_on_disk = sorted(s for s in unregistered if on_disk(s))

        # PG 有登记但磁盘无文件（重灌会失败）
        report.missing_on_disk = sorted(s for s in pg if not on_disk(s))

        # ---- 可选修复 ----
        if fix:
            # 只修"PG 有登记但 Qdrant 无向量"：把登记行标记为失败，
            # 让用户可以直接重灌。**不自动删行** —— 删了就丢失"曾经上传过"的事实。
            if report.missing_in_qdrant:
                with sync_engine.begin() as c:
                    for src in report.missing_in_qdrant:
                        c.execute(
                            text(
                                "UPDATE documents SET status = 'failed', "
                                "meta = coalesce(meta, '{}'::jsonb) "
                                "|| '{\"reconcile\": \"no_vectors_in_qdrant\"}'::jsonb "
                                "WHERE source = :s"
                            ),
                            {"s": src},
                        )
                        report.fixed.append(f"标记为 failed: {src}")
                logger.info("[reconcile] 已修复 %d 条", len(report.fixed))

            # 孤儿向量**不自动删**：可能是"正在入库"的中间态（向量已写、登记行未提交）。
            # 自动删会造成数据丢失，必须人工确认。
            if report.orphan_in_qdrant:
                logger.warning(
                    "[reconcile] 发现 %d 个孤儿 source（Qdrant 有向量、PG 无登记）—— "
                    "**不自动删除**，可能是入库中间态；请人工确认后用 "
                    "DELETE /api/v1/documents 或手工清向量",
                    len(report.orphan_in_qdrant),
                )
    finally:
        sync_engine.dispose()

    return report


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    ap = argparse.ArgumentParser(description="PG 与 Qdrant 对账")
    ap.add_argument("--fix", action="store_true",
                    help="修复可安全判定的不一致（默认只报告）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出（供监控采集）")
    args = ap.parse_args()

    report = reconcile(fix=args.fix)

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print("=" * 72)
        print("📋 对账报告（PG documents ↔ Qdrant payload.source）")
        print("=" * 72)
        print(f"PG 登记 source 数 : {report.pg_documents}")
        print(f"Qdrant source 数  : {report.qdrant_sources}")
        print()
        print(f"❗ PG 有登记但 Qdrant 无向量 : {len(report.missing_in_qdrant)}")
        for s in report.missing_in_qdrant[:20]:
            print(f"     - {s}")
        print(f"❗ Qdrant 有向量但 PG 无登记、磁盘也无文件（真幽灵）: {len(report.orphan_in_qdrant)}")
        for s in report.orphan_in_qdrant[:20]:
            print(f"     - {s}")
        print(f"❗ PG 有登记但磁盘无文件     : {len(report.missing_on_disk)}")
        for s in report.missing_on_disk[:20]:
            print(f"     - {s}")
        print()
        print(f"ℹ️ Qdrant 有向量、PG 无登记、但磁盘有文件: {len(report.unregistered_on_disk)}")
        print("   （走批量 CLI 入库的语料，属已知状态 —— 不写 documents 表，不计为异常）")
        if report.unregistered_on_disk:
            shown = ", ".join(report.unregistered_on_disk[:5])
            more = " 等" if len(report.unregistered_on_disk) > 5 else ""
            print(f"     {shown}{more}")
        if report.qdrant_tenants:
            print()
            print("ℹ️ Qdrant payload 租户分布（前 20）:")
            for t, n in report.qdrant_tenants.items():
                print(f"     {t}: {n}")
        if report.fixed:
            print()
            print(f"🔧 已修复 {len(report.fixed)} 条:")
            for f in report.fixed[:20]:
                print(f"     - {f}")
        print("=" * 72)
        print("✅ 一致" if report.is_clean else "⚠️ 存在不一致（见上）")

    # 退出码：有孤儿向量时返回 2（需人工确认），其它不一致返回 1，完全一致返回 0。
    # 便于 cron/监控直接用退出码判断是否需要告警。
    if report.orphan_in_qdrant:
        return 2
    if not report.is_clean:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
