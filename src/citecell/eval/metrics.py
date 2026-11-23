"""评测指标（ARCHITECTURE.md §9）与 Evaluator 实现。

约定：
* 所有比率型指标用 ``MetricValue(numerator, denominator)``；分母为 0 时 value 为 None（"无数据"）。
* 字段级 F1 写成 ``2TP / (2TP + FP + FN)``，同样是比率形式，因此分子分母都保留。
* 「值准确率」定义为 ``TP / 金标准存在的记录数``，与「字段召回率」数值相同，两者都输出以便对应 §9 表格。

置信区间（``wilson_ci``）：Wilson score 区间，z = 1.959964（双侧 95%）。
  center = (p + z²/2n) / (1 + z²/n)
  half   = z·sqrt(p(1-p)/n + z²/4n²) / (1 + z²/n)
  区间 = [center - half, center + half]。已知答案：5/10 → [0.2366, 0.7634]；0/10 → [0, 0.2775]。
  它不计入 EvalReport（契约 MetricValue 无 CI 字段），在渲染时由分子分母重算。
  分母小于 ``MIN_SAMPLE`` 时报告标注「样本过少」。

分布统计（``DistributionStat``）使用最近秩法（nearest-rank）：p 分位 = 排序后第 ceil(p·n) 个值。

切片：``all``、``type=<值类型>``、``format=<格式>``、``tag=<标签>``（一条记录可属于多个 tag 切片）。
效率分布（尝试延迟、token、每键尝试次数）与注入拦截率只出现在 ``all`` 切片。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime

from citecell.eval.align import AlignedRecord, align
from citecell.eval.geometry import (
    BASIS_QUOTE,
    BASIS_VALUE,
    localization_basis,
    localization_hit,
)
from citecell.models import (
    AttemptOutcome,
    Block,
    DistributionStat,
    DocumentResult,
    EvalReport,
    EvalSlice,
    Evidence,
    ExtractionRecord,
    GoldDocument,
    GoldMention,
    InjectedError,
    InjectedErrorType,
    KeySet,
    MetricValue,
    ParsedDocument,
    RunResult,
    Status,
    TextValue,
)
from citecell.values import ValueNormalizerV1

logger = logging.getLogger("citecell.eval")

EVALUATOR_VERSION = "eval-v1"
MIN_SAMPLE = 30
DEFAULT_IOU_THRESHOLD = 0.5
_Z95 = 1.959963984540054

_normalizer = ValueNormalizerV1()


def wilson_ci(successes: int, trials: int) -> tuple[float, float] | None:
    """Wilson 95% 置信区间。``trials == 0`` 时返回 None。"""
    if trials <= 0:
        return None
    if not 0 <= successes <= trials:
        raise ValueError("successes 必须在 [0, trials] 内")
    n = float(trials)
    p = successes / n
    z2 = _Z95 * _Z95
    denom = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denom
    half = _Z95 * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n)) / denom
    lo = 0.0 if successes == 0 else max(0.0, center - half)
    hi = 1.0 if successes == trials else min(1.0, center + half)
    return lo, hi


def _ratio(name: str, num: int, den: int) -> MetricValue:
    return MetricValue(name=name, numerator=num, denominator=den, value=(num / den) if den else None)


def _percentile(sorted_vals: list[float], p: float) -> float:
    rank = max(1, math.ceil(p * len(sorted_vals)))
    return sorted_vals[rank - 1]


def _dist(name: str, values: Iterable[float]) -> DistributionStat:
    vals = sorted(float(v) for v in values)
    if not vals:
        return DistributionStat(name=name, count=0)
    return DistributionStat(
        name=name,
        count=len(vals),
        mean=sum(vals) / len(vals),
        p50=_percentile(vals, 0.5),
        p90=_percentile(vals, 0.9),
        max=vals[-1],
    )


def _is_correct(pair: AlignedRecord) -> bool:
    """预测有值，金标准有值，且规范化值等价。"""
    pv = pair.pred.value
    gv = pair.gold.value
    if pv is None or gv is None:
        return False
    return _normalizer.equivalent(pv, gv, pair.spec)


def _slice_metrics(
    pairs: Sequence[AlignedRecord],
    iou_threshold: float,
    blocks: Mapping[str, Mapping[str, Block]],
) -> list[MetricValue]:
    tp = fp = fn = 0
    loc_value = loc_fallback = 0
    present = absent = 0
    grounded = grounded_wrong = 0
    absent_hallucinated = 0
    present_missed = 0
    needs_review = needs_review_hit = 0
    text_num = text_den = 0
    ev_total = ev_located = 0
    loc_num = loc_den = 0
    sup_num = sup_den = 0
    dis_num = dis_den = 0
    unanchored = 0
    anchor_quote = anchor_value = 0

    for pair in pairs:
        pred: ExtractionRecord = pair.pred
        has_pred = pred.value is not None
        correct = _is_correct(pair)

        if pair.gold_present:
            present += 1
            if correct:
                tp += 1
            else:
                fn += 1
                if has_pred:
                    fp += 1
            if pred.status == Status.NOT_FOUND:
                present_missed += 1
            gv = pair.gold.value
            if isinstance(gv, TextValue):
                pt = pred.value.text if isinstance(pred.value, TextValue) else ""
                common = sum((Counter(pt) & Counter(gv.text)).values())
                text_num += 2 * common
                text_den += len(pt) + len(gv.text)
        else:
            absent += 1
            if has_pred:
                fp += 1
                absent_hallucinated += 1

        if pred.status == Status.GROUNDED:
            grounded += 1
            if not correct:
                grounded_wrong += 1

        if pred.status == Status.NEEDS_REVIEW:
            needs_review += 1
            if has_pred and (not pair.gold_present or not correct):
                needs_review_hit += 1

        for e in pred.evidence:
            ev_total += 1
            if e.is_located:
                ev_located += 1

        if correct:
            loc_den += 1
            hit = False
            any_quote = False
            all_value = True
            for e in pred.evidence:
                if e.locator is None:
                    continue
                block = blocks.get(e.doc_id, {}).get(e.block_id)
                basis = localization_basis(e, block)
                if basis == BASIS_QUOTE:
                    any_quote = True
                if basis != BASIS_VALUE:
                    all_value = False
                for m in pair.gold.mentions:
                    ok, _ = localization_hit(e, m, block=block, iou_threshold=iou_threshold)
                    if ok:
                        hit = True
            if hit:
                loc_num += 1
            if any_quote:
                loc_fallback += 1
            if all_value:
                loc_value += 1

        value_ev = [e for e in pred.evidence if e.value_char_start is not None]
        if value_ev:
            # 证据锚定：值口径判定落在提及 / 干扰项上（与定位准确率同一几何规则）
            sup_den += 1
            dis_den += 1
            supported = distracted = False
            any_quote = False
            all_value = True
            for e in value_ev:
                block = blocks.get(e.doc_id, {}).get(e.block_id)
                basis = localization_basis(e, block)
                if basis == BASIS_QUOTE:
                    any_quote = True
                if basis != BASIS_VALUE:
                    all_value = False
                for m in pair.gold.mentions:
                    supported = localization_hit(e, m, block=block, iou_threshold=iou_threshold)[0] or supported
                for d in pair.gold.distractors:
                    distracted = localization_hit(e, d, block=block, iou_threshold=iou_threshold)[0] or distracted
            if supported:
                sup_num += 1
            if distracted:
                dis_num += 1
            if not supported and not distracted:
                unanchored += 1
            if any_quote:
                anchor_quote += 1
            if all_value:
                anchor_value += 1

    n = len(pairs)
    return [
        _ratio("value_precision", tp, tp + fp),
        _ratio("value_recall", tp, tp + fn),
        _ratio("value_f1", 2 * tp, 2 * tp + fp + fn),
        _ratio("value_accuracy", tp, present),
        _ratio("text_char_f1_micro", text_num, text_den),
        _ratio("citation_validity_final", ev_located, ev_total),
        _ratio("localization_accuracy", loc_num, loc_den),
        _ratio("localization_value_basis", loc_value, loc_den),
        _ratio("localization_quote_fallback", loc_fallback, loc_den),
        _ratio("evidence_support_rate", sup_num, sup_den),
        _ratio("distractor_hit_rate", dis_num, dis_den),
        _ratio("evidence_unanchored_rate", unanchored, sup_den),
        _ratio("anchor_value_basis", anchor_value, sup_den),
        _ratio("anchor_quote_fallback", anchor_quote, sup_den),
        _ratio("silent_error_rate", grounded_wrong, grounded),
        _ratio("hallucination_rate_absent", absent_hallucinated, absent),
        _ratio("missed_present_rate", present_missed, present),
        _ratio("needs_review_ratio", needs_review, n),
        _ratio("needs_review_hit_rate", needs_review_hit, needs_review),
    ]


def _intercept_metrics(
    aligned_docs: Mapping[str, DocumentResult],
    records_by_cell: Mapping[tuple[str, str], ExtractionRecord],
    injections: Sequence[InjectedError],
) -> list[MetricValue]:
    """按错误类型的拦截率。被拦截 = 该单元格最终不是 grounded（键级注入）；
    MALFORMED_JSON（无键）= 按 attempt_index 指向 DocumentResult.attempts 的 outcome 为 schema_invalid。
    分母为该类型的注入次数；次数为 0 时 value 为 None。"""
    total: Counter[str] = Counter()
    caught: Counter[str] = Counter()
    for inj in injections:
        if inj.doc_id not in aligned_docs:
            raise ValueError("注入记录引用了未对齐的文档")
        dr = aligned_docs[inj.doc_id]
        name = inj.error_type.value
        total[name] += 1
        if inj.key is not None:
            rec = records_by_cell.get((inj.doc_id, inj.key))
            if rec is None:
                raise ValueError("注入记录引用了不存在的单元格")
            if rec.status != Status.GROUNDED:
                caught[name] += 1
        else:
            if not 0 <= inj.attempt_index < len(dr.attempts):
                raise ValueError("注入记录的 attempt_index 越界")
            if dr.attempts[inj.attempt_index].outcome == AttemptOutcome.SCHEMA_INVALID:
                caught[name] += 1
    out: list[MetricValue] = []
    for et in InjectedErrorType:
        out.append(_ratio(f"intercept_rate:{et.value}", caught[et.value], total[et.value]))
    return out


def _efficiency(docs: Sequence[DocumentResult]) -> tuple[list[MetricValue], list[DistributionStat]]:
    attempts = [a for dr in docs for a in dr.attempts]
    cache_hits = sum(1 for a in attempts if a.cache_hit)
    metrics = [_ratio("cache_hit_rate", cache_hits, len(attempts))]
    dists = [
        _dist("attempt_latency_ms", (a.latency_ms for a in attempts)),
        _dist("attempt_prompt_tokens", (a.prompt_tokens for a in attempts if a.prompt_tokens is not None)),
        _dist(
            "attempt_completion_tokens",
            (a.completion_tokens for a in attempts if a.completion_tokens is not None),
        ),
    ]
    return metrics, dists


def _slice(
    name: str,
    pairs: Sequence[AlignedRecord],
    iou_threshold: float,
    blocks: Mapping[str, Mapping[str, Block]],
) -> EvalSlice:
    return EvalSlice(
        slice_name=name,
        n_records=len(pairs),
        metrics=_slice_metrics(pairs, iou_threshold, blocks),
    )


def build_slices(
    pairs: Sequence[AlignedRecord],
    docs: Sequence[DocumentResult],
    *,
    iou_threshold: float,
    injections: Sequence[InjectedError] = (),
    blocks: Mapping[str, Mapping[str, Block]] | None = None,
) -> list[EvalSlice]:
    blocks = blocks or {}
    all_metrics = _slice_metrics(pairs, iou_threshold, blocks)
    records_by_cell = {(p.doc_sha256[:16], p.key): p.pred for p in pairs}
    aligned_by_id = {dr.info.doc_id: dr for dr in docs}
    eff_metrics, eff_dists = _efficiency(docs)
    intercept = _intercept_metrics(aligned_by_id, records_by_cell, injections) if injections else []
    slices = [
        EvalSlice(
            slice_name="all",
            n_records=len(pairs),
            metrics=all_metrics + intercept + eff_metrics,
            distributions=eff_dists
            + [
                _dist("attempts_per_key", (len(p.pred.attempt_ids) for p in pairs)),
            ],
        )
    ]
    for t in sorted({p.spec.value_type.value for p in pairs}):
        subset = [p for p in pairs if p.spec.value_type.value == t]
        slices.append(_slice(f"type={t}", subset, iou_threshold, blocks))
    for f in sorted({p.doc_format.value for p in pairs}):
        subset = [p for p in pairs if p.doc_format.value == f]
        slices.append(_slice(f"format={f}", subset, iou_threshold, blocks))
    for tag in sorted({t for p in pairs for t in p.gold.tags}):
        subset = [p for p in pairs if tag in p.gold.tags]
        slices.append(_slice(f"tag={tag}", subset, iou_threshold, blocks))
    return slices


def dataset_sha256(gold: Sequence[GoldDocument]) -> str:
    """金标准集合的规范哈希：按 doc_sha256 排序，规范 JSON（sort_keys、ensure_ascii=False、紧凑分隔符）。"""
    payload = [g.model_dump(mode="json") for g in sorted(gold, key=lambda g: g.doc_sha256)]
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CiteCellEvaluator:
    """满足 ``interfaces.Evaluator`` 协议的评测器。

    一次调用只能处理**一种**数据来源（ADR-014）：金标准必须全部为同一 DataSource，
    且 ``dataset_id`` 必须以 ``<source>:`` 开头（如 ``synthetic:procurement-v1``）。

    ``evaluate(result, gold, *, parsed, injections)`` 的调用约定：
    * ``parsed``：``{doc_id: ParsedDocument}``，doc_id 为 16 位十六进制（== ``info.doc_id``），
      须与 ``result`` 中证据所引用的文档对应。PDF 定位的值口径依赖它（块的逐字符片段）。
      不传或缺某文档时，该文档的 PDF 证据回退到引文口径，并在报告中计数（``localization_quote_fallback``）。
      键与 ``info.doc_id`` 不一致 → ``ValueError``。
    * ``injections``：可选，见 ``build_slices``。
    """

    version: str = EVALUATOR_VERSION

    def __init__(
        self,
        keyset: KeySet,
        *,
        dataset_id: str,
        created_at: datetime | None = None,
        iou_threshold: float = DEFAULT_IOU_THRESHOLD,
    ) -> None:
        if not 0.0 < iou_threshold <= 1.0:
            raise ValueError("iou_threshold 必须在 (0, 1] 内")
        if created_at is not None and created_at.tzinfo is None:
            raise ValueError("created_at 必须带时区")
        self._keyset = keyset
        self._dataset_id = dataset_id
        self._created_at = created_at
        self._iou = iou_threshold

    def evaluate(
        self,
        result: RunResult,
        gold: Sequence[GoldDocument],
        *,
        parsed: Mapping[str, ParsedDocument] | None = None,
        injections: Sequence[InjectedError] = (),
    ) -> EvalReport:
        for doc_id, pd in (parsed or {}).items():
            if pd.info.doc_id != doc_id:
                raise ValueError("parsed 的键必须是对应文档的 doc_id")
        blocks = {doc_id: pd.block_map() for doc_id, pd in (parsed or {}).items()}
        if not gold:
            raise ValueError("金标准为空，无法评测")
        sources = {g.source for g in gold}
        if len(sources) != 1:
            raise ValueError("一次评测只能包含一种数据来源（ADR-014）")
        source = next(iter(sources))
        if not self._dataset_id.startswith(f"{source.value}:"):
            raise ValueError("dataset_id 必须以数据来源前缀开头，如 synthetic: 或 pointer:")

        al = align(result, gold, self._keyset)
        slices = build_slices(
            al.records,
            al.docs,
            iou_threshold=self._iou,
            injections=injections,
            blocks=blocks,
        )
        logger.info("评测完成：%d 份文档，%d 条记录", len(al.docs), len(al.records))
        return EvalReport(
            run_id=result.manifest.run_id,
            dataset_id=self._dataset_id,
            dataset_sha256=dataset_sha256(gold),
            evaluator_version=self.version,
            created_at=self._created_at or datetime.now(UTC),
            n_documents=len(al.docs),
            n_documents_missing=al.n_missing,
            slices=slices,
        )


__all__ = [
    "DEFAULT_IOU_THRESHOLD",
    "EVALUATOR_VERSION",
    "MIN_SAMPLE",
    "CiteCellEvaluator",
    "build_slices",
    "dataset_sha256",
    "wilson_ci",
]
