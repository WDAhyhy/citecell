"""验收 2：Oracle 端到端。

OracleBackend 按金标准给出完美输出，因此流水线的断言应当全部成立。这是流水线正确性的断言，
不是对外报告的指标；不满足即为缺陷。

XLSX 金额的设计边界：金额单元格只有在「单元格或表头带单位线索」时才可导出。唯一例外是
「单元格和表头都没有任何单位线索的纯数字」（金标准 tag surface=bare_money_plain）：不猜单位，
判为 needs_review + VALUE_UNPARSEABLE。这是设计如此，不是缺陷；其他金额形式的 grounded 断言见
test_xlsx_money_forms.py（hard 档语料）。

定位与证据口径：由评测器按值口径判定（见 test_oracle_metrics_are_perfect）。
"""

from __future__ import annotations

import pytest

from citecell.backends import OracleBackend
from citecell.models import (
    GoldDocument,
    GoldRecord,
    IssueCode,
    KeySet,
    ParsedDocument,
    RunConfig,
    RunResult,
    Status,
)
from citecell.values import ValueNormalizerV1

def _oracle_run(corpus, keyset: KeySet, run_pipeline) -> tuple[OracleBackend, RunResult, RunConfig]:  # noqa: ANN001
    oracle = OracleBackend(corpus.golds, corpus.parsed)
    config = RunConfig(backend=oracle.info())
    result = run_pipeline(oracle, config, corpus.paths, keyset)
    return oracle, result, config


def _iter_records(result: RunResult, corpus) -> list[tuple[ParsedDocument, GoldDocument, object, GoldRecord]]:  # noqa: ANN001
    """按（文档, 键）对齐运行结果与金标准。"""
    gold_by_sha = corpus.gold_by_sha
    out = []
    for doc_result in result.documents:
        gold = gold_by_sha[doc_result.info.sha256]
        rec_by_key = {r.key: r for r in gold.records}
        for rec in doc_result.records:
            out.append((gold, rec_by_key[rec.key], rec))
    return out


def _is_plain_bare_money(gold_rec: GoldRecord) -> bool:
    return "surface=bare_money_plain" in gold_rec.tags


@pytest.fixture(scope="module")
def oracle_result(corpus, keyset, run_pipeline) -> RunResult:  # noqa: ANN001
    return _oracle_run(corpus, keyset, run_pipeline)[1]


def test_present_keys_grounded_and_equivalent(corpus, keyset: KeySet, oracle_result: RunResult) -> None:  # noqa: ANN001
    """present 键：唯一的 needs_review 是「无任何单位线索的纯数字金额」（tag bare_money_plain，设计如此）；
    其余全部 grounded 且值与金标准等价。"""
    specs = {k.key: k for k in keyset.keys}
    vn = ValueNormalizerV1()
    problems: list[str] = []
    n_checked = 0
    n_plain = 0
    for gold, gold_rec, rec in _iter_records(oracle_result, corpus):
        if gold_rec.expected != "present":
            continue
        n_checked += 1
        if _is_plain_bare_money(gold_rec):
            n_plain += 1
            codes = {i.code for i in rec.issues}
            if rec.status != Status.NEEDS_REVIEW or IssueCode.VALUE_UNPARSEABLE not in codes:
                problems.append(f"{gold.format.value} {rec.key}: 期望 needs_review+VALUE_UNPARSEABLE，实际 {rec.status.value} {sorted(c.value for c in codes)}")
            continue
        if rec.status != Status.GROUNDED:
            problems.append(f"{gold.format.value} {rec.key}: 期望 grounded，实际 {rec.status.value} {[i.code.value for i in rec.issues]}")
            continue
        if rec.value is None or gold_rec.value is None or not vn.equivalent(rec.value, gold_rec.value, specs[rec.key]):
            problems.append(f"{gold.format.value} {rec.key}: grounded 但值与金标准不等价")
    assert n_checked > 0
    assert not problems, "流水线缺陷（Oracle 应完美）：\n" + "\n".join(problems[:20])


def test_absent_keys_not_found(corpus, oracle_result: RunResult) -> None:  # noqa: ANN001
    """金标准 absent 的键必须 not_found，且无值、无证据。"""
    problems: list[str] = []
    n = 0
    for gold, gold_rec, rec in _iter_records(oracle_result, corpus):
        if gold_rec.expected != "absent":
            continue
        n += 1
        if rec.status != Status.NOT_FOUND or rec.value is not None or rec.evidence:
            problems.append(f"{gold.format.value} {rec.key}: 实际 {rec.status.value}")
    assert n > 0
    assert not problems, "缺失字段幻觉（Oracle 应完美）：\n" + "\n".join(problems[:20])


def _overall_metrics(result: RunResult, corpus, keyset: KeySet) -> dict[str, tuple[int, int]]:  # noqa: ANN001
    from citecell.eval import CiteCellEvaluator

    parsed = {p.info.doc_id: p for p in corpus.parsed}
    report = CiteCellEvaluator(keyset, dataset_id="synthetic:e2e").evaluate(result, corpus.golds, parsed=parsed)
    all_slice = next(s for s in report.slices if s.slice_name == "all")
    return {m.name: (m.numerator, m.denominator) for m in all_slice.metrics}


def test_oracle_metrics_are_perfect(corpus, keyset: KeySet, oracle_result: RunResult, run_pipeline) -> None:  # noqa: ANN001
    """评测器（值口径，传入 parsed）：Oracle 的定位与证据支持全部满分，且全部走值口径（回退 0）；
    干扰项命中与「既非支持也非干扰」均为 0；静默错误为 0。"""
    m = _overall_metrics(oracle_result, corpus, keyset)
    n_loc, d_loc = m["localization_accuracy"]
    assert d_loc > 0 and n_loc == d_loc, f"定位准确率 {n_loc}/{d_loc}"
    n_sup, d_sup = m["evidence_support_rate"]
    assert d_sup > 0 and n_sup == d_sup, f"证据支持率 {n_sup}/{d_sup}"
    assert m["localization_quote_fallback"][0] == 0, "定位回退到引文口径的条数应为 0"
    assert m["anchor_quote_fallback"][0] == 0, "证据锚定回退到引文口径的条数应为 0"
    assert m["localization_value_basis"] == m["localization_accuracy"]
    assert m["distractor_hit_rate"][0] == 0, "Oracle 不应命中干扰项"
    assert m["evidence_unanchored_rate"][0] == 0, "Oracle 的证据既非支持也非干扰的条数应为 0"
    assert m["silent_error_rate"][0] == 0, "Oracle 不应有静默错误"
