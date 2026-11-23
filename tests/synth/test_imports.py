"""AST 扫描：synth 包不得 import 解析器、取值模块、校验模块或 PyMuPDF（金标准独立性与许可证）。"""

import ast
from pathlib import Path

SYNTH_ROOT = Path(__file__).resolve().parents[2] / "src" / "citecell" / "synth"
FORBIDDEN_PREFIXES = (
    "citecell.parsing",
    "citecell.values",
    "citecell.verify",
    "citecell.backends",
    "fitz",
    "pymupdf",
)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods.add(node.module)
    return mods


def test_synth_package_has_no_forbidden_imports():
    files = sorted(SYNTH_ROOT.rglob("*.py"))
    assert files, "synth 包未找到源文件"
    violations = []
    for path in files:
        for mod in _imported_modules(path):
            if any(mod == p or mod.startswith(p + ".") for p in FORBIDDEN_PREFIXES):
                violations.append(f"{path.relative_to(SYNTH_ROOT)}: {mod}")
    assert not violations, "\n".join(violations)
