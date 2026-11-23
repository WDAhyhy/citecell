"""Oracle 证据定位：必须落在金标准提及的原生位置，引文在块内唯一且包含完整提及。

真实文档用 citecell.synth 在测试运行时生成（docx / xlsx / pdf），再用默认解析器解析。
不提交任何二进制样例。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from citecell.backends import OracleBackend
from citecell.backends import oracle as oracle_mod
from citecell.models import (
    BBox,
    a1_to_row_col,
    ChatMessage,
    ChatRequest,
    DifficultyConfig,
    DocFormat,
    DocxLocator,
    GenerationConfig,
    GoldDocument,
    GoldMention,
    KeySet,
    LLMExtractionOutput,
    ParsedDocument,
    PdfLocator,
    RequestContext,
    XlsxLocator,
)
from citecell.parsing import build_default_registry
from citecell.synth import generate

ROOT = Path(__file__).resolve().parents[2]
KEYSET = KeySet.model_validate_json((ROOT / "configs/keysets/procurement_v1.json").read_text(encoding="utf-8"))
HARD = DifficultyConfig.model_validate_json((ROOT / "configs/synth/hard.json").read_text(encoding="utf-8"))
TAX_KEY = "是否含税"
QUOTE_MAX_CHARS = 80  # 与设计约定一致：引文长度上限


def same_native_place(a, b):  # 实现位于 citecell.backends.oracle
    return oracle_mod.same_native_place(a, b)


def expand_quote(text, start, end):
    return oracle_mod.expand_quote(text, start, end)


def _corpus(out_dir: Path, fmt: DocFormat, seed: int, n: int):
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg = GenerationConfig(seed=seed, n_docs=n, formats=[fmt], keyset=KEYSET, difficulty=HARD)
    golds = [s.gold for s in generate(cfg, out_dir)]
    paths = sorted(out_dir.glob("doc_*"))
    reg = build_default_registry()
    parsed = [reg.for_path(p).parse(p) for p in paths]
    return golds, parsed


def _answer(oracle: OracleBackend, parsed: ParsedDocument, key: str):
    req = ChatRequest(
        messages=[ChatMessage(role="user", content="占位")],
        context=RequestContext(
            doc_id=parsed.info.doc_id,
            window_id="w",
            keys=[key],
            block_ids=[b.block_id for b in parsed.blocks],
        ),
    )
    resp = asyncio.run(oracle.chat(req))
    out = LLMExtractionOutput.model_validate_json(resp.content)
    return out.fields[0]


def _block_contains_mention(block, mention: GoldMention, fmt: str) -> bool:
    """独立于实现的检查：块的原生位置（或 PDF 几何）是否对应该提及。"""
    ml, bl = mention.locator, block.locator
    if fmt == "docx":
        return (
            isinstance(bl, DocxLocator)
            and (bl.body_index, bl.paragraph_number, bl.table_number, bl.row_index, bl.col_index)
            == (ml.body_index, ml.paragraph_number, ml.table_number, ml.row_index, ml.col_index)
        )
    if fmt == "xlsx":
        return isinstance(bl, XlsxLocator) and bl.sheet_name == ml.sheet_name and bl.cell == ml.cell
    if fmt == "pdf":
        if not isinstance(bl, PdfLocator) or bl.page_number != ml.page_number:
            return False
        for f in block.fragments:
            cx, cy = (f.bbox.x0 + f.bbox.x1) / 2, (f.bbox.y0 + f.bbox.y1) / 2
            if any(r.x0 <= cx <= r.x1 and r.y0 <= cy <= r.y1 for r in ml.regions):
                return True
        return False
    return False


def _cases(tmp_path_factory, fmt: DocFormat, seed: int, n: int):
    golds, parsed = _corpus(tmp_path_factory.mktemp(f"loc_{fmt.value}_{seed}"), fmt, seed, n)
    by_sha = {g.doc_sha256: g for g in golds}
    out = []
    for p in parsed:
        g = by_sha[p.info.sha256]
        rec = next(r for r in g.records if r.key == TAX_KEY)
        if rec.expected == "present":
            out.append((g, rec, p))
    return out


@pytest.fixture(scope="module")
def tax_cases(tmp_path_factory):
    cases = {}
    for fmt in (DocFormat.DOCX, DocFormat.XLSX, DocFormat.PDF):
        cases[fmt.value] = _cases(tmp_path_factory, fmt, seed=11, n=12)
    return cases


@pytest.mark.parametrize("fmt", ["docx", "xlsx", "pdf"])
def test_tax_key_evidence_lands_on_gold_mention(tax_cases, fmt):
    cases = tax_cases[fmt]
    assert cases, f"语料中应有 {fmt} 的 {TAX_KEY} present 样本"
    problems = []
    for gold, rec, parsed in cases:
        oracle = OracleBackend([gold], [parsed])
        field = _answer(oracle, parsed, TAX_KEY)
        blocks = {b.block_id: b for b in parsed.blocks}
        assert field.evidence, "present 键必须给出证据"
        ev = field.evidence[0]
        block = blocks[ev.block_id]
        if not any(_block_contains_mention(block, m, fmt) for m in rec.mentions):
            problems.append(f"{gold.doc_sha256[:8]}: 证据块 {ev.block_id} 不是金标准提及所在位置")
        if block.text.count(ev.quote) != 1:
            problems.append(f"{gold.doc_sha256[:8]}: 引文 {ev.quote!r} 在块内出现 {block.text.count(ev.quote)} 次")
        if not any(m.surface_text in ev.quote for m in rec.mentions):
            problems.append(f"{gold.doc_sha256[:8]}: 引文 {ev.quote!r} 不包含完整提及")
        if any(_block_contains_mention(block, d, fmt) for d in rec.distractors) and not any(
            _block_contains_mention(block, m, fmt) for m in rec.mentions
        ):
            problems.append(f"{gold.doc_sha256[:8]}: 证据落在干扰项位置")
    assert not problems, f"{fmt} 共 {len(problems)} 处问题：\n" + "\n".join(problems[:10])


# ---------------------------------------------------------------- 单元测试
def _x(cell: str, *, sheet: str = "项目", fmt_str: str | None = None) -> XlsxLocator:
    row, col = a1_to_row_col(cell)
    return XlsxLocator(
        sheet_index=0,
        sheet_name=sheet,
        cell=cell,
        row_number=row,
        column_number=col,
        number_format=fmt_str,
    )


def test_xlsx_same_place_ignores_number_format_and_style():
    a = _x("B2", fmt_str=None)
    b = _x("B2", fmt_str="General")
    assert same_native_place(a, b)


def test_xlsx_same_place_requires_same_sheet_and_cell():
    assert not same_native_place(_x("B2"), _x("A2"))
    assert not same_native_place(_x("B2"), _x("B2", sheet="其他"))


def test_docx_same_place_compares_position_fields_only():
    a = DocxLocator(body_index=3, paragraph_number=3)
    assert same_native_place(a, DocxLocator(body_index=3, paragraph_number=3))
    assert not same_native_place(a, DocxLocator(body_index=4, paragraph_number=3))
    t = DocxLocator(body_index=3, table_number=1, row_index=0, col_index=1)
    assert same_native_place(t, DocxLocator(body_index=3, table_number=1, row_index=0, col_index=1, style_name="x"))
    assert not same_native_place(t, DocxLocator(body_index=3, table_number=1, row_index=0, col_index=2))


def test_format_mismatch_is_never_same_place():
    assert not same_native_place(DocxLocator(body_index=0, paragraph_number=1), _x("A1"))


def test_pdf_same_place_uses_page_and_region_overlap():
    def pdf(page: int, x0: float, y0: float, x1: float, y1: float) -> PdfLocator:
        return PdfLocator(
            page_number=page,
            page_width=600,
            page_height=800,
            regions=[BBox(x0=x0, y0=y0, x1=x1, y1=y1)],
        )

    a = pdf(1, 10, 10, 100, 20)
    assert same_native_place(a, pdf(1, 50, 12, 150, 22))  # 有重叠
    assert not same_native_place(a, pdf(1, 200, 10, 300, 20))  # 无重叠
    assert not same_native_place(a, pdf(2, 10, 10, 100, 20))  # 页码不同


def test_expand_quote_is_unique_and_contains_mention():
    text = "税费口径为含税（初报价：不含税）"
    start = text.index("含税")
    quote, q_start, q_end = expand_quote(text, start, start + 2)
    assert text.count(quote) == 1
    assert "含税" in quote
    assert text[q_start:q_end] == quote
    assert q_start <= start and q_end >= start + 2


def test_expand_quote_respects_cap_and_keeps_mention():
    filler = "甲" * 200
    text = filler + "含税" + filler + "含税" + filler
    start = text.index("含税")
    quote, q_start, q_end = expand_quote(text, start, start + 2)
    assert len(quote) <= QUOTE_MAX_CHARS
    assert q_start <= start and q_end >= start + 2
    assert text[q_start:q_end] == quote


def test_quote_never_starts_with_whitespace_and_is_unique_after_normalization():
    # 原文中 " 含税" 唯一，但校验器忽略空白后「含税」出现两次（标签「含税情况」与值）
    text = "含税情况 含税（初报价：不含税）"
    start = text.index("含税（")
    quote, qs, qe = expand_quote(text, start, start + 2)
    assert quote == "况 含税"
    assert text[qs:qe] == quote and not quote[0].isspace()
    assert text.count(quote) == 1


def test_expand_quote_of_unique_mention_is_minimal():
    text = "验收日期为2026年3月1日。"
    start = text.index("2026")
    quote, _, _ = expand_quote(text, start, start + len("2026年3月1日"))
    assert text.count(quote) == 1 and "2026年3月1日" in quote
