"""T4 版面单元测试：不依赖 PDF 文件，直接用 PdfChar 构造输入，验证 LINE_JOIN_V1、分段、长块切分、虚拟字符处理。"""

from __future__ import annotations

import pytest

from citecell.models import BBox, BlockFlag, BlockKind, PdfChar, PdfPageInfo
from citecell.parsing.pdf.layout import (
    PdfLayoutV1,
    group_lines,
    join_line_texts,
    split_long_text,
    split_paragraphs,
    _glyphs,
)

PAGE = PdfPageInfo(page_number=1, width=600.0, height=800.0, rotation=0)


def ch(text, x0, top, size=12.0, w=10.0, h=12.0, generated=False):
    """构造一个字符框（PDF_COORDS_V1：左上原点）。"""
    if text in ("\r", "\n"):
        return PdfChar(text=text, bbox=BBox(x0=x0, y0=top, x1=x0, y1=top), font_size=1.0, generated=True)
    return PdfChar(text=text, bbox=BBox(x0=x0, y0=top, x1=x0 + w, y1=top + h), font_size=size, generated=generated)


def line_of(text, x0, top, **kw):
    """把一行文本按等宽字符排开。"""
    out = []
    x = x0
    for chx in text:
        out.append(ch(chx, x, top, **kw))
        x += 10.0
    return out


def build(chars, page=PAGE):
    return PdfLayoutV1().build_blocks("0123456789abcdef", [page], {1: chars})


# ---------------------------------------------------------------------------
# LINE_JOIN_V1
# ---------------------------------------------------------------------------


def test_join_cjk_to_cjk_is_direct_and_cjk_punct_counts_as_cjk():
    text, _ = join_line_texts([("这是第一行，", []), ("第二行", [])])
    assert text == "这是第一行，第二行"


@pytest.mark.parametrize(
    "a, b, expected",
    [
        ("Revenue grew", "in 2024", "Revenue grew in 2024"),  # 英文行尾 + 行首：插入空格
        ("营收 ACME", "公司", "营收 ACME 公司"),  # 英文尾 + CJK 首：插入空格
        ("公司ACME", "增长", "公司ACME 增长"),  # CJK 与 Latin 相邻：插入空格（另一侧为 CJK 时仍不直接相连）
        ("已有空格 ", "下一行", "已有空格 下一行"),  # 前端已有空白：不重复插入
    ],
)
def test_join_inserts_single_space_only_when_not_both_cjk(a, b, expected):
    text, _ = join_line_texts([(a, []), (b, [])])
    assert text == expected


def test_join_fragments_are_offset_per_line():
    bbox = BBox(x0=0, y0=0, x1=1, y1=1)
    text, frags = join_line_texts([("甲乙", [(0, 1, bbox), (1, 2, bbox)]), ("丙", [(0, 1, bbox)])])
    assert text == "甲乙丙"
    assert [(s, e, line) for s, e, line, _ in frags] == [(0, 1, 0), (1, 2, 0), (2, 3, 1)]


# ---------------------------------------------------------------------------
# 虚拟字符
# ---------------------------------------------------------------------------


def test_generated_line_breaks_are_dropped_and_generated_space_is_kept_between_visible_chars():
    chars = [
        ch("A", 72, 100), ch(" ", 82, 100, generated=True), ch("B", 92, 100),
        ch("\r", 102, 100), ch("\n", 102, 100),
    ]
    blocks = build(chars)
    assert [b.text for b in blocks] == ["A B"]


def test_generated_space_on_line_edges_is_discarded():
    chars = [ch(" ", 60, 100, generated=True), ch("A", 72, 100), ch("B", 82, 100), ch(" ", 92, 100, generated=True)]
    assert [b.text for b in build(chars)] == ["AB"]


def test_consecutive_whitespace_collapses_to_one_space():
    chars = [ch("A", 72, 100), ch(" ", 82, 100), ch(" ", 82, 100, generated=True), ch("B", 92, 100)]
    assert [b.text for b in build(chars)] == ["A B"]


def test_spaces_are_attached_to_their_line_by_vertical_position():
    # 第二行 "cd" 的竖向范围内有一个虚拟空格（位于 c、d 之间）：应挂到第二行，得到 "c d"。
    # 行距 20pt 超过回退行距 1.3×12pt，两行分为两块（段落分界规则）。
    # 若空格误挂到第一行，第二行会是 "cd"，本断言会失败。
    chars = line_of("ab", 72, 100) + [ch("c", 72, 120), ch(" ", 78, 126, w=2, generated=True), ch("d", 82, 120)]
    assert [b.text for b in build(chars)] == ["ab", "c d"]


# ---------------------------------------------------------------------------
# 分段
# ---------------------------------------------------------------------------


def test_paragraph_break_on_large_vertical_gap():
    chars = line_of("第一段第一行", 72, 100) + line_of("第一段第二行", 72, 116)
    chars += line_of("第二段第一行", 72, 150) + line_of("第二段第二行", 72, 166)
    blocks = build(chars)
    assert [b.text for b in blocks] == ["第一段第一行第一段第二行", "第二段第一行第二段第二行"]


def test_font_size_change_starts_new_block_and_marks_heading():
    chars = line_of("标题", 72, 80, size=18.0, h=18.0) + line_of("正文一行", 72, 104) + line_of("正文二行", 72, 120)
    blocks = build(chars)
    assert [b.kind for b in blocks] == [BlockKind.HEADING, BlockKind.PARAGRAPH]
    assert blocks[1].section_path == ["标题"]


def test_first_line_indent_after_short_line_starts_new_block():
    # 第一行写到接近右缘（完整行），第二行短且有缩进 → 缩进行另起段
    full = line_of("一" * 40, 72, 100)
    short = line_of("短行", 72, 116)
    indent = line_of("缩进段", 72 + 30, 132)  # 缩进 30pt ≥ 1.5em(18pt)，上一行明显未满
    blocks = build(full + short + indent)
    assert [b.text for b in blocks] == ["一" * 40 + "短行", "缩进段"]


def test_block_ids_and_order_are_sequential_per_page():
    chars = line_of("甲", 72, 100) + line_of("乙", 72, 150)
    blocks = build(chars)
    assert [b.block_id for b in blocks] == ["p1-b1", "p1-b2"]
    assert [b.order for b in blocks] == [0, 1]


def test_page_without_visible_text_yields_no_blocks():
    chars = [ch(" ", 72, 100, generated=True), ch("\r", 72, 100), ch("\n", 72, 100)]
    assert build(chars) == []


def test_split_paragraphs_empty_input():
    assert split_paragraphs([]) == []


def test_group_lines_separates_overlapping_rows_by_height():
    glyphs = _glyphs(line_of("ab", 72, 100) + line_of("cd", 72, 130))
    lines = group_lines(glyphs)
    assert [ln.text for ln in lines] == ["ab", "cd"]


# ---------------------------------------------------------------------------
# 长块切分
# ---------------------------------------------------------------------------


def test_split_long_text_prefers_sentence_end_and_respects_limit():
    text = "甲" * 20 + "。" + "乙" * 20 + "。" + "丙" * 20 + "。"
    pieces = split_long_text(text, 45)
    assert all(e - s <= 45 for s, e in pieces)
    assert "".join(text[s:e] for s, e in pieces) == text
    assert text[pieces[0][1] - 1] == "。"  # 第一段在句末标点处结束


def test_split_long_text_falls_back_to_whitespace_then_hard_cut():
    spaced = "word " * 10
    pieces = split_long_text(spaced, 12)
    assert all(e - s <= 12 for s, e in pieces)
    assert all(not spaced[s:e].startswith(" ") for s, e in pieces)
    hard = "x" * 25
    assert split_long_text(hard, 10) == [(0, 10), (10, 20), (20, 25)]


def test_long_paragraph_is_split_with_flag_and_fragments_follow_text(make_pdf):
    from reportlab.pdfgen import canvas

    from citecell.parsing.pdf import PdfParser

    sentences = ["这是第" + str(i) + "句，内容用于测试长块切分。" for i in range(1, 9)]
    source = "".join(sentences)

    def draw(c):
        c.setFont("STSong-Light", 12)
        y = 760.0
        for i in range(0, len(sentences), 2):
            c.drawString(72, y, "".join(sentences[i : i + 2]))
            y -= 16.0

    path = make_pdf("long", draw)
    doc = PdfParser("pypdfium2", max_block_chars=40).parse(path)
    texts = [b.text for b in doc.blocks]
    assert len(texts) >= 2
    assert "".join(texts) == source  # 拼接后等于源文本（句间无空格）
    for b in doc.blocks:
        assert len(b.text) <= 40
        assert BlockFlag.SPLIT_FROM_LONG_BLOCK in b.flags
        for f in b.fragments:
            assert 0 <= f.start < f.end <= len(b.text)


def test_block_within_limit_is_not_flagged_as_split():
    chars = line_of("短段落", 72, 100)
    blocks = build(chars)
    assert blocks[0].flags == []


# ---------------------------------------------------------------------------
# 文本层可疑
# ---------------------------------------------------------------------------


def test_private_use_and_replacement_ratio_flags_block():
    chars = [ch(c, 72 + 10 * i, 100) for i, c in enumerate("abc")]
    blocks = build(chars)
    assert BlockFlag.TEXT_LAYER_SUSPECT in blocks[0].flags


def test_clean_text_is_not_flagged():
    blocks = build(line_of("正常文字 abc", 72, 100))
    assert BlockFlag.TEXT_LAYER_SUSPECT not in blocks[0].flags
