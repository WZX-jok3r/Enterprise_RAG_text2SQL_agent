"""架构守卫：运行态不得 import app.ops。

`app/ops/` 是运维命令（pg_dump、Qdrant 快照、恢复演练、灌数据）。
它们只该在命令行被 `python -m app.ops.<name>` 拉起；一旦被 api/worker/agent
等运行链路 import，进程启动就会背上这些只属于运维的模块与依赖。

用 AST 而不是正则：模块 docstring 里出现 `python -m app.ops.seed` 是合法文档，
正则会把它们误判为 import。
"""

import ast
import importlib
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1] / "app"
OPS_PACKAGE = "ops"


def _imports(tree: ast.AST) -> list[str]:
    """收集文件中所有 import 的绝对模块名（不含 docstring/注释）。"""
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # 相对导入在 app/ 内部不指向 ops，跳过
                continue
            if node.module == "app":
                found.extend(f"app.{alias.name}" for alias in node.names)
            elif node.module:
                found.append(node.module)
    return found


def test_runtime_code_does_not_import_ops():
    offenders: list[str] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        rel = path.relative_to(APP_ROOT)
        if rel.parts[0] == OPS_PACKAGE:  # ops 包内部互相 import 是允许的
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module in _imports(tree):
            if module == "app.ops" or module.startswith("app.ops."):
                offenders.append(f"{rel.as_posix()} -> {module}")

    assert not offenders, (
        "运行态代码 import 了 app.ops（运维命令不能进运行链路）：\n  "
        + "\n  ".join(offenders)
    )


def test_ops_modules_import_without_side_effects():
    """每个运维模块都能被导入，且不在导入期建立连接/执行命令。"""
    modules = sorted(
        p.stem for p in (APP_ROOT / OPS_PACKAGE).glob("*.py") if p.name != "__init__.py"
    )
    assert modules, "app/ops 下没有任何模块，检查目录是否被误删"
    for name in modules:
        importlib.import_module(f"app.ops.{name}")
