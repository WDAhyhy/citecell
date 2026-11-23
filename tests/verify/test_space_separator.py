"""空白分隔规则（第五轮）：键名或别名的一次出现之后紧跟空白（含 PDF 行连接空格、全角空格），
则这段空白之后开始答案区。空白不是普遍分隔符：不在键名/别名之后的空白不触发。"""

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


POSITIVE = [
    ("key_space_answer_false", "是否含税 不含税（初报价：含税）", "是否含税 不含税", "不含税", False),
    ("key_space_answer_true", "是否含税 含税", "是否含税 含税", "含税", True),
    ("alias_space_answer_true", "含税情况 含税", "含税情况 含税", "含税", True),
    ("fullwidth_space_answer_false", "是否含税　不含税", "是否含税　不含税", "不含税", False),
]


@pytest.mark.parametrize("name,text,quote,value,expect", POSITIVE, ids=[c[0] for c in POSITIVE])
def test_space_after_label_opens_answer_area(name, text, quote, value, expect):
    rec = _run(text, quote, value)
    assert rec.status == Status.GROUNDED, (name, [i.code.value for i in rec.issues])
    assert rec.value.value is expect


NEGATIVE = [
    ("bare_quote_with_label_occurrence", "是否含税 含税", "含税", "含税"),
    ("space_after_non_label_phrase", "是否属于含税价 含税", "是否属于含税价 含税", "含税"),
    ("alias_space_answer_is_negated_value", "含税情况 不含税", "含税情况 不含税", "含税"),
    ("zero_width_inside_question", f"是{ZW}否含税 含税", f"是{ZW}否含税 含税", "含税"),
    ("key_space_answer_is_other_value", "是否含税 不含税", "是否含税 不含税", "含税"),
]


@pytest.mark.parametrize("name,text,quote,value", NEGATIVE, ids=[c[0] for c in NEGATIVE])
def test_space_rule_does_not_relax_rejections(name, text, quote, value):
    rec = _run(text, quote, value)
    assert rec.status == Status.NEEDS_REVIEW, (name, [i.code.value for i in rec.issues])
