"""loader 解析输出的 golden 快照门禁（重构安全网）。

目的：`app/ingestion/loader.py` 的解析层此前**零测试覆盖**（tests/ 中
`load_pdf/load_docx/load_xlsx/compute_file_hash` 命中数为 0）。任何结构性重构
（拆分 / 搬移 / 延迟导入）都缺少"行为未变"的证据。本文件用 golden 快照把它补上。

记录的是什么（逐字节语义）：
    - 整个文档列表序列化后的 sha256（`output_sha256`）—— 漂移即失败
    - 每个文档的 metadata 全量（路径已归一化，见下）
    - 每个文档的 text 长度与 sha256；短文本额外内联全文，便于肉眼 diff

为什么不直接存 documents_to_json 原文：
    `xlsx-sample-large-10000-rows.xlsx` 的单个 sheet 文档就有约 836 KB 文本，
    全量内联会让 fixture 膨胀到几十 MB。sha256 + text_len 已经是逐字节等价的判据，
    `text_preview` 只负责让失败可读。

路径归一化：
    metadata["full_path"] 含绝对路径，换机器/换目录就会误报。写入与比对前
    统一把知识库根目录替换为 `<KB>`，其余部分用正斜杠。

刷新基线（仅在**确认行为变更符合预期**时使用）：
    PowerShell:  $env:LOADER_GOLDEN_UPDATE="1"; python -m pytest tests/test_loader_golden.py -q
    bash:        LOADER_GOLDEN_UPDATE=1 python -m pytest tests/test_loader_golden.py -q
    刷新后必须人工 review golden 文件的 diff，并在提交信息里说明为什么行为变了。

已知未覆盖（如实记档）：
    - OCR 分支：本环境 `pytesseract` 未安装（PIL 已装），`_try_ocr_page` /
      `_ocr_image_bytes` 恒走"跳过 OCR"路径。这两条只能靠代码审阅 + 降级输出比对。
    - marker 兜底：`marker_single` 不在 PATH，`_convert_pdf_with_marker` 恒返回 None。
    - pandas 降级通道：`_load_xlsx_pandas` 仅在 openpyxl 缺失时可达，本环境不可达。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List

import pytest

from app.ingestion import loader

TESTS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TESTS_DIR.parent
KB_ROOT = BACKEND_DIR.parent / "knowledge_base"
GOLDEN_DIR = TESTS_DIR / "fixtures" / "loader_golden"
UPDATE_MODE = os.environ.get("LOADER_GOLDEN_UPDATE") == "1"

# 内联全文的阈值：短文档（md/txt/小 docx）便于 review，长文档只存摘要
INLINE_TEXT_LIMIT = 600
PREVIEW_CHARS = 120


def _sample(name: str) -> Path:
    """知识库样张路径（用正斜杠书写，跨平台一致）。"""
    return KB_ROOT / name


# golden 覆盖的样张清单。新增样张时：加入此列表 → 用 UPDATE=1 刷新 → review diff。
GOLDEN_SAMPLES: List[str] = [
    # 纯文本
    "product_spec.txt",
    "hardware_spec.md",
    "tech_maunal.md",
    "contract_snippet.md",
    "warranty_policy.md",
    # DOCX
    "docx-sample-with-table.docx",
    "docx-sample-with-images.docx",
    "faq.docx",
    "sample-files.com-table-document.docx",
    "sample-files.com-image-document.docx",
    # XLSX（含 10000 行大表：触发 _build_sheet_summary 摘要块）
    "cost_data.xlsx",
    "xlsx-sample-multiple-sheets.xlsx",
    "xlsx-sample-large-10000-rows.xlsx",
    # PDF（含双栏表格 / A3 / 横向 / 50 页多页）
    "pdf_examples/sample-pdf-with-table.pdf",
    "pdf_examples/pdf-sample-with-table.pdf",
    "pdf_examples/pdf-sample-a3.pdf",
    "pdf_examples/pdf-sample-landscape.pdf",
    "pdf_examples/pdf-sample-50-pages.pdf",
]


def _golden_path(sample: str) -> Path:
    return GOLDEN_DIR / (sample.replace("/", "__") + ".json")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize(value: Any) -> Any:
    """把绝对路径替换为 <KB>，使快照跨机器可比。"""
    if isinstance(value, str):
        text = value.replace(str(KB_ROOT), "<KB>")
        return text.replace(str(KB_ROOT).replace("\\", "/"), "<KB>")
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    return value


def build_record(sample: str) -> Dict[str, Any]:
    """调用真实解析器，生成该样张的 golden 记录。"""
    path = _sample(sample)
    documents = loader.load_file(path)
    serializer = getattr(loader, "documents_to_json")
    payload = serializer(documents)

    recorded: List[Dict[str, Any]] = []
    for index, doc in enumerate(documents):
        text = doc.text
        item: Dict[str, Any] = {
            "index": index,
            "metadata": _normalize(json.loads(json.dumps(doc.metadata, ensure_ascii=False))),
            "text_len": len(text),
            "text_sha256": _sha256(text),
            "text_preview": text[:PREVIEW_CHARS],
        }
        if len(text) <= INLINE_TEXT_LIMIT:
            item["text"] = text
        recorded.append(item)

    return {
        "sample": sample,
        "document_count": len(documents),
        "output_sha256": _sha256(payload),
        "documents": recorded,
    }


def _diff(expected: Any, actual: Any, where: str = "$", limit: int = 12) -> List[str]:
    """产出可读的差异列表（最多 limit 条），避免失败输出淹没在巨型 JSON 里。"""
    out: List[str] = []

    def walk(exp: Any, act: Any, path: str) -> None:
        if len(out) >= limit:
            return
        if type(exp) is not type(act):
            out.append(f"{path}: 类型 {type(exp).__name__} -> {type(act).__name__}")
            return
        if isinstance(exp, dict):
            for key in sorted(set(exp) | set(act)):
                if key not in exp:
                    out.append(f"{path}.{key}: 新增 {act[key]!r}")
                elif key not in act:
                    out.append(f"{path}.{key}: 丢失（原值 {exp[key]!r}）")
                else:
                    walk(exp[key], act[key], f"{path}.{key}")
            return
        if isinstance(exp, list):
            if len(exp) != len(act):
                out.append(f"{path}: 列表长度 {len(exp)} -> {len(act)}")
            for i in range(min(len(exp), len(act))):
                walk(exp[i], act[i], f"{path}[{i}]")
            return
        if exp != act:
            out.append(f"{path}: {exp!r} -> {act!r}")

    walk(expected, actual, where)
    if len(out) >= limit:
        out.append(f"...（差异超过 {limit} 条，已截断）")
    return out


@pytest.mark.parametrize("sample", GOLDEN_SAMPLES)
def test_loader_output_matches_golden(sample: str) -> None:
    """同一份样张的解析输出必须与 golden 逐字节一致。"""
    assert _sample(sample).exists(), f"样张缺失: {_sample(sample)}"

    actual = build_record(sample)
    target = _golden_path(sample)

    if UPDATE_MODE:
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(actual, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        pytest.skip(f"golden 已刷新: {target.name}")

    assert target.exists(), (
        f"缺少 golden 基线: {target}\n"
        f'首次生成请运行: $env:LOADER_GOLDEN_UPDATE="1"; python -m pytest tests/test_loader_golden.py -q'
    )

    expected = json.loads(target.read_text(encoding="utf-8"))
    diffs = _diff(expected, actual)
    assert not diffs, (
        f"{sample} 的解析输出与 golden 不一致（{len(diffs)} 处差异）：\n"
        + "\n".join(f"  - {d}" for d in diffs)
        + "\n\n若行为变更是预期的，用 LOADER_GOLDEN_UPDATE=1 刷新并 review diff。"
    )


def test_golden_covers_every_supported_format() -> None:
    """五个格式入口都必须至少有一个 golden 样张 —— 否则"全绿"是假的。

    只与 SUPPORTED_EXTENSIONS 对齐，不依赖 FORMAT_LOADER_MAP：后者的形态（
    真实函数引用 → 模块路径路由）本身就是重构对象，守门测试不该绑在待改形态上。
    """
    suffixes = {Path(name).suffix.lower() for name in GOLDEN_SAMPLES}
    supported = set(loader.SUPPORTED_EXTENSIONS)
    missing = supported - suffixes
    assert not missing, f"以下格式缺少 golden 样张: {sorted(missing)}"
    assert suffixes == supported, (
        f"golden 样张后缀 {sorted(suffixes)} 与 SUPPORTED_EXTENSIONS {sorted(supported)} 不一致"
    )


def test_scan_finds_every_golden_sample() -> None:
    """目录扫描必须能覆盖 golden 用到的全部样张（含 pdf_examples/ 子目录）。"""
    scanned = {path.name for path in loader.scan_knowledge_base()}
    expected = {Path(name).name for name in GOLDEN_SAMPLES}
    missing = expected - scanned
    assert not missing, f"scan_knowledge_base 未覆盖: {sorted(missing)}"


def test_golden_rebuild_is_deterministic() -> None:
    """同一份样张连续解析两次必须完全一致 —— golden 的前提是解析确定性。"""
    sample = "pdf_examples/pdf-sample-a3.pdf"
    first = build_record(sample)
    second = build_record(sample)
    diffs = _diff(first, second)
    assert not diffs, f"解析结果不确定，golden 不可用：\n" + "\n".join(diffs)
