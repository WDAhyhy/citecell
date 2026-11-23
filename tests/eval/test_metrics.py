"""指标的分子与分母、P/R/F1、静默错误率、幻觉率、误判缺失率、needs_review、切片、拦截率、Wilson 区间。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from citecell.eval import CiteCellEvaluator, wilson_ci
from citecell.eval.metrics import _dist
from citecell.models import DataSource, InjectedError, InjectedErrorType, MetricValue

from .conftest import CREATED, DATASET_ID, DOC_A, DOC_B, KEYSET, SHA_A, build_dataset


def _evaluate(ds, *, injections=(), **ctor):
    ev = CiteCellEvaluator(ds.keyset, dataset_id=DATASET_ID, created_at=CREATED, **ctor)
    return ev.evaluate(ds.result, ds.gold, injections=injections)


def _slice(report, name: str):
    (s,) = [x for x in report.slices if x.slice_name == name]
    return s


def _all(report) -> dict[str, MetricValue]:
    return {m.name: m for m in _slice(report, "all").metrics}


def _assert_ratio(m: MetricValue, num: int, den: int) -> None:
    assert (m.numerator, m.denominator) == (num, den), m.name
    if den == 0:
        assert m.value is None, m.name
    else:
        assert m.value == pytest.approx(num / den, abs=1e-12), m.name


# ---------------------------------------------------------------- 10 条已知结果
def test_known_answer_counts(dataset):
    m = _all(_evaluate(dataset))

    # TP=4；FP=3（错误的 grounded、幻觉、错误的 needs_review）；FN=3（错误的 grounded、错误的 needs_review、误判缺失）
    _assert_ratio(m["value_precision"], 4, 7)
    _assert_ratio(m["value_recall"], 4, 7)
    _assert_ratio(m["value_f1"], 8, 14)  # 2TP / (2TP+FP+FN)
    _assert_ratio(m["value_accuracy"], 4, 7)

    # 静默错误率：grounded 共 5 条，其中 1 条值错误
    _assert_ratio(m["silent_error_rate"], 1, 5)
    # 幻觉率：金标准 absent 共 3 条，其中 1 条预测有值
    _assert_ratio(m["hallucination_rate_absent"], 1, 3)
    # 误判缺失率：金标准 present 共 7 条，其中 1 条 not_found
    _assert_ratio(m["missed_present_rate"], 1, 7)
    # needs_review：共 2 条（幻觉 1 + 错误 1），两条都是错误候选
    _assert_ratio(m["needs_review_ratio"], 2, 10)
    _assert_ratio(m["needs_review_hit_rate"], 2, 2)


def test_known_answer_text_and_citation_metrics(dataset):
    m = _all(_evaluate(dataset))
    # 文本字段字符级 F1：只有 vendor 的金标准是文本且存在，"甲公司" 完全一致 → 2*3 / (3+3)
    _assert_ratio(m["text_char_f1_micro"], 6, 6)
    # 最终引文有效率：5 条证据，全部 exact
    _assert_ratio(m["citation_validity_final"], 5, 5)
    # 正确记录 4 条，证据均落在金标准提及上
    _assert_ratio(m["localization_accuracy"], 4, 4)
    # 带值区间的证据 5 条，均落在金标准提及上
    _assert_ratio(m["evidence_support_rate"], 5, 5)
    # 没有干扰项 → 分子 0，分母 5（有值区间证据的记录数）
    _assert_ratio(m["distractor_hit_rate"], 0, 5)


def test_efficiency_metrics(dataset):
    report = _evaluate(dataset)
    _assert_ratio(_all(report)["cache_hit_rate"], 1, 3)  # 3 次尝试，1 次缓存命中
    d = {x.name: x for x in _slice(report, "all").distributions}
    lat = d["attempt_latency_ms"]
    assert lat.count == 3
    assert lat.mean == pytest.approx((80 + 50 + 100) / 3)
    assert lat.p50 == 80.0  # 最近秩：ceil(0.5*3)=2 → 排序后 [50, 80, 100] 的第 2 个
    assert lat.p90 == 100.0  # ceil(0.9*3)=3
    assert lat.max == 100.0
    assert d["attempt_prompt_tokens"].count == 1
    assert d["attempt_prompt_tokens"].p50 == 1000.0
    assert d["attempt_completion_tokens"].count == 1
    assert d["attempts_per_key"].count == 10


def test_empty_distribution_has_none_statistics():
    d = _dist("x", [])
    assert d.count == 0
    assert d.mean is None and d.p50 is None and d.p90 is None and d.max is None


# ---------------------------------------------------------------- 切片
def test_slices_by_type_format_and_tag(dataset):
    report = _evaluate(dataset)
    assert [s.slice_name for s in report.slices] == [
        "all",
        "type=date",
        "type=money",
        "type=number",
        "type=percent",
        "type=text",
        "format=docx",
        "format=pdf",
        "tag=cross_block",
        "tag=surface=plain",
    ]
    assert _slice(report, "all").n_records == 10
    assert _slice(report, "format=pdf").n_records == 5
    assert _slice(report, "format=docx").n_records == 5
    assert _slice(report, "type=money").n_records == 2
    assert _slice(report, "tag=cross_block").n_records == 1
    # pdf 切片：grounded 共 4 条（total、vendor、tax、term），其中 term 错误 → 1/4
    _assert_ratio({m.name: m for m in _slice(report, "format=pdf").metrics}["silent_error_rate"], 1, 4)


def test_slices_are_deterministic(dataset):
    a = _evaluate(dataset).model_dump(mode="json")
    b = _evaluate(dataset).model_dump(mode="json")
    assert a == b


# ---------------------------------------------------------------- 干扰项
def test_distractor_hit_is_counted_separately():
    # 金标准提及在 y=900（与预测证据不重合），干扰项在 y=100（与预测证据重合）
    ds = build_dataset(total_mention_y=900.0, total_distractor_y=100.0)
    m = _all(_evaluate(ds))
    _assert_ratio(m["distractor_hit_rate"], 1, 5)
    # total_amount 的证据没有落在金标准提及上：支持率 4/5
    _assert_ratio(m["evidence_support_rate"], 4, 5)
    # 正确记录中定位失败的只有 total_amount：3/4
    _assert_ratio(m["localization_accuracy"], 3, 4)


# ---------------------------------------------------------------- 拦截率
def test_injection_intercept_rates(dataset):
    injections = [
        # 静默错误单元格仍为 grounded → 未被拦截
        InjectedError(doc_id=DOC_A, key="term_months", error_type=InjectedErrorType.WRONG_BLOCK_ID, attempt_index=0),
        # 幻觉单元格为 needs_review → 被拦截
        InjectedError(doc_id=DOC_B, key="tax_rate", error_type=InjectedErrorType.HALLUCINATED_VALUE, attempt_index=0),
        # 无键的畸形输出：attempt 0 为 schema_invalid → 被拦截
        InjectedError(doc_id=DOC_B, key=None, error_type=InjectedErrorType.MALFORMED_JSON, attempt_index=0),
    ]
    m = _all(_evaluate(dataset, injections=injections))
    _assert_ratio(m["intercept_rate:wrong_block_id"], 0, 1)
    _assert_ratio(m["intercept_rate:hallucinated_value"], 1, 1)
    _assert_ratio(m["intercept_rate:malformed_json"], 1, 1)
    # 没有注入的类型：分母为 0，value 为 None
    _assert_ratio(m["intercept_rate:distractor_value"], 0, 0)


def test_no_injections_means_no_intercept_metrics(dataset):
    m = _all(_evaluate(dataset))
    assert not [name for name in m if name.startswith("intercept_rate:")]


def test_injection_with_bad_attempt_index_raises(dataset):
    bad = [InjectedError(doc_id=DOC_B, key=None, error_type=InjectedErrorType.MALFORMED_JSON, attempt_index=9)]
    with pytest.raises(ValueError, match="越界"):
        _evaluate(dataset, injections=bad)


def test_injection_unknown_cell_raises(dataset):
    bad = [InjectedError(doc_id=DOC_A, key="no_such_key", error_type=InjectedErrorType.WRONG_BLOCK_ID, attempt_index=0)]
    with pytest.raises(ValueError, match="不存在的单元格"):
        _evaluate(dataset, injections=bad)


# ---------------------------------------------------------------- Wilson 区间（已知答案）
def test_wilson_known_answers():
    lo, hi = wilson_ci(5, 10)
    assert lo == pytest.approx(0.2366, abs=5e-4)
    assert hi == pytest.approx(0.7634, abs=5e-4)
    lo0, hi0 = wilson_ci(0, 10)
    assert lo0 == 0.0
    assert hi0 == pytest.approx(0.2775, abs=5e-4)  # z²/(n+z²) = 3.8415/13.8415
    assert wilson_ci(0, 0) is None
    lo_full, hi_full = wilson_ci(10, 10)
    assert hi_full == 1.0
    assert lo_full == pytest.approx(0.7225, abs=5e-4)


def test_wilson_rejects_out_of_range():
    with pytest.raises(ValueError):
        wilson_ci(11, 10)


# ---------------------------------------------------------------- 对齐与报告元数据
def test_gold_document_without_prediction_is_counted(dataset):
    # 结果中只有文档 A；金标准中的文档 B 计入 n_documents_missing，不静默丢弃
    partial = dataset.result.model_copy(update={"documents": [dataset.result.documents[0]]})
    report = CiteCellEvaluator(KEYSET, dataset_id=DATASET_ID, created_at=CREATED).evaluate(partial, dataset.gold)
    assert report.n_documents == 1
    assert report.n_documents_missing == 1
    assert _slice(report, "all").n_records == 5


def test_unlabeled_prediction_raises(dataset):
    gold_a_only = [g for g in dataset.gold if g.doc_sha256 == SHA_A]
    with pytest.raises(ValueError, match="没有金标准"):
        CiteCellEvaluator(KEYSET, dataset_id=DATASET_ID, created_at=CREATED).evaluate(dataset.result, gold_a_only)


def test_mixed_sources_raise(dataset):
    mixed = [dataset.gold[0], dataset.gold[1].model_copy(update={"source": DataSource.POINTER})]
    with pytest.raises(ValueError, match="一种数据来源"):
        CiteCellEvaluator(KEYSET, dataset_id=DATASET_ID, created_at=CREATED).evaluate(dataset.result, mixed)


def test_dataset_id_must_match_source(dataset):
    with pytest.raises(ValueError, match="前缀"):
        CiteCellEvaluator(KEYSET, dataset_id="pointer:x", created_at=CREATED).evaluate(dataset.result, dataset.gold)


def test_key_set_mismatch_raises(dataset):
    # 金标准少一个键 → 预测键集合与金标准不一致
    g0 = dataset.gold[0]
    trimmed = g0.model_copy(update={"records": g0.records[:-1]})
    with pytest.raises(ValueError, match="键集合"):
        CiteCellEvaluator(KEYSET, dataset_id=DATASET_ID, created_at=CREATED).evaluate(
            dataset.result, [trimmed, dataset.gold[1]]
        )


def test_naive_created_at_rejected():
    with pytest.raises(ValueError, match="时区"):
        CiteCellEvaluator(KEYSET, dataset_id=DATASET_ID, created_at=datetime(2026, 1, 1))


def test_report_metadata(dataset):
    report = _evaluate(dataset)
    assert report.run_id == "run-test-1"
    assert report.dataset_id == DATASET_ID
    assert report.evaluator_version == "eval-v1"
    assert report.n_documents == 2
    assert report.n_documents_missing == 0
    assert report.created_at == CREATED
    assert len(report.dataset_sha256) == 64
    assert report.dataset_sha256 == _evaluate(dataset).dataset_sha256


def test_dataset_hash_changes_with_gold(dataset):
    base = _evaluate(dataset).dataset_sha256
    other = build_dataset(total_mention_y=900.0)
    assert _evaluate(other).dataset_sha256 != base


def test_default_created_at_is_aware(dataset):
    report = CiteCellEvaluator(KEYSET, dataset_id=DATASET_ID).evaluate(dataset.result, dataset.gold)
    assert report.created_at.tzinfo == UTC

