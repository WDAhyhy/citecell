"""运行清单（ARCH §11）。

清单只含哈希、版本、配置与计数；不含正文、引文、提示词、绝对路径。文件名仅在
``RunConfig.record_filenames=True`` 时由流水线写入 ``ManifestDocument.display_name``。

未实现：``git_commit`` / ``git_dirty`` 目前恒为 None（本模块不调用 git；需要时由调用方传入或后续补充）。
"""

from __future__ import annotations

import importlib.metadata
import platform
from collections.abc import Sequence
from datetime import datetime

from citecell import __version__
from citecell.models import (
    ComponentVersion,
    DocumentInfo,
    KeySet,
    ManifestDocument,
    RunConfig,
    RunManifest,
    UsageStats,
)
from citecell.canonical import canonical_sha256
from citecell.textnorm.normalize import VERSION as NORMALIZER_VERSION
from citecell.values import ValueNormalizerV1
from citecell.verify.localizer import LOCALIZER_VERSION
from citecell.verify.merger import MERGER_VERSION
from citecell.verify.verifier import VERIFIER_VERSION

_CORE_DISTRIBUTIONS = ("pydantic", "httpx", "python-docx", "openpyxl", "defusedxml", "pypdfium2")
_PDF_DISTRIBUTIONS = {"pypdfium2": "pypdfium2", "pdfminer": "pdfminer.six"}


def collect_dependencies(pdf_backend: str) -> dict[str, str]:
    """已安装的核心依赖与所选 PDF 后端的版本（包名 -> 版本，按包名排序）。未安装的包不列出。"""
    names = list(_CORE_DISTRIBUTIONS)
    pdf_dist = _PDF_DISTRIBUTIONS.get(pdf_backend, pdf_backend)
    if pdf_dist not in names:
        names.append(pdf_dist)
    out: dict[str, str] = {}
    for name in names:
        try:
            out[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            continue
    return dict(sorted(out.items()))


def static_components(*, planner_version: str, parser_infos: Sequence[DocumentInfo] = ()) -> list[ComponentVersion]:
    """各组件版本（解析器版本取自本次运行实际使用的文档，去重后排序）。"""
    parsers = sorted(
        {(i.parser_name, i.parser_version) for i in parser_infos if i.parser_name != "none"}
    )
    comps = [
        ComponentVersion(name="normalizer", version=NORMALIZER_VERSION),
        ComponentVersion(name="value-normalizer", version=ValueNormalizerV1().version),
        ComponentVersion(name="verifier", version=VERIFIER_VERSION),
        ComponentVersion(name="localizer", version=LOCALIZER_VERSION),
        ComponentVersion(name="merger", version=MERGER_VERSION),
        ComponentVersion(name="planner", version=planner_version),
    ]
    comps += [ComponentVersion(name=f"parser:{name}", version=version) for name, version in parsers]
    return comps


def build_manifest(
    *,
    run_id: str,
    created_at: datetime,
    finished_at: datetime | None,
    config: RunConfig,
    keyset: KeySet,
    documents: Sequence[ManifestDocument],
    components: Sequence[ComponentVersion],
    template_id: str,
    template_sha256: str,
    output_schema: dict,
    dependencies: dict[str, str],
    usage: UsageStats,
    notes: str | None = None,
) -> RunManifest:
    return RunManifest(
        run_id=run_id,
        citecell_version=__version__,
        created_at=created_at,
        finished_at=finished_at,
        git_commit=None,
        git_dirty=None,
        python_version=platform.python_version(),
        platform=platform.platform(terse=True),
        dependencies=dependencies,
        components=list(components),
        prompt_template_id=template_id,
        prompt_template_sha256=template_sha256,
        output_schema_sha256=canonical_sha256(output_schema),
        config=config,
        config_sha256=canonical_sha256(config),
        keyset_name=keyset.name,
        keyset_version=keyset.version,
        keyset_sha256=canonical_sha256(keyset),
        documents=list(documents),
        usage=usage,
        notes=notes,
    )


__all__ = ["build_manifest", "collect_dependencies", "static_components"]
