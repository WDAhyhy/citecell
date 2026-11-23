"""证据支持率、干扰项命中率的口径：值区间（与定位准确率一致），回退引文并计数；
第三类「既非支持也非干扰」显式计数（ARCH §9）。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from citecell.eval import CiteCellEvaluator, render_json, render_markdown
from citecell.models import (
    BackendInfo,
    BBox,
    Block,
    BlockKind,
    DataSource,
    DocFormat,
    DocumentInfo,
    DocumentResult,
    Evidence,
    ExtractionMethod,
    ExtractionRecord,
    GoldDocument,
    GoldMention,
    GoldRecord,
    KeySet,
    KeySpec,
    LocatorPrecision,
    ManifestDocument,
    MatchLevel,
    MoneyValue,
    ParsedDocument,
    PdfLocator,
    RunConfig,
    RunManifest,
    RunResult,
    Status,
    TextFragment,
    ValueType,
)

SHA = "dd" * 32
DOC = SHA[:16]
CREATED = datetime(2026, 11, 23, tzinfo=UTC)
KEYSET = KeySet(name="anchor-keys", version="1", keys=[KeySpec(key="total_amount", value_type=ValueType.MONEY)])
TEXT = "1200元" + "无" * 15  # 共 20 个字符
CHAR_W = 10.0


def char_box(i: int) -> BBox:
    x0 = 72.0 + CHAR_W * i
    return BBox(x0=x0, y0=100.0, x1=x0 + CHAR_W, y1=112.0)


def span_box(start: int, end: int) -> BBox:
    return BBox(x0=72.0 + CHAR_W * start, y0=100.0, x1=72.0 + CHAR_W * end, y1=112.0)


QUOTE_BOX = span_box(0, len(TEXT))  # 引文区域：整段（宽）
MENTION_RANGE = (0, 5)  # 金标准提及 = "1200元"
DISTRACTOR_RANGE = (10, 20)  # 干扰项覆盖引文后半段
UNANCHORED_RANGE = (5, 10)  # 落在提及与干扰项之间的空隙上（用于第三类）


def _pdf_parsed() -> ParsedDocument:
    frags = [TextFragment(start=i, end=i + 1, page_number=1, line=0, bbox=char_box(i)) for i in range(len(TEXT))]
    block = Block(
        doc_id=DOC,
        block_id="p1-b1",
        kind=BlockKind.PARAGRAPH,
        text=TEXT,
        order=0,
        locator=PdfLocator(page_number=1, page_width=612.0, page_height=792.0, regions=[QUOTE_BOX]),
        fragments=frags,
    )
    info = DocumentInfo(doc_id=DOC, sha256=SHA, format=DocFormat.PDF, size_bytes=1, page_count=1,
                        parser_name="fixture", parser_version="0")
    return ParsedDocument(info=info, blocks=[block])


def _evidence(value_range: tuple[int, int] | None) -> Evidence:
    vs, ve = value_range if value_range else (None, None)
    return Evidence(
        doc_id=DOC,
        block_id="p1-b1",
        quote=TEXT,
        match_level=MatchLevel.EXACT,
        char_start=0,
        char_end=len(TEXT),
        value_char_start=vs,
        value_char_end=ve,
        locator=PdfLocator(page_number=1, page_width=612.0, page_height=792.0, regions=[QUOTE_BOX]),
        precision=LocatorPrecision.CHAR,
        occurrences=1,
    )


def _mention(rng: tuple[int, int], text: str) -> GoldMention:
    return GoldMention(
        locator=PdfLocator(page_number=1, page_width=612.0, page_height=792.0,
                           regions=[span_box(*rng)]),
        surface_text=text,
    )


def _dataset(value_range: tuple[int, int] | None, *, with_distractor: bool = True):
    rec = ExtractionRecord(
        doc_id=DOC,
        key="total_amount",
        status=Status.GROUNDED,
        value=MoneyValue(amount=Decimal("1200")),
        value_text="1200元",
        evidence=[_evidence(value_range)],
        method=ExtractionMethod.LLM,
    )
    gold = GoldDocument(
        doc_sha256=SHA,
        format=DocFormat.PDF,
        source=DataSource.SYNTHETIC,
        keyset_name=KEYSET.name,
        keyset_version=KEYSET.version,
        records=[
            GoldRecord(
                key="total_amount",
                expected="present",
                value=MoneyValue(amount=Decimal("1200")),
                mentions=[_mention(MENTION_RANGE, "1200元")],
                distractors=[_mention(DISTRACTOR_RANGE, "干扰")] if with_distractor else [],
            )
        ],
    )
    manifest = RunManifest(
        run_id="run-anchor-1",
        citecell_version="0.0.1",
        created_at=CREATED,
        python_version="3.13",
        platform="linux",
        prompt_template_id="tpl",
        prompt_template_sha256=SHA,
        output_schema_sha256=SHA,
        config=RunConfig(backend=BackendInfo(backend_id="fake", provider="fake", model="m")),
        config_sha256=SHA,
        keyset_name=KEYSET.name,
        keyset_version=KEYSET.version,
        keyset_sha256=SHA,
        documents=[ManifestDocument(doc_id=DOC, sha256=SHA, format=DocFormat.PDF, size_bytes=1)],
    )
    doc = DocumentResult(
        info=DocumentInfo(doc_id=DOC, sha256=SHA, format=DocFormat.PDF, size_bytes=1, page_count=1,
                          parser_name="fixture", parser_version="0"),
        records=[rec],
    )
    return RunResult(manifest=manifest, documents=[doc]), [gold]


def _evaluate(result, gold, parsed=None):
    return CiteCellEvaluator(KEYSET, dataset_id="synthetic:anchor-v1", created_at=CREATED).evaluate(
        result, gold, parsed=parsed
    )


def _metric(report, name):
    (s,) = [x for x in report.slices if x.slice_name == "all"]
    return {m.name: m for m in s.metrics}[name]


def _ratio(report, name) -> tuple[int, int]:
    m = _metric(report, name)
    return m.numerator, m.denominator


# ------------------------------------------------ 已知答案：引文覆盖提及与干扰项
def test_quote_box_geometry_known_answer():
    # 引文框与提及框 IoU = 50/200 = 0.25；与干扰项框 IoU = 100/200 = 0.5
    from citecell.eval.geometry import iou

    assert iou(QUOTE_BOX, span_box(*MENTION_RANGE)) == pytest.approx(0.25)
    assert iou(QUOTE_BOX, span_box(*DISTRACTOR_RANGE)) == pytest.approx(0.5)


def test_value_basis_supports_and_does_not_hit_distractor():
    result, gold = _dataset(MENTION_RANGE)
    report = _evaluate(result, gold, parsed={DOC: _pdf_parsed()})
    assert _ratio(report, "evidence_support_rate") == (1, 1)
    assert _ratio(report, "distractor_hit_rate") == (0, 1)
    assert _ratio(report, "evidence_unanchored_rate") == (0, 1)
    assert _metric(report, "anchor_value_basis").numerator == 1
    assert _metric(report, "anchor_quote_fallback").numerator == 0


def test_quote_basis_would_hit_distractor_and_miss_support():
    # 无解析结果 → 回退引文口径，且必须被计数
    result, gold = _dataset(MENTION_RANGE)
    report = _evaluate(result, gold, parsed=None)
    assert _ratio(report, "evidence_support_rate") == (0, 1)
    assert _ratio(report, "distractor_hit_rate") == (1, 1)
    assert _metric(report, "anchor_quote_fallback").numerator == 1
    assert _metric(report, "anchor_value_basis").numerator == 0


def test_unanchored_value_is_third_class_not_swallowed():
    result, gold = _dataset(UNANCHORED_RANGE)
    report = _evaluate(result, gold, parsed={DOC: _pdf_parsed()})
    assert _ratio(report, "evidence_support_rate") == (0, 1)
    assert _ratio(report, "distractor_hit_rate") == (0, 1)
    assert _ratio(report, "evidence_unanchored_rate") == (1, 1)


def test_block_without_fragments_falls_back_and_is_counted():
    parsed = _pdf_parsed()
    blk = parsed.blocks[0].model_copy(update={"fragments": []})
    parsed = parsed.model_copy(update={"blocks": [blk]})
    result, gold = _dataset(MENTION_RANGE)
    report = _evaluate(result, gold, parsed={DOC: parsed})
    assert _metric(report, "anchor_quote_fallback").numerator == 1
    assert _ratio(report, "distractor_hit_rate") == (1, 1)


def test_no_distractors_means_zero_distractor_hits_not_unanchored():
    result, gold = _dataset(MENTION_RANGE, with_distractor=False)
    report = _evaluate(result, gold, parsed={DOC: _pdf_parsed()})
    assert _ratio(report, "distractor_hit_rate") == (0, 1)
    assert _ratio(report, "evidence_support_rate") == (1, 1)
    assert _ratio(report, "evidence_unanchored_rate") == (0, 1)


def test_report_shows_anchor_basis_and_counts():
    result, gold = _dataset(UNANCHORED_RANGE)
    report = _evaluate(result, gold, parsed={DOC: _pdf_parsed()})
    md = render_markdown([report])
    assert "证据锚定口径" in md
    assert "既非支持也非干扰" in md
    payload = json.loads(render_json([report]))
    anchor = payload["reports"][0]["anchor"]
    assert anchor["basis"] == "value"
    assert anchor["unanchored_records"] == 1
    assert anchor["judged_records"] == 1
    assert anchor["quote_fallback_records"] == 0


def test_report_shows_fallback_count_for_anchor():
    result, gold = _dataset(MENTION_RANGE)
    report = _evaluate(result, gold, parsed=None)
    payload = json.loads(render_json([report]))
    assert payload["reports"][0]["anchor"]["quote_fallback_records"] == 1
