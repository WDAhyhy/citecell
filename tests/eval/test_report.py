"""报告：Markdown 中的每个数字都可追溯到 EvalReport（或由其分子分母按 Wilson 重算）；
合成与真实分节；样本过少与无数据标注；JSON 为确定性的规范输出。"""

from __future__ import annotations

import json
import re

import pytest

from citecell.eval import CiteCellEvaluator, render_json, render_markdown, wilson_ci
from citecell.eval.metrics import MIN_SAMPLE
from citecell.eval.report import NO_DATA, SMALL_SAMPLE
from citecell.models import (
    DataSource,
    EvalReport,
    EvalSlice,
    MetricValue,
)

from .conftest import CREATED, DATASET_ID, KEYSET, build_dataset

_NUM = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?")


def _report(ds, dataset_id=DATASET_ID) -> EvalReport:
    return CiteCellEvaluator(KEYSET, dataset_id=dataset_id, created_at=CREATED).evaluate(ds.result, ds.gold)


def _pointer_report(ds) -> EvalReport:
    gold = [g.model_copy(update={"source": DataSource.POINTER}) for g in ds.gold]
    return CiteCellEvaluator(KEYSET, dataset_id="pointer:real-dev-v1", created_at=CREATED).evaluate(ds.result, gold)


def _tokens(text: str) -> list[str]:
    return _NUM.findall(text)


def test_every_markdown_number_traces_to_report():
    ds = build_dataset()
    report = _report(ds)
    md = render_markdown([report])
    js = render_json([report])

    # 允许的数字：JSON 文本中出现过的数字串（报告字段与 CI 均在 JSON 中）
    allowed_strings = set(_tokens(js))
    allowed_floats = [float(t) for t in allowed_strings]

    assert NO_DATA not in js  # 本数据集的所有比率都有分母
    for tok in _tokens(md):
        if tok in allowed_strings:
            continue
        # Markdown 中的小数是对 JSON 中全精度浮点数的舍入：容差为该小数位数的半个最小单位
        x = float(tok)
        decimals = len(tok.split(".")[1]) if "." in tok else 0
        tol = 0.5 * 10 ** (-decimals) + 1e-12
        assert any(abs(x - y) <= tol for y in allowed_floats), f"数字 {tok} 无法追溯到报告"


def test_markdown_contains_key_numbers():
    report = _report(build_dataset())
    md = render_markdown([report])
    assert "| `silent_error_rate` | 1 | 5 | 0.2000 |" in md
    assert "| `hallucination_rate_absent` | 1 | 3 | 0.3333 |" in md
    assert "| `value_f1` | 8 | 14 | 0.5714 |" in md


def test_synthetic_and_pointer_are_sectioned_separately():
    ds = build_dataset()
    syn = _report(ds)
    ptr = _pointer_report(ds)
    md = render_markdown([ptr, syn])  # 传入顺序相反也必须得到同一输出
    assert md == render_markdown([syn, ptr])
    i_syn = md.index("## 合成数据（synthetic）")
    i_ptr = md.index("## 真实指针集（pointer）")
    assert i_syn < i_ptr
    # 两节各自包含自己的数据集，没有合并总数
    assert md.count("### 数据集") == 2


def test_small_sample_is_labelled():
    md = render_markdown([_report(build_dataset())])
    assert SMALL_SAMPLE in md  # 记录数 10 < MIN_SAMPLE


def test_no_data_is_labelled_for_zero_denominator():
    m = MetricValue(name="silent_error_rate", numerator=0, denominator=0, value=None)
    report = EvalReport(
        run_id="r",
        dataset_id=DATASET_ID,
        dataset_sha256="0" * 64,
        evaluator_version="eval-v1",
        created_at=CREATED,
        n_documents=0,
        slices=[EvalSlice(slice_name="all", n_records=0, metrics=[m])],
    )
    md = render_markdown([report])
    assert f"| `silent_error_rate` | 0 | 0 | {NO_DATA} | {NO_DATA} | {NO_DATA} |" in md
    assert NO_DATA in render_markdown([])


def test_json_is_authoritative_and_has_ci():
    report = _report(build_dataset())
    payload = json.loads(render_json([report]))
    assert payload["min_sample"] == MIN_SAMPLE
    (entry,) = payload["reports"]
    assert entry["source"] == "synthetic"
    assert entry["report"] == json.loads(json.dumps(report.model_dump(mode="json")))
    ci = entry["ci95"]["all"]["silent_error_rate"]
    # 已知答案：1/5 的 Wilson 区间
    lo, hi = wilson_ci(1, 5)
    assert ci["lo"] == pytest.approx(lo)
    assert ci["hi"] == pytest.approx(hi)
    assert ci["sample_small"] is True


def test_render_is_deterministic():
    ds = build_dataset()
    assert render_markdown([_report(ds)]) == render_markdown([_report(ds)])
    assert render_json([_report(ds)]) == render_json([_report(ds)])


def test_unknown_source_prefix_is_rejected():
    report = _report(build_dataset()).model_copy(update={"dataset_id": "other:x"})
    with pytest.raises(ValueError, match="来源前缀"):
        render_markdown([report])
