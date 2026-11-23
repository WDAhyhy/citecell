"""值区间选择测试：引文内多处与值等价的提及时，取第一个通过全部守卫的提及。

守卫（疑问标签“是否”、否定词、符号、序数）按每个提及自身的起点判断，而不是只看引文起点。
文档在测试中即时构造（DOCX / XLSX / PDF 三种块类型），文本可含 PDF 行连接处的空格。
"""

from __future__ import annotations

import pytest

from citecell.models import (
    BBox,
    Block,
    BlockKind,
    Candidate,
    DocFormat,
    DocumentInfo,
    DocxLocator,
    EnumOption,
    EvidenceClaim,
    ExtractionMethod,
    IssueCode,
    KeySpec,
    ParsedDocument,
    PdfLocator,
    TableContext,
    Status,
    ValueType,
    XlsxLocator,
)
from citecell.verify import Verifier

DOC_ID = "0123456789abcdef"

BOOL = KeySpec(
    key="是否含税",
    value_type=ValueType.BOOLEAN,
    enum_options=[EnumOption(value="true", aliases=["含税"]), EnumOption(value="false", aliases=["不含税"])],
)
ENUM = KeySpec(
    key="采购方式",
    value_type=ValueType.ENUM,
    enum_options=[EnumOption(value="公开招标"), EnumOption(value="非公开招标")],
)
TEXT = KeySpec(key="负责人", value_type=ValueType.TEXT)
NUM = KeySpec(key="温度", value_type=ValueType.NUMBER)
MONEY = KeySpec(key="合同金额", value_type=ValueType.MONEY)


def _block(text: str, fmt: DocFormat) -> Block:
    if fmt == DocFormat.DOCX:
        return Block(
            doc_id=DOC_ID,
            block_id="para1",
            kind=BlockKind.PARAGRAPH,
            text=text,
            order=0,
            locator=DocxLocator(body_index=0, paragraph_number=1),
        )
    if fmt == DocFormat.XLSX:
        return Block(
            doc_id=DOC_ID,
            block_id="s1-A1",
            kind=BlockKind.TABLE_CELL,
            text=text,
            order=0,
            table=TableContext(
                table_id="s1", row_index=0, col_index=0
            ),
            locator=XlsxLocator(sheet_index=0, sheet_name="S1", cell="A1", row_number=1, column_number=1),
        )
    return Block(
        doc_id=DOC_ID,
        block_id="p1-b1",
        kind=BlockKind.PARAGRAPH,
        text=text,
        order=0,
        locator=PdfLocator(
            page_number=1,
            page_width=600,
            page_height=800,
            regions=[BBox(x0=0, y0=0, x1=500, y1=20)],
        ),
    )


def _run(text: str, spec: KeySpec, quote: str, value: str, fmt: DocFormat = DocFormat.DOCX):
    info = DocumentInfo(
        doc_id=DOC_ID,
        sha256=DOC_ID + "0" * 48,
        format=fmt,
        size_bytes=1,
        parser_name="test",
        parser_version="0",
    )
    block = _block(text, fmt)
    doc = ParsedDocument(info=info, blocks=[block])
    c = Candidate(
        doc_id=DOC_ID,
        key=spec.key,
        value_text=value,
        evidence=[EvidenceClaim(block_id=block.block_id, quote=quote)],
        method=ExtractionMethod.LLM,
        producer_id="llm:test",
    )
    return Verifier().verify(c, doc, spec)


def _span_text(rec, text: str) -> tuple[str, int]:
    ev = rec.evidence[0]
    assert ev.value_char_start is not None and ev.value_char_end is not None
    return text[ev.value_char_start : ev.value_char_end], ev.value_char_start


# ---------------------------------------------------------------------------
# 泄漏案例：值区间必须落在答案上（修复前会落在问句标签上）
# ---------------------------------------------------------------------------

LABEL_THEN_ANSWER = [
    ("colon", "是否含税：含税", "是否含税：含税", "含税", "：含税"),
]


@pytest.mark.parametrize("name,text,quote,value,_ctx", LABEL_THEN_ANSWER, ids=[c[0] for c in LABEL_THEN_ANSWER])
def test_value_span_goes_to_answer_not_question_label(name, text, quote, value, _ctx):
    rec = _run(text, BOOL, quote, value)
    assert rec.status == Status.GROUNDED, (name, [i.code for i in rec.issues])
    span_text, start = _span_text(rec, text)
    assert span_text == "含税"
    assert start == text.rindex("含税")  # 答案处，而非标签处
    assert rec.value.value is True


# ---------------------------------------------------------------------------
# 反例与边界
# ---------------------------------------------------------------------------


def test_answer_after_key_space_grounded():
    """键名之后紧跟空白即进入答案区（第五轮规则）。"""
    text = "是否含税 含税"
    rec = _run(text, BOOL, text, "含税")
    assert rec.status == Status.GROUNDED


def test_answer_after_wei_separator_grounded():
    """“为”是答案区分隔符（第四轮规则）：“。是否含税为含税”中的答案可 grounded。"""
    text = "。是否含税为含税"
    rec = _run(text, BOOL, "为含税", "含税")
    assert rec.status == Status.GROUNDED
    assert rec.value.value is True


def test_answer_before_label_takes_first_passing_mention():
    text = "含税，是否含税"
    rec = _run(text, BOOL, text, "含税")
    assert rec.status == Status.GROUNDED
    assert _span_text(rec, text)[1] == 0


def test_all_mentions_guarded_stays_needs_review():
    """“含税”同时位于键名“是否含税”内部（标签区）与问句中，没有值候选，保持 needs_review。"""
    text = "是否含税，是否含税"
    rec = _run(text, BOOL, text, "含税")
    assert rec.status == Status.NEEDS_REVIEW


def test_only_label_stays_needs_review():
    rec = _run("是否含税", BOOL, "是否含税", "含税")
    assert rec.status == Status.NEEDS_REVIEW


def test_numeric_multiple_mentions_first_unguarded_chosen():
    text = "负5度、5度"  # 注：全角逗号被 T2 的千分位排除规则误判，已报告，此处换用顿号
    rec = _run(text, NUM, text, "5")
    assert rec.status == Status.GROUNDED
    span_text, start = _span_text(rec, text)
    assert span_text == "5" and start == text.rindex("5")


def test_numeric_only_negative_mention_stays_needs_review():
    rec = _run("负5度", NUM, "负5度", "5")
    assert rec.status == Status.NEEDS_REVIEW
    assert IssueCode.VALUE_CROSSES_QUOTE_BOUNDARY in {i.code for i in rec.issues}


def test_money_multiple_equal_mentions_first_kept_without_new_warning():
    text = "合同金额200万元（大写：贰佰万元整）"
    rec = _run(text, MONEY, text, "200万元")
    assert rec.status == Status.GROUNDED
    span_text, start = _span_text(rec, text)
    assert span_text.startswith("200万元") and start == text.index("200万元")
    assert {i.code for i in rec.issues} <= {IssueCode.QUOTE_CROSS_BLOCK, IssueCode.RENDER_NOISE_STRIPPED}


def test_enum_span_on_matching_option_not_on_other_option():
    text = "采购方式：非公开招标，公开招标"
    rec = _run(text, ENUM, text, "公开招标")
    assert rec.status == Status.GROUNDED
    span_text, start = _span_text(rec, text)
    assert span_text == "公开招标" and start == text.rindex("公开招标")


def test_text_value_label_occurrence_not_chosen():
    text = "是否张三：张三"
    rec = _run(text, TEXT, text, "张三")
    assert rec.status == Status.GROUNDED
    assert _span_text(rec, text)[1] == text.rindex("张三")


@pytest.mark.parametrize("fmt", [DocFormat.DOCX, DocFormat.XLSX, DocFormat.PDF])
def test_label_then_answer_all_formats(fmt):
    text = "。是否含税：含税"
    rec = _run(text, BOOL, text, "含税", fmt)
    assert rec.status == Status.GROUNDED
    assert _span_text(rec, text)[1] == text.rindex("含税")
