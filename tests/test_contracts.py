"""契约冒烟测试（T0）。只读取 src/citecell/models.py 与 interfaces.py，不修改契约。

覆盖：
- models / interfaces 可以导入；
- 模块中所有 pydantic BaseModel 子类都能生成 JSON Schema；
- 若干状态不变量的正反例（fuzzy 不能 grounded、not_found 不得带值等）。
"""

from __future__ import annotations

import inspect

import pytest
from pydantic import BaseModel, ValidationError

import citecell.interfaces as interfaces
import citecell.models as models
from citecell.models import (
    Evidence,
    ExtractionMethod,
    ExtractionRecord,
    Issue,
    IssueCode,
    MatchLevel,
    MetricValue,
    Severity,
    Status,
    TextValue,
)

DOC = "0123456789abcdef"


def _all_basemodels() -> list[type[BaseModel]]:
    out: list[type[BaseModel]] = []
    for _, obj in inspect.getmembers(models, inspect.isclass):
        if issubclass(obj, BaseModel) and obj.__module__ == models.__name__:
            out.append(obj)
    return out


def test_interfaces_importable() -> None:
    names = [n for n in dir(interfaces) if n[:1].isupper()]
    assert "Verifier" in names and "Pipeline" in names


def test_models_have_expected_size() -> None:
    # 契约冻结后类数量应稳定；此处只保证非空，避免误删整段
    assert len(_all_basemodels()) >= 40


@pytest.mark.parametrize("cls", _all_basemodels(), ids=lambda c: c.__name__)
def test_basemodel_json_schema(cls: type[BaseModel]) -> None:
    schema = cls.model_json_schema()
    assert isinstance(schema, dict)
    assert schema.get("type") == "object" or "$defs" in schema or "anyOf" in schema or "oneOf" in schema


def test_runconfig_t0_fields_present() -> None:
    fields = models.RunConfig.model_fields
    for name in ("profile", "prefilter", "max_keys_per_window", "pdf_backend"):
        assert name in fields, f"RunConfig 缺少字段 {name}"
    assert fields["pdf_backend"].default == "pypdfium2"


def test_fuzzy_evidence_cannot_be_grounded() -> None:
    ev = Evidence(
        doc_id=DOC,
        block_id="b1",
        quote="近似引文",
        match_level=MatchLevel.FUZZY,
        match_score=0.9,
    )
    with pytest.raises(ValidationError, match="fuzzy"):
        ExtractionRecord(
            doc_id=DOC,
            key="amount",
            status=Status.GROUNDED,
            value=TextValue(text="x"),
            evidence=[ev],
            method=list(ExtractionMethod)[0],
        )


def test_not_found_must_not_carry_value() -> None:
    with pytest.raises(ValidationError, match="not_found"):
        ExtractionRecord(doc_id=DOC, key="amount", status=Status.NOT_FOUND, value=TextValue(text="x"))


def test_not_found_plain_is_valid() -> None:
    rec = ExtractionRecord(doc_id=DOC, key="amount", status=Status.NOT_FOUND)
    assert rec.value is None and rec.evidence == []


def test_needs_review_requires_warning_or_error() -> None:
    with pytest.raises(ValidationError, match="needs_review"):
        ExtractionRecord(doc_id=DOC, key="amount", status=Status.NEEDS_REVIEW, issues=[])
    ok = ExtractionRecord(
        doc_id=DOC,
        key="amount",
        status=Status.NEEDS_REVIEW,
        issues=[Issue(code=list(IssueCode)[0], severity=Severity.WARNING)],
    )
    assert ok.status == Status.NEEDS_REVIEW


def test_failed_requires_error_issue() -> None:
    with pytest.raises(ValidationError, match="failed"):
        ExtractionRecord(
            doc_id=DOC,
            key="amount",
            status=Status.FAILED,
            issues=[Issue(code=list(IssueCode)[0], severity=Severity.WARNING)],
        )


def test_located_evidence_requires_offsets() -> None:
    with pytest.raises(ValidationError, match="char_start"):
        Evidence(doc_id=DOC, block_id="b1", quote="q", match_level=MatchLevel.EXACT)


def test_metric_value_ratio_invariant() -> None:
    ok = MetricValue(name="precision", numerator=1, denominator=4, value=0.25)
    assert ok.value == 0.25
    none_ok = MetricValue(name="precision", numerator=0, denominator=0, value=None)
    assert none_ok.value is None
    with pytest.raises(ValidationError):
        MetricValue(name="precision", numerator=0, denominator=0, value=0.0)
    with pytest.raises(ValidationError):
        MetricValue(name="precision", numerator=1, denominator=4, value=0.5)
