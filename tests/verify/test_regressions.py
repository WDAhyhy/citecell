"""回归测试：值只在被截出的引文片段上校验（独立审查发现的严重缺陷）。

每个用例给出块文本、引文、值与键定义。修复前这些用例中的截断案例会被错误地判为 grounded；
控制组（引文完整覆盖值）必须仍然 grounded。
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
    Status,
    TableContext,
    ValueType,
    XlsxLocator,
)
from citecell.verify import SimpleMerger, Verifier

DOC_ID = "0123456789abcdef"


def _doc(blocks: list[Block], fmt: DocFormat = DocFormat.DOCX) -> ParsedDocument:
    info = DocumentInfo(
        doc_id=DOC_ID,
        sha256=DOC_ID + "0" * 48,
        format=fmt,
        size_bytes=1,
        parser_name="test",
        parser_version="0",
    )
    return ParsedDocument(info=info, blocks=[b.model_copy(update={"order": i}) for i, b in enumerate(blocks)])


def para(n: int, text: str) -> Block:
    return Block(
        doc_id=DOC_ID,
        block_id=f"para{n}",
        kind=BlockKind.PARAGRAPH,
        text=text,
        order=0,
        locator=DocxLocator(body_index=n - 1, paragraph_number=n),
    )


def cell(ref: str, text: str) -> Block:
    col, row = ord(ref[0]) - 64, int(ref[1:])
    return Block(
        doc_id=DOC_ID,
        block_id=f"s1-{ref}",
        kind=BlockKind.TABLE_CELL,
        text=text,
        order=0,
        table=TableContext(table_id="s1", row_index=row - 1, col_index=col - 1),
        locator=XlsxLocator(sheet_index=0, sheet_name="S1", cell=ref, row_number=row, column_number=col),
    )


def cand(key: str, block_id: str, quote: str, value: str | None) -> Candidate:
    return Candidate(
        doc_id=DOC_ID,
        key=key,
        value_text=value,
        evidence=[EvidenceClaim(block_id=block_id, quote=quote)],
        method=ExtractionMethod.LLM,
        producer_id="llm:test",
    )


def run(blocks, spec: KeySpec, block_id: str, quote: str, value: str | None, fmt=DocFormat.DOCX):
    doc = _doc(blocks, fmt)
    return Verifier().verify(cand(spec.key, block_id, quote, value), doc, spec)


# 每个截断案例：(名称, 块文本, 块ID, 引文, 值, 键定义, 块格式)
TRUNCATION_CASES = [
    ("money_tail_of_bigger_number", "合同金额为11,200元。", "para1", "1,200元", "1,200元",
     KeySpec(key="合同金额", value_type=ValueType.MONEY), DocFormat.DOCX),
    ("year_fragment", "签订于2012年", "para1", "12", "12",
     KeySpec(key="签订年份", value_type=ValueType.NUMBER), DocFormat.DOCX),
    ("percent_tail", "付款比例为15%", "para1", "5%", "5%",
     KeySpec(key="付款比例", value_type=ValueType.PERCENT), DocFormat.DOCX),
    ("wan_tail", "合同金额120万元", "para1", "20万元", "20万元",
     KeySpec(key="合同金额", value_type=ValueType.MONEY), DocFormat.DOCX),
    ("date_truncated_day", "签订日期2024-03-15", "para1", "2024-03-1", "2024-03-1",
     KeySpec(key="签订日期", value_type=ValueType.DATE), DocFormat.DOCX),
    ("unit_cut_before_unit", "工期12个月", "para1", "工期12", "12",
     KeySpec(key="工期", value_type=ValueType.NUMBER, unit="天"), DocFormat.DOCX),
    ("money_cut_before_wan", "¥100万元", "para1", "¥100", "¥100",
     KeySpec(key="合同金额", value_type=ValueType.MONEY), DocFormat.DOCX),
    ("negative_sign_cut", "增减为-5", "para1", "5", "5",
     KeySpec(key="增减", value_type=ValueType.NUMBER), DocFormat.DOCX),
    ("cn_money_tail", "壹佰贰拾万元", "para1", "贰拾万元", "贰拾万元",
     KeySpec(key="合同金额", value_type=ValueType.MONEY), DocFormat.DOCX),
    ("boolean_negated", "本报价不含税价格。", "para1", "含税价格", "true",
     KeySpec(key="是否含税", value_type=ValueType.BOOLEAN,
             enum_options=[EnumOption(value="true", aliases=["含税"]),
                           EnumOption(value="false", aliases=["不含税"])]), DocFormat.DOCX),
    ("enum_negated", "采用非公开招标方式。", "para1", "公开招标", "公开招标",
     KeySpec(key="采购方式", value_type=ValueType.ENUM,
             enum_options=[EnumOption(value="公开招标"), EnumOption(value="非公开招标")]), DocFormat.DOCX),
]


@pytest.mark.parametrize("name,text,block_id,quote,value,spec,fmt", TRUNCATION_CASES,
                         ids=[c[0] for c in TRUNCATION_CASES])
def test_truncated_quote_is_not_grounded(name, text, block_id, quote, value, spec, fmt):
    rec = run([para(1, text)], spec, block_id, quote, value, fmt)
    assert rec.status != Status.GROUNDED, (name, rec.status, [i.code for i in rec.issues])
    assert rec.status == Status.NEEDS_REVIEW


def test_truncated_case_reports_boundary_or_negation_code():
    spec = KeySpec(key="合同金额", value_type=ValueType.MONEY)
    rec = run([para(1, "合同金额为11,200元。")], spec, "para1", "1,200元", "1,200元")
    assert IssueCode.VALUE_CROSSES_QUOTE_BOUNDARY in {i.code for i in rec.issues}


def test_negation_code_reported_for_enum():
    spec = KeySpec(key="采购方式", value_type=ValueType.ENUM,
                   enum_options=[EnumOption(value="公开招标"), EnumOption(value="非公开招标")])
    rec = run([para(1, "采用非公开招标方式。")], spec, "para1", "公开招标", "公开招标")
    assert IssueCode.NEGATION_CONTEXT in {i.code for i in rec.issues}


def test_control_full_value_inside_quote_stays_grounded():
    spec = KeySpec(key="合同金额", value_type=ValueType.MONEY)
    rec = run([para(1, "合同金额为11,200元。")], spec, "para1", "11,200元", "11,200元")
    assert rec.status == Status.GROUNDED
    assert rec.value.amount == 11200


def test_control_quote_with_full_unit_grounded():
    spec = KeySpec(key="工期", value_type=ValueType.NUMBER, unit="月")
    rec = run([para(1, "工期12个月。")], spec, "para1", "工期12个月", "12个月")
    assert rec.status == Status.GROUNDED


def test_relocation_path_does_not_bypass_boundary_check():
    spec = KeySpec(key="合同金额", value_type=ValueType.MONEY)
    blocks = [para(1, "合同金额为11,200元。"), para(2, "其他条款。")]
    rec = run(blocks, spec, "para2", "1,200元", "1,200元")
    assert rec.status == Status.NEEDS_REVIEW


def test_render_noise_path_does_not_bypass_boundary_check():
    spec = KeySpec(key="单价", value_type=ValueType.NUMBER)
    rec = run([cell("B3", "125.00")], spec, "s1-B3", "[s1-B3 | 单价] 25.00", "25.00", DocFormat.XLSX)
    assert rec.status == Status.NEEDS_REVIEW


def test_value_only_in_quote_fragment_of_other_block_rejected_across_join():
    """跨块拼接时，值跨越两块的接缝不得被拆开后分别判定为等价。"""
    spec = KeySpec(key="合同金额", value_type=ValueType.MONEY)
    blocks = [para(1, "合同金额为1"), para(2, "200元整。")]
    rec = run(blocks, spec, "para1", "合同金额为1200元整", "1,200元")
    assert rec.status == Status.NEEDS_REVIEW


def test_boundary_issue_is_retryable():
    from citecell.verify import RETRYABLE_ISSUE_CODES

    assert IssueCode.VALUE_CROSSES_QUOTE_BOUNDARY in RETRYABLE_ISSUE_CODES


def test_merger_keeps_unit_conflict_visible():
    """12天(grounded) + 12个月(needs_review, UNIT_MISMATCH) 合并后必须仍可见冲突。"""
    spec = KeySpec(key="工期", value_type=ValueType.NUMBER, unit="天")
    doc = _doc([para(1, "工期12天。"), para(2, "工期12个月。")])
    good = Verifier().verify(cand("工期", "para1", "工期12天", "12天"), doc, spec)
    bad = Verifier().verify(cand("工期", "para2", "工期12个月", "12个月"), doc, spec)
    assert good.status == Status.GROUNDED
    assert bad.status == Status.NEEDS_REVIEW
    assert IssueCode.UNIT_MISMATCH in {i.code for i in bad.issues}
    out = SimpleMerger().resolve(DOC_ID, spec, [good, bad])
    assert out.status == Status.GROUNDED
    assert len(out.alternatives) >= 1
    assert any(i.severity.value == "warning" for i in out.issues)
