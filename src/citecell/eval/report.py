"""评测报告输出：Markdown 与 JSON（ARCHITECTURE.md §9、§11；ADR-014）。

* JSON 为权威输出：``EvalReport`` 的完整转储，外加每个比率指标的 Wilson 95% 区间与样本量标记。
* Markdown 由同一份数据渲染；其中每个数字都来自 EvalReport，或由其分子分母按 ``metrics.wilson_ci`` 重算。
* 合成数据（synthetic）与真实指针集（pointer）分两节展示，绝不合并为一个总数。
* 分母为 0 的指标显示「无数据」；分母 < ``MIN_SAMPLE`` 的指标标注「样本过少」。
* 排序确定：数据来源按 synthetic → pointer；同一来源内按 dataset_id、run_id 排序；
  切片与指标顺序沿用 EvalReport 中的顺序（由评测器确定）。
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from citecell.eval.metrics import MIN_SAMPLE, wilson_ci
from citecell.models import EvalReport, EvalSlice, MetricValue

SOURCE_ORDER = ("synthetic", "pointer")
SOURCE_TITLE = {
    "synthetic": "合成数据（synthetic）",
    "pointer": "真实指针集（pointer）",
}
NO_DATA = "无数据"
SMALL_SAMPLE = "样本过少"


def _source_of(report: EvalReport) -> str:
    src = report.dataset_id.split(":", 1)[0]
    if src not in SOURCE_ORDER:
        raise ValueError("dataset_id 缺少合法的数据来源前缀（synthetic: 或 pointer:）")
    return src


def ordered_reports(reports: Sequence[EvalReport]) -> list[EvalReport]:
    """确定性排序：来源顺序 → dataset_id → run_id。"""
    return sorted(
        reports,
        key=lambda r: (SOURCE_ORDER.index(_source_of(r)), r.dataset_id, r.run_id),
    )


LOC_BASIS = "value"
LOC_BASIS_TEXT = "值区间（PDF 用解析片段还原；DOCX 用值字符区间；XLSX 用单元格）"


def _localization(report: EvalReport) -> dict[str, int] | None:
    """定位口径的计数，取自 all 切片的指标；回退必须显式计数，不得静默。"""
    for s in report.slices:
        if s.slice_name != "all":
            continue
        by = {m.name: m for m in s.metrics}
        acc = by.get("localization_accuracy")
        val = by.get("localization_value_basis")
        fb = by.get("localization_quote_fallback")
        if acc is None or val is None or fb is None:
            return None
        return {
            "judged_records": acc.denominator,
            "value_basis_records": val.numerator,
            "quote_fallback_records": fb.numerator,
        }
    return None


def _anchor(report: EvalReport) -> dict[str, int] | None:
    """证据锚定（支持 / 干扰项）口径的计数，取自 all 切片。第三类（既非支持也非干扰）显式输出。"""
    for s in report.slices:
        if s.slice_name != "all":
            continue
        by = {m.name: m for m in s.metrics}
        val = by.get("anchor_value_basis")
        fb = by.get("anchor_quote_fallback")
        un = by.get("evidence_unanchored_rate")
        if val is None or fb is None or un is None:
            return None
        return {
            "judged_records": val.denominator,
            "value_basis_records": val.numerator,
            "quote_fallback_records": fb.numerator,
            "unanchored_records": un.numerator,
        }
    return None


def _fmt_value(m: MetricValue) -> str:
    return NO_DATA if m.value is None else f"{m.value:.4f}"


def _fmt_ci(m: MetricValue) -> str:
    ci = wilson_ci(m.numerator, m.denominator)
    if ci is None:
        return NO_DATA
    return f"[{ci[0]:.4f}, {ci[1]:.4f}]"


def _note(m: MetricValue) -> str:
    if m.denominator == 0:
        return NO_DATA
    if m.denominator < MIN_SAMPLE:
        return SMALL_SAMPLE
    return ""


def _fmt_stat(x: float | None, digits: int = 3) -> str:
    return NO_DATA if x is None else f"{x:.{digits}f}"


def _render_slice(s: EvalSlice) -> list[str]:
    lines = [f"#### 切片 `{s.slice_name}`（记录数 {s.n_records}）"]
    if s.n_records < MIN_SAMPLE:
        lines.append(f"> {SMALL_SAMPLE}：记录数 {s.n_records} < {MIN_SAMPLE}，比率的置信区间很宽。")
    if s.metrics:
        lines.append("")
        lines.append("| 指标 | 分子 | 分母 | 值 | CI（Wilson，双侧） | 备注 |")
        lines.append("|---|---:|---:|---:|---|---|")
        for m in s.metrics:
            lines.append(
                f"| `{m.name}` | {m.numerator} | {m.denominator} | {_fmt_value(m)} | {_fmt_ci(m)} | {_note(m)} |"
            )
    if s.distributions:
        lines.append("")
        lines.append("| 分布 | 样本数 | 均值 | P50 | P90 | 最大 |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for d in s.distributions:
            lines.append(
                f"| `{d.name}` | {d.count} | {_fmt_stat(d.mean)} | {_fmt_stat(d.p50)} | "
                f"{_fmt_stat(d.p90)} | {_fmt_stat(d.max)} |"
            )
    lines.append("")
    return lines


def render_markdown(reports: Sequence[EvalReport]) -> str:
    """渲染 Markdown。合成与真实数据分节；无报告时输出「无数据」。"""
    if not reports:
        return f"# CiteCell 评测报告\n\n{NO_DATA}\n"
    lines = ["# CiteCell 评测报告", ""]
    ordered = ordered_reports(reports)
    for src in SOURCE_ORDER:
        group = [r for r in ordered if _source_of(r) == src]
        if not group:
            continue
        lines.append(f"## {SOURCE_TITLE[src]}")
        lines.append("")
        for report in group:
            lines.append(f"### 数据集 `{report.dataset_id}`")
            lines.append("")
            lines.append(f"- run_id：`{report.run_id}`")
            lines.append(f"- 评测器版本：`{report.evaluator_version}`")
            lines.append(f"- 文档数：{report.n_documents}（指针集跳过：{report.n_documents_missing}）")
            loc = _localization(report)
            if loc is not None:
                lines.append(f"- 定位口径：{LOC_BASIS_TEXT}")
                lines.append(
                    f"- 定位口径计数：正确记录 {loc['judged_records']} 条，其中值口径 "
                    f"{loc['value_basis_records']} 条；回退引文口径 {loc['quote_fallback_records']} 条"
                )
            anc = _anchor(report)
            if anc is not None:
                lines.append(f"- 证据锚定口径：{LOC_BASIS_TEXT}")
                lines.append(
                    f"- 证据锚定计数：有值区间证据的记录 {anc['judged_records']} 条，其中值口径 "
                    f"{anc['value_basis_records']} 条；回退引文口径 {anc['quote_fallback_records']} 条；"
                    f"既非支持也非干扰 {anc['unanchored_records']} 条"
                )
            lines.append(f"- 生成时间：{report.created_at.isoformat()}")
            lines.append("")
            for s in report.slices:
                lines.extend(_render_slice(s))
    return "\n".join(lines).rstrip() + "\n"


def _ci_payload(report: EvalReport) -> dict[str, dict[str, dict[str, object]]]:
    out: dict[str, dict[str, dict[str, object]]] = {}
    for s in report.slices:
        per: dict[str, dict[str, object]] = {}
        for m in s.metrics:
            ci = wilson_ci(m.numerator, m.denominator)
            per[m.name] = {
                "lo": None if ci is None else ci[0],
                "hi": None if ci is None else ci[1],
                "sample_small": m.denominator < MIN_SAMPLE,
            }
        out[s.slice_name] = per
    return out


def render_json(reports: Sequence[EvalReport]) -> str:
    """权威 JSON：规范 JSON（sort_keys、ensure_ascii=False、紧凑分隔符）。"""
    payload = {
        "ci_method": "wilson",
        "ci_confidence": 0.95,
        "min_sample": MIN_SAMPLE,
        "reports": [
            {
                "source": _source_of(r),
                "report": r.model_dump(mode="json"),
                "ci95": _ci_payload(r),
                "localization": {"basis": LOC_BASIS, **(_localization(r) or {})},
                "anchor": {"basis": LOC_BASIS, **(_anchor(r) or {})},
            }
            for r in ordered_reports(reports)
        ],
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


__all__ = ["NO_DATA", "SMALL_SAMPLE", "ordered_reports", "render_json", "render_markdown"]
