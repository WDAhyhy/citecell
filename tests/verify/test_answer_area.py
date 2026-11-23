"""答案区规则（第四轮）：问句“是否…”之后的第一个分隔符（冒号、为、是）开始答案区。

正例（应 grounded）：带冒号的整句、无冒号的“标签为答案”、答案含否定位的修订写法。
反例（必须仍然 needs_review）：裸引文（其中一处在标签里）、只有标签、答案不在答案区、标签区写法、零宽插入。
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
    ExtractionMethod,
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
    aliases=["含税情况", "税费口径"],
    enum_options=[EnumOption(value="true", aliases=["含税"]), EnumOption(value="false", aliases=["不含税"])],
)
ZW = "​"


def _run(text: str, quote: str, value: str):
    info = DocumentInfo(doc_id=DOC_ID, sha256=DOC_ID + "0" * 48, format=DocFormat.DOCX, size_bytes=1, parser_name="t", parser_version="0")
    b = Block(doc_id=DOC_ID, block_id="para1", kind=BlockKind.PARAGRAPH, text=text, order=0, locator=DocxLocator(body_index=0, paragraph_number=1))
    doc = ParsedDocument(info=info, blocks=[b])
    c = Candidate(
        doc_id=DOC_ID,
        key=BOOL.key,
        value_text=value,
        evidence=[EvidenceClaim(block_id="para1", quote=quote)],
        method=ExtractionMethod.LLM,
        producer_id="llm:test",
    )
    return Verifier().verify(c, doc, BOOL)


# ---------------------------------------------------------------------------
# 正例
# ---------------------------------------------------------------------------

POSITIVE = [
    ("colon_whole_sentence", "是否含税：含税", "是否含税：含税", "含税", True),
    ("oracle_label_is_answer", "是否含税为含税（初报价：不含税；修订前：不含税）。", "为含税", "含税", True),
    ("oracle_answer_false_with_revisions", "是否含税为不含税（初报价：含税；修订前：含税）。", "为不含税", "不含税", False),
    ("oracle_answer_false_plain", "是否含税为不含税", "为不含税", "不含税", False),
    ("colon_answer_false", "是否含税：不含税", "是否含税：不含税", "不含税", False),
]


@pytest.mark.parametrize("name,text,quote,value,expect", POSITIVE, ids=[c[0] for c in POSITIVE])
def test_answer_area_positive_grounded(name, text, quote, value, expect):
    rec = _run(text, quote, value)
    assert rec.status == Status.GROUNDED, (name, [i.code.value for i in rec.issues])
    assert rec.value.value is expect


# ---------------------------------------------------------------------------
# 反例（必须保持拒绝）
# ---------------------------------------------------------------------------

NEGATIVE = [
    ("bare_quote_label_occurrence", "是否含税：含税", "含税", "含税"),
    ("label_only", "是否含税", "是否含税", "含税"),
    ("answer_not_in_answer_area", "是否属于含税价：否", "含税价", "含税"),
    ("alias_label_answer_other", "含税情况：不含税", "含税情况：不含税", "含税"),
    ("label_colon_answer_unlisted", "含税：否", "含税：否", "含税"),
    ("paren_option_list_label", "含税（含税/不含税）：不含税", "含税（含税/不含税）：不含税", "含税"),
    ("zero_width_question", f"是{ZW}{ZW}否含税", "含税", "含税"),
    ("zero_width_label_only", f"是否{ZW}{ZW}含税", "含税", "含税"),
]


@pytest.mark.parametrize("name,text,quote,value", NEGATIVE, ids=[c[0] for c in NEGATIVE])
def test_label_contexts_stay_rejected(name, text, quote, value):
    rec = _run(text, quote, value)
    assert rec.status == Status.NEEDS_REVIEW, (name, [i.code.value for i in rec.issues])
