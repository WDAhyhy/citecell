"""PDF 写出（T5b）：金标准几何与直接提取的一致性、坐标换算、折行、确定性。

回读只使用 pypdfium2（T0 选定的后端）直接提取字符，不调用 CiteCell 的 PDF 解析器（ADR-013）。
测试数据在运行时由代码生成（虚构内容），不提交二进制样例。
"""

import hashlib
import random
import re

import pypdfium2 as pdfium
import pytest

from citecell.models import PdfLocator
from citecell.synth.ir import DocIR, ParaIR, Placement
from citecell.synth.keysets import load_keyset, load_preset
from citecell.synth.pdf_typeset import A4_HEIGHT, A4_WIDTH, TypesetOptions, _Item, typeset, wrap
from citecell.synth.realize import build_ir
from citecell.synth.sampler import sample_facts
from citecell.synth.writers.pdf_writer import _face, write

# ----------------------------------------------------------------- 夹具


def _build(seed: int, preset: str, cross: float | None = None) -> DocIR:
    ks = load_keyset()
    diff = load_preset(preset)
    if cross is not None:
        diff = diff.model_copy(update={"cross_block_rate": cross})
    rng = random.Random(seed)
    facts = sample_facts(rng, ks, diff)
    return build_ir(facts, ks, diff, rng, "docx")


def _extract(data: bytes) -> list[tuple[int, str, float, float]]:
    """用 pypdfium2 直接提取每个字符的中心点，返回 (页码, 字符, x, y)，y 为左上原点换算后的值。"""
    out: list[tuple[int, str, float, float]] = []
    pdf = pdfium.PdfDocument(data)
    for i in range(len(pdf)):
        page = pdf.get_page(i)
        _, h = page.get_size()
        tp = page.get_textpage()
        text = tp.get_text_range()
        assert len(text) == tp.count_chars()
        for idx in range(tp.count_chars()):
            left, bottom, right, top = tp.get_charbox(idx)
            out.append((i + 1, text[idx], (left + right) / 2, h - (bottom + top) / 2))
    return out


def _inside(box, x: float, y: float) -> bool:
    return box.x0 <= x <= box.x1 and box.y0 <= y <= box.y1


def _strip(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _region_text(chars, pl: Placement) -> str:
    loc: PdfLocator = pl.locator
    picked = [
        ch
        for page, ch, x, y in chars
        if page == loc.page_number and any(_inside(b, x, y) for b in loc.regions)
    ]
    return "".join(picked)


# ----------------------------------------------------------------- 金标准与直接提取一致


@pytest.mark.parametrize("seed,preset", [(1, "medium"), (2, "hard"), (5, "hard")])
def test_extracted_chars_inside_gold_boxes_match_surface(seed, preset):
    ir = _build(seed, preset)
    data, placements = write(ir)
    chars = _extract(data)
    assert placements, "应至少有一处值出现"
    for pl in placements:
        got = _strip(_region_text(chars, pl))
        assert got == _strip(pl.surface_text), (pl.sid, got, pl.surface_text)


@pytest.mark.parametrize("seed,preset", [(1, "medium"), (2, "hard")])
def test_every_span_is_fully_recorded(seed, preset):
    """每个 IR 值（含干扰值）的所有记账片段按页序拼接后，逐字等于其 surface。"""
    ir = _build(seed, preset)
    _, placements = write(ir)
    by_sid: dict[int, list[Placement]] = {}
    for pl in placements:
        by_sid.setdefault(pl.sid, []).append(pl)
    assert set(by_sid) == {sp.sid for sp in ir.spans}
    for sp in ir.spans:
        pieces = "".join(p.surface_text for p in by_sid[sp.sid])
        assert pieces == sp.surface


def test_placements_are_char_none_and_regions_on_page():
    ir = _build(1, "medium")
    _, placements = write(ir)
    for pl in placements:
        assert pl.char_start is None and pl.char_end is None
        loc = pl.locator
        assert isinstance(loc, PdfLocator)
        assert loc.page_width == A4_WIDTH and loc.page_height == A4_HEIGHT
        for b in loc.regions:
            assert 0 <= b.x0 <= b.x1 <= A4_WIDTH
            assert 0 <= b.y0 <= b.y1 <= A4_HEIGHT


# ----------------------------------------------------------------- 折行（cross_block_rate=1）


def test_cross_block_rate_one_splits_a_value_across_two_lines():
    ir = _build(7, "hard", cross=1.0)
    _, placements = write(ir)
    wrapped = [pl for pl in placements if len(pl.locator.regions) >= 2]
    assert wrapped, "cross_block_rate=1 时应至少有一个值被折行切开"
    pl = wrapped[0]
    ys = sorted({round(b.y0, 2) for b in pl.locator.regions})
    assert len(ys) >= 2, "被切开的值应在两行上各有一个框"
    chars = _extract(write(ir)[0])
    assert _strip(_region_text(chars, pl)) == _strip(pl.surface_text)


def test_page_breaks_split_values_into_per_page_mentions():
    """页面很短时：跨页的值拆为每页一条记账，逐页拼接等于 surface；表格组不跨页拆开。"""
    opts = TypesetOptions(page_width=A4_WIDTH, page_height=300.0)
    ir = _build(4, "hard", cross=1.0)
    data, placements = write(ir, options=opts)
    chars = _extract(data)
    assert max(c[0] for c in chars) > 1, "应产生多页"
    by_sid: dict[int, list[Placement]] = {}
    for pl in placements:
        by_sid.setdefault(pl.sid, []).append(pl)
        assert _strip(_region_text(chars, pl)) == _strip(pl.surface_text)
    for sp in ir.spans:
        assert "".join(p.surface_text for p in by_sid[sp.sid]) == sp.surface


def test_wrap_preserves_text_and_width():
    text = "采购合同 ABC-123 ，。 测试文字 2024/03/05 ¥1,000.00 元整" * 3
    items = [_Item(ch, 10.0 if ord(ch) > 127 else 5.0, None) for ch in text]
    avail = 60.0
    lines = wrap(items, avail)
    assert "".join(i.ch for line in lines for i in line) == text
    for line in lines:
        width = sum(i.w for i in line)
        assert width <= avail or len(line) == 1


# ----------------------------------------------------------------- 坐标换算


def test_coordinate_flip_keeps_baseline_inside_glyph_box():
    """reportlab 原点在左下；记账为左上原点。换算后字形框必须包含基线，且中心接近 pypdfium2 给出的字符框中心。"""
    opts = TypesetOptions(page_width=A4_WIDTH, page_height=A4_HEIGHT)
    pages = typeset([ParaIR(["测试文字"])], _face(), opts)
    g = pages[0].glyphs[0]
    data = write(DocIR(fmt="docx", layout="fixed", blocks=[ParaIR(["测试文字"])]))[0]
    pdf = pdfium.PdfDocument(data)
    page = pdf.get_page(0)
    _, h = page.get_size()
    tp = page.get_textpage()
    left, bottom, right, top = tp.get_charbox(0)
    # 记账框（左上原点）→ 与 pypdfium2 框（左下原点）换算比较
    box_top = h - top
    box_bottom = h - bottom
    assert box_top <= g.baseline <= box_bottom, (box_top, g.baseline, box_bottom)
    assert abs((g.top + g.bottom) / 2 - (box_top + box_bottom) / 2) < 3.0
    assert abs((g.x0 + g.x1) / 2 - (left + right) / 2) < 3.0


# ----------------------------------------------------------------- 确定性


def test_same_seed_gives_identical_bytes_and_gold_geometry():
    d1, p1 = write(_build(3, "hard"))
    d2, p2 = write(_build(3, "hard"))
    assert hashlib.sha256(d1).hexdigest() == hashlib.sha256(d2).hexdigest()
    assert p1 == p2


def test_pdf_metadata_times_are_fixed():
    data, _ = write(_build(3, "medium"))
    creation = re.findall(rb"/CreationDate \(([^)]*)\)", data)
    modification = re.findall(rb"/ModDate \(([^)]*)\)", data)
    assert creation == [b"D:20000101000000+00'00'"]
    assert modification == creation
