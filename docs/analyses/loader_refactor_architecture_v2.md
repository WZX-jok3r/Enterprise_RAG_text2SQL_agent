# loader.py 重构方案 · 第二轮架构校正（v2）

> 归档说明（2026-11-11）：本文原位于 `output/`（未纳入版本管理），现已迁至 `docs/analyses/`，路径引用同步更新为 `docs/analyses/`。文中 §1.7、附录 B 关于“两个 venv 均损坏”的内容为 2026-11-04 快照；现状只保留 `backend/.venv`（可用），根 `.venv` 已删除，`scripts/check.ps1` 指向 `backend/.venv`，故 Phase -1 的前置门槛已完成。

- 校正日期：2026-11-04
- 前序文档：`docs/analyses/loader_refactor_audit.md`（v1，本文以"v1 §n"引用其章节）
- 性质：**只读修订**。本次未修改任何 Python/测试/配置文件，唯一新建文件为本报告。
- 证据基础：全部结论基于真实源码行号；Python 导入机制关键点已用**标准库内存沙箱实验**逐项验证（见附录 A，实验中未创建任何文件）；项目虚拟环境当前损坏导致耗时类数据无法实测的项目，全部标注 **（需要动态验证）** 并附复测命令（附录 C）。

---

## 目录

1. [原方案（v1）存在的问题](#1-原方案v1存在的问题)
2. [修正后的目标目录结构](#2-修正后的目标目录结构)
3. [门面（facade）设计方案](#3-门面facade设计方案)
4. [延迟导入设计](#4-延迟导入设计)
5. [公共接口兼容方案](#5-公共接口兼容方案)
6. [各场景导入链路图](#6-各场景导入链路图)
7. [函数全局变量与 monkeypatch 兼容性分析](#7-函数全局变量与-monkeypatch-兼容性分析)
8. [修订后的分阶段迁移计划](#8-修订后的分阶段迁移计划)
9. [风险和测试方案](#9-风险和测试方案)
- [附录 A：导入机制沙箱实验记录](#附录-a导入机制沙箱实验记录)
- [附录 B：环境实测记录（venv 损坏证据）](#附录-b环境实测记录venv-损坏证据)
- [附录 C：待动态验证项与复测命令](#附录-c待动态验证项与复测命令)

---

## 1. 原方案（v1）存在的问题

### 1.1 v1 没有真正解决重依赖传染（核心缺陷，自我矛盾）

v1 §5-P3 已把"import loader 即付出全部代价"列为问题，但 v1 §6.2 设计的门面却要求 `loader/__init__.py` "**re-export 全部历史公共名（含 HAS_*、FORMAT_LOADER_MAP、四个快照全局量）**"。按 Python 导入机制，这一行要求直接判了懒加载死刑：

- `HAS_PYMUPDF / HAS_PDFPLUMBER / PYMUPDF_GET_TABLES_AVAILABLE` 等标志的值**产生于 compat.py 的模块体执行**（原 loader.py L38–132 的 try-import 与 `pymupdf.open()` 探测）。要在门面 re-export 它们，门面必须**导入期**执行 `from .compat import ...` → pymupdf、pdfplumber 立即加载。
- `FORMAT_LOADER_MAP` 在 v1 中保持"后缀 → 函数对象"形态（原 L1499–1505），5 个值是对 `load_pdf` 等函数的**直接引用** → 门面必须在导入期 import 全部 5 个解析器模块 → 全部格式的重库被拉满。
- 结论：v1 拆分后 `from app.ingestion.loader import compute_file_hash` 的导入足迹**与拆分前完全相同**。v1 只解决了"文件组织"，没有解决"加载时机"。

### 1.2 v1 完全遗漏了父包 `app/ingestion/__init__.py` 这一层传染源

真实代码（`backend/app/ingestion/__init__.py` L11–19）是**急切三连导入**：

```python
from app.ingestion.loader import (Document, compute_file_hash, load_all_documents, load_file, scan_knowledge_base)
from app.ingestion.chunking import Chunk, chunk_all, chunk_document, chunks_to_json
from app.ingestion.indexer import ProcessedCache, process_file, run_ingest
```

Python 导入任何子模块（包括 `app.ingestion.loader`）都会**先执行父包 `__init__.py`**（语言保证，见附录 A-G 与设计依据）。因此即使 loader 门面做到零重导入，第三行仍会把：

```
indexer.py → app.vector.qdrant（qdrant.py L19-20 qdrant_client；L40 embedding → requests；
             L41 version_visibility → sqlalchemy + app.models.document → app.models.base(sqlalchemy)；
             L47-51 jieba try-import；observability.langfuse 顶层轻量、SDK 延迟）
```

全部拉进任何触碰 `app.ingestion.*` 的进程。**"把 compute_file_hash 移进 utils.py 就解决传染"确实不成立**——传染源有两层（loader 自身 + 父包），必须两层同时治理。这是 v2 与 v1 最大的结构差异：`app/ingestion/__init__.py` 本身也要薄化为 PEP 562 懒门面。

### 1.3 v1 的 compat.py 是"单一能力块"，任何解析都拉全部探测

v1 §6.2 让所有解析器共享一个 `compat.py`（pymupdf+fitz+Rect+pdfplumber+pandas+openpyxl+docx+pytesseract+PIL 的探测全在一个模块体里）。后果：解析一个 TXT 也会连带执行 PyMuPDF 的 `pymupdf.open()` 能力探测（原 L110–132）并加载 pandas/openpyxl 的可用性检测。**capabilities 必须按域拆分**（pdf / docx / xlsx / ocr 各自独立、互不导入），v2 以 `_compat/` 子包落实。

### 1.4 v1 低估了 `__globals__` 绑定陷阱，且未选"最简可靠"的解法

v1 §6.3 为保 `test_versioned_indexing.py` L59 的 monkeypatch，给出"scanner.py 函数体内延迟读取包属性"方案。该方案**可行**（沙箱实验 D 证实），但：

- 它把正确性押在"实施者永远不把函数内 import 优化成模块顶层 import"上——沙箱实验 E 证实：**子模块顶层 `from 门面 import 配置量` 会冻结副本，monkeypatch 门面对其完全失效**，且失败是静默的（测试之外无任何报错面）。
- 存在更简单的解法 v1 没有选：**读配置全局量的 3 个函数（`load_file`/`scan_knowledge_base`/`load_all_documents`，合计约 90 行）干脆不搬离门面**。函数与全局量同处一个模块字典时，`func.__globals__ is facade.__dict__`（沙箱实验 A/C 证实），monkeypatch 语义与今天的单文件 loader.py **逐位等价**，零延迟绑定魔法、零脆弱模式。职责纯度上"门面稍厚"是明确可接受的交换（这 3 个函数本就是"包入口路由"职责）。

### 1.5 v1 未发现"日志器名称漂移"这一可观测行为差异

`app/core/logging.py` L25 文本格式含 `%(name)s`，L46 JSON 格式含 `"logger": record.name`。今天全部解析日志都在单一 logger `"app.ingestion.loader"`（loader.py L143）名下；v1 按自然写法在各子模块 `logging.getLogger(__name__)` 会使日志记录里的 logger 名变成 `app.ingestion.loader.pdf_loader` 等——**消息文本不变，但日志行的 name 字段、ELK/Loki 的 logger 维度全部漂移**，违反 v1 自己定下的"逐字节不变"验收标准，v1 却未提及。v2 修正：共享固定名 logger（§3.4）。

### 1.6 v1 的 Phase 排序在"导入减负"维度失效

v1 把 PDF 集群放最后（Phase 6）以控风险，方向没错；但因 1.1–1.3 的缺陷，v1 全程没有任何一个阶段能兑现"轻量导入"收益。v2 重排后（§8）：**Phase 2（父包薄化）即兑现"摆脱向量层传染"**，Phase 4–6 逐域兑现"解析库按需加载"，每个阶段的收益可独立验收。

### 1.7 v1 假设验证门禁可直接执行——实测环境已损坏

v1 §9 Phase 0 要求跑 `pytest`/`check.ps1` 建立基线。实测（附录 B）：仓库内 `.venv` 与 `backend/.venv` 的基础解释器均指向**已不存在**的 `C:\Users\25160\.conda\envs\local_env\python.exe`（venv 启动器直接报 `No Python at ...`；root `.venv/pyvenv.cfg` 记录 `home = C:\Users\25160\.conda\envs\local_env`）。系统 PATH 上的 Python 3.13 无项目依赖。**任何迁移阶段开工前必须先重建虚拟环境**（`uv sync` 或按 AGENTS.md 重装），否则全部验收门禁都是空谈。此为 v2 的 Phase -1 前置项。

### 1.8 v1 保留但需要修正归属的一个细节

v1 把 `_try_ocr_page`（PDF 页 OCR，用到 `pymupdf.Matrix` + `page.get_pixmap`，L669–685）与 `_ocr_image_bytes`（图片字节 OCR，只用 PIL+pytesseract，L1089–1113）一并放进 `ocr.py`。若 `ocr.py` 顶层为满足 `_try_ocr_page` 而导入 `pymupdf`，则 **DOCX 图片 OCR 路径会无辜拉入 PyMuPDF**。v2 修正：`_try_ocr_page` 归还 PDF 域（`pdf_page.py`，它本质是"页面渲染操作恰好调用 tesseract"），`ocr.py` 只承载与格式无关的 `_ocr_image_bytes`；pytesseract/PIL 的可用性探测统一放 `_compat/ocr.py`，两域共享（§2、§4.3）。

### v1 中依旧成立、v2 全盘继承的部分

- `loader.py → loader/` 包化保持导入路径不变（`app.ingestion.loader` 对 5 个既有导入方零改动，v1 §4.1 清单仍有效）；
- 按格式纵切 + `base.py` 沉底 + 门面 re-export 总框架；
- golden-master 逐字节门禁（Qdrant 点 ID 由 text+source 派生，test_versioned_indexing L36–43）；
- 缺陷分轨原则：P1（marker 块在页循环内，原 L905–928）、P2（pdfplumber 每页重开整文件，原 L246×L818）等**不在结构迁移中修**；
- 死代码/死参数原样随迁、清理另立 PR。

---

## 2. 修正后的目标目录结构

```
backend/app/ingestion/
├── __init__.py                 # ★v2 修订点①：由急切三连导入（L11-19）改为 PEP 562 懒门面
│                               #    _LAZY_ATTRS 映射 12 个历史公共名 → 所属子模块；__all__ 保持不变
├── loader/                     # 原 loader.py 转包；导入路径 app.ingestion.loader 不变
│   ├── __init__.py             # ★门面（目标 ~200 行，顶层导入仅 stdlib + settings + base）：
│   │                           #    ① 4 个 settings 快照全局量（原 L137-141 定义点不动）
│   │                           #    ② FORMAT_LOADER_MAP（保持 ext→callable 形态，值改为懒闭包）
│   │                           #    ③ load_file / scan_knowledge_base / load_all_documents（原文迁移）
│   │                           #    ④ _LAZY_ATTRS + __getattr__/__dir__（PEP 562 懒 re-export）
│   │                           #    ⑤ TYPE_CHECKING 静态视图（IDE/mypy 可见性）
│   ├── __main__.py             # 保留 `python -m app.ingestion.loader` 行为（原 L1586-1592 等价物）
│   ├── base.py                 # 轻量内核（stdlib only）：Document、compute_file_hash、
│   │                           #    _data_to_markdown_table、documents_to_json/from_json、
│   │                           #    ★固定名共享 logger = logging.getLogger("app.ingestion.loader")
│   ├── _compat/
│   │   ├── __init__.py         # 空（仅 docstring）；_compat 各域文件互不导入
│   │   ├── pdf.py              # warnings 过滤器(原 L33-34) → pymupdf/fitz 别名(L38-48) → Rect(L51-57,
│   │   │                       #    死导入原样保留) → pdfplumber+版本(L80-89) → get_tables 探测(L110-132)
│   │   ├── docx.py             # python-docx 探测（原 L66-71）
│   │   ├── xlsx.py             # openpyxl + pandas 探测（原 L59-64、L73-78）
│   │   └── ocr.py              # pytesseract + PIL 探测（原 L91-108）；未安装时 None 归一化绑定（§5.4）
│   ├── ocr.py                  # 仅 _ocr_image_bytes（格式无关的图片字节 OCR）
│   ├── pdf_page.py             # preprocess_page / detect_columns / reorder_text_blocks_by_column /
│   │                           #    extract_logical_page_number / ★_try_ocr_page（v1→v2 归属修正，§1.8）
│   ├── pdf_tables.py           # extract_tables_with_pdfplumber / with_get_tables / with_dict /
│   │                           #    extract_tables_from_page / _detect_likely_table_pdf
│   ├── pdf_marker.py           # _convert_pdf_with_marker（subprocess 隔离带）
│   ├── pdf_loader.py           # load_pdf（函数体逐字不动；USE_MARKER_FOR_PDF 域内快照，§7.4）
│   ├── docx_loader.py          # load_docx / _extract_docx_images
│   ├── xlsx_loader.py          # load_xlsx / _load_xlsx_openpyxl / _load_xlsx_pandas
│   ├── xlsx_summary.py         # _SUMMARY_MIN_ROWS / _HEADER_SYNONYMS / _parse_number /
│   │                           #    _is_sequence_col / _build_sheet_summary（纯 stdlib）
│   └── text_loader.py          # load_txt / load_md（只依赖 base，轻域直连导入，见 §4.2）
├── chunking/                   # 不动
└── indexer.py                  # 不动（其顶层 from app.ingestion.loader import ... 全部命中门面真实属性）
```

与 v1 §6.2 的差异汇总：

| 差异点 | v1 | v2 | 动因 |
|--------|----|----|------|
| 父包 `app/ingestion/__init__.py` | 不动 | **改为 PEP 562 懒门面** | §1.2 双层传染源 |
| compat | 单文件，被门面/解析器共享 | `_compat/` 域拆分，只被对应域导入 | §1.3 按需 capabilities |
| `registry.py`/`scanner.py` | 独立子模块 + 函数内延迟绑定门面属性 | **并入门面**（`load_file`/`scan_knowledge_base`/`load_all_documents` 与配置全局量同字典） | §1.4 `__globals__` 最简可靠解 |
| OCR 归组 | `_try_ocr_page`+`_ocr_image_bytes` 同在 ocr.py | `_try_ocr_page` 归 `pdf_page.py`；ocr.py 只放 `_ocr_image_bytes` | §1.8 避免 docx-OCR 拉 pymupdf |
| logger | 未处理（默认各子模块 `__name__`） | base.py 固定名共享 logger | §1.5 日志逐字节不变 |
| `HAS_*`/库绑定名的门面暴露 | 隐式要求 eager re-export（致命） | PEP 562 懒表条目（首次 getattr 才触发对应 `_compat` 域加载） | §1.1 |

---

## 3. 门面（facade）设计方案

> 以下代码为**设计示意**（实施阶段落地用），本次审计未写入任何项目文件。

### 3.1 `app/ingestion/loader/__init__.py`（包门面）

设计取舍：**"轻的直连、重的懒、读配置的留在门面"**。

```python
# -*- 目标形态骨架（~200 行） -*-
"""loader 包门面。
契约（勿破坏，均有测试/生产代码依赖，见 architecture_v2 §5/§7）：
1. KNOWLEDGE_BASE_DIR/SUPPORTED_EXTENSIONS/PDF_SUBDIR/USE_MARKER_FOR_PDF 四个 settings
   快照定义在本模块。tests/test_versioned_indexing.py L59 直接 monkeypatch
   loader.KNOWLEDGE_BASE_DIR，依赖 scan_knowledge_base.__globals__ 即本模块字典。
2. load_file/scan_knowledge_base/load_all_documents 必须物理定义在本模块（同上原因）。
3. 任何子模块不得在顶层 from 本门面 import 配置量（会冻结副本、patch 失效，见 v2 §7.3 实验E）。
"""
import importlib
import logging
from typing import Any, Dict, Tuple

from app.core.config import settings

# 轻内核：stdlib only，导入零成本；logger 固定名保证日志 name 字段与单文件时代逐字节一致
from app.ingestion.loader.base import (
    Document,
    compute_file_hash,
    documents_from_json,
    documents_to_json,
    logger,
)

# ---- settings 导入期快照（原 loader.py L137-141 语义与定义点保持） ----
KNOWLEDGE_BASE_DIR = settings.knowledge_base_dir
SUPPORTED_EXTENSIONS = settings.supported_extensions
PDF_SUBDIR = settings.pdf_subdir
USE_MARKER_FOR_PDF = settings.use_marker_for_pdf

# ---- 懒分发路由：ext → (模块路径, 函数名)。重域解析器一律懒；轻域(text)也走同一路由保持机制统一 ----
_LOADER_ROUTES: Dict[str, Tuple[str, str]] = {
    ".txt":  ("app.ingestion.loader.text_loader", "load_txt"),
    ".md":   ("app.ingestion.loader.text_loader", "load_md"),
    ".docx": ("app.ingestion.loader.docx_loader", "load_docx"),
    ".xlsx": ("app.ingestion.loader.xlsx_loader", "load_xlsx"),
    ".pdf":  ("app.ingestion.loader.pdf_loader",  "load_pdf"),
}

def _make_lazy_loader(module_path: str, func_name: str):
    """生成与原 FORMAT_LOADER_MAP 值同形态的 callable：调用时才 import 解析器。"""
    def _lazy_load(file_path):
        fn = getattr(importlib.import_module(module_path), func_name)
        return fn(file_path)
    _lazy_load.__name__ = func_name
    _lazy_load.__qualname__ = func_name
    return _lazy_load

# 形态保持：ext → callable（原 L1499-1505 的消费方式 loader(file_path) 完全不变）
FORMAT_LOADER_MAP = {ext: _make_lazy_loader(m, f) for ext, (m, f) in _LOADER_ROUTES.items()}

def load_file(file_path):          # 原 L1508-1517 原文迁移（读到的 SUPPORTED_EXTENSIONS/
    ...                            # FORMAT_LOADER_MAP 均在本模块字典 → monkeypatch 语义不变）

def scan_knowledge_base():         # 原 L1520-1543 原文迁移（同上，__globals__ 即本模块字典）
    ...

def load_all_documents():          # 原 L1546-1568 原文迁移
    ...

# ---- PEP 562 懒 re-export：覆盖"原模块属性全集"，首次访问才触发对应子模块加载 ----
_LAZY_ATTRS: Dict[str, str] = {
    # 五个格式解析器（历史上是 loader 模块级公开名）
    "load_pdf":  "app.ingestion.loader.pdf_loader",
    "load_docx": "app.ingestion.loader.docx_loader",
    "load_xlsx": "app.ingestion.loader.xlsx_loader",
    "load_txt":  "app.ingestion.loader.text_loader",
    "load_md":   "app.ingestion.loader.text_loader",
    # PDF 域辅助（历史公开名 + 私有名，保持 getattr 全集兼容）
    "preprocess_page": "app.ingestion.loader.pdf_page",
    "detect_columns": "app.ingestion.loader.pdf_page",
    "reorder_text_blocks_by_column": "app.ingestion.loader.pdf_page",
    "extract_logical_page_number": "app.ingestion.loader.pdf_page",
    "_try_ocr_page": "app.ingestion.loader.pdf_page",
    "extract_tables_from_page": "app.ingestion.loader.pdf_tables",
    "extract_tables_with_pdfplumber": "app.ingestion.loader.pdf_tables",
    "extract_tables_with_get_tables": "app.ingestion.loader.pdf_tables",
    "extract_tables_with_dict": "app.ingestion.loader.pdf_tables",
    "_detect_likely_table_pdf": "app.ingestion.loader.pdf_tables",
    "_convert_pdf_with_marker": "app.ingestion.loader.pdf_marker",
    "_data_to_markdown_table": "app.ingestion.loader.base",
    "_extract_docx_images": "app.ingestion.loader.docx_loader",
    "_ocr_image_bytes": "app.ingestion.loader.ocr",
    "_load_xlsx_openpyxl": "app.ingestion.loader.xlsx_loader",
    "_load_xlsx_pandas": "app.ingestion.loader.xlsx_loader",
    "_parse_number": "app.ingestion.loader.xlsx_summary",
    "_is_sequence_col": "app.ingestion.loader.xlsx_summary",
    "_build_sheet_summary": "app.ingestion.loader.xlsx_summary",
    "_SUMMARY_MIN_ROWS": "app.ingestion.loader.xlsx_summary",
    "_HEADER_SYNONYMS": "app.ingestion.loader.xlsx_summary",
    # capabilities 标志与探测值（归属对应 _compat 域；访问即触发该域库加载，语义与今天一致）
    "HAS_PYMUPDF": "app.ingestion.loader._compat.pdf",
    "HAS_PDFPLUMBER": "app.ingestion.loader._compat.pdf",
    "_pdfplumber_version": "app.ingestion.loader._compat.pdf",
    "PYMUPDF_GET_TABLES_AVAILABLE": "app.ingestion.loader._compat.pdf",
    "HAS_DOCX": "app.ingestion.loader._compat.docx",
    "HAS_OPENPYXL": "app.ingestion.loader._compat.xlsx",
    "HAS_PANDAS": "app.ingestion.loader._compat.xlsx",
    "HAS_TESSERACT": "app.ingestion.loader._compat.ocr",
    "HAS_PIL": "app.ingestion.loader._compat.ocr",
    # 历史模块级绑定的库对象名（原 L38-108 产物；零消费方，仅保 getattr 全集）
    "pymupdf": "app.ingestion.loader._compat.pdf",
    "Rect": "app.ingestion.loader._compat.pdf",
    "pdfplumber": "app.ingestion.loader._compat.pdf",
    "pd": "app.ingestion.loader._compat.xlsx",
    "openpyxl": "app.ingestion.loader._compat.xlsx",
    "DocxDocument": "app.ingestion.loader._compat.docx",
    "pytesseract": "app.ingestion.loader._compat.ocr",
    "Image": "app.ingestion.loader._compat.ocr",
}

def __getattr__(name: str) -> Any:          # PEP 562（Python≥3.7；本项目 requires-python>=3.12）
    target = _LAZY_ATTRS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(target), name)

def __dir__():
    return sorted(set(globals()) | set(_LAZY_ATTRS))

__all__ = [  # 与 v1 兼容面一致
    "Document", "compute_file_hash", "load_file", "scan_knowledge_base", "load_all_documents",
    "load_pdf", "load_docx", "load_xlsx", "load_txt", "load_md",
    "documents_to_json", "documents_from_json", "FORMAT_LOADER_MAP",
    "KNOWLEDGE_BASE_DIR", "SUPPORTED_EXTENSIONS", "PDF_SUBDIR", "USE_MARKER_FOR_PDF",
]

# 静态分析可见性（运行时不执行，导入零成本）
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from app.ingestion.loader.pdf_loader import load_pdf
    from app.ingestion.loader.docx_loader import load_docx
    # ...（与 _LAZY_ATTRS 同步；供 IDE/mypy/pyright 解析）
```

要点说明：

1. **`__getattr__` 只做转发、不缓存**（不写回 `globals()`）。缓存会把"转发视图"变成"冻结副本"，一旦有人 patch 子模块属性将出现门面/子模块两个真相；不缓存时每次访问是 `sys.modules` 字典查找 + `getattr`，成本可忽略（沙箱 B/G 证实机制正确、真实属性优先于 `__getattr__`）。
2. **四个配置快照与三个入口函数物理驻留门面**——这不是妥协，而是把 v1 的"延迟绑定契约"降维成"`__globals__` 同字典"的语言事实（§7）。
3. `_LAZY_ATTRS` 覆盖**原模块属性全集**（含私有下划线名与库绑定名），保证 `getattr(loader, 任意历史名)` 语义连续；代价是访问 `HAS_*`/`pymupdf` 等会触发对应域真实加载——这与今天"导入 loader 即加载"相比只会更晚、不会更早，方向安全。

### 3.2 `app/ingestion/__init__.py`（父包薄化，修订点①）

```python
"""ingestion - 入库领域：解析(loader) → 分块(chunking) → 索引(indexer)。（docstring 保留原文并补充懒加载说明）"""
import importlib
from typing import Any

_LAZY_ATTRS = {
    "Document": "app.ingestion.loader",
    "compute_file_hash": "app.ingestion.loader",
    "load_file": "app.ingestion.loader",
    "scan_knowledge_base": "app.ingestion.loader",
    "load_all_documents": "app.ingestion.loader",
    "Chunk": "app.ingestion.chunking",
    "chunk_all": "app.ingestion.chunking",
    "chunk_document": "app.ingestion.chunking",
    "chunks_to_json": "app.ingestion.chunking",
    "ProcessedCache": "app.ingestion.indexer",
    "process_file": "app.ingestion.indexer",
    "run_ingest": "app.ingestion.indexer",
}
__all__ = [ ... 与原 L21-25 完全一致的 12 项 ... ]

def __getattr__(name: str) -> Any:
    target = _LAZY_ATTRS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(target), name)

def __dir__():
    return sorted(set(globals()) | set(_LAZY_ATTRS))
```

影响面核查（全仓 grep 证据）：

- `from app.ingestion import indexer`（test_versioned_indexing L5）、`from app.ingestion import loader`（L56）——**子模块形式导入不经过 `__getattr__`**，由 import 机制直接加载，行为不变；
- `from app.ingestion.chunking import ...`（indexer L31、test_chunking L3 等）——同上不受影响；
- `from app.ingestion import Document/load_file` 这类**名字形式**导入：grep 全仓（backend/、scripts/、eval/）**当前无生产代码使用**，但属公开契约（原 `__all__`），PEP 562 完整保留（沙箱 B 证实 from-import 触发 `__getattr__`）；
- 无模块依赖"导入 `app.ingestion` 包产生的预载副作用"（grep 无 `import app.ingestion$` 式用法）。
- 附带收益：`test_chunking.py` 今天经父包 init 连带加载 indexer+qdrant_client+sqlalchemy+jieba；薄化后 chunking 单测进程足迹显著缩小（需要动态验证量化）。

### 3.3 `base.py`（轻内核 + 固定名 logger，修订点②）

内容 = v1 base.py（Document/compute_file_hash/_data_to_markdown_table/序列化两函数）+ 一行关键新增：

```python
import logging
# 固定为原单文件模块的 logger 名（loader.py L143 的 __name__ 值），
# 保证 core/logging.py L25/L46 输出中的 name/logger 字段与重构前逐字节一致。
logger = logging.getLogger("app.ingestion.loader")
```

全部子模块 `from app.ingestion.loader.base import logger`，不再各自 `getLogger(__name__)`。依赖仅 stdlib（hashlib/json/logging/dataclasses/typing）。`_data_to_markdown_table` 的入参原地变异行为（原 L219–221，v1 P10）**原样保留**。

### 3.4 `_compat/` 域文件（capabilities 按需加载，修订点③）

- `_compat/pdf.py` = 原 L33–57 + L80–89 + L110–132 **原文剪切**（顺序敏感：`warnings.filterwarnings` 必须先于 pymupdf 导入，与原文件内相对顺序一致）。仅被 `pdf_page/pdf_tables/pdf_loader` 顶层导入；`pytesseract` 相关部分不放这里（OCR 域自治）。
- `_compat/docx.py` = 原 L66–71；仅被 `docx_loader` 导入。
- `_compat/xlsx.py` = 原 L59–64 + L73–78；仅被 `xlsx_loader` 导入。（openpyxl 与 pandas 同文件同时探测：pandas 降级路径虽然近乎死路，但拆出独立懒探测属于过度设计——两者都是 pyproject 硬依赖，见 v1 §5-P5，简化决策记档。）
- `_compat/ocr.py` = 原 L91–108；被 `ocr.py` 与 `pdf_page.py`（`_try_ocr_page` 需要 `HAS_TESSERACT`）导入。
- **None 归一化**（唯一刻意的小改写）：各域探测失败时把库名绑定为 `None`（今天的行为是"名字不绑定"）。原因：子模块需要 `from ..._compat.ocr import pytesseract` 这种顶层导入，而名字缺失会使 import 语句本身炸掉。安全性论证：所有使用点都在 `HAS_*` 守卫之后（L671→L678、L1096→L1109 等），守卫先返回，`None` 永远不会被解引用 → 运行时行为不变。差异仅在 `hasattr` 视角：今天库缺失时 `hasattr(loader, "pytesseract")` 为 False，v2 经懒表访问得到 `None`（触发 `_compat.ocr` 加载）。零消费方（grep），记入兼容矩阵（§5.4）。

---

## 4. 延迟导入设计

### 4.1 三层懒加载总体结构

| 层 | 机制 | 触发时机 | 解决什么 |
|----|------|---------|---------|
| L1 父包 | `app/ingestion/__init__.py` PEP 562 `__getattr__` | 首次访问 `Document/process_file/...` 等名字 | 切断 loader→indexer→qdrant/sqlalchemy/requests/jieba 传染（v1 §1.2 缺口） |
| L2 loader 门面 | `__getattr__` 懒表 + `FORMAT_LOADER_MAP` 懒闭包（`importlib.import_module` 于**调用时**） | 首次 `load_file(某格式)` / 首次 getattr 某解析器名 | 门面导入足迹 = stdlib + settings + base；5 个解析器域互不连带 |
| L3 函数内定向懒 | ① `docx_loader` 在图片处理段函数内 `from ...loader.ocr import _ocr_image_bytes`；② `load_pdf` 的 OCR 分支所在的 `pdf_page._try_ocr_page` 经顶层 `_compat.ocr` 引用（同域同车，不再函数内懒） | DOCX 首次实际处理图片时 | pytesseract/PIL 不进"无图文档"的解析足迹 |

明确**不采用**的机制（均为过度设计或引入新风险）：

- 第三方懒导入库（`lazy_loader`/`lazy-imports`）——新依赖，违背"不装依赖"约束；
- meta_path finder / import hook 魔法——不可调试；
- `importlib.metadata` 探测代替 try-import——语义不同（已安装≠可导入），改变 HAS_* 判定口径；
- 对 `pandas` 单独函数内懒加载——fallback 路径近乎死路（pyproject 硬依赖），复杂度不划算。

### 4.2 懒 vs 直连的判据

- **重域（pdf/docx/xlsx）**：`FORMAT_LOADER_MAP` 懒闭包 + `_LAZY_ATTRS` 懒表，门面顶层绝不 import。
- **轻域（text_loader/xlsx_summary/base）**：允许门面或域内**直连顶层导入**（stdlib only，导入成本≈0），不必强行懒化——"能懒则懒"让位于"简单可靠"。
- **域内**：同域子模块间（如 pdf_loader→pdf_page/pdf_tables/pdf_marker）用普通顶层导入（它们必然同生共死，懒化无收益）；跨域仅两处：xlsx_loader→xlsx_summary（轻，顶层直连）、docx_loader→ocr（函数内懒）。

### 4.3 首次调用的线程安全性与时延

- `importlib.import_module` 走标准 import 机制，自带 per-module 锁，多线程并发首解析安全。
- worker 侧 `process_file` 经 `asyncio.to_thread` 执行（ingest.py L53），首解析的一次性冷导入发生在**线程池内**，不阻塞事件循环；ARQ `max_jobs=1`（docs/wiki/06 L48），并发触发概率≈0。
- 冷导入具体时延无法在本环境实测（venv 损坏，附录 B）——**需要动态验证**，复测命令见附录 C 第 2 条。预期量级仅为"把今天启动时的一次性成本移到首次解析"，总成本不增。

### 4.4 导入期副作用的时点迁移（如实记档）

| 副作用 | 今天 | v2 | 可观测差异 |
|--------|------|----|-----------|
| `warnings.filterwarnings`×2（L33–34） | 任何进程 import loader 时 | 首次进入 PDF 域时；过滤器仍在 pymupdf 导入**之前**执行（_compat/pdf.py 内顺序保留） | 被抑制的告警只可能来自 pymupdf 自身，而 pymupdf 仅经 _compat/pdf.py 导入（grep 全仓唯一导入点）→ 无可观测差异 |
| `pymupdf.open()` 能力探测（L116） | import 时执行（创建/关闭空文档） | 首次 PDF 解析时执行 | 探测**结果**不变；执行时点后移。探测块自身的 `except Exception` 包裹原样保留。今天探测大概率走版本回退分支的问题（v1 §5-P3）仍按"缺陷分轨"原则不修 |
| jieba 告警检查（qdrant.py L53–54） | import app.ingestion 即触发 | 首次触碰 indexer/qdrant 时 | 与 loader 无关，父包薄化的附带效果，告警文本与时点记档 |

---

## 5. 公共接口兼容方案

### 5.1 任务书指定的兼容路径逐项核对

| # | 兼容路径 | v2 机制 | 依据 |
|---|---------|--------|------|
| 1 | `from app.ingestion.loader import Document` | 门面顶层**真实**导入 base（轻，刻意 eager） | 沙箱 G：真实属性不走 `__getattr__`，零魔法 |
| 2 | `from app.ingestion.loader import compute_file_hash` | 同上 | ingestion_service L21 直接命中 |
| 3 | `from app.ingestion.loader import load_file` | 门面内**物理定义**的真实函数 | `__globals__` 同字典（§7） |
| 4 | `from app.ingestion.loader import scan_knowledge_base` | 门面内物理定义 | test_versioned_indexing L65 |
| 5 | `from app.ingestion import Document` | 父包 PEP 562 `__getattr__` → 转发 loader 门面 → base | 沙箱 B：from-import 触发 `__getattr__` |
| 6 | `from app.ingestion import load_file` | 同上 | 同上 |
| 7 | `loader.KNOWLEDGE_BASE_DIR = some_path` 后 `loader.scan_knowledge_base()` | 赋值命中门面真实全局量；scan 的 `__globals__` 就是门面字典，调用时读取 | 沙箱 A/C：`facade_fn.__globals__ is pkg.__dict__ == True`；patch 后读到新值 |

第 7 条即 `test_versioned_indexing.py::test_legacy_scanner_cannot_republish_managed_version_files`（L56–65，`monkeypatch.setattr(loader, "KNOWLEDGE_BASE_DIR", tmp_path)`）的精确等价物：**测试零改动通过**。注意 `monkeypatch.setattr` 要求属性预先存在（默认 `raising=True`）——四个配置量是门面顶层真实定义，非懒表条目，满足该前提（这也是"配置量必须是真实属性而非 `__getattr__` 条目"的硬规则来源）。

### 5.2 生产代码导入方（v1 §4.1 的 5 处）逐一核对

| 导入方 | 语句 | v2 命中路径 | 改动 |
|--------|------|------------|------|
| `app/ingestion/__init__.py` | `from app.ingestion.loader import Document, compute_file_hash, load_all_documents, load_file, scan_knowledge_base` | 父包自身改为懒表后，这些名字改为首次访问转发；语义等价 | 父包文件本身重写（修订点①），`__all__` 不变 |
| `app/ingestion/indexer.py` L25–30 | `from app.ingestion.loader import load_file, compute_file_hash, scan_knowledge_base, Document` | 四者均为门面**真实**属性（eager） | **零改动**；且 test L15 `monkeypatch.setattr(indexer, "load_file", ...)` 语义保持（indexer 模块字典里的名字绑定，与 loader 无关） |
| `app/services/ingestion_service.py` L21 | `from app.ingestion.loader import compute_file_hash` | 门面真实属性（base eager） | **零改动**；且本链路（API 上传）自此不再连带任何解析库 |
| `app/worker/tasks/versioned_ingest.py` L8 | 同上 | 同上 | **零改动** |
| `tests/test_versioned_indexing.py` L5/L56 | `from app.ingestion import indexer` / `from app.ingestion import loader` | 子模块形式，不经 `__getattr__` | **零改动**（含 L58–59 双 monkeypatch、L65 调用） |

### 5.3 次级兼容面（历史 `getattr` 全集）

- `loader.load_pdf / load_docx / ...` 及全部私有函数名、`HAS_*`、库绑定名：`_LAZY_ATTRS` 兜底（§3.1 清单），grep 证实当前**零外部消费方**，属防御性兼容。
- `FORMAT_LOADER_MAP`：保持"`ext → callable(路径) → List[Document]`"消费形态。**唯一理论破坏面**：值从真函数变为懒闭包，`FORMAT_LOADER_MAP[".pdf"] is load_pdf` 这类同一性断言不再成立；grep 全仓无任何代码读取该表（v1 §4.3），闭包补齐 `__name__/__qualname__` 保证日志/调试观感。
- `python -m app.ingestion.loader`：包化后模块 `__main__` 块失效，由 `loader/__main__.py`（3 行：调用 `load_all_documents()` 并打印，原 L1586–1592 等价）保留入口。grep 确认无脚本/CI 引用该入口，属低成本兜底。
- `loader.logger`：门面保留真实属性（绑定 base 的固定名 logger，同一对象）。
- 函数签名：全部不变（`load_file(file_path)`、`scan_knowledge_base()`、`compute_file_hash(file_path)` 等；懒闭包 `_lazy_load(file_path)` 与透传调用等价）。pickle 按引用序列化函数的场景不存在（ARQ 入队用字符串任务名，ingestion_service L114；无 multiprocessing 使用 loader 函数）。

### 5.4 如实记档的行为微差（均零消费方，静态证据充分）

| 微差 | 今天 | v2 | 影响评估 |
|------|------|----|---------|
| 库缺失时的名字存在性 | `pytesseract` 等名字不绑定（hasattr False） | `_compat` 域内 `None` 归一化，经懒表可得 `None` | 使用点全在 `HAS_*` 守卫后（L671/L1096 等），不可达差异；grep 零消费方 |
| `load_pdf.__module__` 等元数据 | `"app.ingestion.loader"` | `"app.ingestion.loader.pdf_loader"` | 无代码消费；日志 name 字段已由固定 logger 解决（§3.3） |
| `HAS_*`/`USE_MARKER_FOR_PDF` 的 patch 落点 | 均可 patch `loader.<NAME>` 生效 | 配置四量仍可（门面真实属性）；`HAS_*` patch 落点移至 `_compat.<域>.<NAME>`（门面懒表是只读转发视图）；`USE_MARKER_FOR_PDF` 门面副本保留、生效副本在 `pdf_loader`（§7.4） | grep：三者均无任何测试/生产 patch；矩阵记档，规则写入门面 docstring |
| warnings 过滤器/get_tables 探测触发时点 | import 时 | 首次 PDF 解析时 | §4.4，无可观测输出差异 |

---

## 6. 各场景导入链路图

> 口径定义：**冷进程**从零执行该场景操作，最终 `sys.modules` 中的项目模块与第三方顶层库集合。今天（单文件 loader.py + 急切父包）六个场景的足迹**完全相同**，先给基线，再给 v2 对照。

### 6.0 今天（基线）：六个场景共用的加载链

```
任何 `from app.ingestion...import 任意名` 语句
 ├─ app/__init__.py                       (docstring only)
 ├─ app/ingestion/__init__.py             ★急切三连导入（L11-19）
 │   ├─ app.ingestion.loader（单文件全量执行 L1-1592）
 │   │   ├─ warnings 过滤器 ×2（L33-34，进程级副作用）
 │   │   ├─ pymupdf（或 fitz）＋Rect（L38-57）
 │   │   ├─ pandas（L59-64）、python-docx（L66-71）、openpyxl（L73-78）
 │   │   ├─ pdfplumber（L80-89）、pytesseract?（L91-98）、PIL?（L100-108）
 │   │   ├─ pymupdf.open() 能力探测（L110-132，导入期执行）
 │   │   └─ app.core.config → pydantic_settings → pydantic(+pydantic-core)/python-dotenv
 │   ├─ app.ingestion.chunking{base,table,text}（stdlib + config，轻）
 │   └─ app.ingestion.indexer
 │       └─ app.vector.qdrant
 │           ├─ qdrant_client(+models)（传递依赖 httpx 等，版本清单需要动态验证）
 │           ├─ app.vector.embedding → requests ＋ app.core.retry(requests)
 │           │   ＋ app.core.performance(requests, app.core.trace[stdlib])
 │           │   ＋ app.observability.langfuse（顶层轻，SDK 延迟初始化）
 │           ├─ app.vector.version_visibility → sqlalchemy
 │           │   └─ app.models.document → app.models.base(sqlalchemy)
 │           └─ jieba?（try-import，L47-51）
 └─ 目标模块自身
```

即：**今天只 import `compute_file_hash` 也会连带加载 pymupdf、pdfplumber、pandas、openpyxl、python-docx、(pytesseract/PIL 若安装)、qdrant_client、sqlalchemy、requests、jieba(若安装)**，外加两个导入期副作用。这正是 `analytics/schema_infer.py` L14–16 注释吐槽并绕路的事实基础。

### 6.1 场景一：仅导入 `compute_file_hash`

**v2 链路**：

```
from app.ingestion.loader import compute_file_hash
 ├─ app/__init__.py                                    (轻)
 ├─ app/ingestion/__init__.py                          (懒门面，~30 行，无下游导入)
 ├─ app/ingestion/loader/__init__.py                   (轻门面)
 │   ├─ importlib/logging/typing                       (stdlib)
 │   ├─ app.core.config → pydantic_settings/pydantic(pydantic-core)/python-dotenv
 │   └─ app.ingestion.loader.base                      (hashlib/json/logging/dataclasses，stdlib)
 └─ 完成
```

| 维度 | 今天 | v2 |
|------|------|----|
| 项目模块 | 基线全集（含 indexer/qdrant/embedding/version_visibility/models/chunking×3） | app、app.ingestion(薄)、loader(薄)、loader.base、app.core.config |
| 第三方 | pymupdf、pdfplumber、pandas、openpyxl、docx、qdrant_client、sqlalchemy、requests、jieba?、pytesseract?、PIL?、pydantic 系 | **仅 pydantic/pydantic-core/pydantic-settings/python-dotenv**（config 链，无法也无需避免） |
| 导入期副作用 | warnings×2、pymupdf.open() 探测 | 无 |

### 6.2 场景二：仅导入 `Document`

与场景一**完全同链**（Document 在 base.py，门面 eager 导出）。今天/v2 对比同上表。`from app.ingestion import Document` 形式多经过一次父包 `__getattr__` 转发（沙箱 B 验证），足迹不变。

### 6.3 场景三：导入 `load_file`，尚未解析

与场景一同链。关键差异点：`FORMAT_LOADER_MAP` 的值是持有**模块路径字符串**的闭包，构造表本身零导入；`load_file`/`scan_knowledge_base`/`load_all_documents` 物理在门面（stdlib 级）。今天该场景 = 基线全集（因单文件模块体全量执行）。

### 6.4 场景四：实际解析 PDF（首次 `.pdf` 经 `load_file`）

```
load_file(x.pdf) → 懒闭包 importlib.import_module("app.ingestion.loader.pdf_loader")
 ├─ 前置：场景三的轻链（已就位）
 ├─ app.ingestion.loader.pdf_loader
 │   ├─ app.ingestion.loader._compat.pdf   → warnings过滤器→pymupdf、pdfplumber；
 │   │                                        PYMUPDF_GET_TABLES_AVAILABLE 探测（此刻才 pymupdf.open()）
 │   ├─ app.ingestion.loader.pdf_page      → _compat.pdf（sys.modules 命中）＋ _compat.ocr（见下）
 │   ├─ app.ingestion.loader.pdf_tables    → _compat.pdf ＋ base(_data_to_markdown_table)
 │   ├─ app.ingestion.loader.pdf_marker    → stdlib only（shutil/subprocess/sys/tempfile 系）
 │   ├─ app.ingestion.loader._compat.pdf   → 域内 settings 快照 USE_MARKER_FOR_PDF（§7.4）
 │   └─ base.Document / base.logger        （已就位）
 ├─ app.ingestion.loader._compat.ocr       → pytesseract?、PIL?（pdf_page._try_ocr_page 顶层需要
 │                                            HAS_TESSERACT/pytesseract 名字；与 pymupdf 同域同车）
 └─ 运行期（非导入）：need_ocr 页 → tesseract 子进程；marker 兜底 → marker_single 子进程
```

| 集合 | 内容 |
|------|------|
| 新增项目模块 | pdf_loader、pdf_page、pdf_tables、pdf_marker、_compat.pdf、_compat.ocr |
| 新增第三方 | **pymupdf、pdfplumber**；pytesseract/PIL 若安装（`_compat.ocr` 被 pdf_page 顶层引用，见下注） |
| 不加载 | pandas、openpyxl、python-docx、qdrant 层（若宿主进程本来没有） |

> 注（v1→v2 修正的代价说明）：v2 把 `_try_ocr_page` 放 `pdf_page.py`，其顶层需绑定 `HAS_TESSERACT/pytesseract` 名字 → `_compat.ocr` 随 PDF 域一同加载（pytesseract/PIL 的 **import** 成本进入 PDF 场景；tesseract **执行**仍只在 need_ocr 页发生）。若要把 pytesseract/PIL 的 import 也推迟到首个 OCR 页，需把 `_try_ocr_page` 的守卫改为函数内导入——这会改写函数体，违反"逐字迁移"纪律，**本轮不做**，列为后续可选优化（收益小：pytesseract 是薄封装、PIL 中等，需要动态验证实测占比）。

第二次及以后解析 PDF：`importlib.import_module` 直接命中 `sys.modules`，仅字典查找开销。

### 6.5 场景五：实际解析 XLSX（首次）

```
load_file(x.xlsx) → importlib("app.ingestion.loader.xlsx_loader")
 ├─ app.ingestion.loader._compat.xlsx → openpyxl、pandas（双探测原文迁移）
 ├─ app.ingestion.loader.xlsx_summary → stdlib only（纯函数域）
 └─ base（已就位）
```

| 集合 | 内容 |
|------|------|
| 新增第三方 | **openpyxl、pandas** |
| 不加载 | pymupdf、pdfplumber、python-docx、pytesseract、PIL、qdrant 层 |

### 6.6 场景六：执行 `scan_knowledge_base()`

| 维度 | 今天 | v2 |
|------|------|----|
| 新增模块 | 基线全集（导入即全载） | **零新增**（函数在门面，运行期只用 pathlib.glob） |
| 读取全局量 | loader 模块字典 `KNOWLEDGE_BASE_DIR/SUPPORTED_EXTENSIONS/PDF_SUBDIR` | 门面字典同名变量（`__globals__` 同字典，monkeypatch 可见性逐位等价） |

### 6.7 进程级视角汇总（收益的真实边界，不过度声明）

| 进程 | 今天的常驻重依赖 | v2 后 | 说明 |
|------|-----------------|-------|------|
| API（uvicorn） | 全部解析库 + 向量层 + ORM | **解析库全部出局**；向量层保留 | 向量层（qdrant_client/sqlalchemy/requests）是检索路由的固有依赖，与 loader 无关——v2 消除的是"ingestion 父包顺手带进来的那一层"以及全部解析库；API 进程从不解析文件（grep：仅 compute_file_hash），故解析库**永不加载** |
| ARQ worker | 启动即全套解析库（即使队列只有 md） | 按队列内容按需：md/txt 任务不触 pymupdf；首见某格式才载该域 | worker 顶层 import `analytics.seed`（ingest.py L16）仍常驻 openpyxl+sqlalchemy——**既有传染源，超出本方案范围**，记档为后续优化点 |
| CLI indexer | 全套 | 同 worker | |
| analytics（schema_infer 一侧） | 为躲 loader 而重复实现 `_parse_number/_is_sequence_col`（schema_infer L14–16、L51–80） | v2 之后"躲"的必要性消失，但**合并重复实现仍是行为变更**（`￥` 口径漂移，v1 §5-P7），继续分轨处理 | |

---

## 7. 函数全局变量与 monkeypatch 兼容性分析

> 本节回应任务书"特别检查函数的 `__globals__` 与模块级全局变量关系，不能只检查函数是否能够被重新导出"。全部机制结论经内存沙箱实验验证（附录 A，Python 3.13.13 标准库；项目 requires-python>=3.12，PEP 562 自 3.7 起生效，语义一致）。

### 7.1 语言机制基线（实验编号对应附录 A）

- **A**：定义在包 `__init__.py` 里的函数，`fn.__globals__ is 包.__dict__`——函数对全局名的读取是**调用时**对自身 `__globals__` 的字典查找；
- **C**：`setattr(包模块, "CONFIG", new)`（即 `monkeypatch.setattr`）改的就是该字典 → 门面内函数立即读到新值；
- **E（陷阱）**：子模块顶层 `from 门面 import CONFIG` 产生**冻结副本**（绑定进子模块字典）→ 此后 patch 门面**静默失效**；
- **D**：子模块函数体内 `from 门面 import CONFIG`（调用时执行）→ 每次都读门面字典的当前值，patch 可见；
- **B/G**：PEP 562 `__getattr__` 仅拦截**不存在的**属性；真实模块全局量优先，`from 包 import 名字` 正常触发 `__getattr__`。

今天 `test_versioned_indexing.py` L59+L65 能工作的原理正是 A+C：`scan_knowledge_base.__globals__ is loader.py.__dict__`，patch 改字典、scan 读字典。

### 7.2 v2 中每个"读模块级全局量"的函数落位与绑定方式

| 函数 | 读取的全局名 | v2 物理位置 | 绑定方式 | patch `loader.<NAME>` 可见性 | 今天是否有消费方 patch |
|------|-------------|------------|---------|------------------------------|----------------------|
| `load_file` | `SUPPORTED_EXTENSIONS`、`FORMAT_LOADER_MAP` | 门面 | `__globals__` 同字典（A/C） | ✅ 逐位等价 | 无（grep），但机制保留 |
| `scan_knowledge_base` | `KNOWLEDGE_BASE_DIR`、`SUPPORTED_EXTENSIONS`、`PDF_SUBDIR` | 门面 | 同上 | ✅ **测试硬契约，逐位等价** | **有：test_versioned_indexing L59** |
| `load_all_documents` | （经上两函数间接） | 门面 | 同上 | ✅ | 无 |
| `load_pdf` | `USE_MARKER_FOR_PDF`；`HAS_PYMUPDF/HAS_PDFPLUMBER/_pdfplumber_version`（日志行 L784–790）；`pymupdf` | `pdf_loader.py` | `USE_MARKER_FOR_PDF`：**域内快照**（§7.4）；HAS_*/库名：顶层 `from _compat.pdf import ...`（冻结，常量语义） | HAS_*/库名：patch 落点移至 `_compat.pdf`（门面懒表是只读转发）；`USE_MARKER_FOR_PDF`：见 §7.4 | 全部无（grep：这些名字在 loader.py 之外零引用） |
| `extract_tables_from_page` | `HAS_PDFPLUMBER`、`PYMUPDF_GET_TABLES_AVAILABLE` | `pdf_tables.py` | 顶层 from `_compat.pdf`（冻结，常量语义） | patch 落点移至 `_compat.pdf` | 无 |
| `extract_tables_with_pdfplumber` | `HAS_PDFPLUMBER`、`pdfplumber` | `pdf_tables.py` | 同上 | 同上 | 无 |
| `_try_ocr_page` | `HAS_TESSERACT`、`pymupdf`、`pytesseract` | `pdf_page.py` | 顶层 from `_compat.ocr` / `_compat.pdf`（冻结） | patch 落点移至 `_compat.*` | 无 |
| `_ocr_image_bytes` | `HAS_TESSERACT`、`HAS_PIL`、`pytesseract`（`Image` 函数内导入，原文 L1099-1100 不动） | `ocr.py` | 顶层 from `_compat.ocr` | 同上 | 无 |
| `load_docx` | `HAS_DOCX`、`DocxDocument` | `docx_loader.py` | 顶层 from `_compat.docx` | 同上 | 无 |
| `load_xlsx` | `HAS_OPENPYXL`、`HAS_PANDAS` | `xlsx_loader.py` | 顶层 from `_compat.xlsx` | 同上 | 无 |
| `_load_xlsx_openpyxl` | `openpyxl`、`_SUMMARY_MIN_ROWS` | `xlsx_loader.py`（常量同域） | 顶层直连 | 常量同模块，patch 语义与今天等价 | 无 |
| `_build_sheet_summary` | `_HEADER_SYNONYMS`（+ 经 `_parse_number/_is_sequence_col`） | `xlsx_summary.py`（常量同域） | 同模块 | 等价 | 无 |

**铁律（写入门面 docstring，评审清单强制项）**：

1. 四个 settings 快照量**只允许定义在门面**，子模块一律不得顶层 `from app.ingestion.loader import <配置量>`（实验 E 陷阱）；
2. 需要读门面配置量的函数只有 `load_file/scan_knowledge_base/load_all_documents/（load_pdf 的一处例外，见 §7.4）`，前三者物理驻留门面，天然合规；
3. `HAS_*`/库对象名属"常量语义"（进程生命周期内不变、零 patch 消费方），允许子模块顶层冻结导入；若未来出现需要运行时翻转 `HAS_*` 的测试，patch 目标是 `_compat.<域>`，矩阵记档。

### 7.3 `test_versioned_indexing.py` 的逐步推演（零改动通过论证）

```python
from app.ingestion import indexer          # L5：子模块导入。父包 init 为懒门面（轻）→ indexer 模块体执行
                                           #     indexer 顶层 from app.ingestion.loader import load_file...
                                           #     → loader 门面（轻）+base。与今天等价（indexer 自己持有名字绑定）
monkeypatch.setattr(indexer, "load_file", fake)   # L15：patch indexer 模块字典 → process_file 读到假实现 ✅ 与今天逐位一致
from app.ingestion import loader           # L56：子模块导入，拿到门面模块对象 ✅
monkeypatch.setattr(type(settings), "knowledge_base_dir", property(...))  # L58：settings 类型层 patch ✅ 不涉及 loader
monkeypatch.setattr(loader, "KNOWLEDGE_BASE_DIR", tmp_path)               # L59：setattr 门面模块字典
                                           #     前提：KNOWLEDGE_BASE_DIR 是门面真实属性（raising=True 满足）✅
loader.scan_knowledge_base()               # L65：scan.__globals__ is 门面.__dict__（实验 A）
                                           #     调用时全局名查找读到 tmp_path（实验 C）✅
                                           #     glob 语义、排序、.versions 排除（非递归 glob 隐式性质）原文未动 ✅
indexer.process_file(version, object())    # L66-67：ValueError 分支在 indexer L156-158，未动 ✅
```

### 7.4 `USE_MARKER_FOR_PDF` 的专项决策（唯一有分歧空间的量）

今天：定义于 loader L141、读取于 `load_pdf` L910，同字典。v2 `load_pdf` 物理迁至 `pdf_loader.py`，三个候选：

| 方案 | 做法 | 函数体改动 | patch `loader.USE_MARKER_FOR_PDF` | 评估 |
|------|------|-----------|-----------------------------------|------|
| x：函数内延迟绑定门面 | `load_pdf` 内 `from app.ingestion import loader as _pkg` 后读 `_pkg.USE_MARKER_FOR_PDF` | 2 行 | ✅ 保留 | 引入实验 E 的脆弱模式，仅为一个零消费方的标志——过度防御 |
| **y：域内快照（推荐）** | `pdf_loader.py` 顶层 `USE_MARKER_FOR_PDF = settings.use_marker_for_pdf`（与门面 L141 同款的导入期快照） | **0 行** | ❌ 落点移至 `pdf_loader.USE_MARKER_FOR_PDF`（门面副本保留为兼容视图，两处同源于 settings，生产值恒等） | 函数体逐字不动；grep 证实该标志除定义/使用外全仓零引用、无 patch 消费方；两副本 diverge 仅当有人运行时改 settings 字段且期望传导——今天同样不支持（导入期快照） |
| z：调用时读 `settings.use_marker_for_pdf` | 函数体改 1 处 | 1 行 | ❌ 且语义从快照变 live | 行为变更（settings 运行时可变性暴露），违反"不改行为"纪律，弃 |

采用 **y**，并在门面 `USE_MARKER_FOR_PDF` 定义处注释"兼容视图；生效副本位于 pdf_loader，改动开关请改 settings/env"。

### 7.5 `__globals__` 视角的循环依赖防线

v2 顶层依赖白名单（静态可 grep 检查）：

```
base.py        → stdlib                      （叶子）
_compat/*.py   → stdlib + 各自第三方 + settings（叶子；域间互不导入）
xlsx_summary   → stdlib                      （叶子）
ocr.py         → _compat.ocr + base
pdf_page.py    → _compat.pdf + _compat.ocr + base
pdf_tables.py  → _compat.pdf + base
pdf_marker.py  → stdlib + base(logger)
pdf_loader.py  → _compat.pdf + pdf_page + pdf_tables + pdf_marker + base + settings
docx_loader.py → _compat.docx + base；ocr（函数内懒）
xlsx_loader.py → _compat.xlsx + xlsx_summary + base
text_loader.py → base
门面 __init__   → stdlib + settings + base    （永不顶层导入任何域子模块）
父包 __init__   → stdlib                      （永不顶层导入 loader/chunking/indexer）
```

环不可能出现：唯一的"反向边"（子模块→门面）被降级为 §7.4 方案 y 后已**完全消失**——v2 中没有任何子模块需要在任何时机读门面属性。`from app.ingestion.loader.base import ...` 形式的子模块导入会先触发门面 init，但门面 init 只依赖 base/stdlib/settings，先于任何域子模块完成，顺序天然安全（含"直接冷导入 `app.ingestion.loader.pdf_page`"的场景：父包 init → 门面 init → base → `_compat` → pdf_page，单向）。

---

## 8. 修订后的分阶段迁移计划

> 总纪律继承 v1 §9（只搬运不改写、缺陷分轨、golden-master 逐字节门禁、每阶段单 commit 可回滚），v2 新增两条：**每阶段附加"导入足迹守卫"验收**（附录 C 第 1 条脚本）；**logger 名冻结**（§3.3）。分支 `refactor/loader-split-v2`。

### Phase -1（v2 新增，硬前置）：修复验证环境

> **状态（2026-11-11）：已完成**。根 `.venv` 已删除，只保留 `backend/.venv`（`uv sync --locked` 重建，全量回归通过）。下文保留当时的口径作为历史记录。

- 事实：root `.venv` 与 `backend\.venv` 的 `home` 指向已删除的 conda env（附录 B），当时**无法运行 pytest/check.ps1/任何导入实测**。
- 动作：重建虚拟环境（AGENTS.md：Python 3.12+，`pip install -e . && pip install pytest pytest-asyncio`，或 uv 重 sync）。**不升级任何第三方版本**（按 pyproject 现有约束安装）。
- 成功判据：`python -m pytest -q` 可执行并产出可对照基线（当前基线 **745 passed / 147 skipped，合计 892，0 failed**，随时由 `scripts/check.ps1` 复现）。
- 回滚：不适用（环境操作，不入 git）。

### Phase 0：行为与足迹双基线（不改源码）

- v1 Phase 0 全部保留（pytest/check.ps1/eval 基线、golden-master 样张快照【需 owner 批准补测试资产】、P1 缺陷工单）。
- v2 新增：
  1. **导入足迹基线**：对 §6 六个场景各跑一次附录 C 第 1 条守卫脚本，记录 `sys.modules` 集合快照（今天六场景应全部命中"污染集"非空的断言失败——这本身就是基线证据）；
  2. `python -X importtime` 三份存档：`from app.ingestion.loader import compute_file_hash`、`... import load_file`、worker 入口（附录 C 第 2 条）。
- 成功判据：基线数据落盘 `output/`（沿用现有评测产物目录约定）。

### Phase 1：包化 + base 沉底 + 门面骨架

- 新增：`loader/__init__.py`（含四快照量、load_file/scan/load_all **原文**、`FORMAT_LOADER_MAP`（本阶段仍持真函数直引，text 域先切懒闭包亦可，统一从 P4 起切——本阶段以"最少的机制引入"为先）、`__getattr__` 骨架（表内容随迁出进度逐阶段填充）、TYPE_CHECKING 块）、`loader/base.py`（Document/hash/md 表/序列化/固定名 logger）、`loader/text_loader.py`（load_txt/load_md）、`loader/__main__.py`。
- 同 commit 删除 `loader.py`（git rename 语义，历史可溯）。
- 修改：无其他文件（父包 init 本阶段**不动**，急切链仍在 → 导入足迹收益尚未兑现，明示预期）。
- 保留接口：§5.1 全部 7 项 + §5.3 全集（`__getattr__` 表本阶段暂指向门面自身残留名——实现上表为空即可，因为名字还在门面真实存在）。
- 测试：pytest 全量；**重点单点**：`test_versioned_indexing.py` 4 用例；golden-master 全样张；守卫脚本记录（允许仍失败，父包未薄化）。
- 成功判据：与基线逐项一致；`from app.ingestion.loader import ...` 五名全部可用；`loader.__file__` 指向 `__init__.py`（包化生效的直观信号）。
- 回滚：revert 单 commit，`loader.py` 恢复。

### Phase 2：父包 `app/ingestion/__init__.py` 薄化（首个足迹收益兑现点）

- 修改：仅该文件（急切三连导入 → PEP 562 懒表，`__all__` 12 项不变）。
- 测试：pytest 全量（含 test_chunking/test_versioned_indexing/test_ingest_worker）；`python -c "from app.ingestion import Document, load_file, process_file, run_ingest; print('ok')"`；守卫脚本：**S1/S2/S3/S6 场景自此应断言通过**（qdrant_client/sqlalchemy/requests/jieba/解析库全部不在足迹）。
- 成功判据：S1–S3/S6 守卫绿；S4/S5 仍红（预期，解析器还在门面）。
- 回滚：revert；父包恢复急切导入，其他阶段不受影响（本阶段与 loader 包内部结构完全正交）。

### Phase 3：xlsx_summary 拆出（纯函数打样）

- 新增：`loader/xlsx_summary.py`；门面 `_LAZY_ATTRS` 首批登记（`_parse_number` 等 5 名）或轻量直连 re-export（二选一，推荐懒表统一机制）。
- 修改：门面（此时 `_load_xlsx_openpyxl` 仍在门面，顶层从 xlsx_summary 直连导入——轻域允许，§4.2）。
- 测试：pytest + golden-master（**必须含 ≥1000 行大表样张**以触发 summary 路径；v1 §9 Phase 2 同款要求）。
- 回滚：revert。

### Phase 4：docx 域 + OCR 基建（`_compat` 机制首次落地）

- 新增：`_compat/{__init__,docx,ocr}.py`、`ocr.py`（`_ocr_image_bytes`）、`docx_loader.py`。
- 修改：门面（`.docx` 路由切懒闭包；`load_docx/_extract_docx_images/_ocr_image_bytes/HAS_DOCX/HAS_TESSERACT/HAS_PIL/DocxDocument/pytesseract/Image` 登记懒表；门面顶部原 L66–71、L91–108 探测段删除——**首次从门面剥离重库**）。
- 测试：pytest + golden-master（docx 样张：含表格+内嵌图；OCR 环境缺失时验证 `has_ocr=False` 降级路径逐字节一致）；守卫脚本新增断言：S1–S3/S6 足迹不含 `docx/pytesseract/PIL`。
- 成功判据：docx 样张三类 Document 输出逐字节一致（含 `ocr_text` 空串键）；日志行 name 字段不变（固定 logger 生效验证点）。
- 回滚：revert。

### Phase 5：xlsx 域

- 新增：`_compat/xlsx.py`、`xlsx_loader.py`。
- 修改：门面（`.xlsx` 路由切懒闭包；相关名登记懒表；删除门面 L59–64/L73–78 探测段）。
- 测试：pytest + golden-master（sheet/summary/row 三类 Document）；pandas 降级路径在隔离环境验证或标注"需要动态验证"（v1 同款处理）；openpyxl/pandas 两路径的**异常语义差异**（部分保留 vs 全弃，原 L1451 vs L1490）以损坏样张做 golden 对照。
- 回滚：revert。

### Phase 6：PDF 域（3 个子 commit，风险最高、收益最大）

- 6a：新增 `_compat/pdf.py` + `pdf_page.py`（含 `_try_ocr_page`）；门面登记对应懒表名；`load_pdf` 暂留门面并改为顶层直连这两个子模块（本 commit 门面仍载 pymupdf，足迹未变——**先动结构后切时机**，两步各自可回滚）。
- 6b：新增 `pdf_tables.py` + `pdf_marker.py`；`load_pdf` 迁出至 `pdf_loader.py`（函数体逐字迁移；`USE_MARKER_FOR_PDF` 域内快照按 §7.4 方案 y）；`.pdf` 路由切懒闭包；**删除门面顶部原 L32–57、L80–132 全部探测段**。
- 6c：验证 commit：门面顶层导入收敛为 stdlib+settings+base；`__getattr__`/`__dir__`/`__all__` 终态核对；`importtime` 复测归档。
- 测试：每子 commit pytest + golden-master（PDF 样张矩阵：数字版表格页/双栏页/旋转页/逻辑页码页/扫描件【OCR 环境可用时】）；6c 跑守卫脚本全绿 + `check.ps1 -Full` eval 门禁（AGENTS.md 对 ingestion 变更的硬要求）。
- 成功判据：S4 足迹 == {门面链} ∪ {pdf 域 6 模块} ∪ {pymupdf, pdfplumber, (pytesseract, PIL)} 且不含 pandas/openpyxl/docx/qdrant 层（宿主原本没有时）；S1–S3/S5/S6 不回退。
- 回滚：三个子 commit 独立 revert；6a/6b 中间态均自洽可用。

### Phase 7：收尾与固化

- 动作：`__main__.py` 终验；门面行数/职责终态核对（~200 行，零重顶层导入）；`docs/wiki/06` L10 链接与 AGENTS.md 结构描述更新（文档一致性，owner 确认后进行）；向 owner 提交**建议新增的守护测试清单**（本审计不创建）：
  1. 导入足迹守卫（附录 C 第 1 条脚本 → `tests/test_loader_import_footprint.py`）；
  2. 门面懒表 from-import / getattr / `__dir__` 行为测试；
  3. monkeypatch `loader.KNOWLEDGE_BASE_DIR` → scan 生效的显式契约测试（现由 test_versioned_indexing 间接覆盖，建议显式化）；
  4. golden-master 样张集固化为 fixtures + 对比测试。
- 回滚：revert。

### 收益兑现时间线（区别于 v1 的关键改进）

| 阶段 | 兑现收益 |
|------|---------|
| P2 完成 | `from app.ingestion.loader import compute_file_hash` 摆脱向量层（qdrant_client/sqlalchemy/requests/jieba） |
| P4 完成 | API/轻量场景摆脱 python-docx/pytesseract/PIL |
| P6 完成 | **P3 全部兑现**：门面轻链（S1/S2/S3/S6 仅 config 链）、解析库全面按需、导入期副作用退出启动路径 |

---

## 9. 风险和测试方案

### 9.1 风险登记册（v2 修订）

| # | 风险 | 场景/证据 | 缓解 | 回滚 |
|---|------|----------|------|------|
| R1 | 冻结陷阱：实施者把门面配置量顶层导入进子模块 | 沙箱实验 E 证实静默失效 | v2 已将读配置的函数全部留在门面 + §7.2 铁律写入 docstring + 评审清单；Phase 7 建议守护测试 | revert 当期 commit |
| R2 | PEP 562 静态工具盲区 | IDE/mypy 不跟踪 `__getattr__` | TYPE_CHECKING 块与 `_LAZY_ATTRS` 同步维护；`__all__` 完整 | 不适用（工具层） |
| R3 | `__getattr__` 异常协议写错 | 返回 None 或抛非 AttributeError 会破坏 `hasattr`/from-import 语义 | 骨架代码统一 `raise AttributeError(...)`（§3.1，沙箱 B/G 验证过该实现） | revert |
| R4 | 懒闭包改变 `FORMAT_LOADER_MAP` 值同一性 | `is` 比较失败 | grep 证实零消费方（§5.3）；闭包补 `__name__/__qualname__` | revert |
| R5 | logger 名漂移破坏日志采集维度 | core/logging.py L25/L46 含 name/logger 字段 | base.py 固定名共享 logger（§3.3）；P4 起以日志 diff 验证 | revert |
| R6 | 首次解析的进程内冷导入时延 | worker 首单 PDF 任务耗时增加一次性导入成本 | 线程池内执行不阻塞事件循环；ARQ max_jobs=1；时延量化 **需要动态验证**（附录 C-2，venv 修复后实测） | 不可接受时在 worker 启动钩子预热 `importlib.import_module("...pdf_loader")`（一行，语义=回到今天的启动期加载，但仅限 worker 进程） |
| R7 | warnings 过滤器时点后移 | pymupdf 弃用告警漏出 | 过滤器与库导入同在 `_compat/pdf.py`，相对顺序保留（§4.4）；grep 证实 pymupdf 唯一导入点 | revert |
| R8 | `HAS_*`/`USE_MARKER_FOR_PDF` patch 落点迁移 | 未来有人写 patch 门面的测试 | §5.4 矩阵记档 + 门面 docstring 声明生效副本位置 | 不适用（文档层面） |
| R9 | 循环导入 | 子模块顶层反向依赖门面 | §7.5 白名单：v2 中**不存在**任何子模块→门面的顶层或函数内导入（§7.4 方案 y 消除了最后一处）；白名单可 grep 审计 | revert |
| R10 | 验证环境不可用导致阶段失控 | 附录 B：两 venv 均指向已删除 conda env | **Phase -1 硬前置**；环境未修复前禁止任何迁移 commit | 不适用 |
| R11 | golden-master 覆盖不到的分支（OCR 缺环境、pandas 降级路径、marker 子进程） | §8 P4/P5/P6 标注 | 无法构造的分支显式记档"需要动态验证"，PR 描述中如实申报（AGENTS.md skip 报告惯例） | revert |
| R12 | 文本漂移导致向量 ID 漂移、新旧块并存 | v1 §10-R3 继承 | golden-master 逐字节门禁不变；点 ID 机制未动（qdrant `_generate_id` 不在改动面） | revert + 以旧代码幂等重灌 |

### 9.2 测试方案（分层）

| 层 | 内容 | 工具/命令 | 阶段 |
|----|------|----------|------|
| L0 环境 | venv 重建、依赖按 pyproject 锁定 | Phase -1 | 前置 |
| L1 单测回归 | `python -m pytest -q`（backend/），skip 集合与基线一致；重点单点 `tests/test_versioned_indexing.py -v` | pytest | 每阶段 |
| L2 兼容面断言 | §5.1 七条路径 + §5.3 次级面逐条 `python -c` 冒烟（附录 C 第 3 条给出脚本） | 命令行 | 每阶段 |
| L3 导入足迹守卫 | sys.modules 污染集断言（附录 C 第 1 条）；六场景各一次 | 脚本（产物存 output/，实施期经批准可固化为测试） | P2 起逐场景转绿 |
| L4 行为逐字节 | golden-master：每格式样张 `documents_to_json(load_file(f))` + `compute_file_hash(f)` 前后 diff == 空 | 一次性对比脚本 → fixtures 固化 | 每阶段 |
| L5 日志逐字节 | 固定样张解析日志（text 与 json 两种 LOG_FORMAT）diff：消息与 name 字段均不变 | 脚本对比 | P4 起 |
| L6 检索基线 | `.\scripts\check.ps1 -Full`（需基础设施 + 已索引 Qdrant） | AGENTS.md 门禁 | P6/终验 |
| L7 时延画像 | `-X importtime` 冷导入档案对比（附录 C 第 2 条）；worker 首单任务耗时抽样 | 实测 | P0 基线 / P6c 复测 |

**每阶段"迁移成功"总判据**：L1 无新增失败 + L2 全通过 + L3 该阶段应绿场景全绿 + L4 零 diff + L5（P4 起）零 diff；任一失败即 revert 当期 commit 后重做，不带病推进。

---

## 附录 A：导入机制沙箱实验记录

方法约束：本次任务只允许新建本报告文件，故验证**不落地任何项目/临时文件**——用 `types.ModuleType` + `exec(code, mod.__dict__)` 在内存中精确重建"包 `__init__` + 子模块"执行语义（与真实模块机制同构：模块即"在以自身 `__dict__` 为 globals 的命名空间里 exec 过的代码对象"）。环境：系统 Python 3.13.13（项目 requires-python>=3.12；所验机制自 3.7/PEP 562 起语义稳定）。

| 实验 | 验证点 | 结果 |
|------|--------|------|
| A | 门面内定义函数的 `__globals__ is 门面.__dict__` | True |
| B | 门面 `__getattr__`（PEP 562）承接 `from pkg import 未定义名`（懒导出可用） | True |
| C | `setattr(门面, "CONFIG", 新值)`（monkeypatch 等价操作）后，门面函数调用时读到新值 | True |
| D | 子模块函数体内 `from 门面 import CONFIG`（调用时执行）读到 patch 后新值（延迟绑定可行） | True |
| E | 子模块**顶层** `from 门面 import CONFIG` 冻结副本，patch 门面对其**不可见**（陷阱实锤） | True（读到旧值 "orig"） |
| F | `importlib.import_module` 命中 sys.modules 已注册子模块（懒闭包第二次调用零成本） | True |
| G | 真实存在的模块属性优先于 `__getattr__`（配置快照量不会被懒表机制干扰） | True |

推论（用于 §3/§7 设计决策）：v1 §6.3 的延迟绑定方案可行（D）但存在被"好心的顶层导入优化"静默破坏的路径（E）；v2 选择"读写同字典"（A+C）彻底移除该风险面。

## 附录 B：环境实测记录（venv 损坏证据）

- `D:\PythonProjects\personal_rag\.venv\Scripts\python.exe -c ...` → `No Python at '"C:\Users\25160\.conda\envs\local_env\python.exe'`
- `D:\PythonProjects\personal_rag\backend\.venv\Scripts\python.exe` → 同样报错
- root `.venv\pyvenv.cfg`：`home = C:\Users\25160\.conda\envs\local_env`（该 conda env 已不存在）、`version_info = 3.12.14`、`uv = 0.13.0`
- 系统 PATH Python：3.13.13（Loomy 运行时），`import pymupdf` → ModuleNotFoundError（无项目依赖）

**结论**：导入耗时、pytest、check.ps1、eval 在本会话一律无法实测；所有耗时/足迹类论断均以静态导入图 + 沙箱机制实验为据，量化数据整体标注 **需要动态验证**，并在 Phase -1/0 补齐。**除耗时外的本报告结论不受影响**（导入链是源码静态事实）。

## 附录 C：待动态验证项与复测命令（venv 修复后由实施者执行）

1. **导入足迹守卫脚本**（六场景各跑一次；`-B` 禁写字节码；断言"不该在场"的模块集合为空）：

```powershell
# 场景 S1/S2/S3/S6 期望 bad 为空集（P2 后 qdrant 系出局、P6 后解析库出局）
python -B -c "import sys; from app.ingestion.loader import compute_file_hash; bad = {'pymupdf','fitz','pdfplumber','pandas','openpyxl','docx','pytesseract','PIL','qdrant_client','sqlalchemy','requests','jieba'} & set(sys.modules); print('S1 polluters:', bad or 'CLEAN'); assert not bad, bad"
# S4：解析一个 PDF 样张后断言 {'pandas','openpyxl','docx','qdrant_client','sqlalchemy'} 缺席；{'pymupdf','pdfplumber'} 在场
# S5：解析 XLSX 样张后断言 {'pymupdf','pdfplumber','docx'} 缺席；{'openpyxl','pandas'} 在场
```

2. **导入时延画像**（基线与 P6c 各存档一份）：

```powershell
python -B -X importtime -c "from app.ingestion.loader import compute_file_hash" 2> ..\output\importtime_s1_after.txt
python -B -X importtime -c "from app.ingestion.loader import load_file"          2> ..\output\importtime_s3_after.txt
python -B -X importtime -c "from app.ingestion.indexer import process_file"      2> ..\output\importtime_worker_after.txt
python -B -c "import time,importlib;t=time.perf_counter();importlib.import_module('pymupdf');print('pymupdf cold ms:',(time.perf_counter()-t)*1000)"
```

3. **兼容面冒烟**（L2 层，逐条对应 §5.1/§5.3）：

```powershell
python -B -c "from app.ingestion.loader import Document, compute_file_hash, load_file, scan_knowledge_base; from app.ingestion import Document as D2, load_file as L2; from app.ingestion import loader; loader.KNOWLEDGE_BASE_DIR = loader.KNOWLEDGE_BASE_DIR; print('compat smoke OK')"
python -B -c "from app.ingestion import loader; print(sorted(n for n in dir(loader) if not n.startswith('__'))[:20])"
python -B -c "import app.ingestion.loader as m; print(type(m).__name__, hasattr(m, '__path__'))"   # 期望 package True
```

4. 其余待验证项：worker 首单 PDF 任务冷导入时延（R6）；`pymupdf.open()` 探测块的真实异常路径（v1 附录 A 遗留项）；qdrant_client 传递依赖精确清单（§6.0）；OCR/pandas 降级分支的 golden 覆盖可行性（R11）。

---

*报告完。本文件为本次任务唯一新建产物；项目源码、测试、配置均未改动（git 工作区中其余未提交变更为审计开始前已存在）。*
