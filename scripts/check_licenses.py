"""依赖许可证检查（只读取已安装包的元数据与 LICENSE 文件，不联网）。

用法：
    .venv/bin/python scripts/check_licenses.py --default-only
    .venv/bin/python scripts/check_licenses.py --all
    .venv/bin/python scripts/check_licenses.py --default-only --write docs/THIRD_PARTY_LICENSES.md

判定规则（见 docs/ARCHITECTURE.md §13）：
- 白名单 PASS：MIT、BSD-2-Clause、BSD-3-Clause、Apache-2.0、ISC、PSF-2.0、Python-2.0
- MPL-2.0、LGPL：WARN（需人工批准）
- GPL / AGPL：FAIL
- 无法识别：FAIL（要求人工核对 dist-info 中列出的 LICENSE 文件）
- 双许可（OR）：只要其中一个选项在白名单内即 PASS，并输出 NOTE

退出码：存在 FAIL 时为 1，否则为 0。
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"

PERMISSIVE = {
    "MIT",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "Apache-2.0",
    "ISC",
    "PSF-2.0",
    "Python-2.0",
}
WARN_LICENSES = {"MPL-2.0", "LGPL"}
# 与白名单相近但不在白名单内的 SPDX 变体：不自动放行，输出 WARN 由人工确认
NEAR_PERMISSIVE = {"MIT-0", "MIT-CMU"}
FAIL_PREFIXES = ("GPL", "AGPL")

# 元数据字符串 -> 规范许可证标识（小写匹配）
ALIASES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bagpl\b|affero", re.I), "AGPL"),
    (re.compile(r"\blgpl\b|lesser general public", re.I), "LGPL"),
    (re.compile(r"\bgpl\b|general public license", re.I), "GPL"),
    (re.compile(r"\bmpl[- ]?2\.0\b|mozilla public license 2", re.I), "MPL-2.0"),
    (re.compile(r"\bapache[- ]?(software )?license[- ]?(version )?2\.0|apache-2\.0|apache 2\.0", re.I), "Apache-2.0"),
    (re.compile(r"\bmit\b", re.I), "MIT"),
    (re.compile(r"\bisc\b", re.I), "ISC"),
    # "PSFL" 是部分包的元数据写法。defusedxml 0.7.1 的 LICENSE 文件经人工核对为
    # "PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2"，故 PSFL 按 PSF-2.0 处理。
    (re.compile(r"\bpsf\b|\bpsfl\b|python software foundation|psf-2\.0|python-2\.0", re.I), "PSF-2.0"),
    (re.compile(r"\bbsd[- ]?3|3-clause|new bsd|modified bsd", re.I), "BSD-3-Clause"),
    (re.compile(r"\bbsd[- ]?2|2-clause|simplified bsd|freebsd", re.I), "BSD-2-Clause"),
]


@dataclass
class Result:
    name: str
    version: str
    status: str  # PASS / WARN / FAIL
    license_raw: str
    license_id: str
    license_files: list[str] = field(default_factory=list)
    note: str = ""


def norm(name: str) -> str:
    return canonicalize_name(name)


def default_roots() -> list[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return [Requirement(r).name for r in data["project"]["dependencies"]]


def extra_roots() -> list[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    names: list[str] = []
    for reqs in data["project"].get("optional-dependencies", {}).values():
        names.extend(Requirement(r).name for r in reqs)
    return names


def closure(roots: list[str]) -> tuple[dict[str, md.Distribution | None], list[str]]:
    """计算传递依赖闭包。返回 {规范名: Distribution 或 None(未安装)} 与 缺失列表。"""
    seen: dict[str, md.Distribution | None] = {}
    stack = list(roots)
    while stack:
        raw = stack.pop()
        key = norm(raw)
        if key in seen:
            continue
        try:
            dist = md.distribution(raw)
        except md.PackageNotFoundError:
            seen[key] = None
            continue
        seen[key] = dist
        for req_str in dist.requires or []:
            req = Requirement(req_str)
            # 只取默认（非 extra）依赖：带 extra 标记的跳过
            if req.marker is not None and not req.marker.evaluate({"extra": ""}):
                continue
            stack.append(req.name)
    missing = [k for k, v in seen.items() if v is None]
    return seen, missing


def license_files(dist: md.Distribution) -> list[str]:
    """收集 dist-info 中的许可证文件：优先 License-File 头，并补充 licenses/ 目录与常见文件名。"""
    out: set[str] = set(dist.metadata.get_all("License-File") or [])
    for f in dist.files or []:
        parts = [p.upper() for p in f.parts]
        base = f.name.upper()
        in_lic_dir = any(p in ("LICENSES", "LICENSE", "LICENSES.D", "LICENCES") for p in parts[:-1])
        if in_lic_dir or base.startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE")):
            out.add(str(f))
    return sorted(out)


def classify_token(token: str) -> str | None:
    token = token.strip().strip("()")
    if not token:
        return None
    if token in PERMISSIVE or token in WARN_LICENSES or token in NEAR_PERMISSIVE or token in ("GPL", "AGPL", "LGPL"):
        return token
    for pat, name in ALIASES:
        if pat.search(token):
            return name
    return None


def classify(raw: str) -> tuple[str, str]:
    """返回 (规范标识, 说明)。规范标识为 白名单名 / WARN 名 / GPL / AGPL / UNKNOWN。"""
    text = raw.strip()
    if not text:
        return "UNKNOWN", "无许可证元数据"
    # SPDX 表达式：OR 任一选项可用即可；AND 必须全部满足
    if re.search(r"\sOR\s", text):
        opts = [classify_token(t) for t in re.split(r"\s+OR\s+", text)]
        ok = [o for o in opts if o in PERMISSIVE]
        if ok:
            return ok[0], f"双许可（OR），选用 {ok[0]}"
        return classify_token(re.split(r"\s+OR\s+", text)[0]) or "UNKNOWN", "双许可但无白名单选项"
    raw_parts = [t for t in re.split(r"\s+AND\s+|;|,", text) if t.strip()]
    parts = [classify_token(t) for t in raw_parts]
    unknown = [t.strip() for t, p in zip(raw_parts, parts) if p is None]
    for p in parts:
        if p in ("GPL", "AGPL") or (p or "").startswith(FAIL_PREFIXES):
            return p or "UNKNOWN", ""
    for p in parts:
        if p in WARN_LICENSES or p in NEAR_PERMISSIVE:
            return p, ""
    if parts and all(p in PERMISSIVE for p in parts):
        note = "多许可（AND）" if len(parts) > 1 else ""
        if unknown:
            note = (note + "；" if note else "") + f"含未识别子项: {unknown}"
        return parts[0], note  # type: ignore[return-value]
    if parts and parts[0] is not None:
        return parts[0], f"含未识别子项: {unknown}" if unknown else ""
    return "UNKNOWN", ""


def read_meta_license(dist: md.Distribution) -> tuple[str, str]:
    """返回 (原始字符串, 来源说明)。优先级：License-Expression > License（短文本）> Classifier。"""
    meta = dist.metadata
    expr = (meta.get("License-Expression") or "").strip()
    if expr:
        return expr, "License-Expression"
    lic = (meta.get("License") or "").strip()
    if lic and len(lic) <= 120 and "\n" not in lic:
        return lic, "License"
    classifiers = [c for c in (meta.get_all("Classifier") or []) if c.startswith("License ::")]
    if classifiers:
        return "; ".join(c.split("::")[-1].strip() for c in classifiers), "Classifier"
    if lic:
        return lic.splitlines()[0][:120], "License(多行,取首行)"
    return "", "无"


def bsd_clause_from_files(dist: md.Distribution, files: list[str]) -> str | None:
    """BSD 变体无法从元数据区分时，读取 LICENSE 文件正文判断子句数。"""
    for rel in files:
        try:
            text = Path(str(dist.locate_file(rel))).read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            continue
        if "neither the name" in text or "endorse" in text:
            return "BSD-3-Clause"
        if "redistributions" in text:
            return "BSD-2-Clause"
    return None


def check_one(name: str, dist: md.Distribution | None, optional: bool = False) -> Result:
    if dist is None and optional:
        return Result(name, "-", "NOTE", "", "-", note="可选 extra 未安装（按设计）")
    if dist is None:
        return Result(name, "-", "FAIL", "", "UNKNOWN", note="未安装，无法核验（先在 .venv 中安装）")
    version = dist.version
    raw, src = read_meta_license(dist)
    files = license_files(dist)
    ident, note = classify(raw)
    if ident == "UNKNOWN" and "bsd" in raw.lower():
        # 元数据只说 "BSD"（无子句数）：以 LICENSE 正文确认子句数
        clause = bsd_clause_from_files(dist, files)
        if clause:
            ident = clause
            note = (note + "；" if note else "") + "BSD 子句数由 LICENSE 正文判定"
    if ident in PERMISSIVE:
        status = "PASS"
    elif ident in WARN_LICENSES:
        status = "WARN"
        note = (note + "；" if note else "") + "需人工批准"
    elif ident in NEAR_PERMISSIVE:
        status = "WARN"
        note = (note + "；" if note else "") + "白名单外的 MIT 变体，需人工确认"
    elif ident in ("GPL", "AGPL") or ident.startswith(FAIL_PREFIXES):
        status = "FAIL"
    else:
        status = "FAIL"
        note = (note + "；" if note else "") + "无法识别，需人工核对 LICENSE 文件"
    if not files:
        note = (note + "；" if note else "") + "dist-info 中无 LICENSE 文件"
    elif len(files) > 5:
        note = (note + "；" if note else "") + f"随附 {len(files)} 份许可/声明文件，需人工抽查 NOTICE（未逐一核验）"
    if src:
        note = (note + "；" if note else "") + f"来源:{src}"
    return Result(name, version, status, raw or "-", ident, files, note)


def render_md(results: list[Result], mode: str) -> str:
    lines = [
        "# 第三方依赖许可证清单",
        "",
        f"> 由 `scripts/check_licenses.py --{mode}` 自动生成（读取已安装包的 importlib.metadata 与 LICENSE 文件）。",
        "> 未经脚本核实的条目不得在此填写结论。",
        "",
        "| 包 | 版本 | 状态 | 元数据许可证 | 判定 | LICENSE 文件 | 备注 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        files = "<br>".join(r.license_files) if r.license_files else "-"
        lines.append(
            f"| {r.name} | {r.version} | {r.status} | {r.license_raw} | {r.license_id} | {files} | {r.note} |"
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--default-only", action="store_true")
    g.add_argument("--all", action="store_true")
    ap.add_argument("--write", type=Path, default=None)
    args = ap.parse_args(argv)

    roots = default_roots()
    if args.all:
        roots = roots + extra_roots()
    dists, missing = closure(roots)
    # 只出现在 extra 中、未在默认依赖闭包里的包，未安装属于设计（如 PyMuPDF 不装）
    default_closure, _ = closure(default_roots())
    optional_only = set(dists) - set(default_closure)
    results = [check_one(k, v, optional=(k in optional_only)) for k, v in sorted(dists.items())]

    for r in results:
        print(f"{r.status:4} {r.name} {r.version} | {r.license_raw} -> {r.license_id} | {r.note}")
    n_fail = sum(r.status == "FAIL" for r in results)
    n_warn = sum(r.status == "WARN" for r in results)
    print(f"-- 共 {len(results)} 个包，FAIL={n_fail} WARN={n_warn}")

    if args.write is not None:
        out = ROOT / args.write if not args.write.is_absolute() else args.write
        out.write_text(render_md(results, "all" if args.all else "default-only"), encoding="utf-8")
        print(f"已写入 {args.write}")

    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
