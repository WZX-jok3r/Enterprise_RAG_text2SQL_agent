"""格式隔离守卫：把"解耦"变成可执行的断言。

背景（2026-11-12 实测）：`from app.ingestion.loader import compute_file_hash`
冷启动 **2022 ms / 1864 个模块**，连带 pymupdf、pdfplumber、pandas、openpyxl、
python-docx、qdrant_client、sqlalchemy、requests、jieba 全部进场。根因有两层：
父包 `app/ingestion/__init__.py` 的急切三连导入 + loader 单文件模块体全量执行。

本文件的职责不是"今天全绿"，而是给重构立**可验收的目标**：

- `TestProbeSanity` / `test_harness_*`：保证测量手段本身可信（否则会出现
  "探针坏了 → 集合为空 → 假绿"这种最糟糕的结果）；
- `TestTargetIsolation`：分场景、分层的目标断言，今天一律 `xfail(strict=True)`。
  某项真正满足时它会 **XPASS(strict) 失败**，逼我们摘掉标记 ——
  这样每一层的收益兑现都会自动"敲门"，而不是靠人记。

各项与重构阶段的对应关系写在 reason 里，可直接当阶段清单读。

说明：探测在**独立子进程**里做，因为 sys.modules 只能反映当前进程；
同进程内早已 import 过的库无法再判定"会不会被加载"。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, FrozenSet

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
KB_ROOT = BACKEND_DIR.parent / "knowledge_base"

# 解析类重依赖（按格式归属）
PARSER_LIBS = (
    "pymupdf", "fitz",            # PDF
    "pdfplumber",                 # PDF 表格
    "pandas", "openpyxl",         # XLSX
    "docx",                       # DOCX（python-docx）
    "pytesseract", "PIL",         # OCR
)
# 向量层与传输层（与解析无关，来自 indexer → app.vector.qdrant）
VECTOR_LIBS = ("qdrant_client", "sqlalchemy", "requests", "jieba")
PROBE = tuple(dict.fromkeys(PARSER_LIBS + VECTOR_LIBS + ("numpy",)))

_PDF_ONLY = frozenset({"pymupdf", "fitz", "pdfplumber"})
_XLSX_ONLY = frozenset({"pandas", "openpyxl"})
_DOCX_ONLY = frozenset({"docx"})


def _scenario_action(name: str) -> str:
    txt = (KB_ROOT / "product_spec.txt").as_posix()
    xlsx = (KB_ROOT / "cost_data.xlsx").as_posix()
    pdf = (KB_ROOT / "pdf_examples" / "pdf-sample-a3.pdf").as_posix()
    actions = {
        "import_hash": "from app.ingestion.loader import compute_file_hash",
        "scan": "from app.ingestion.loader import scan_knowledge_base; scan_knowledge_base()",
        "parse_txt": (
            "from pathlib import Path;"
            "from app.ingestion.loader import load_file;"
            f'load_file(Path("{txt}"))'
        ),
        "parse_xlsx": (
            "from pathlib import Path;"
            "from app.ingestion.loader import load_file;"
            f'load_file(Path("{xlsx}"))'
        ),
        "parse_pdf": (
            "from pathlib import Path;"
            "from app.ingestion.loader import load_file;"
            f'load_file(Path("{pdf}"))'
        ),
    }
    return actions[name]


SCENARIOS = ("import_hash", "scan", "parse_txt", "parse_xlsx", "parse_pdf")


def _run_python(code: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _measure(action: str) -> Dict[str, object]:
    """在子进程里执行 action，返回它加载的重依赖集合。"""
    probe_repr = repr(set(PROBE))
    code = "\n".join(
        [
            "import sys",
            action,
            f"probe = {probe_repr}",
            "hits = sorted(set(sys.modules) & probe)",
            'print("FOOTPRINT:" + ",".join(hits))',
            'print("MODCOUNT:%d" % len(sys.modules))',
        ]
    )
    proc = _run_python(code)
    modules: FrozenSet[str] = frozenset()
    modcount = -1
    for line in (proc.stdout or "").splitlines():
        if line.startswith("FOOTPRINT:"):
            payload = line[len("FOOTPRINT:"):].strip()
            modules = frozenset(p for p in payload.split(",") if p)
        elif line.startswith("MODCOUNT:"):
            modcount = int(line[len("MODCOUNT:"):].strip() or -1)
    return {
        "returncode": proc.returncode,
        "modules": modules,
        "modcount": modcount,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "code": code,
    }


@pytest.fixture(scope="session")
def footprints() -> Dict[str, Dict[str, object]]:
    """每个场景只跑一次子进程（今天单次约 2 s，5 个场景约 11 s）。"""
    return {name: _measure(_scenario_action(name)) for name in SCENARIOS}


# --------------------------------------------------------------------------
# 1) 测量手段自身的可信度（这两条必须永远为绿）
# --------------------------------------------------------------------------

def test_probe_detects_known_heavy_import() -> None:
    """探针必须真的能看见重依赖 —— 否则所有隔离断言都会因"空集合"而假绿。"""
    result = _measure("import pandas as pd; import pymupdf")
    assert result["returncode"] == 0, result["stderr"]
    found = result["modules"]
    assert "pandas" in found and "pymupdf" in found, f"探针失效，仅测到: {sorted(found)}"


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_harness_reports_every_scenario(scenario, footprints) -> None:
    """子进程必须正常退出并打印足迹；崩溃不能被 xfail 悄悄吞掉。"""
    result = footprints[scenario]
    assert result["returncode"] == 0, (
        f"场景 {scenario} 子进程失败（rc={result['returncode']}）：\n{result['stderr'][-1500:]}"
    )
    assert "FOOTPRINT:" in (result["stdout"] or ""), (
        f"场景 {scenario} 未输出足迹：\n{result['stdout']}\n{result['stderr'][-800:]}"
    )


# --------------------------------------------------------------------------
# 2) 目标隔离（今天 xfail，兑现后 strict XPASS 逼我们摘标记）
# --------------------------------------------------------------------------

def _assert_absent(footprints, scenario: str, forbidden: FrozenSet[str], hint: str) -> None:
    loaded = set(footprints[scenario]["modules"])
    overlapping = loaded & forbidden
    assert not overlapping, (
        f"[{scenario}] 仍加载了本不该在场的依赖: {sorted(overlapping)}\n"
        f"{hint}\n实际足迹: {sorted(loaded)}（共 {footprints[scenario]['modcount']} 个模块）"
    )


@pytest.mark.xfail(
    strict=True,
    reason="父包 app/ingestion/__init__.py 仍急切导入 indexer → qdrant 层（重构阶段二薄化后应转绿）",
)
def test_import_hash_does_not_pull_vector_layer(footprints) -> None:
    _assert_absent(
        footprints, "import_hash", frozenset(VECTOR_LIBS),
        "compute_file_hash 只做 md5，不该需要 qdrant/sqlalchemy/requests/jieba。",
    )


@pytest.mark.xfail(
    strict=True,
    reason="loader 单文件模块体全量执行，解析库必然进场（重构阶段三~六陆续迁出后转绿）",
)
def test_import_hash_does_not_pull_parsers(footprints) -> None:
    _assert_absent(
        footprints, "import_hash", frozenset(PARSER_LIBS),
        "仅导入哈希函数不应加载任何解析库。",
    )


@pytest.mark.xfail(
    strict=True,
    reason="scan_knowledge_base 与重依赖同处单文件模块（阶段三包化 + 门面轻化后转绿）",
)
def test_scan_does_not_pull_parsers(footprints) -> None:
    _assert_absent(
        footprints, "scan", frozenset(PARSER_LIBS),
        "目录扫描只用 pathlib，不该加载解析库。",
    )


@pytest.mark.xfail(
    strict=True,
    reason=".txt 路由目前直接引用模块内函数，解析库随模块体加载（阶段四迁出 text 域后转绿）",
)
def test_parse_txt_does_not_pull_binary_parsers(footprints) -> None:
    _assert_absent(
        footprints, "parse_txt", _PDF_ONLY | _XLSX_ONLY | _DOCX_ONLY,
        "解析纯文本不应加载 PDF/XLSX/DOCX 解析库。",
    )


@pytest.mark.xfail(
    strict=True,
    reason=".xlsx 与 PDF/DOCX 同在单文件模块（阶段五迁出 xlsx 域后转绿）",
)
def test_parse_xlsx_does_not_pull_other_formats(footprints) -> None:
    _assert_absent(
        footprints, "parse_xlsx", _PDF_ONLY | _DOCX_ONLY,
        "解析 XLSX 不应加载 PDF/DOCX 解析库。",
    )


@pytest.mark.xfail(
    strict=True,
    reason=".pdf 与 XLSX/DOCX 同在单文件模块（阶段六迁出 PDF 域后转绿）",
)
def test_parse_pdf_does_not_pull_other_formats(footprints) -> None:
    _assert_absent(
        footprints, "parse_pdf", _XLSX_ONLY | _DOCX_ONLY,
        "解析 PDF 不应加载 pandas/openpyxl/python-docx。",
    )
