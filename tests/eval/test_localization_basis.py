"""定位准确率的口径：PDF 用值区间（解析片段还原），缺失时回退引文区域并显式计数（ARCH §9）。"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from citecell.eval import CiteCellEvaluator, render_json, render_markdown
from citecell.eval import geometry as geo
from citecell.eval.geometry import evidence_hits_mention, iou
from citecell.models import (
    BBox,
    Block,
    BlockKind,
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
    BackendInfo,
    XlsxLocator,
)

SHA = "cc" * 32
DOC = SHA[:16]
CREATED = datetime(2026, 11, 23, tzinfo=UTC)
KEYSET = KeySet(name="loc-keys", version="1", keys=[KeySpec(key="total_amount", value_type=ValueType.MONEY)])

TEXT = "这是一段很长的左侧上下文用来撑开引文区域总额1200元"
VSTART = TEXT.index("1200元")
VEND = VSTART + len("1200元")
CHAR_W = 10.0


def char_box(i: int, y0: float = 100.0) -> BBox:
    x0 = 72.0 + CHAR_W * i
    return BBox(x0=x0, y0=y0, x1=x0 + CHAR_W, y1=y0 + 12.0)


def union(boxes: list[BBox]) -> BBox:
    return BBox(
        x0=min(b.x0 for b in boxes),
        y0=min(b.y0 for b in boxes),
        x1=max(b.x1 for b in boxes),
        y1=max(b.y1 for b in boxes),
    )


QUOTE_BOX = union([char_box(i) for i in range(len(TEXT))])  # 整段引文的区域（宽）
VALUE_BOX = union([char_box(i) for i in range(VSTART, VEND)])  # 值的框（窄）


def pdf_parsed(*, block_id: str = "p1-b1", fragments: bool = True, text: str = TEXT) -> ParsedDocument:
    frags = (
        [TextFragment(start=i, end=i + 1, page_number=1, line=0, bbox=char_box(i)) for i in range(len(text))]
        if fragments
        else []
    )
    block = Block(
        doc_id=DOC,
        block_id=block_id,
        kind=BlockKind.PARAGRAPH,
        text=text,
        order=0,
        locator=PdfLocator(page_number=1, page_width=612.0, page_height=792.0, regions=[QUOTE_BOX]),
        fragments=frags,
    )
    info = DocumentInfo(doc_id=DOC, sha256=SHA, format=DocFormat.PDF, size_bytes=1, page_count=1,
                        parser_name="fixture", parser_version="0")
    return ParsedDocument(info=info, blocks=[block])


def pdf_evidence(*, value: bool = True, block_id: str = "p1-b1") -> Evidence:
    return Evidence(
        doc_id=DOC,
        block_id=block_id,
        quote=TEXT,
        match_level=MatchLevel.EXACT,
        char_start=0,
        char_end=len(TEXT),
        value_char_start=VSTART if value else None,
        value_char_end=VEND if value else None,
        locator=PdfLocator(page_number=1, page_width=612.0, page_height=792.0, regions=[QUOTE_BOX]),
        precision=LocatorPrecision.CHAR,
        occurrences=1,
    )


def pdf_dataset(*, evidence: Evidence | None = None):
    gold_loc = PdfLocator(page_number=1, page_width=612.0, page_height=792.0, regions=[VALUE_BOX])
    rec = ExtractionRecord(
        doc_id=DOC,
        key="total_amount",
        status=Status.GROUNDED,
        value=MoneyValue(amount=Decimal("1200")),
        value_text="1200元",
        # grounded 至少需要一条带值区间的证据；回退用例额外附带一条无值区间的证据
        evidence=evidence if isinstance(evidence, list) else [evidence or pdf_evidence()],
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
                mentions=[GoldMention(locator=gold_loc, surface_text="1200元")],
            )
        ],
    )
    manifest = RunManifest(
        run_id="run-loc-1",
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
    ev = CiteCellEvaluator(KEYSET, dataset_id="synthetic:loc-v1", created_at=CREATED)
    return ev.evaluate(result, gold, parsed=parsed)


def _metric(report, name):
    (s,) = [x for x in report.slices if x.slice_name == "all"]
    return {m.name: m for m in s.metrics}[name]


# ------------------------------------------------ 已知答案：宽引文、窄值
def test_quote_box_is_wide_and_quote_basis_would_fail():
    # 引文框是值框的很多倍宽：IoU = 40 / (10 × 整段字符数) < 0.5
    assert VALUE_BOX.x1 - VALUE_BOX.x0 == pytest.approx(5 * CHAR_W)
    assert iou(QUOTE_BOX, VALUE_BOX) < 0.5
    assert iou(QUOTE_BOX, VALUE_BOX) == pytest.approx(5 * CHAR_W / (len(TEXT) * CHAR_W), abs=1e-9)


def test_value_basis_hits_where_quote_basis_misses():
    result, gold = pdf_dataset()
    report = _evaluate(result, gold, parsed={DOC: pdf_parsed()})
    loc = _metric(report, "localization_accuracy")
    assert (loc.numerator, loc.denominator) == (1, 1)
    assert _metric(report, "localization_value_basis").numerator == 1
    assert _metric(report, "localization_quote_fallback").numerator == 0


def test_no_parsed_result_falls_back_to_quote_and_is_counted():
    result, gold = pdf_dataset()
    report = _evaluate(result, gold, parsed=None)
    # 引文口径：IoU < 0.5 → 未命中；回退必须被计数
    assert (_metric(report, "localization_accuracy").numerator, _metric(report, "localization_accuracy").denominator) == (0, 1)
    assert _metric(report, "localization_quote_fallback").numerator == 1
    assert _metric(report, "localization_value_basis").numerator == 0


@pytest.mark.parametrize(
    "ev_kwargs, parsed_kwargs",
    [
        ({"value": False}, {}),  # 缺少 value_char_start/end
        ({}, {"block_id": "p9-b9"}),  # 解析结果里没有该块
        ({}, {"fragments": False}),  # 块没有逐字符片段
    ],
    ids=["no-value-offsets", "block-missing", "no-fragments"],
)
def test_fallback_paths_are_counted(ev_kwargs, parsed_kwargs):
    # 第二条证据带值区间（使记录满足 grounded 不变量）；无值区间的证据应回退并被计数
    evs = [pdf_evidence(**ev_kwargs), pdf_evidence()] if ev_kwargs == {"value": False} else [pdf_evidence(**ev_kwargs)]
    result, gold = pdf_dataset(evidence=evs)
    report = _evaluate(result, gold, parsed={DOC: pdf_parsed(**parsed_kwargs)})
    assert _metric(report, "localization_quote_fallback").numerator == 1
    # 无值区间的证据只能回退到引文（未命中）；带值区间的证据在值口径下命中（仅 no-value-offsets 用例）
    expected_hit = 1 if ev_kwargs == {"value": False} else 0
    assert _metric(report, "localization_accuracy").numerator == expected_hit


def test_value_regions_merge_same_line_and_split_lines():
    p = pdf_parsed()
    block = p.blocks[0]
    regions = geo.value_regions(block, VSTART, VEND)
    assert regions is not None and len(regions) == 1
    page, box = regions[0]
    assert page == 1
    assert box == VALUE_BOX  # 同一行的片段合并为一个框
    # 跨两行：每行一个框
    two_lines = [
        TextFragment(start=0, end=2, page_number=1, line=0, bbox=char_box(0)),
        TextFragment(start=2, end=4, page_number=1, line=1, bbox=char_box(2, y0=130.0)),
    ]
    b2 = block.model_copy(update={"fragments": two_lines})
    assert len(geo.value_regions(b2, 0, 4)) == 2


def test_value_regions_none_without_overlapping_fragments():
    block = pdf_parsed().blocks[0]
    assert geo.value_regions(block, 0, 0) is None or geo.value_regions(block, 0, 0) == []
    no_frag = pdf_parsed(fragments=False).blocks[0]
    assert geo.value_regions(no_frag, VSTART, VEND) is None


def test_pdf_value_hit_requires_same_page():
    regions = [(2, VALUE_BOX)]
    gold = PdfLocator(page_number=1, page_width=612.0, page_height=792.0, regions=[VALUE_BOX])
    assert not geo.pdf_value_hit(regions, gold, iou_threshold=0.5)


def test_localization_hit_reports_basis():
    ev = pdf_evidence()
    block = pdf_parsed().blocks[0]
    gold = GoldMention(
        locator=PdfLocator(page_number=1, page_width=612.0, page_height=792.0, regions=[VALUE_BOX]),
        surface_text="1200元",
    )
    assert geo.localization_hit(ev, gold, block=block, iou_threshold=0.5) == (True, "value")
    assert geo.localization_hit(ev, gold, block=None, iou_threshold=0.5) == (False, "quote")


# ------------------------------------------------ DOCX / XLSX 不受 PDF 口径改动影响
def test_docx_uses_value_char_interval_not_quote():
    ev = Evidence(
        doc_id=DOC,
        block_id="para1",
        quote="0123456789",
        match_level=MatchLevel.EXACT,
        char_start=0,
        char_end=10,
        value_char_start=2,
        value_char_end=5,
        locator=DocxLocator(body_index=0, paragraph_number=1),
        precision=LocatorPrecision.CHAR,
        occurrences=1,
    )
    miss = GoldMention(locator=DocxLocator(body_index=0, paragraph_number=1), char_start=6, char_end=8,
                       surface_text="67")
    hit = GoldMention(locator=DocxLocator(body_index=0, paragraph_number=1), char_start=4, char_end=8,
                      surface_text="4567")
    assert geo.localization_hit(ev, miss, block=None, iou_threshold=0.5) == (False, "value")
    assert geo.localization_hit(ev, hit, block=None, iou_threshold=0.5) == (True, "value")
    # 旧口径（引文区间）仍用于证据支持率，不受影响
    assert evidence_hits_mention(ev, miss, iou_threshold=0.5)


def test_docx_without_value_offsets_falls_back_to_quote_and_is_labelled():
    ev = Evidence(
        doc_id=DOC,
        block_id="para1",
        quote="0123456789",
        match_level=MatchLevel.EXACT,
        char_start=0,
        char_end=10,
        locator=DocxLocator(body_index=0, paragraph_number=1),
        precision=LocatorPrecision.CHAR,
        occurrences=1,
    )
    gold = GoldMention(locator=DocxLocator(body_index=0, paragraph_number=1), char_start=6, char_end=8,
                       surface_text="67")
    assert geo.localization_hit(ev, gold, block=None, iou_threshold=0.5) == (True, "quote")


def test_xlsx_same_cell_is_value_basis():
    loc = XlsxLocator(sheet_index=0, sheet_name="项目", cell="B3", row_number=3, column_number=2)
    ev = Evidence(doc_id=DOC, block_id="s1-B3", quote="1200", match_level=MatchLevel.EXACT, char_start=0,
                  char_end=4, value_char_start=0, value_char_end=4, locator=loc,
                  precision=LocatorPrecision.CELL, occurrences=1)
    gold = GoldMention(locator=loc, surface_text="1200")
    assert geo.localization_hit(ev, gold, block=None, iou_threshold=0.5) == (True, "value")


# ------------------------------------------------ 报告显示口径与回退数
def test_report_and_json_show_basis_and_fallback_count():
    result, gold = pdf_dataset(evidence=[pdf_evidence(value=False), pdf_evidence()])
    report = _evaluate(result, gold, parsed={DOC: pdf_parsed()})
    md = render_markdown([report])
    assert "定位口径" in md
    assert "值区间" in md
    assert "回退" in md
    import json

    payload = json.loads(render_json([report]))
    loc = payload["reports"][0]["localization"]
    assert loc["basis"] == "value"
    assert loc["quote_fallback_records"] == 1
    assert loc["judged_records"] == 1


def test_parsed_doc_id_mismatch_raises():
    result, gold = pdf_dataset()
    other = pdf_parsed().model_copy(update={})
    wrong = {"ffffffffffffffff": other}
    with pytest.raises(ValueError, match="parsed"):
        _evaluate(result, gold, parsed=wrong)
