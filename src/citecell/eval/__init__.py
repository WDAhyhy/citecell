"""评测指标与报告（ARCHITECTURE.md §9；T9）。"""

from citecell.eval.metrics import (
    EVALUATOR_VERSION,
    MIN_SAMPLE,
    CiteCellEvaluator,
    dataset_sha256,
    wilson_ci,
)
from citecell.eval.report import render_json, render_markdown

__all__ = [
    "EVALUATOR_VERSION",
    "MIN_SAMPLE",
    "CiteCellEvaluator",
    "dataset_sha256",
    "render_json",
    "render_markdown",
    "wilson_ci",
]
