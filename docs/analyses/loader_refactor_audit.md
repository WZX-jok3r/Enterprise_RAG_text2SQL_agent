# `backend/app/ingestion/loader.py` 只读架构审计与重构方案

- 审计对象：[loader.py](file:///d:/PythonProjects/personal_rag/backend/app/ingestion/loader.py)（1592 行）
- 审计方式：纯静态代码阅读 + 全仓引用搜索（未修改/创建/删除任何项目文件，未运行任何改变状态的操作）
- 审计日期：2026-11-04
- 结论均可追溯到真实代码；无法静态确认之处已标注「需要动态验证」。

---

## 1. 当前 loader.py 的整体职责概览

该文件名义上是「文档加载器」，实际是一个**多职责聚合文件**，至少承担 10 类职责：

| # | 职责 | 代码规模（约） |
|---|------|------|
| 1 | 第三方库能力探测与版本兼容（pymupdf/fitz、pdfplumber、pytesseract、PIL、openpyxl、pandas、python-docx） | L23–L141，约 120 行 |
| 2 | 统一数据结构 `Document` 与序列化 | L150–L161、L1575–L1583 |
| 3 | 文件哈希（增量入库判重） | L164–L170 |
| 4 | PDF 解析全链路：页面预处理、表格三级降级提取、双栏检测重排、逻辑页码、OCR 兜底、marker 外部 CLI 兜底、Document 组装 | L177–L945，约 770 行 |
| 5 | TXT/MD 解析（含编码探测） | L952–L1018 |
| 6 | DOCX 解析：段落、表格、图片提取 + 图片 OCR | L1021–L1226，约 205 行 |
| 7 | XLSX 解析：openpyxl/pandas 双通道 + 大表聚合摘要生成 | L1229–L1492，约 265 行 |
| 8 | 格式分发注册表（`FORMAT_LOADER_MAP` + `load_file`） | L1499–L1517 |
| 9 | 知识库目录扫描与批量加载 | L1520–L1568 |
| 10 | 配置快照与模块级全局状态（`KNOWLEDGE_BASE_DIR` 等） | L134–L143 |

PDF 相关逻辑占全文件近一半，且与 OCR、子进程调用（marker CLI）、布局分析等横切能力直接交织在 `load_pdf` 一条调用链里。

---

## 2. 类、函数、常量清单

### 2.1 类（1 个）

| 类 | 位置 | 说明 |
|---|---|---|
| `Document`（dataclass） | L150–L161 | 字段 `text: str`、`metadata: Dict[str, Any]`；方法 `to_dict()`、`from_dict()`。全链路统一文档块结构，被 indexer 消费（`doc.to_dict()`，[indexer.py L189](file:///d:/PythonProjects/personal_rag/backend/app/ingestion/indexer.py#L189)） |

### 2.2 常量与全局变量

| 名称 | 位置 | 性质 | 说明 |
|---|---|---|---|
| `HAS_PYMUPDF` / `HAS_PANDAS` / `HAS_DOCX` / `HAS_OPENPYXL` / `HAS_PDFPLUMBER` / `HAS_TESSERACT` / `HAS_PIL` | L38–L108 | 导入期能力开关 | 全部在 import 时冻结，函数内部直接读全局 |
| `_pdfplumber_version` | L82–L87 | 版本缓存 | 仅用于日志 |
| `PYMUPDF_GET_TABLES_AVAILABLE` | L110–L132 | 导入期探测 | **import 时实际执行 `pymupdf.open()` 建临时文档探测 `get_tables`**，属导入期副作用 |
| `Rect` | L51–L57 | 死导入 | 导入后全文件未使用 |
| `_io`（`import io as _io`） | L104 | 死导入 | 未使用（`_ocr_image_bytes` 内部又 `import io`） |
| `KNOWLEDGE_BASE_DIR` / `SUPPORTED_EXTENSIONS` / `PDF_SUBDIR` / `USE_MARKER_FOR_PDF` | L137–L141 | settings 快照 | import 时从 `app.core.config.settings` 绑定；**测试直接 monkeypatch `loader.KNOWLEDGE_BASE_DIR`**（[test_versioned_indexing.py L59](file:///d:/PythonProjects/personal_rag/backend/tests/test_versioned_indexing.py#L59)） |
| `logger` | L143 | 日志器 | — |
| `_SUMMARY_MIN_ROWS = 1000` | L1251 | 业务阈值 | 大表摘要触发行数 |
| `_HEADER_SYNONYMS` | L1266–L1270 | 业务词表 | 英文列名→中文同义词，供 BM25 命中 |
| `FORMAT_LOADER_MAP` | L1499–L1505 | 注册表 | 后缀 → 解析函数映射 |

### 2.3 函数清单（30 个，含私有函数）

图例：副作用 = 文件/子进程/全局状态；「对外」= 被 loader.py 之外的模块调用。

| # | 函数 | 位置 | 主要职责 | 输入 | 返回 | 调用者（内部） | 调用的其他函数 | 副作用 | 对外公共接口 |
|---|------|------|---------|------|------|--------------|----------------|--------|--------------|
| 1 | `compute_file_hash` | L164–L170 | MD5 文件哈希 | `Path` | `str` | — | 无 | 文件读 | **是**：indexer、ingestion_service、versioned_ingest |
| 2 | `preprocess_page` | L177–L211 | PDF 页预处理：旋转/纸张/图片型检测，判 `need_ocr` | pymupdf `page` | `Dict` | `load_pdf` | `page.get_text/get_images` | 无（纯页面操作） | 否 |
| 3 | `_data_to_markdown_table` | L214–L230 | 二维表→Markdown 表格 | `List[List]` | `str` | #4/#5/#6/#24 | 无 | **原地修改入参**（`row.append` 补列） | 否 |
| 4 | `extract_tables_with_pdfplumber` | L233–L299 | 表格策略1：pdfplumber（4 层参数兼容） | `Path, int` | `List[Dict]` | #7 | #3 | **文件读**（重新打开 PDF） | 否 |
| 5 | `extract_tables_with_get_tables` | L302–L331 | 表格策略2：PyMuPDF `get_tables()` | `page` | `List[Dict]` | #7 | #3 | 无 | 否 |
| 6 | `extract_tables_with_dict` | L334–L450 | 表格策略3：dict 坐标聚类兜底（约 117 行） | `page` | `List[Dict]` | #7 | #3 | 无 | 否 |
| 7 | `extract_tables_from_page` | L453–L487 | 表格三级降级编排 | `page, Path, int` | `List[Dict]` | `load_pdf` | #4/#5/#6 | 经 #4 间接文件读 | 否 |
| 8 | `_convert_pdf_with_marker` | L490–L538 | 调 `marker_single` CLI 转 Markdown | `Path, timeout=900` | `Optional[str]` | `load_pdf` | 无（subprocess） | **子进程 + `tempfile.mkdtemp` 从不清理（泄漏）** | 否 |
| 9 | `_detect_likely_table_pdf` | L541–L580 | 启发式：x0 聚类判断疑似无线表格 | pymupdf `doc` | `bool` | `load_pdf` | 无 | 无 | 否 |
| 10 | `detect_columns` | L583–L643 | 双栏/多栏布局检测 | `page, threshold=0.45` | `List[Tuple]` | `load_pdf` | 无 | 无；**`threshold` 参数从未被使用** | 否 |
| 11 | `reorder_text_blocks_by_column` | L646–L666 | 按列优先重排文本块（纯函数） | `blocks, columns` | `List[dict]` | `load_pdf` | 无 | 无 | 否 |
| 12 | `_try_ocr_page` | L669–L685 | PDF 页面 OCR（pixmap→pytesseract） | `page` | `str` | `load_pdf` | `pymupdf.Matrix`、`pytesseract` | 无（内存渲染） | 否 |
| 13 | `extract_logical_page_number` | L692–L753 | 正则提取逻辑页码（纯函数） | `str` | `Optional[int]` | `load_pdf` | 无 | 无；**函数内冗余 `import re`（L703）** | 否 |
| 14 | `load_pdf` | L756–L945 | PDF 主解析（约 190 行编排） | `Path` | `List[Document]` | `FORMAT_LOADER_MAP` | #2/#7/#8/#9/#10/#11/#12/#13 | 文件读；经 #8 有子进程 | 间接（经注册表） |
| 15 | `load_txt` | L952–L989 | TXT 解析 + 编码探测循环 | `Path` | `List[Document]` | `FORMAT_LOADER_MAP` | 无 | 文件读 | 间接 |
| 16 | `load_md` | L992–L1018 | MD 原文读取 | `Path` | `List[Document]` | `FORMAT_LOADER_MAP` | 无 | 文件读 | 间接 |
| 17 | `_extract_docx_images` | L1021–L1086 | DOCX 图片双轨提取（inline + rels） | docx `doc, Path` | `List[Dict]` | #19 | 无 | 无 | 否 |
| 18 | `_ocr_image_bytes` | L1089–L1113 | 图片字节 OCR（PIL+pytesseract） | `bytes` | `str` | #19 | 无 | 无；**函数内重复 `import io`/`from PIL import Image`** | 否 |
| 19 | `load_docx` | L1116–L1226 | DOCX 主解析：段落+表格+图片OCR（约 110 行） | `Path` | `List[Document]` | `FORMAT_LOADER_MAP` | #17/#18/#3（未用，表格为手写 md 拼接） | 文件读 | 间接 |
| 20 | `load_xlsx` | L1229–L1246 | XLSX 分发（openpyxl 优先/pandas 降级） | `Path` | `List[Document]` | `FORMAT_LOADER_MAP` | #24/#25 | 文件读（经下游） | 间接 |
| 21 | `_parse_number` | L1254–L1262 | 单元格→数值（纯函数） | `str` | `Optional[float]` | #22/#23 | 无 | 无；**在 [schema_infer.py L51–L65](file:///d:/PythonProjects/personal_rag/backend/app/analytics/schema_infer.py#L51-L65) 有文档化的重复实现** | 否 |
| 22 | `_is_sequence_col` | L1273–L1289 | 序号列判定（纯函数） | `str, List[float]` | `bool` | #23 | 无 | 无；schema_infer 亦有同口径实现 | 否 |
| 23 | `_build_sheet_summary` | L1292–L1400 | 大表聚合摘要生成（约 110 行，纯函数） | `file_name, sheet_name, rows_data, max_chars=480` | `str` | #24 | #21/#22 | 无；**`file_name` 参数从未被使用** | 否 |
| 24 | `_load_xlsx_openpyxl` | L1403–L1453 | openpyxl 通道：整表→Markdown + 大表摘要 | `Path, documents` | `List[Document]` | #20 | #3/#23 | 文件读；**入参 documents 被原地修改又返回**；**L1411 `merged_ranges` 计算后未使用（docstring 宣称支持合并单元格，实际未生效）** | 否 |
| 25 | `_load_xlsx_pandas` | L1456–L1492 | pandas 通道：逐行 Document | `Path, documents` | `List[Document]` | #20 | 无 | 文件读；错误时返回 `[]` 与 #24 返回「已提取部分」的约定不一致 | 否 |
| 26 | `load_file` | L1508–L1517 | 后缀分发入口 | `Path` | `List[Document]` | `load_all_documents`、外部 | `FORMAT_LOADER_MAP` 中各 loader | 经下游 | **是**：indexer.process_file（L176）、`__init__.py` 再导出 |
| 27 | `scan_knowledge_base` | L1520–L1543 | 扫描知识库目录（根目录 + `pdf_examples/`） | 无 | `List[Path]` | #28、外部 | 无 | 目录遍历；读模块全局 `KNOWLEDGE_BASE_DIR`/`SUPPORTED_EXTENSIONS`/`PDF_SUBDIR` | **是**：indexer.run_ingest（L262）、测试 |
| 28 | `load_all_documents` | L1546–L1568 | 批量加载 + 统计 | 无 | `Dict` | `__main__` | #26/#27 | 经下游 | 仅 `__init__.py` 再导出，**无任何生产调用方** |
| 29 | `documents_to_json` | L1575–L1577 | Document 列表→JSON | `List[Document]` | `str` | 无 | `Document.to_dict` | 无 | **全仓零调用（死代码）** |
| 30 | `documents_from_json` | L1580–L1583 | JSON→Document 列表 | `str` | `List[Document]` | 无 | `Document.from_dict` | 无 | **全仓零调用（死代码）** |

### 2.4 第三方库依赖

| 库 | 必需性 | 用途 |
|---|---|---|
| `pymupdf`（降级 `fitz`） | 硬依赖（缺失则 PDF 全挂） | 文本/表格/渲染/页面信息 |
| `pdfplumber` | 可选（表格策略1） | 高精度表格 |
| `pytesseract` | 可选（OCR） | PDF 页面 OCR、DOCX 图片 OCR |
| `Pillow` | 可选（DOCX 图片 OCR 前置） | 图片解码/缩放 |
| `python-docx` | DOCX 必需 | 段落/表格/图片关系 |
| `openpyxl` | XLSX 优先通道 | 工作簿读取 |
| `pandas` | XLSX 降级通道 | 工作簿读取 |

内部模块依赖仅一个：`from app.core.config import settings`（L134）。**loader.py 不依赖 app 内任何其他业务模块，无循环依赖。**

---

## 3. 职责混杂分析

| 职责 | 当前承担函数 | 是否应独立 | 依赖关系 | 拆分必要性/优先级 |
|---|---|---|---|---|
| 文件类型识别 | `load_file`（`SUPPORTED_EXTENSIONS` + `FORMAT_LOADER_MAP` 双重校验） | 是（注册表模块） | 依赖全部格式解析函数 | 中/中。注意 `SUPPORTED_EXTENSIONS`（settings）与 `FORMAT_LOADER_MAP`（本地）是两个真相源，可能漂移 |
| 文件解析（各格式） | `load_pdf`/`load_docx`/`load_xlsx`/`load_txt`/`load_md` | 是，按格式拆 | 各自依赖能力开关与工具函数 | **高/高**。这是文件膨胀的主因 |
| PDF 页面预处理 | `preprocess_page` | 是（并入 PDF 布局模块） | 仅被 `load_pdf` 调用 | 高/中 |
| PDF 文本提取 | `load_pdf` 内联（L827–L855）+ `detect_columns` + `reorder_text_blocks_by_column` | 是（布局/重排独立） | 被 `load_pdf` 调用 | 高/中 |
| PDF 表格提取 | `extract_tables_from_page` + 3 个策略函数 + `_detect_likely_table_pdf` + `_convert_pdf_with_marker` | **是，最优先** | 依赖 `_data_to_markdown_table`、能力开关 | **最高/最高**：约 400 行、三级降级 + marker 兜底，是文件中最复杂、变更最频繁的子系统 |
| OCR 识别 | `_try_ocr_page`（PDF 页）、`_ocr_image_bytes`（DOCX 图） | 是（合并为一个 OCR 封装模块） | 共享 pytesseract；分别被 `load_pdf`/`load_docx` 调用 | 中/中。同一能力散落两处，能力开关判断重复 |
| DOCX 图片提取 | `_extract_docx_images` | 随 DOCX 模块走 | 仅被 `load_docx` 调用 | 高/中 |
| XLSX 数据读取 | `load_xlsx`/`_load_xlsx_openpyxl`/`_load_xlsx_pandas` | 是 | 依赖 `_data_to_markdown_table`、摘要函数 | 高/中 |
| XLSX 大表摘要 | `_build_sheet_summary`/`_parse_number`/`_is_sequence_col` + 2 常量 | **是**：纯分析逻辑，与「读文件」无关 | 被 `_load_xlsx_openpyxl` 调用；`_parse_number`/`_is_sequence_col` 被 analytics 被迫重复实现 | 高/中。独立后可消除与 schema_infer 的重复（见 §5.4） |
| 文本清洗 | 分散（`strip()`、编码探测循环 L963–L970） | 否 | — | 低：体量小，不构成独立模块价值 |
| Document 对象构建 | `Document` + `to_dict`/`from_dict` | 是（数据结构模块） | 被所有解析函数与 indexer 依赖 | 高/高。是全部模块的公共底座，应最先独立 |
| 文件哈希计算 | `compute_file_hash` | 是（工具模块） | 与解析无关；被 services/worker/indexer 直接导入 | **高/高**：3 个外部调用方仅为哈希就 import 整个重依赖文件 |
| 知识库目录扫描 | `scan_knowledge_base` | 是 | 依赖模块全局配置 | 中/中。**注意测试对其模块全局的 monkeypatch 约束**（见 §4） |
| 批量文件加载 | `load_all_documents` | 随扫描模块走 | 调用 #26/#27 | 低：无生产调用方，保留兼容导出即可 |
| 序列化与反序列化 | `documents_to_json`/`documents_from_json` | 随 Document 模块走 | 零调用 | 低：死代码，保留导出、标注待评估删除 |

---

## 4. 项目内调用关系

### 4.1 谁导入了 loader.py（全仓搜索结果）

| 调用方 | 导入内容 | 使用点 |
|---|---|---|
| [app/ingestion/\_\_init\_\_.py L11–L17](file:///d:/PythonProjects/personal_rag/backend/app/ingestion/__init__.py#L11-L17) | `Document, compute_file_hash, load_all_documents, load_file, scan_knowledge_base` | 包级再导出（`__all__`） |
| [app/ingestion/indexer.py L25–L30](file:///d:/PythonProjects/personal_rag/backend/app/ingestion/indexer.py#L25-L30) | `load_file, compute_file_hash, scan_knowledge_base, Document` | `process_file` L176/L180/L224；`run_ingest` L262/L295 |
| [app/services/ingestion_service.py L21](file:///d:/PythonProjects/personal_rag/backend/app/services/ingestion_service.py#L21) | `compute_file_hash` | `submit` L94（`asyncio.to_thread`） |
| [app/worker/tasks/versioned_ingest.py L8](file:///d:/PythonProjects/personal_rag/backend/app/worker/tasks/versioned_ingest.py#L8) | `compute_file_hash` | L43/L54 |
| [tests/test_versioned_indexing.py L56–L65](file:///d:/PythonProjects/personal_rag/backend/tests/test_versioned_indexing.py#L56-L65) | `from app.ingestion import loader` | **monkeypatch `loader.KNOWLEDGE_BASE_DIR`（L59）**；调用 `loader.scan_knowledge_base()`；另在 L15 monkeypatch `indexer.load_file` |
| [app/analytics/schema_infer.py L13–L16](file:///d:/PythonProjects/personal_rag/backend/app/analytics/schema_infer.py#L13-L16) | **刻意不导入** | 注释明确说明：loader 会连带拉入 pandas/pymupdf 重依赖，故本地重复实现 `_parse_number`/`_is_sequence_col` |

`backend/eval/` 目录未引用 loader。

### 4.2 公共接口 vs 内部实现

- **对外公共接口（必须保持导入路径稳定）**：`Document`、`compute_file_hash`、`load_file`、`scan_knowledge_base`、`load_all_documents`（再导出）、以及**模块级全局 `KNOWLEDGE_BASE_DIR`**（被测试 monkeypatch，属事实上的公共接口）。
- **仅内部使用**：其余 24 个函数与全部 `HAS_*` 开关。`FORMAT_LOADER_MAP` 虽可被外部访问但全仓无外部引用。
- **其他模块直接依赖内部实现的情况**：仅有测试对 `loader.KNOWLEDGE_BASE_DIR` 全局的 monkeypatch 一处；无模块引用私有函数。

### 4.3 循环依赖风险

依赖方向单一，**当前无循环依赖**：

```
app.core.config ──► loader.py ──► indexer.py ──► worker/tasks、services
                        ▲                            │
                        └── services/worker 直接取 compute_file_hash
```

风险点在重构后：若新模块之间出现 `registry → 各 loader → registry` 或 `capabilities ↔ 解析模块` 的互引才会产生循环。§6 的结构设计保证依赖单向（见各文件「依赖」列）。

### 4.4 真实调用关系示意图（以代码为准）

```
业务入口层
  indexer.process_file()                     indexer.run_ingest()
    ├─► load_file(path)                        ├─► scan_knowledge_base()
    │     └─► FORMAT_LOADER_MAP[ext]           └─► compute_file_hash()
    │           ├─► load_pdf()
    │           │     ├─► preprocess_page()
    │           │     ├─► [need_ocr] _try_ocr_page()
    │           │     ├─► extract_tables_from_page()
    │           │     │     ├─► extract_tables_with_pdfplumber() ─► _data_to_markdown_table()
    │           │     │     ├─► extract_tables_with_get_tables() ─► _data_to_markdown_table()
    │           │     │     └─► extract_tables_with_dict()       ─► _data_to_markdown_table()
    │           │     ├─► detect_columns() + reorder_text_blocks_by_column()
    │           │     ├─► extract_logical_page_number()
    │           │     ├─► _detect_likely_table_pdf() ─► _convert_pdf_with_marker() [subprocess]
    │           │     └─► Document(...) 组装
    │           ├─► load_docx()
    │           │     ├─► 段落/表格（手写 Markdown 拼接）
    │           │     ├─► _extract_docx_images()
    │           │     └─► _ocr_image_bytes()
    │           ├─► load_xlsx()
    │           │     ├─► _load_xlsx_openpyxl() ─► _data_to_markdown_table()
    │           │     │                        └─► _build_sheet_summary() ─► _parse_number()/_is_sequence_col()
    │           │     └─► _load_xlsx_pandas()
    │           ├─► load_txt()   （编码探测循环）
    │           └─► load_md()
    ├─► compute_file_hash()
    └─► Document.to_dict() ─► chunking.chunk_document()

旁路调用（不经过解析）
  services/ingestion_service.submit() ─► compute_file_hash()
  worker/tasks/versioned_ingest       ─► compute_file_hash()
  loader.__main__ ─► load_all_documents() ─► scan_knowledge_base() + load_file()
```

---

## 5. 当前代码存在的主要架构问题

以下每条均给出代码依据；仅报告静态可证的问题。

1. **单文件职责过载**：1592 行内混合 10 类职责（§1），任何一处解析逻辑变更都需触碰整个文件；PDF 链路（约 770 行）与 XLSX 摘要（约 265 行）彼此零共享却同文件。
2. **导入期副作用与重依赖传染**：
   - L33–L34 修改全局 `warnings` 过滤器；
   - L110–L132 在 import 时执行 `pymupdf.open()` 探测 `get_tables`；
   - 任何 `from app.ingestion.loader import compute_file_hash` 的调用方（如 ingestion_service、worker）都会连带加载 pymupdf/pandas/openpyxl 等全部重依赖。[schema_infer.py L13–L16](file:///d:/PythonProjects/personal_rag/backend/app/analytics/schema_infer.py#L13-L16) 的注释是直接证据：analytics 被迫重复实现纯函数以回避导入 loader。
3. **模块级配置快照 + 测试耦合**：`KNOWLEDGE_BASE_DIR` 等在 import 时绑定（L137–L141），[test_versioned_indexing.py L59](file:///d:/PythonProjects/personal_rag/backend/tests/test_versioned_indexing.py#L59) 直接 monkeypatch 该全局。这使「目录扫描」与「模块全局名」成为事实公共契约，重构必须保持该名字在 `app.ingestion.loader` 命名空间可写。
4. **解析逻辑零测试保护**：30 个函数中仅 `scan_knowledge_base` 有间接覆盖（详见 §8）。`load_pdf`/`load_docx`/`load_xlsx`/OCR/表格三级降级/哈希/序列化均无测试，重构没有安全网。
5. **死代码与失效参数**：
   - `Rect`（L51–L57）、`_io`（L104）导入后未使用；
   - `documents_to_json`/`documents_from_json`（L1575–L1583）全仓零调用；
   - `load_all_documents` 无生产调用方（仅 `__main__` 与再导出）；
   - `detect_columns` 的 `threshold=0.45` 参数未被使用（L583–L643，函数体实际用硬编码 `0.08`）；
   - `_build_sheet_summary` 的 `file_name` 参数未被使用（L1292）；
   - `_load_xlsx_openpyxl` 的 `merged_ranges`（L1411）计算后未使用——docstring 宣称「保留合并单元格信息」，实际未生效（行为现状如此，重构不应顺手修改）；
   - `extract_logical_page_number` 内冗余 `import re`（L703）。
6. **重复实现（跨模块）**：`_parse_number`/`_is_sequence_col` 与 schema_infer 的 `parse_number`/`is_sequence_col` 同口径两份（一方有注释说明原因是 loader 依赖过重）。抽出无重依赖的纯函数模块后具备消除条件。
7. **兼容性判断散落约 6 处**：pymupdf/fitz 双层 fallback（L38–L48）、Rect 双层导入（L51–L57）、`get_tables` 导入期探测 + 调用期二次 `hasattr`（L110–L132、L309）、pdfplumber 四层参数兼容嵌套 try（L251–L276）、`_try_ocr_page` 内再次捕获 `ImportError`（L680）。同一类「版本/能力兼容」没有集中点。
8. **宽泛异常吞没**：所有格式入口均为 `except Exception: logger.error(...); return []`（L943–L945、L987–L989、L1016–L1018、L1224–L1226、L1451–L1453、L1490–L1492），调用方（indexer）只能把「文件为空」和「解析器崩溃」同样记为 `status="empty"`（[indexer.py L177–L184](file:///d:/PythonProjects/personal_rag/backend/app/ingestion/indexer.py#L177-L184)）。`_load_xlsx_openpyxl` 失败返回「已提取部分」、`_load_xlsx_pandas` 失败返回 `[]`，错误约定不一致。
9. **资源泄漏**：`_convert_pdf_with_marker` 的 `tempfile.mkdtemp`（L520）无任何清理路径，每次 marker 调用泄漏一个临时目录。
10. **隐式输入修改**：`_data_to_markdown_table` 原地 `row.append("")` 补齐调用方的二维列表（L219–L221），对当前调用方无害但属隐藏副作用。
11. **纯函数与 I/O 未分离**：`_build_sheet_summary`、`_parse_number`、`_is_sequence_col`、`extract_logical_page_number`、`reorder_text_blocks_by_column`、`_data_to_markdown_table` 都是纯函数，却与文件/子进程 I/O 函数混排，导致纯函数也无法被轻量单测引用（引用即触发全部重依赖导入）。
12. **双真相源**：`load_file` 同时依据 `SUPPORTED_EXTENSIONS`（settings，L1511）与 `FORMAT_LOADER_MAP`（本地，L1514）做两次判断，两者需人工保持同步。
13. **第三方库强耦合**：pymupdf 的 `page`/`doc` 对象直接穿透 8 个函数签名，无任何隔离层；未来更换 PDF 引擎的改动面等于整个 PDF 区块。（是否需要抽象层属架构决策，本次重构不建议引入，见 §11。）

---

## 6. 推荐的新目录结构

设计原则：保持 `app.ingestion.loader` 导入路径完全兼容；依赖单向；每个文件一类职责；不引入新的抽象层。

方案：将 `loader.py` 原地转为 **`loader/` 包**（`loader.py` → `loader/__init__.py`，内容不变），再逐步把内部实现抽到包内子模块。`__init__.py` 作为**门面（facade）**，继续持有全部对外名字（含 `KNOWLEDGE_BASE_DIR` 全局），测试的 monkeypatch 语义不变。

```
backend/app/ingestion/
├── __init__.py                # 不动（继续从 .loader 再导出）
├── loader/
│   ├── __init__.py            # 门面：再导出全部公共名字；保留 KNOWLEDGE_BASE_DIR 等
│   │                          #   配置全局、scan_knowledge_base、load_all_documents、
│   │                          #   FORMAT_LOADER_MAP/load_file（约 100–130 行）
│   ├── document.py            # Document dataclass + documents_to_json/documents_from_json
│   ├── capabilities.py        # 第三方能力探测与版本兼容的唯一集中点：
│   │                          #   pymupdf/fitz fallback、Rect、HAS_*、
│   │                          #   PYMUPDF_GET_TABLES_AVAILABLE、warnings 过滤
│   ├── utils.py               # compute_file_hash、data_to_markdown_table
│   │                          #   （无重依赖，analytics 未来可安全引用以消除重复实现）
│   ├── text_loader.py         # load_txt、load_md
│   ├── docx_loader.py         # load_docx、_extract_docx_images
│   ├── xlsx_loader.py         # load_xlsx、_load_xlsx_openpyxl、_load_xlsx_pandas
│   ├── xlsx_summary.py        # _build_sheet_summary、_parse_number、_is_sequence_col、
│   │                          #   _SUMMARY_MIN_ROWS、_HEADER_SYNONYMS（纯分析，无 I/O）
│   ├── ocr.py                 # _try_ocr_page、_ocr_image_bytes（pytesseract/PIL 统一封装）
│   ├── pdf_loader.py          # load_pdf（页面编排与 Document 组装）
│   ├── pdf_tables.py          # extract_tables_from_page + 三个策略函数 +
│   │                          #   _detect_likely_table_pdf + _convert_pdf_with_marker
│   └── pdf_layout.py          # preprocess_page、detect_columns、
│                              #   reorder_text_blocks_by_column、extract_logical_page_number
├── chunking/                  # 不动
└── indexer.py                 # 不动
```

包内依赖方向（单向，无环）：

```
capabilities ─┐
document ─────┼─► utils ─► {text,docx,xlsx,xlsx_summary,ocr,pdf_layout,pdf_tables,pdf_loader} ─► __init__(门面)
              └───────────────────────────────────────────────────────────────────────────────►
其中：pdf_loader 依赖 pdf_tables/pdf_layout/ocr/document/capabilities
      xlsx_loader 依赖 xlsx_summary/utils/document/capabilities
      docx_loader 依赖 ocr/document/capabilities
      __init__ 依赖所有子模块（注册表在此组装）
```

不采用的备选：保留 `loader.py` 单文件门面 + 平级兄弟模块（如 `ingestion/parsing/`）。可行，但会让 `ingestion/` 根部出现 10+ 个平级文件、且与既有 `chunking/` 包风格不一致，故不推荐。

---

## 7. 原函数到新文件的迁移映射表

| 原成员（loader.py） | 新位置 | 备注 |
|---|---|---|
| `Document`、`documents_to_json`、`documents_from_json` | `loader/document.py` | 公共底座，最先迁移 |
| `compute_file_hash` | `loader/utils.py` | 保持从门面导出 |
| `_data_to_markdown_table` | `loader/utils.py` | 保持私有名与行为（含原地补列）不变 |
| 导入兼容块（L23–L132）：`HAS_*`、`Rect`、`_pdfplumber_version`、`PYMUPDF_GET_TABLES_AVAILABLE`、warnings 过滤 | `loader/capabilities.py` | 导入期副作用时序保持不变 |
| `load_txt`、`load_md` | `loader/text_loader.py` | — |
| `load_docx`、`_extract_docx_images` | `loader/docx_loader.py` | — |
| `_try_ocr_page`、`_ocr_image_bytes` | `loader/ocr.py` | 两处 OCR 归一 |
| `load_xlsx`、`_load_xlsx_openpyxl`、`_load_xlsx_pandas` | `loader/xlsx_loader.py` | — |
| `_build_sheet_summary`、`_parse_number`、`_is_sequence_col`、`_SUMMARY_MIN_ROWS`、`_HEADER_SYNONYMS` | `loader/xlsx_summary.py` | 纯函数模块，无重依赖 |
| `preprocess_page`、`detect_columns`、`reorder_text_blocks_by_column`、`extract_logical_page_number` | `loader/pdf_layout.py` | — |
| `extract_tables_from_page`、`extract_tables_with_pdfplumber`、`extract_tables_with_get_tables`、`extract_tables_with_dict`、`_detect_likely_table_pdf`、`_convert_pdf_with_marker` | `loader/pdf_tables.py` | 最复杂子系统，单独成文件 |
| `load_pdf` | `loader/pdf_loader.py` | 仅编排，依赖上面三个 pdf/ocr 模块 |
| `FORMAT_LOADER_MAP`、`load_file` | `loader/__init__.py`（门面） | 也可放 `registry.py`；因注册表需引用全部解析函数，放门面最简单且避免环 |
| `KNOWLEDGE_BASE_DIR`、`SUPPORTED_EXTENSIONS`、`PDF_SUBDIR`、`USE_MARKER_FOR_PDF`、`logger` | `loader/__init__.py`（门面） | **测试 monkeypatch 契约要求 `KNOWLEDGE_BASE_DIR` 必须留在 `app.ingestion.loader` 命名空间** |
| `scan_knowledge_base`、`load_all_documents` | `loader/__init__.py`（门面） | 二者读取上述模块全局；留在门面可原样保持 monkeypatch 语义（若未来移出，须改为动态读门面属性） |
| `if __name__ == "__main__"` 块（L1586–L1592） | `loader/__init__.py` 或删除 | 包的 `__init__.py` 不再支持 `python -m` 直跑；如需保留本地冒烟，可另建 `python -m app.ingestion.loader` 的 `__main__.py`（属可选，不算必须新增） |

---

## 8. 现有测试覆盖映射表

搜索范围：`backend/tests/`（51 个测试文件）与 `backend/eval/`。

| 原有函数/职责 | 对应测试 | 覆盖情况 | 重构风险 |
|---|---|---|---|
| `scan_knowledge_base` | [test_versioned_indexing.py L55–L67](file:///d:/PythonProjects/personal_rag/backend/tests/test_versioned_indexing.py#L55-L67) | 间接覆盖：验证 `.versions/` 不被扫描；**依赖 monkeypatch `loader.KNOWLEDGE_BASE_DIR`** | **高**：全局名或读取方式变化会直接挂测试 |
| `load_file` | test_versioned_indexing.py L15 | 仅以 monkeypatch 桩替换（`indexer.load_file`），真实逻辑未测 | 中：需保持 indexer 命名空间内 `load_file` 名字可被 patch |
| `load_pdf` / 表格三级降级 / 双栏重排 / 逻辑页码 / marker | 无 | **零覆盖** | **高**：行为回归无法被测试捕获 |
| `_try_ocr_page` / `_ocr_image_bytes`（OCR） | 无 | 零覆盖 | 高 |
| `load_docx` / `_extract_docx_images` | 无 | 零覆盖 | 高 |
| `load_xlsx` / `_load_xlsx_openpyxl` / `_load_xlsx_pandas` | 无 | 零覆盖 | 高 |
| `_build_sheet_summary` / `_parse_number` / `_is_sequence_col` | 无（[test_schema_infer.py](file:///d:/PythonProjects/personal_rag/backend/tests/test_schema_infer.py) 测的是 schema_infer 的同口径副本，非 loader 本体） | 零覆盖 | 中：纯函数，迁移前补测成本低 |
| `load_txt`（编码探测）/ `load_md` | 无 | 零覆盖 | 中 |
| `compute_file_hash` | 无 | 零覆盖（但被 3 个生产模块直接导入） | 中：签名/路径变更影响面大 |
| `Document` / `to_dict` / `from_dict` | 无 | 零覆盖 | 中 |
| `documents_to_json` / `documents_from_json` | 无 | 零覆盖且零调用 | 低 |
| `load_all_documents` | 无 | 零覆盖、零生产调用 | 低 |
| 下游联动（chunking） | [test_chunking.py](file:///d:/PythonProjects/personal_rag/backend/tests/test_chunking.py) | 覆盖 chunking 自身，不覆盖 loader 输出 | — |
| 入库编排 | [test_ingest_worker.py](file:///d:/PythonProjects/personal_rag/backend/tests/test_ingest_worker.py)、[test_document_versions.py](file:///d:/PythonProjects/personal_rag/backend/tests/test_document_versions.py) | 全部注入 fake `process_file`，不触 loader | — |

**结论**：除目录扫描外，解析层完全没有测试保护。任何拆分都应先补「特征测试」（见 §9 阶段 0）。另注意仓库规则（AGENTS.md）要求 `.\scripts\check.ps1 -Full` 对检索类变更做基线评估——解析输出变化会体现在检索基线上，可作为额外的回归信号（需要动态验证）。

---

## 9. 分阶段重构计划

总原则：**每阶段一次独立提交；只移动代码，不改逻辑、不改签名、不升级依赖、不修 bug（含 §5.9 的 temp 泄漏也留到重构后单独处理）**。每阶段开始时 `loader` 门面对外导出集合保持不变。

### 阶段 0：补特征测试（前置安全网）

- 内容：为 5 个格式入口 + 纯函数建立基线测试——用 `knowledge_base/` 现有样例文件断言「当前实际输出」（Document 数量、metadata 键、关键文本片段）；纯函数（`_data_to_markdown_table`、`extract_logical_page_number`、`_parse_number`、`_is_sequence_col`、`_build_sheet_summary`、`reorder_text_blocks_by_column`）直接单测。可选：记录一次 `check.ps1 -Full` 检索基线。
- 新增/修改：仅 `backend/tests/` 新增测试文件（此阶段属重构实施期工作，不在本次审计范围内执行）。
- 成功判据：新测试在当前代码上全绿。
- 回滚：删除新增测试即可，无生产代码变化。

### 阶段 1：`loader.py` → `loader/` 包（纯搬家）

- 迁移：整个文件内容原样变为 `loader/__init__.py`，删除 `loader.py`。
- 新增：`backend/app/ingestion/loader/__init__.py`。修改：删除旧 `loader.py`。
- 保留接口：全部（导入路径 `app.ingestion.loader` 语义不变，包模块同样支持 `from ... import X` 与属性 monkeypatch）。
- 测试：`python -m pytest -q`、`scripts/check.ps1`；重点确认 `test_versioned_indexing.py` 全绿。
- 成功判据：所有现有测试通过，`from app.ingestion.loader import load_file, Document, compute_file_hash, scan_knowledge_base, load_all_documents` 可用。
- 回滚：`git revert` 本提交。

### 阶段 2：抽出零依赖叶子模块（`document.py` + `utils.py`）

- 迁移：`Document`、`documents_to_json/from_json` → `document.py`；`compute_file_hash`、`_data_to_markdown_table` → `utils.py`。
- 修改：`loader/__init__.py` 改为从子模块导入并再导出（含 `__all__` 不变）。
- 保留接口：`Document`、`compute_file_hash` 从 `app.ingestion.loader` 可导入。
- 测试：阶段 0 测试 + 全量 pytest。
- 成功判据：测试全绿；`app.ingestion.loader.Document is app.ingestion.loader.document.Document`。
- 回滚：`git revert`。

### 阶段 3：抽出 `capabilities.py`

- 迁移：L23–L132 全部能力探测/兼容代码与 `HAS_*`、`PYMUPDF_GET_TABLES_AVAILABLE`、warnings 过滤。
- 关键点：**导入期副作用时序不变**（门面导入 capabilities 即触发，等价于现状）；门面再导出全部开关名。
- 测试：全量 pytest + 一次真实 PDF 解析冒烟（需要动态验证）。
- 回滚：`git revert`。

### 阶段 4：抽出简单格式（`text_loader.py`、`xlsx_summary.py`、`xlsx_loader.py`、`docx_loader.py`、`ocr.py`）

- 迁移：按 §7 映射表。建议顺序：text → xlsx_summary（纯函数）→ xlsx → ocr → docx。
- 修改：门面再导出 `load_txt/load_md/load_docx/load_xlsx`（`FORMAT_LOADER_MAP` 仍在门面，引用新位置函数）。
- 测试：阶段 0 特征测试 + 全量 pytest。
- 成功判据：各格式输出与迁移前逐字段一致（特征测试保证）。
- 回滚：每个文件一次提交，逐个 `git revert`。

### 阶段 5：抽出 PDF 三件套（`pdf_layout.py` → `pdf_tables.py` → `pdf_loader.py`）

- 迁移：按依赖顺序逐个抽出；`load_pdf` 最后移动。
- 测试：阶段 0 的 PDF 特征测试 + 全量 pytest；本阶段完成后建议跑一次 `scripts/check.ps1 -Full` 对比检索基线（解析输出未变则基线应不变；需要动态验证）。
- 成功判据：同文件同输入产生的 Document 列表与迁移前完全一致。
- 回滚：逐提交 `git revert`。

### 阶段 6：门面收尾

- 内容：整理 `loader/__init__.py` 的 `__all__` 与注释；确认 `scan_knowledge_base`/`load_all_documents`/配置全局按 §7 留在门面；删除迁移中产生的过渡注释。不做任何行为变更。
- 测试：全量 pytest + `check.ps1`。
- 回滚：`git revert`。

**贯穿性验收**：每阶段后 `from app.ingestion import loader; loader.KNOWLEDGE_BASE_DIR` 可读写、`test_versioned_indexing.py` 不修改而全绿，是硬约束。

---

## 10. 重构风险与回滚方案

| 风险 | 依据 | 缓解 | 回滚 |
|---|---|---|---|
| 测试对 `loader.KNOWLEDGE_BASE_DIR` 的 monkeypatch 失效 | test_versioned_indexing.py L59 | 配置全局与 `scan_knowledge_base` 留在门面 `__init__.py`，不移入子模块 | `git revert` 对应阶段 |
| 导入期副作用时序变化（warnings 过滤、pymupdf 探测） | L33–L34、L110–L132 | capabilities 由门面第一行导入，时序等价；阶段 3 后做真实 PDF 冒烟 | `git revert` |
| 解析行为隐性回归（无测试区） | §8 零覆盖 | 阶段 0 先行；特征测试断言实际输出而非「应有输出」 | 特征测试本身即回归定位器 |
| 循环导入 | 注册表引用全部解析函数 | 依赖方向约束见 §6；`FORMAT_LOADER_MAP` 留在门面 | 调整注册表位置 |
| 外部导入路径破坏 | §4.1 五个调用方 | 门面再导出集合冻结；每阶段跑全量 pytest（含 import 期） | `git revert` |
| `__main__` 冒烟入口失效 | L1586–L1592 | 包化后 `python app/ingestion/loader.py` 不再可行；如需保留，加 `loader/__main__.py`（可选） | 不影响生产 |
| 顺手修 bug 导致行为漂移 | §5 所列缺陷 | 明确纪律：泄漏/死参数/吞异常等问题**重构完成后另开任务处理** | — |
| `check.ps1 -Full` 基线波动 | 检索基线对解析输出敏感 | 阶段 5 后对比基线；差异为 0 才算通过（需要动态验证） | 依据基线差异定位阶段 |

---

## 11. 最终建议

1. **按 §9 顺序执行，阶段 0 不可跳过**。当前解析层零测试是最大风险，特征测试是一次性投入、长期收益。
2. **优先迁移顺序**：`Document`/`compute_file_hash`/`_data_to_markdown_table`（叶子，收益立现）→ `xlsx_summary` 纯函数组（可顺带为消除 schema_infer 重复实现铺路）→ PDF 表格子系统（最复杂、最需隔离）。
3. **门面策略是兼容性的关键**：保持 `app.ingestion.loader` 导入路径与 `KNOWLEDGE_BASE_DIR` 全局契约，五个调用方与测试零改动。
4. **本次重构不做的事**（避免范围蔓延）：不抽象 PDF 引擎接口、不修复 temp 泄漏/死参数/异常吞没、不改 `SUPPORTED_EXTENSIONS` 与 `FORMAT_LOADER_MAP` 双真相源、不升级任何依赖。这些问题已在 §5 记录，建议重构落地后逐项立项。
5. **验证基线**：以 `python -m pytest -q` + `scripts/check.ps1` 为每阶段门禁；检索级回归用 `check.ps1 -Full`（需运行中的基础设施与已索引 Qdrant，属动态验证）。
6. 重构完成后，`loader/` 包约 12 个文件、每个 60–450 行，单文件职责单一；`compute_file_hash` 等轻量调用方未来可改为从 `app.ingestion.loader.utils` 精确导入，摆脱重依赖传染（届时再评估，非本次必须）。

---

### 附：需要动态验证的事项清单

- 阶段 3/5 后的真实 PDF/DOCX/XLSX 解析冒烟（静态分析无法覆盖运行时行为）。
- `check.ps1 -Full` 检索基线对比（依赖运行中的 Qdrant/Postgres 与已索引语料）。
- `PYMUPDF_GET_TABLES_AVAILABLE` 在当前环境 Python/PyMuPDF 版本下的实际取值（影响表格策略2是否生效）。
- marker CLI（`marker_single`）在生产环境是否可用及其超时表现。
