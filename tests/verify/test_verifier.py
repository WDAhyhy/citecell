"""T7 验收测试：Verifier + Localizer（ARCH §6.2 / §6.4）。

全部输入都是测试运行时手工构造的 ParsedDocument（不依赖解析器、不读取真实文档）。
每个测试对应 docs/TASKS.md T7 验收的一条（编号见测试名前缀 ``acc_NN``）。
"""

from __future__ import annotations

from citecell.models import (
    BBox,
    Block,
    BlockFlag,
    BlockKind,
    Candidate,
    DerivationClaim,
    DerivationOp,
    DerivationOperand,
    DocFormat,
    DocumentInfo,
    DocxLocator,
    EnumOption,
    EvidenceClaim,
    EvidenceFlag,
    ExtractionMethod,
    IssueCode,
    KeySpec,
    MatchLevel,
    MoneyValue,
    NumberValue,
    ParsedDocument,
    PdfLocator,
    Severity,
    Status,
    TableContext,
    TextFragment,
    ValueType,
    XlsxLocator,
)
from citecell.textnorm import Matcher, Normalizer
from citecell.values import ValueNormalizerV1
from citecell.verify import RETRYABLE_ISSUE_CODES, LocalizerV1, Verifier, strip_render_noise

DOC_ID = "0123456789abcdef"
_VN = ValueNormalizerV1()


# ---------------------------------------------------------------------------
# 构造器
# ---------------------------------------------------------------------------


def _doc(blocks: list[Block], fmt: DocFormat = DocFormat.DOCX) -> ParsedDocument:
    info = DocumentInfo(
        doc_id=DOC_ID,
        sha256=DOC_ID + "0" * 48,
        format=fmt,
        size_bytes=1,
        parser_name="test",
        parser_version="0",
    )
    fixed = [b.model_copy(update={"order": i}) for i, b in enumerate(blocks)]
    return ParsedDocument(info=info, blocks=fixed)


def para(n: int, text: str, flags: list[BlockFlag] | None = None) -> Block:
    return Block(
        doc_id=DOC_ID,
        block_id=f"para{n}",
        kind=BlockKind.PARAGRAPH,
        text=text,
        order=0,
        locator=DocxLocator(body_index=n - 1, paragraph_number=n),
        flags=flags or [],
    )


def cell(ref: str, text: str, hidden: bool = False) -> Block:
    """XLSX 单元格块，ref 如 'B3'（单字母列）。"""
    col = ord(ref[0]) - 64
    row = int(ref[1:])
    return Block(
        doc_id=DOC_ID,
        block_id=f"s1-{ref}",
        kind=BlockKind.TABLE_CELL,
        text=text,
        order=0,
        table=TableContext(table_id="s1", row_index=row - 1, col_index=col - 1),
        locator=XlsxLocator(
            sheet_index=0,
            sheet_name="S1",
            cell=ref,
            row_number=row,
            column_number=col,
            hidden=hidden,
        ),
        flags=[BlockFlag.HIDDEN] if hidden else [],
    )


def pdf_block(k: int, text: str, fragments: list[TextFragment] | None = None, page: int = 1) -> Block:
    return Block(
        doc_id=DOC_ID,
        block_id=f"p{page}-b{k}",
        kind=BlockKind.PARAGRAPH,
        text=text,
        order=0,
        locator=PdfLocator(
            page_number=page,
            page_width=600,
            page_height=800,
            regions=[BBox(x0=0, y0=90, x1=500, y1=140)],
        ),
        fragments=fragments or [],
    )


def char_fragments(text: str, line_break: int, page: int = 1) -> list[TextFragment]:
    """逐字片段：前 line_break 个字符在第 0 行，其余在第 1 行。每字宽 10pt。"""
    out: list[TextFragment] = []
    for i, _ in enumerate(text):
        line = 0 if i < line_break else 1
        out.append(
            TextFragment(
                start=i,
                end=i + 1,
                page_number=page,
                line=line,
                bbox=BBox(x0=i * 10, y0=100 + line * 20, x1=i * 10 + 10, y1=112 + line * 20),
            )
        )
    return out


def spec(key: str, vt: ValueType = ValueType.TEXT, **kw) -> KeySpec:
    return KeySpec(key=key, value_type=vt, **kw)



def cand(
    key: str,
    claims: list[tuple[str, str]],
    value: str | None,
    *,
    derivation: DerivationClaim | None = None,
) -> Candidate:
    return Candidate(
        doc_id=DOC_ID,
        key=key,
        value_text=value,
        evidence=[EvidenceClaim(block_id=b, quote=q) for b, q in claims],
        derivation=derivation,
        method=ExtractionMethod.LLM,
        producer_id="llm:test",
    )


def money(key: str = "合同金额") -> KeySpec:
    return spec(key, ValueType.MONEY)


def _check_grounded_invariants(rec, doc: ParsedDocument, s: KeySpec) -> None:
    """ARCH §6.2 的 grounded 性质（测试 17 的属性）：原文子串归一化包含引文；值区间解析后等价。"""
    norm = Normalizer()
    blocks = doc.block_map()
    for e in rec.evidence:
        assert e.is_located
        text = blocks[e.block_id].text
        assert e.char_start is not None and e.char_end is not None
        nq = norm.normalize(e.quote).text
        assert nq in norm.normalize(text[e.char_start : e.char_end]).text
        if e.value_char_start is not None:
            assert e.value_char_end is not None
            vspan = text[e.value_char_start : e.value_char_end]
            assert _VN.equivalent(_VN.parse_value(vspan, s), rec.value, s)


# ---------------------------------------------------------------------------
# acc_01：逐字引文 + money；PDF 跨两行引文 → 2 个框，精度 char
# ---------------------------------------------------------------------------


def test_acc_01_exact_money_grounded_with_value_span():
    s = money()
    doc = _doc([para(1, "合同金额为1,200元整，签约后支付。")])
    rec = Verifier().verify(cand(s.key, [("para1", "合同金额为1,200元整")], "1,200元"), doc, s)
    assert rec.status == Status.GROUNDED
    assert rec.value == MoneyValue(amount=1200)
    ev = rec.evidence[0]
    assert ev.match_level == MatchLevel.EXACT
    assert ev.flags.count(EvidenceFlag.VALUE_BEARING) == 1
    text = doc.block_map()["para1"].text
    # 金额提及包含尾随的“整”（find_money_spans 的整段提及），解析结果仍为 1200 元
    assert text[ev.value_char_start : ev.value_char_end] == "1,200元整"
    _check_grounded_invariants(rec, doc, s)


def test_acc_01_pdf_quote_across_two_lines_gives_two_regions_char_precision():
    s = money()
    text = "合同金额为1,200元，付款方式为分期付款"
    doc = _doc([pdf_block(1, text, char_fragments(text, line_break=8))], fmt=DocFormat.PDF)
    # 引文 "为1,200元，付款方式" 从第 0 行跨到第 1 行（第 8 个字符处换行）
    rec = Verifier().verify(cand(s.key, [("p1-b1", "为1,200元，付款方式")], "1,200元"), doc, s)
    assert rec.status == Status.GROUNDED
    ev = rec.evidence[0]
    assert ev.precision.value == "char"
    assert len(ev.locator.regions) == 2
    _check_grounded_invariants(rec, doc, s)


# ---------------------------------------------------------------------------
# acc_02：空白与全半角差异 → NORMALIZED → grounded
# ---------------------------------------------------------------------------


def test_acc_02_whitespace_and_fullwidth_gives_normalized_grounded():
    s = money()
    doc = _doc([para(1, "合同 金额为１,２００元。")])
    rec = Verifier().verify(cand(s.key, [("para1", "合同金额为1,200元")], "1,200元"), doc, s)
    assert rec.status == Status.GROUNDED
    assert rec.evidence[0].match_level == MatchLevel.NORMALIZED
    _check_grounded_invariants(rec, doc, s)


# ---------------------------------------------------------------------------
# acc_03：改写过的引文 → needs_review，QUOTE_NOT_IN_BLOCK 或 QUOTE_FUZZY_ONLY
# ---------------------------------------------------------------------------


def test_acc_03_paraphrased_quote_needs_review_with_retryable_code():
    s = money()
    doc = _doc([para(1, "合同金额为一百二十万元，签约后支付。")])
    rec = Verifier().verify(cand(s.key, [("para1", "合同总价大约是一百二十万元左右")], "120万元"), doc, s)
    assert rec.status == Status.NEEDS_REVIEW
    codes = {i.code for i in rec.issues}
    assert codes & {IssueCode.QUOTE_NOT_IN_BLOCK, IssueCode.QUOTE_FUZZY_ONLY}
    assert codes & RETRYABLE_ISSUE_CODES
    assert rec.evidence == []


# ---------------------------------------------------------------------------
# acc_04：block_id 写错但引文唯一 → grounded + RELOCATED，claimed_block_id 为原值
# ---------------------------------------------------------------------------


def test_acc_04_wrong_block_id_relocated_when_unique():
    s = money()
    doc = _doc([para(1, "合同金额为1,200元。"), para(2, "其他条款：付款周期为30天。")])
    rec = Verifier().verify(cand(s.key, [("para2", "合同金额为1,200元")], "1,200元"), doc, s)
    assert rec.status == Status.GROUNDED
    ev = rec.evidence[0]
    assert ev.block_id == "para1"
    assert ev.claimed_block_id == "para2"
    assert EvidenceFlag.RELOCATED in ev.flags


# ---------------------------------------------------------------------------
# acc_05：block_id 写错且引文多处出现 → needs_review，QUOTE_AMBIGUOUS
# ---------------------------------------------------------------------------


def test_acc_05_wrong_block_id_ambiguous_quote_needs_review():
    s = money()
    doc = _doc([para(1, "合同金额为1,200元。"), para(2, "合同金额为1,200元。"), para(3, "无关内容")])
    rec = Verifier().verify(cand(s.key, [("para3", "合同金额为1,200元")], "1,200元"), doc, s)
    assert rec.status == Status.NEEDS_REVIEW
    assert IssueCode.QUOTE_AMBIGUOUS in {i.code for i in rec.issues}


# ---------------------------------------------------------------------------
# acc_06：跨相邻块引文 → 拆成 2 条证据，CROSS_BLOCK_SPLIT，grounded
# ---------------------------------------------------------------------------


def test_acc_06_quote_spanning_adjacent_blocks_is_split():
    s = money()
    doc = _doc([para(3, "合同总额为人民币120万元"), para(4, "整，签约后支付。")])
    rec = Verifier().verify(cand(s.key, [("para3", "合同总额为人民币120万元整")], "120万元"), doc, s)
    assert rec.status == Status.GROUNDED
    assert len(rec.evidence) == 2
    assert [e.block_id for e in rec.evidence] == ["para3", "para4"]
    assert all(EvidenceFlag.CROSS_BLOCK_SPLIT in e.flags for e in rec.evidence)
    _check_grounded_invariants(rec, doc, s)


# ---------------------------------------------------------------------------
# acc_07：引文不含值 → needs_review，VALUE_NOT_IN_QUOTE
# ---------------------------------------------------------------------------


def test_acc_07_value_not_in_quote_needs_review():
    s = spec("合同金额", ValueType.NUMBER)
    doc = _doc([para(1, "合同金额为1,200元。")])
    rec = Verifier().verify(cand(s.key, [("para1", "合同金额")], "1200"), doc, s)
    assert rec.status == Status.NEEDS_REVIEW
    assert IssueCode.VALUE_NOT_IN_QUOTE in {i.code for i in rec.issues}


# ---------------------------------------------------------------------------
# acc_08：120万元 与 ¥1,200,000.00 等价 → grounded
# ---------------------------------------------------------------------------


def test_acc_08_wan_yuan_equals_full_amount():
    s = money()
    doc = _doc([para(1, "合同金额：¥1,200,000.00。")])
    rec = Verifier().verify(cand(s.key, [("para1", "¥1,200,000.00")], "120万元"), doc, s)
    assert rec.status == Status.GROUNDED
    assert rec.value == MoneyValue(amount=1200000)
    _check_grounded_invariants(rec, doc, s)


# ---------------------------------------------------------------------------
# acc_09：not_found；别名出现时 info POSSIBLE_MISS；review_possible_miss 时 needs_review
# ---------------------------------------------------------------------------


def test_acc_09_not_found_plain():
    s = money()
    doc = _doc([para(1, "其他条款。")])
    rec = Verifier().verify(cand(s.key, [], None), doc, s)
    assert rec.status == Status.NOT_FOUND
    assert rec.issues == []


def test_acc_09_possible_miss_info_and_review_mode():
    s = spec("合同金额", ValueType.MONEY, aliases=["合同总额"])
    doc = _doc([para(1, "合同总额为1,200元。")])
    c = cand(s.key, [], None)

    rec = Verifier().verify(c, doc, s)
    assert rec.status == Status.NOT_FOUND
    assert [(i.code, i.severity) for i in rec.issues] == [(IssueCode.POSSIBLE_MISS, Severity.INFO)]

    rec_review = Verifier(review_possible_miss=True).verify(c, doc, s)
    assert rec_review.status == Status.NEEDS_REVIEW
    assert IssueCode.POSSIBLE_MISS in {i.code for i in rec_review.issues}


# ---------------------------------------------------------------------------
# acc_10：渲染前缀剥离后 grounded，RENDER_NOISE_STRIPPED
# ---------------------------------------------------------------------------


def test_acc_10_render_noise_stripped_then_grounded():
    s = spec("单价", ValueType.NUMBER)
    doc = _doc([cell("B3", "25.00")], fmt=DocFormat.XLSX)
    rec = Verifier().verify(cand(s.key, [("s1-B3", "[s1-B3 | 单价] 25.00")], "25.00"), doc, s)
    assert rec.status == Status.GROUNDED
    assert EvidenceFlag.RENDER_NOISE_STRIPPED in rec.evidence[0].flags
    assert strip_render_noise("[s1-B3 | 单价] 25.00") == "25.00"


# ---------------------------------------------------------------------------
# acc_11：隐藏单元格 → grounded，HIDDEN_SOURCE 警告
# ---------------------------------------------------------------------------


def test_acc_11_hidden_cell_grounded_with_warning():
    s = spec("数量", ValueType.NUMBER)
    doc = _doc([cell("C5", "100", hidden=True)], fmt=DocFormat.XLSX)
    rec = Verifier().verify(cand(s.key, [("s1-C5", "100")], "100"), doc, s)
    assert rec.status == Status.GROUNDED
    assert EvidenceFlag.HIDDEN_SOURCE in rec.evidence[0].flags
    assert any(i.code == IssueCode.HIDDEN_SOURCE and i.severity == Severity.WARNING for i in rec.issues)


def test_acc_11_hidden_cell_excluded_when_configured():
    s = spec("数量", ValueType.NUMBER)
    doc = _doc([cell("C5", "100", hidden=True)], fmt=DocFormat.XLSX)
    rec = Verifier(include_hidden_cells=False).verify(cand(s.key, [("s1-C5", "100")], "100"), doc, s)
    assert rec.status == Status.NEEDS_REVIEW
    assert IssueCode.BLOCK_NOT_FOUND in {i.code for i in rec.issues}


# ---------------------------------------------------------------------------
# acc_12：derivation → needs_review DERIVATION_UNSUPPORTED；many → CARDINALITY_UNSUPPORTED
# ---------------------------------------------------------------------------


def test_acc_12_derivation_unsupported():
    s = money()
    doc = _doc([para(1, "合同金额为1,200元。")])
    d = DerivationClaim(op=DerivationOp.SUM, operands=[DerivationOperand(evidence_index=0, value_text="1,200元")])
    rec = Verifier().verify(cand(s.key, [("para1", "合同金额为1,200元")], "1,200元", derivation=d), doc, s)
    assert rec.status == Status.NEEDS_REVIEW
    assert IssueCode.DERIVATION_UNSUPPORTED in {i.code for i in rec.issues}


def test_acc_12_cardinality_many_unsupported():
    s = money("多笔金额")
    s = s.model_copy(update={"cardinality": "many"})
    doc = _doc([para(1, "多笔金额为1,200元。")])
    rec = Verifier().verify(cand(s.key, [("para1", "多笔金额为1,200元")], "1,200元"), doc, s)
    assert rec.status == Status.NEEDS_REVIEW
    assert IssueCode.CARDINALITY_UNSUPPORTED in {i.code for i in rec.issues}


# ---------------------------------------------------------------------------
# acc_13：enum 别名 → grounded；boolean 别名 → grounded；enum 越界 → ENUM_OUT_OF_RANGE
# ---------------------------------------------------------------------------


_TAX_OPTS = [
    EnumOption(value="含税", aliases=["含增值税"]),
    EnumOption(value="不含税", aliases=["税外"]),
]


def test_acc_13_enum_alias_grounded():
    s = spec("价格口径", ValueType.ENUM, enum_options=_TAX_OPTS)
    doc = _doc([para(1, "报价按含增值税计算。")])
    rec = Verifier().verify(cand(s.key, [("para1", "含增值税")], "含税"), doc, s)
    assert rec.status == Status.GROUNDED
    assert rec.value.value == "含税"


def test_acc_13_boolean_alias_grounded():
    s = spec(
        "是否含税",
        ValueType.BOOLEAN,
        enum_options=[EnumOption(value="true", aliases=["含税"]), EnumOption(value="false", aliases=["不含税"])],
    )
    doc = _doc([para(1, "本报价不含税。")])
    rec = Verifier().verify(cand(s.key, [("para1", "不含税")], "false"), doc, s)
    assert rec.status == Status.GROUNDED
    assert rec.value.value is False


def test_acc_13_enum_out_of_range():
    s = spec("价格口径", ValueType.ENUM, enum_options=_TAX_OPTS)
    doc = _doc([para(1, "报价按含增值税计算。")])
    rec = Verifier().verify(cand(s.key, [("para1", "含增值税")], "未知口径"), doc, s)
    assert rec.status == Status.NEEDS_REVIEW
    assert IssueCode.ENUM_OUT_OF_RANGE in {i.code for i in rec.issues}


def test_value_unparseable_does_not_raise():
    s = money()
    doc = _doc([para(1, "合同金额为1,200元。")])
    rec = Verifier().verify(cand(s.key, [("para1", "合同金额为1,200元")], "一百二十万"), doc, s)
    assert rec.status == Status.NEEDS_REVIEW
    assert IssueCode.VALUE_UNPARSEABLE in {i.code for i in rec.issues}


# ---------------------------------------------------------------------------
# acc_14：spec.unit="月" 而原文写 12年 → needs_review，UNIT_MISMATCH
# ---------------------------------------------------------------------------


def test_acc_14_unit_mismatch():
    s = spec("合同期限", ValueType.NUMBER, unit="月")
    doc = _doc([para(1, "合同期限为12年。")])
    rec = Verifier().verify(cand(s.key, [("para1", "合同期限为12年")], "12"), doc, s)
    assert rec.status == Status.NEEDS_REVIEW
    assert IssueCode.UNIT_MISMATCH in {i.code for i in rec.issues}


def test_unit_match_is_grounded():
    s = spec("合同期限", ValueType.NUMBER, unit="月")
    doc = _doc([para(1, "合同期限为12个月。")])
    rec = Verifier().verify(cand(s.key, [("para1", "合同期限为12个月")], "12个月"), doc, s)
    assert rec.status == Status.GROUNDED
    assert rec.value == NumberValue(value=12, unit="月")


# ---------------------------------------------------------------------------
# acc_16：确定性；输出满足契约（构造即校验）
# ---------------------------------------------------------------------------


def test_acc_16_deterministic_and_contract_valid():
    s = money()
    doc = _doc([para(1, "合同金额为1,200元。"), para(2, "合同金额为1,300元。")])
    cases = [
        cand(s.key, [("para1", "合同金额为1,200元")], "1,200元"),
        cand(s.key, [("para2", "合同金额为1,200元")], "1,200元"),
        cand(s.key, [("para1", "改写的引文")], "1,200元"),
        cand(s.key, [], None),
    ]
    v = Verifier()
    for c in cases:
        a = v.verify(c, doc, s)
        b = Verifier().verify(c, doc, s)
        assert a.model_dump() == b.model_dump()


# ---------------------------------------------------------------------------
# acc_17：属性——grounded 的区间文本归一化包含引文；值区间解析后等价
# ---------------------------------------------------------------------------


def test_acc_17_property_over_several_documents():
    s = money()
    texts = [
        "合同金额为1,200元整。",
        "合同金额：¥1,200,000.00，另付税费。",
        "总金额合计人民币120万元。",
    ]
    quotes = ["合同金额为1,200元整", "¥1,200,000.00", "人民币120万元"]
    values = ["1,200元", "120万元", "120万元"]
    for text, q, val in zip(texts, quotes, values):
        doc = _doc([para(1, text)])
        rec = Verifier().verify(cand(s.key, [("para1", q)], val), doc, s)
        if rec.status == Status.GROUNDED:
            _check_grounded_invariants(rec, doc, s)


# ---------------------------------------------------------------------------
# 辅助：匹配器与定位器的单元测试
# ---------------------------------------------------------------------------


def test_matcher_rejects_cut_nfkc_expansion():
    """T1 规则：不得在 NFKC 展开内部切开命中（ARCH §6.1）。"""
    assert Matcher().find("2", "50㎡") == []


def test_localizer_span_level_partial_overlap_is_line_precision():
    text = "合同金额为1,200元整"
    frags = [TextFragment(start=0, end=len(text), page_number=1, line=0, bbox=BBox(x0=0, y0=0, x1=100, y1=12))]
    blk = pdf_block(1, text, frags)
    loc, prec = LocalizerV1().locate(blk, 5, 10)
    assert prec.value == "line"
    assert len(loc.regions) == 1
    assert 0 < loc.regions[0].x0 < loc.regions[0].x1 < 100


def test_fuzzy_never_grounded_even_if_only_fuzzy_match():
    s = money()
    doc = _doc([para(1, "合同金额为1,200元整，签约后支付。")])
    # "2OO" 中的字母 O 不是数字 0，NORMALIZED 不命中；只能走 FUZZY 诊断，结果必须是 needs_review
    rec = Verifier().verify(cand(s.key, [("para1", "合同金额为1,2OO元整")], "1,200元"), doc, s)
    assert rec.status == Status.NEEDS_REVIEW
    assert rec.evidence == []
    assert IssueCode.QUOTE_FUZZY_ONLY in {i.code for i in rec.issues}


def test_contract_mismatch_raises_value_error():
    import pytest

    s = money()
    doc = _doc([para(1, "合同金额为1,200元。")])
    with pytest.raises(ValueError):
        Verifier().verify(cand("其他键", [], None), doc, s)


# ---------------------------------------------------------------------------
# 额外覆盖：TEXT / DATE / PERCENT 值，块内多次出现，PDF 跨页切分
# ---------------------------------------------------------------------------


def test_text_value_located_inside_quote():
    s = spec("负责人", ValueType.TEXT)
    doc = _doc([para(1, "项目负责人：张三，联系电话 1381234。")])
    rec = Verifier().verify(cand(s.key, [("para1", "负责人：张三")], "张三"), doc, s)
    assert rec.status == Status.GROUNDED
    ev = rec.evidence[0]
    assert doc.block_map()["para1"].text[ev.value_char_start : ev.value_char_end] == "张三"


def test_date_and_percent_values_grounded():
    s = spec("签署日期", ValueType.DATE)
    doc = _doc([para(1, "签署日期为2024年3月5日。")])
    rec = Verifier().verify(cand(s.key, [("para1", "签署日期为2024年3月5日")], "2024-03-05"), doc, s)
    assert rec.status == Status.GROUNDED

    s2 = spec("税率", ValueType.PERCENT)
    doc2 = _doc([para(1, "税率为13%。")])
    rec2 = Verifier().verify(cand(s2.key, [("para1", "税率为13%")], "13%"), doc2, s2)
    assert rec2.status == Status.GROUNDED
    assert rec2.value.value == 13


def test_multiple_occurrences_in_claimed_block_take_first_with_flag():
    s = money()
    doc = _doc([para(1, "金额1,200元，另一笔金额1,200元。")])
    rec = Verifier().verify(cand(s.key, [("para1", "金额1,200元")], "1,200元"), doc, s)
    assert rec.status == Status.GROUNDED
    assert EvidenceFlag.AMBIGUOUS_OCCURRENCE in rec.evidence[0].flags
    assert rec.evidence[0].char_start == 0


def test_pdf_quote_across_page_boundary_is_split_per_page():
    s = money()
    text = "合同金额为1,200元，付款。"
    frags = [
        TextFragment(
            start=i,
            end=i + 1,
            page_number=1 if i < 6 else 2,
            line=0,
            bbox=BBox(x0=i * 10, y0=0, x1=i * 10 + 10, y1=12),
        )
        for i in range(len(text))
    ]
    doc = _doc([pdf_block(1, text, frags)], fmt=DocFormat.PDF)
    # 引文本身仅跨页，不含值：应拆成两段证据，且两段都标记 CROSS_BLOCK_SPLIT
    rec = Verifier().verify(cand(s.key, [("p1-b1", "合同金额为1,200元")], "1,200元"), doc, s)
    assert [(e.char_start, e.char_end) for e in rec.evidence] == [(0, 6), (6, 11)]
    assert [e.locator.page_number for e in rec.evidence] == [1, 2]
    assert all(EvidenceFlag.CROSS_BLOCK_SPLIT in e.flags for e in rec.evidence)
