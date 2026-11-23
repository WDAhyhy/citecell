"""评测测试的共享夹具：全部数据在运行时构造，不提交任何文档样例。

固定数据集（2 份文档 × 5 键 = 10 条记录），结果已知：
  文档 A（PDF）：
    total_amount  grounded 且正确（金标准 1200）
    vendor        grounded 且正确（金标准 "甲公司"）
    tax_rate      grounded 且正确（金标准 12）
    term_months   grounded 但错误（预测 24，金标准 36）      → 静默错误
    signed_date   not_found，金标准 absent                  → 正确的缺失
  文档 B（DOCX）：
    total_amount  grounded 且正确（金标准 800）
    vendor        not_found，金标准 absent                  → 正确的缺失
    tax_rate      needs_review，预测 5，金标准 absent        → 幻觉（被标为待复核）
    term_months   needs_review 且错误（预测 6，金标准 12）
    signed_date   not_found，金标准 present（2026-03-01）     → 误判缺失

由此得到：TP=4、FP=3、FN=3；grounded=5（静默错误 1）；absent=3（幻觉 1）；present=7（误判缺失 1）；
needs_review=2（均命中）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from citecell.models import (
    Attempt,
    AttemptOutcome,
    BackendInfo,
    BBox,
    DateValue,
    DataSource,
    DocFormat,
    DocumentInfo,
    DocumentResult,
    DocxLocator,
    Evidence,
    ExtractionMethod,
    ExtractionRecord,
    GoldDocument,
    GoldMention,
    GoldRecord,
    Issue,
    IssueCode,
    KeySet,
    KeySpec,
    LocatorPrecision,
    ManifestDocument,
    MatchLevel,
    MoneyValue,
    NumberValue,
    PdfLocator,
    PercentValue,
    RunConfig,
    RunManifest,
    RunResult,
    Severity,
    Status,
    TextValue,
    ValueType,
)

SHA_A = "aa" * 32
SHA_B = "bb" * 32
DOC_A = SHA_A[:16]
DOC_B = SHA_B[:16]
CREATED = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
DATASET_ID = "synthetic:test-v1"

KEYSET = KeySet(
    name="test-keys",
    version="1",
    keys=[
        KeySpec(key="total_amount", value_type=ValueType.MONEY, currency="CNY"),
        KeySpec(key="vendor", value_type=ValueType.TEXT),
        KeySpec(key="tax_rate", value_type=ValueType.PERCENT),
        KeySpec(key="term_months", value_type=ValueType.NUMBER, unit="月"),
        KeySpec(key="signed_date", value_type=ValueType.DATE),
    ],
)
KEY_ORDER = [k.key for k in KEYSET.keys]


def pdf_loc(y0: float, page: int = 1) -> PdfLocator:
    return PdfLocator(
        page_number=page,
        page_width=612.0,
        page_height=792.0,
        regions=[BBox(x0=72.0, y0=y0, x1=300.0, y1=y0 + 12.0)],
    )


def docx_loc(paragraph: int) -> DocxLocator:
    return DocxLocator(body_index=paragraph - 1, paragraph_number=paragraph)


def evidence(doc_id: str, locator, quote: str) -> Evidence:
    """引文即值本身：value_char 覆盖整段引文，字符区间为 [0, len(quote))。"""
    n = len(quote)
    return Evidence(
        doc_id=doc_id,
        block_id="p1-b1",
        quote=quote,
        match_level=MatchLevel.EXACT,
        char_start=0,
        char_end=n,
        value_char_start=0,
        value_char_end=n,
        locator=locator,
        precision=LocatorPrecision.CHAR,
        occurrences=1,
    )


def mention(locator, surface: str, *, char_span: bool = True) -> GoldMention:
    return GoldMention(
        locator=locator,
        char_start=0 if char_span else None,
        char_end=len(surface) if char_span else None,
        surface_text=surface,
    )


def grounded(doc_id: str, key: str, value, ev: Evidence, text: str) -> ExtractionRecord:
    return ExtractionRecord(
        doc_id=doc_id,
        key=key,
        status=Status.GROUNDED,
        value=value,
        value_text=text,
        evidence=[ev],
        method=ExtractionMethod.LLM,
    )


def not_found(doc_id: str, key: str) -> ExtractionRecord:
    return ExtractionRecord(doc_id=doc_id, key=key, status=Status.NOT_FOUND)


def needs_review(doc_id: str, key: str, value, text: str) -> ExtractionRecord:
    return ExtractionRecord(
        doc_id=doc_id,
        key=key,
        status=Status.NEEDS_REVIEW,
        value=value,
        value_text=text,
        issues=[Issue(code=IssueCode.CONFLICTING_CANDIDATES, severity=Severity.WARNING, message="fixture")],
    )


def attempt(doc_id: str, idx: int, outcome: AttemptOutcome, *, latency: float, cache_hit: bool = False,
            prompt_tokens: int | None = None, completion_tokens: int | None = None) -> Attempt:
    return Attempt(
        attempt_id=f"{doc_id}-{idx}",
        doc_id=doc_id,
        window_id="w1",
        keys=KEY_ORDER,
        backend_id="fake",
        prompt_template_id="tpl",
        request_sha256=SHA_A,
        started_at=CREATED,
        latency_ms=latency,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        outcome=outcome,
        cache_hit=cache_hit,
    )


def _manifest() -> RunManifest:
    backend = BackendInfo(backend_id="fake", provider="fake", model="fixture-model")
    return RunManifest(
        run_id="run-test-1",
        citecell_version="0.0.1",
        created_at=CREATED,
        python_version="3.13",
        platform="linux",
        prompt_template_id="tpl",
        prompt_template_sha256=SHA_A,
        output_schema_sha256=SHA_B,
        config=RunConfig(backend=backend),
        config_sha256=SHA_A,
        keyset_name=KEYSET.name,
        keyset_version=KEYSET.version,
        keyset_sha256=SHA_B,
        documents=[
            ManifestDocument(doc_id=DOC_A, sha256=SHA_A, format=DocFormat.PDF, size_bytes=1),
            ManifestDocument(doc_id=DOC_B, sha256=SHA_B, format=DocFormat.DOCX, size_bytes=1),
        ],
    )


def build_dataset(
    *,
    total_mention_y: float = 100.0,
    total_distractor_y: float | None = None,
) -> SimpleNamespace:
    """返回 result（RunResult）、gold（GoldDocument 列表）、keyset 与各文档的 ID。

    ``total_mention_y``：文档 A 的 total_amount 金标准提及的纵坐标（默认与预测证据重合）。
    ``total_distractor_y``：若给出，则在该处添加 total_amount 的干扰项提及。
    """
    # ---- 文档 A（PDF）
    a_loc = {k: pdf_loc(100.0 + 20 * i) for i, k in enumerate(KEY_ORDER)}
    a_total_gold_loc = pdf_loc(total_mention_y)
    a_distractors = []
    if total_distractor_y is not None:
        a_distractors = [mention(pdf_loc(total_distractor_y), "1200元")]

    a_records = [
        grounded(DOC_A, "total_amount", MoneyValue(amount=Decimal("1200")),
                 evidence(DOC_A, a_loc["total_amount"], "总额1200元"), "1200元"),
        grounded(DOC_A, "vendor", TextValue(text="甲公司"),
                 evidence(DOC_A, a_loc["vendor"], "甲公司"), "甲公司"),
        grounded(DOC_A, "tax_rate", PercentValue(value=Decimal("12")),
                 evidence(DOC_A, a_loc["tax_rate"], "税率12%"), "12%"),
        grounded(DOC_A, "term_months", NumberValue(value=Decimal("24"), unit="月"),
                 evidence(DOC_A, a_loc["term_months"], "期限24个月"), "24个月"),
        not_found(DOC_A, "signed_date"),
    ]
    a_gold = [
        GoldRecord(key="total_amount", expected="present", value=MoneyValue(amount=Decimal("1200")),
                   mentions=[mention(a_total_gold_loc, "1200元")], distractors=a_distractors,
                   tags=["surface=plain"]),
        GoldRecord(key="vendor", expected="present", value=TextValue(text="甲公司"),
                   mentions=[mention(a_loc["vendor"], "甲公司")], tags=["surface=plain"]),
        GoldRecord(key="tax_rate", expected="present", value=PercentValue(value=Decimal("12")),
                   mentions=[mention(a_loc["tax_rate"], "12%")]),
        GoldRecord(key="term_months", expected="present", value=NumberValue(value=Decimal("36"), unit="月"),
                   mentions=[mention(a_loc["term_months"], "36个月")], tags=["cross_block"]),
        GoldRecord(key="signed_date", expected="absent"),
    ]

    # ---- 文档 B（DOCX）
    b_loc = {k: docx_loc(i + 1) for i, k in enumerate(KEY_ORDER)}
    b_records = [
        grounded(DOC_B, "total_amount", MoneyValue(amount=Decimal("800")),
                 evidence(DOC_B, b_loc["total_amount"], "合计800元"), "800元"),
        not_found(DOC_B, "vendor"),
        needs_review(DOC_B, "tax_rate", PercentValue(value=Decimal("5")), "5%"),
        needs_review(DOC_B, "term_months", NumberValue(value=Decimal("6"), unit="月"), "6个月"),
        not_found(DOC_B, "signed_date"),
    ]
    b_gold = [
        GoldRecord(key="total_amount", expected="present", value=MoneyValue(amount=Decimal("800")),
                   mentions=[mention(b_loc["total_amount"], "800元")], tags=["surface=plain"]),
        GoldRecord(key="vendor", expected="absent"),
        GoldRecord(key="tax_rate", expected="absent"),
        GoldRecord(key="term_months", expected="present", value=NumberValue(value=Decimal("12"), unit="月"),
                   mentions=[mention(b_loc["term_months"], "12个月")]),
        GoldRecord(key="signed_date", expected="present", value=DateValue(iso="2026-03-01", precision="day"),
                   mentions=[mention(b_loc["signed_date"], "2026年3月1日")]),
    ]

    doc_a = DocumentResult(
        info=DocumentInfo(doc_id=DOC_A, sha256=SHA_A, format=DocFormat.PDF, size_bytes=1,
                          page_count=1, parser_name="fixture", parser_version="0"),
        records=a_records,
        attempts=[attempt(DOC_A, 0, AttemptOutcome.OK, latency=80.0, cache_hit=True)],
    )
    doc_b = DocumentResult(
        info=DocumentInfo(doc_id=DOC_B, sha256=SHA_B, format=DocFormat.DOCX, size_bytes=1,
                          parser_name="fixture", parser_version="0"),
        records=b_records,
        attempts=[
            attempt(DOC_B, 0, AttemptOutcome.SCHEMA_INVALID, latency=50.0),
            attempt(DOC_B, 1, AttemptOutcome.OK, latency=100.0, prompt_tokens=1000, completion_tokens=50),
        ],
    )
    result = RunResult(manifest=_manifest(), documents=[doc_a, doc_b])
    gold = [
        GoldDocument(doc_sha256=SHA_A, format=DocFormat.PDF, source=DataSource.SYNTHETIC,
                     keyset_name=KEYSET.name, keyset_version=KEYSET.version, records=a_gold),
        GoldDocument(doc_sha256=SHA_B, format=DocFormat.DOCX, source=DataSource.SYNTHETIC,
                     keyset_name=KEYSET.name, keyset_version=KEYSET.version, records=b_gold),
    ]
    return SimpleNamespace(result=result, gold=gold, keyset=KEYSET, doc_a=DOC_A, doc_b=DOC_B)


@pytest.fixture
def dataset() -> SimpleNamespace:
    return build_dataset()


@pytest.fixture
def make_dataset():
    return build_dataset
