"""守卫回归测试：符号、序数、疑问标签、多处出现（审查发现的 4 个仍泄漏案例与对应的召回用例）。

每个用例：块文本、引文、值、键定义。期望在守卫规则之下：
* 泄漏案例必须判 needs_review（不 grounded）；
* 正例（真实答案、无守卫上下文）必须仍 grounded。
"""

from __future__ import annotations

import pytest

from citecell.models import (
    Block,
    BlockKind,
    Candidate,
    DocFormat,
    DocumentInfo,
    DocxLocator,
    EnumOption,
    EvidenceClaim,
    EvidenceFlag,
    ExtractionMethod,
    IssueCode,
    KeySpec,
    ParsedDocument,
    Status,
    ValueType,
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
NUM = KeySpec(key="温度", value_type=ValueType.NUMBER)
MONTHS = KeySpec(key="质保期", value_type=ValueType.NUMBER, unit="月")
MONEY = KeySpec(key="合同金额", value_type=ValueType.MONEY)


def _doc(text: str) -> ParsedDocument:
    info = DocumentInfo(
        doc_id=DOC_ID,
        sha256=DOC_ID + "0" * 48,
        format=DocFormat.DOCX,
        size_bytes=1,
        parser_name="test",
        parser_version="0",
    )
    b = Block(
        doc_id=DOC_ID,
        block_id="para1",
        kind=BlockKind.PARAGRAPH,
        text=text,
        order=0,
        locator=DocxLocator(body_index=0, paragraph_number=1),
    )
    return ParsedDocument(info=info, blocks=[b])


def _run(text: str, spec: KeySpec, quote: str, value: str):
    c = Candidate(
        doc_id=DOC_ID,
        key=spec.key,
        value_text=value,
        evidence=[EvidenceClaim(block_id="para1", quote=quote)],
        method=ExtractionMethod.LLM,
        producer_id="llm:test",
    )
    return Verifier().verify(c, _doc(text), spec)


def _codes(rec) -> set[IssueCode]:
    return {i.code for i in rec.issues}


# ---------------------------------------------------------------------------
# 泄漏案例（修复前必须失败）
# ---------------------------------------------------------------------------

LEAKS = [
    ("sign_arabic", "温度为负5度", "5", "5", NUM, IssueCode.VALUE_CROSSES_QUOTE_BOUNDARY),
    ("sign_chinese_numeral", "温度为负五度", "五", "五", NUM, IssueCode.VALUE_CROSSES_QUOTE_BOUNDARY),
    ("question_label", "是否含税", "含税", "含税", BOOL, IssueCode.NEGATION_CONTEXT),
    ("question_label_and_answer", "是否含税：不含税", "含税", "含税", BOOL, IssueCode.NEGATION_CONTEXT),
    ("ordinal", "第六个月支付尾款", "六个月", "六个月", MONTHS, IssueCode.VALUE_CROSSES_QUOTE_BOUNDARY),
]


@pytest.mark.parametrize("name,text,quote,value,spec,code", LEAKS, ids=[c[0] for c in LEAKS])
def test_leak_case_is_needs_review(name, text, quote, value, spec, code):
    rec = _run(text, spec, quote, value)
    assert rec.status == Status.NEEDS_REVIEW, (name, rec.status, [i.code for i in rec.issues])
    assert code in _codes(rec)


def test_question_label_message_says_question_context():
    rec = _run("是否含税", BOOL, "含税", "含税")
    msgs = [i.message for i in rec.issues if i.code == IssueCode.NEGATION_CONTEXT]
    assert msgs and all("疑问标签上下文" in m for m in msgs)


def test_multiple_occurrences_with_one_guarded_is_needs_review():
    rec = _run("负5度，5度", NUM, "5", "5")
    assert rec.status == Status.NEEDS_REVIEW
    assert IssueCode.VALUE_CROSSES_QUOTE_BOUNDARY in _codes(rec)


def test_question_occurrence_among_answers_blocks_grounding():
    rec = _run("是否含税，价格含税", BOOL, "含税", "含税")
    assert rec.status == Status.NEEDS_REVIEW
    assert IssueCode.NEGATION_CONTEXT in _codes(rec)


# ---------------------------------------------------------------------------
# 召回用例（修复前后都必须 grounded）
# ---------------------------------------------------------------------------

RECALL = [
    ("months_plain", "质保期为六个月", "六个月", "六个月", MONTHS, 6),
    ("months_teen", "质保期为十六个月", "十六个月", "十六个月", MONTHS, 16),
    ("bool_question_plus_answer_full_quote", "是否含税：含税", "是否含税：含税", "含税", BOOL, True),
    ("bool_answer_with_colon_context", "是否含税：含税", "：含税", "含税", BOOL, True),
    ("bool_not_taxed", "该价格不含税", "不含税", "不含税", BOOL, False),
    ("enum_nongov", "采购方式为非公开招标", "非公开招标", "非公开招标", ENUM, None),
    ("money_prefix_outside", "人民币120万元", "120万元", "120万元", MONEY, None),
    ("number_no_sign", "温度为5度", "5", "5", NUM, None),
]


@pytest.mark.parametrize("name,text,quote,value,spec,expect", RECALL, ids=[c[0] for c in RECALL])
def test_recall_case_stays_grounded(name, text, quote, value, spec, expect):
    rec = _run(text, spec, quote, value)
    assert rec.status == Status.GROUNDED, (name, [i.code for i in rec.issues])
    if expect is not None and spec.value_type == ValueType.NUMBER and spec.unit:
        assert rec.value.value == expect
    if expect is not None and spec.value_type == ValueType.BOOLEAN:
        assert rec.value.value is expect


def test_two_plain_answers_remain_grounded_and_flagged_ambiguous():
    rec = _run("价格含税，合同含税", BOOL, "含税", "含税")
    assert rec.status == Status.GROUNDED
    assert EvidenceFlag.AMBIGUOUS_OCCURRENCE in rec.evidence[0].flags
