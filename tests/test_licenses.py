"""许可证检查的测试（T0）。调用 scripts/check_licenses.py 中的函数，不联网。

断言：
- 默认依赖闭包中没有 GPL / AGPL，且没有 FAIL；
- 未安装 PyMuPDF（fitz / pymupdf）；
- 分类函数对典型 SPDX 字符串的判定正确。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_licenses.py"


def _load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_licenses", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclass 需要模块已注册
    spec.loader.exec_module(mod)
    return mod


checker = _load_checker()


def test_default_closure_has_no_gpl_or_agpl_and_no_fail() -> None:
    dists, missing = checker.closure(checker.default_roots())
    assert not missing, f"默认依赖未安装: {missing}"
    results = [checker.check_one(k, v) for k, v in sorted(dists.items())]
    bad = [r for r in results if r.license_id in ("GPL", "AGPL") or r.license_id.startswith(("GPL", "AGPL"))]
    assert not bad, f"默认闭包含 GPL/AGPL: {[r.name for r in bad]}"
    fails = [r for r in results if r.status == "FAIL"]
    assert not fails, f"默认闭包存在 FAIL: {[(r.name, r.note) for r in fails]}"


def test_pymupdf_not_installed() -> None:
    assert importlib.util.find_spec("fitz") is None
    assert importlib.util.find_spec("pymupdf") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("MIT", "MIT"),
        ("Apache-2.0", "Apache-2.0"),
        ("Apache-2.0 OR BSD-3-Clause", "Apache-2.0"),
        ("PSF-2.0", "PSF-2.0"),
        ("GPL-3.0-only", "GPL"),
        ("AGPL-3.0-only", "AGPL"),
        ("LGPL-2.1-or-later", "LGPL"),
        ("MPL-2.0", "MPL-2.0"),
        ("Totally Unknown License", "UNKNOWN"),
    ],
)
def test_classify(raw: str, expected: str) -> None:
    ident, _ = checker.classify(raw)
    assert ident == expected
