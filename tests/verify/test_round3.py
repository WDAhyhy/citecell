"""第三轮审查的回归测试（标签区、表头加固、归一化守卫与子句否定、数字边界、单位在引文之后、复杂度、合并与兜底）。

每组的反例（应 needs_review）与对照（应 grounded）成对出现。文档即时构造；表格文档经 openpyxl /
python-docx 生成并由真实解析器解析。
"""

from __future__ import annotations

import random
import time
from pathlib import Path

import docx
import openpyxl
import pytest

from citecell.models import (
    BBox,
    Block,
    BlockFlag,
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
    Status,
    ValueType,
)
from citecell.parsing import DocxParser, XlsxParser
from citecell.verify import SimpleMerger, Verifier

DOC_ID = "0123456789abcdef"


# ---------------------------------------------------------------------------
# 构造器
# ---------------------------------------------------------------------------


def _info(fmt: DocFormat) -> DocumentInfo:
    return DocumentInfo(doc_id=DOC_ID, sha256=DOC_ID + "0" * 48, format=fmt, size_bytes=1, parser_name="t", parser_version="0")


def para(n: int, text: str) -> Block:
    return Block(
        doc_id=DOC_ID,
        block_id=f"para{n}",
        kind=BlockKind.PARAGRAPH,
        text=text,
        order=0,
        locator=DocxLocator(body_index=n - 1, paragraph_number=n),
    )


def pdf(k: int, text: str, flags: list[BlockFlag] | None = None) -> Block:
    return Block(
        doc_id=DOC_ID,
        block_id=f"p1-b{k}",
        kind=BlockKind.PARAGRAPH,
        text=text,
        order=0,
        locator=PdfLocator(page_number=1, page_width=600, page_height=800, regions=[BBox(x0=0, y0=0, x1=500, y1=20)]),
        flags=flags or [],
    )


def run(blocks: list[Block], spec: KeySpec, block_id: str, quote: str, value: str, fmt: DocFormat = DocFormat.DOCX, verifier: Verifier | None = None):
    doc = ParsedDocument(info=_info(fmt), blocks=[b.model_copy(update={"order": i}) for i, b in enumerate(blocks)])
    c = Candidate(
        doc_id=DOC_ID,
        key=spec.key,
        value_text=value,
        evidence=[EvidenceClaim(block_id=block_id, quote=quote)],
        method=ExtractionMethod.LLM,
        producer_id="llm:test",
    )
    return (verifier or Verifier()).verify(c, doc, spec)


def codes(rec) -> set[IssueCode]:
    return {i.code for i in rec.issues}


BOOL = KeySpec(
    key="是否含税",
    value_type=ValueType.BOOLEAN,
    aliases=["含税情况", "税费口径"],
    enum_options=[EnumOption(value="true", aliases=["含税"]), EnumOption(value="false", aliases=["不含税"])],
)
ENUM = KeySpec(
    key="付款方式",
    value_type=ValueType.ENUM,
    enum_options=[EnumOption(value="电汇"), EnumOption(value="现金")],
)
ENUM_CASH = KeySpec(
    key="采购方式",
    value_type=ValueType.ENUM,
    enum_options=[EnumOption(value="公开招标"), EnumOption(value="现金")],
)
NUM = KeySpec(key="温度", value_type=ValueType.NUMBER)
DAYS = KeySpec(key="工期", value_type=ValueType.NUMBER, unit="天")
MONEY = KeySpec(key="合同金额", value_type=ValueType.MONEY)
PCT = KeySpec(key="付款比例", value_type=ValueType.PERCENT)
TEXT = KeySpec(key="负责人", value_type=ValueType.TEXT)


# ---------------------------------------------------------------------------
# 1. 标签区域排除
# ---------------------------------------------------------------------------

LABEL_CASES = [
    ("alias_contains_value", "含税情况：不含税", "含税情况：不含税", "含税"),
    ("label_colon_answer_unlisted", "含税：否", "含税：否", "含税"),
    ("paren_option_list", "含税（含税/不含税）：不含税", "含税（含税/不含税）：不含税", "含税"),
]


@pytest.mark.parametrize("name,text,quote,value", LABEL_CASES, ids=[c[0] for c in LABEL_CASES])
def test_label_region_is_not_value(name, text, quote, value):
    rec = run([para(1, text)], BOOL, "para1", quote, value)
    assert rec.status == Status.NEEDS_REVIEW, (name, [i.code for i in rec.issues])


def test_enum_paren_option_list_not_value():
    rec = run([para(1, "付款方式（电汇/现金）：现金")], ENUM, "para1", "付款方式（电汇/现金）：现金", "电汇")
    assert rec.status == Status.NEEDS_REVIEW


def test_enum_answer_after_paren_list_grounded():
    rec = run([para(1, "付款方式（电汇/现金）：现金")], ENUM, "para1", "付款方式（电汇/现金）：现金", "现金")
    assert rec.status == Status.GROUNDED


def test_checkbox_unchecked_option_not_value():
    rec = run([para(1, "□含税 ☑不含税")], BOOL, "para1", "□含税 ☑不含税", "含税")
    assert rec.status == Status.NEEDS_REVIEW


def test_checkbox_checked_option_grounded():
    rec = run([para(1, "□含税 ☑不含税")], BOOL, "para1", "□含税 ☑不含税", "不含税")
    assert rec.status == Status.GROUNDED
    assert rec.value.value is False


def test_control_no_colon_no_label_still_grounded():
    rec = run([para(1, "税费口径为含税")], BOOL, "para1", "税费口径为含税", "含税")
    assert rec.status == Status.GROUNDED


def test_numeric_key_not_affected_by_label_rule():
    rec = run([para(1, "温度：5度")], NUM, "para1", "温度：5度", "5")
    assert rec.status == Status.GROUNDED


# ---------------------------------------------------------------------------
# 2. 表头单位加固（xlsx / docx）
# ---------------------------------------------------------------------------


def _xlsx(tmp: Path, cells: dict[str, object], formats: dict[str, str] | None = None, hidden_rows: tuple[int, ...] = ()):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "S1"
    for ref, v in cells.items():
        ws[ref] = v
    for ref, fmt in (formats or {}).items():
        ws[ref].number_format = fmt
    for r in hidden_rows:
        ws.row_dimensions[r].hidden = True
    path = tmp / "t.xlsx"
    wb.save(path)
    return XlsxParser().parse(path)


def run_doc(doc: ParsedDocument, spec: KeySpec, block_id: str, quote: str, value: str, verifier: Verifier | None = None):
    """对解析器产出的文档直接校验（doc_id 与解析结果一致）。"""
    c = Candidate(
        doc_id=doc.info.doc_id,
        key=spec.key,
        value_text=value,
        evidence=[EvidenceClaim(block_id=block_id, quote=quote)],
        method=ExtractionMethod.LLM,
        producer_id="llm:test",
    )
    return (verifier or Verifier()).verify(c, doc, spec)


def _xlsx_verify(doc, block_id: str, value: str, spec: KeySpec = MONEY, verifier: Verifier | None = None):
    text = next(b.text for b in doc.blocks if b.block_id == block_id)
    return run_doc(doc, spec, block_id, text, value, verifier)


@pytest.mark.parametrize("header", ["金额（美元）", "金额（港元）", "金额（千美元）", "金额（欧元）"])
def test_foreign_currency_header_not_a_unit(tmp_path, header):
    doc = _xlsx(tmp_path, {"A1": header, "A2": "1000"})
    rec = _xlsx_verify(doc, "s1-A2", "1000")
    assert rec.status == Status.NEEDS_REVIEW
    assert IssueCode.VALUE_UNPARSEABLE in codes(rec)


def test_number_format_literal_known_unit_used(tmp_path):
    doc = _xlsx(tmp_path, {"A1": "金额", "A2": 120}, formats={"A2": '0"万元"'})
    rec = _xlsx_verify(doc, "s1-A2", "120")
    assert rec.status == Status.GROUNDED
    assert rec.value.amount == 1200000


def test_number_format_other_literal_not_used(tmp_path):
    doc = _xlsx(tmp_path, {"A1": "金额", "A2": 120}, formats={"A2": '0"港元"'})
    rec = _xlsx_verify(doc, "s1-A2", "120")
    assert rec.status == Status.NEEDS_REVIEW


def test_number_format_scaling_comma_not_used(tmp_path):
    doc = _xlsx(tmp_path, {"A1": "金额", "A2": 120}, formats={"A2": "¥0.0,,"})
    rec = _xlsx_verify(doc, "s1-A2", "120")
    assert rec.status == Status.NEEDS_REVIEW


def test_note_row_is_not_a_unit_header(tmp_path):
    doc = _xlsx(tmp_path, {"A1": "万元以上须审批", "A2": "1200"})
    rec = _xlsx_verify(doc, "s1-A2", "1200")
    assert rec.status == Status.NEEDS_REVIEW
    assert rec.value is None


def test_long_sentence_header_is_not_a_header(tmp_path):
    long = "本表所有金额单位以人民币元计算并须经审批后方可使用的说明"
    doc = _xlsx(tmp_path, {"A1": long, "A2": "130"})
    rec = _xlsx_verify(doc, "s1-A2", "130")
    assert rec.status == Status.NEEDS_REVIEW


def test_blank_row_stops_header_scan_across_tables(tmp_path):
    doc = _xlsx(tmp_path, {"A1": "金额（万元）", "A2": "120", "A4": "130"})
    rec = _xlsx_verify(doc, "s1-A4", "130")
    assert rec.status == Status.NEEDS_REVIEW
    assert IssueCode.VALUE_UNPARSEABLE in codes(rec)


def test_control_header_in_same_table_still_grounded(tmp_path):
    doc = _xlsx(tmp_path, {"A1": "金额（万元）", "A2": "120", "A3": "130"})
    rec = _xlsx_verify(doc, "s1-A3", "130")
    assert rec.status == Status.GROUNDED
    assert rec.value.amount == 1300000


def test_hidden_header_ignored_when_excluded(tmp_path):
    doc = _xlsx(tmp_path, {"A1": "金额（万元）", "A2": "130"}, hidden_rows=(1,))
    rec = run_doc(doc, MONEY, "s1-A2", "130", "130", Verifier(include_hidden_cells=False))
    assert rec.status == Status.NEEDS_REVIEW


def test_docx_header_not_in_first_row(tmp_path):
    d = docx.Document()
    t = d.add_table(rows=3, cols=1)
    t.cell(0, 0).text = "说明"
    t.cell(1, 0).text = "金额（万元）"
    t.cell(2, 0).text = "130"
    path = tmp_path / "t.docx"
    d.save(path)
    doc = DocxParser().parse(path)
    bid = next(b.block_id for b in doc.blocks if b.text == "130")
    rec = run_doc(doc, MONEY, bid, "130", "130")
    assert rec.status == Status.GROUNDED
    assert rec.value.amount == 1300000


def test_conflicting_headers_in_same_column_stay_unparseable(tmp_path):
    doc = _xlsx(tmp_path, {"A1": "金额（元）", "A2": "（万元）", "A3": "130"})
    rec = _xlsx_verify(doc, "s1-A3", "130")
    assert rec.status == Status.NEEDS_REVIEW


# ---------------------------------------------------------------------------
# 3. 归一化上下文守卫与子句级否定
# ---------------------------------------------------------------------------

ZW = "​"

NORM_LEAKS = [
    ("zw_negation_bu", f"这个不{ZW}{ZW}含税价格", "含税价格", "含税"),
    ("zw_question", f"是{ZW}{ZW}否含税", "含税", "含税"),
    ("zw_minus_sign", f"温度为−{ZW}5度", "5", "5"),
    ("zw_negative_cn", f"温度为负{ZW}五度", "五", "五"),
    ("zw_ordinal", f"第{ZW}六个月支付尾款", "六个月", "六个月"),
    ("clause_far_negation_before", "不属于含税价格", "含税价格", "含税"),
    ("clause_far_negation_after", "含税的说法不成立", "含税", "含税"),
    ("clause_question_before_colon_answer", "是否属于含税价：否", "含税价", "含税"),
]


@pytest.mark.parametrize("name,text,quote,value", NORM_LEAKS, ids=[c[0] for c in NORM_LEAKS])
def test_normalized_context_and_clause_negation_leaks(name, text, quote, value):
    spec = NUM if name in ("zw_minus_sign", "zw_negative_cn", "zw_ordinal") else BOOL
    if name == "zw_ordinal":
        spec = KeySpec(key="质保期", value_type=ValueType.NUMBER, unit="月")
    rec = run([para(1, text)], spec, "para1", quote, value)
    assert rec.status == Status.NEEDS_REVIEW, (name, [i.code for i in rec.issues])


def test_clause_negation_for_enum():
    rec = run([para(1, "不接受现金")], ENUM_CASH, "para1", "现金", "现金")
    assert rec.status == Status.NEEDS_REVIEW


CTRL_NEG = [
    ("negated_inside_alias_ok", "该价格不含税", "不含税", "不含税", BOOL, False),
    ("non_negated_enum_ok", "采购方式为非公开招标", "非公开招标", "非公开招标", KeySpec(key="采购方式", value_type=ValueType.ENUM, enum_options=[EnumOption(value="公开招标"), EnumOption(value="非公开招标")]), None),
    ("question_then_colon_answer_ok", "是否含税：含税", "是否含税：含税", "含税", BOOL, True),
]


@pytest.mark.parametrize("name,text,quote,value,spec,expect", CTRL_NEG, ids=[c[0] for c in CTRL_NEG])
def test_recall_controls_stay_grounded(name, text, quote, value, spec, expect):
    rec = run([para(1, text)], spec, "para1", quote, value)
    assert rec.status == Status.GROUNDED, (name, [i.code for i in rec.issues])
    if expect is not None:
        assert rec.value.value is expect


# ---------------------------------------------------------------------------
# 4. 数字边界（跨空白、相邻字符、跨块、会计负数）
# ---------------------------------------------------------------------------


def test_adjacent_dot_digit_cut():
    rec = run([para(1, "版本12.5.3")], NUM, "para1", "12.5", "12.5")
    assert rec.status == Status.NEEDS_REVIEW
    assert IssueCode.VALUE_CROSSES_QUOTE_BOUNDARY in codes(rec)


def test_space_separated_digits_cut():
    rec = run([para(1, "共1 200元")], MONEY, "para1", "200元", "200元")
    assert rec.status == Status.NEEDS_REVIEW


def test_pdf_line_join_comma_space_cut():
    rec = run([pdf(1, "合同金额为11, 200元整。")], MONEY, "p1-b1", "200元整", "200元整", DocFormat.PDF)
    assert rec.status == Status.NEEDS_REVIEW


def test_cross_block_digits_cut_at_block_start():
    blocks = [pdf(1, "合同金额为11,"), pdf(2, "200元整。")]
    rec = run(blocks, MONEY, "p1-b2", "200元整", "200元整", DocFormat.PDF)
    assert rec.status == Status.NEEDS_REVIEW


@pytest.mark.parametrize("text,quote", [("亏损(5,000)元", "5,000"), ("亏损（5,000）元", "5,000")])
def test_accounting_negative_parenthesized_not_grounded(text, quote):
    rec = run([para(1, text)], NUM, "para1", quote, "5000")
    assert rec.status == Status.NEEDS_REVIEW


def test_control_digits_followed_by_unit_grounded():
    rec = run([para(1, "合同金额为1,200元整。")], MONEY, "para1", "1,200元", "1,200元")
    assert rec.status == Status.GROUNDED


# ---------------------------------------------------------------------------
# 5. 单位在引文之后（values.units.match_unit）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tail", ["（月）", " months", " 个月", "　个月", "-month", " 周"])
def test_unit_after_quote_mismatch(tail):
    text = "工期12" + tail
    rec = run([para(1, text)], DAYS, "para1", "工期12", "12")
    assert rec.status == Status.NEEDS_REVIEW, (tail, [i.code for i in rec.issues])
    assert IssueCode.UNIT_MISMATCH in codes(rec) or IssueCode.VALUE_CROSSES_QUOTE_BOUNDARY in codes(rec)


def test_unit_after_quote_matching_unit_grounded():
    rec = run([para(1, "工期12天")], DAYS, "para1", "工期12天", "12天")
    assert rec.status == Status.GROUNDED
    assert rec.value.unit == "天"


def test_truncated_quote_cutting_matching_unit_stays_needs_review():
    """引文截在与键单位相同的单位之前：沿用既有截断判定（数字与单位边界），不得 grounded。"""
    rec = run([para(1, "工期12天")], DAYS, "para1", "工期12", "12")
    assert rec.status == Status.NEEDS_REVIEW


# ---------------------------------------------------------------------------
# 6. 复杂度与资源上限
# ---------------------------------------------------------------------------


def test_many_occurrences_capped_and_fast():
    text = "含税" * 40
    t0 = time.perf_counter()
    rec = run([para(1, text)], BOOL, "para1", "含税", "含税")
    elapsed = time.perf_counter() - t0
    assert rec.status == Status.NEEDS_REVIEW
    assert any("出现次数过多" in i.message for i in rec.issues)
    assert elapsed < 2.0


def test_long_block_8000_chars_bounded_time():
    text = "的" * 8000
    t0 = time.perf_counter()
    rec = run([para(1, text)], TEXT, "para1", "的", "的")
    elapsed = time.perf_counter() - t0
    assert rec.status == Status.NEEDS_REVIEW
    assert elapsed < 5.0


def test_block_over_20000_chars_needs_review_without_exception():
    text = "合同金额为1,200元。" + "的" * 20000
    rec = run([para(1, text)], MONEY, "para1", "1,200元", "1,200元")
    assert rec.status == Status.NEEDS_REVIEW
    assert any("块过长" in i.message for i in rec.issues)


# ---------------------------------------------------------------------------
# 7. 合并器与整引文兜底
# ---------------------------------------------------------------------------


def test_merge_grounded_with_unit_conflict_warns_conflicting():
    doc = ParsedDocument(
        info=_info(DocFormat.DOCX),
        blocks=[para(1, "工期12天。").model_copy(update={"order": 0}), para(2, "工期12个月。").model_copy(update={"order": 1})],
    )
    v = Verifier()
    good = v.verify(
        Candidate(doc_id=DOC_ID, key="工期", value_text="12天", evidence=[EvidenceClaim(block_id="para1", quote="工期12天")], method=ExtractionMethod.LLM, producer_id="a"),
        doc, DAYS,
    )
    bad = v.verify(
        Candidate(doc_id=DOC_ID, key="工期", value_text="12个月", evidence=[EvidenceClaim(block_id="para2", quote="工期12个月")], method=ExtractionMethod.LLM, producer_id="b"),
        doc, DAYS,
    )
    out = SimpleMerger().resolve(DOC_ID, DAYS, [good, bad])
    assert out.status == Status.GROUNDED
    assert any(i.code == IssueCode.CONFLICTING_CANDIDATES and i.severity.value == "warning" for i in out.issues)
    assert len(out.alternatives) >= 1


def test_fallback_requires_quote_text_equal_value_text():
    """引文文本与值文本不同（六个月 vs 6个月）时兜底不生效；“六个月”作为等价提及仍可 grounded。"""
    rec = run([para(1, "质保期为六个月")], KeySpec(key="质保期", value_type=ValueType.NUMBER, unit="月"), "para1", "六个月", "6个月")
    assert rec.status == Status.GROUNDED



def test_fallback_control_equal_text_grounded():
    rec = run([para(1, "质保期为六个月")], KeySpec(key="质保期", value_type=ValueType.NUMBER, unit="月"), "para1", "六个月", "六个月")
    assert rec.status == Status.GROUNDED


# ---------------------------------------------------------------------------
# 8. 随机模糊测试：无数字含义的词不得 grounded（random.Random(0)）
# ---------------------------------------------------------------------------

# (词, 是否有数字含义)。有数字含义者只用于构造样本，不参与断言。
_VOCAB: list[tuple[str, bool]] = [
    ("一般", False), ("五金", False), ("千万不要", False), ("一元化", False), ("一直", False),
    ("万一", False), ("不一定", False), ("一起", False), ("十分", False), ("半天", False),
    ("三心二意", False), ("千方百计", False), ("二手", False), ("百般", False), ("万能", False),
    ("一个人", False), ("五湖四海", False), ("千山万水", False), ("一切", False), ("二话", False),
    ("一百", True), ("两千", True), ("十二个月", True), ("三万", True), ("五十", True), ("12", True), ("100元", True),
]
_FUZZ_KNOWN_T2: set[str] = set()  # 若复现的词归属 T2，在此登记并在回复中说明


def test_fuzz_non_numeric_words_never_grounded():
    """对每个无数字含义的词，取其中所有能被 parse_value 解析为 NUMBER 的子串作为值，引文即该词：均不得 grounded。"""
    from citecell.values import ValueNormalizerV1

    vn = ValueNormalizerV1()
    spec = KeySpec(key="数量", value_type=ValueType.NUMBER)
    rng = random.Random(0)
    non_numeric = [w for w, flag in _VOCAB if not flag]
    grounded: list[tuple[str, str]] = []
    for _ in range(2000):
        w = rng.choice(non_numeric)
        if rng.random() < 0.5:
            w = w + rng.choice(non_numeric)
        if w in _FUZZ_KNOWN_T2:
            continue
        values = []
        for i in range(len(w)):
            for j in range(i + 1, len(w) + 1):
                try:
                    vn.parse_value(w[i:j], spec)
                    values.append(w[i:j])
                except ValueError:
                    pass
        for v in sorted(set(values)):
            rec = run([para(1, f"数量为{w}。")], spec, "para1", w, v)
            if rec.status == Status.GROUNDED:
                grounded.append((w, v))
    assert grounded == [], f"无数字含义的词被判 grounded：{sorted(set(grounded))[:10]}"


def test_hidden_header_included_keeps_hidden_source_warning(tmp_path):
    doc = _xlsx(tmp_path, {"A1": "金额（万元）", "A2": "130"}, hidden_rows=(1,))
    rec = run_doc(doc, MONEY, "s1-A2", "130", "130", Verifier(include_hidden_cells=True))
    assert rec.status == Status.GROUNDED
    assert any(i.code == IssueCode.HIDDEN_SOURCE and i.severity.value == "warning" for i in rec.issues)
