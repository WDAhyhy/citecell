"""跨页续接标记 CONTINUED_ON_NEXT_PAGE 的测试（PDF 由 reportlab 即时生成）。"""

from __future__ import annotations

import pytest

from citecell.models import BlockFlag
from citecell.parsing.pdf import PdfParser

CJK = "STSong-Light"
MARK = BlockFlag.CONTINUED_ON_NEXT_PAGE


def _page(lines, y0=760.0, size=12, step=16.0):
    def draw(c):
        c.setFont(CJK, size)
        y = y0
        for t in lines:
            c.drawString(72, y, t)
            y -= step

    return draw


def _two_pages(make_pdf, name, p1_lines, p2_lines, *, p2_y0=760.0, p2_size=12, p1_size=12):
    draw1 = _page(p1_lines, size=p1_size)
    draw2 = _page(p2_lines, y0=p2_y0, size=p2_size)
    return make_pdf(name, draw1, extra_pages=draw2)


def test_unfinished_sentence_continuing_on_next_page_is_marked_on_last_block_only(make_pdf):
    path = _two_pages(
        make_pdf,
        "cont-yes",
        ["第一段开头的文字内容", "这一句话没有结束，接着下一页"],
        ["的内容继续在这里并且结束。"],
    )
    doc = PdfParser("pypdfium2").parse(path)
    assert [b.block_id for b in doc.blocks] == ["p1-b1", "p2-b1"]
    assert MARK in doc.blocks[0].flags
    assert MARK not in doc.blocks[1].flags


def test_sentence_ending_with_full_stop_is_not_marked(make_pdf):
    path = _two_pages(
        make_pdf,
        "cont-period",
        ["第一段开头的文字内容", "这一句话已经结束。"],
        ["下一页的新段落内容"],
    )
    doc = PdfParser("pypdfium2").parse(path)
    assert MARK not in doc.blocks[0].flags


def test_next_page_block_in_middle_is_not_marked(make_pdf):
    path = _two_pages(
        make_pdf,
        "cont-middle",
        ["第一段开头的文字内容", "这一句话没有结束，接着下一页"],
        ["页面中部的内容"],
        p2_y0=420.0,  # 页面中部，不在上部 15% 边距区
    )
    doc = PdfParser("pypdfium2").parse(path)
    assert MARK not in doc.blocks[0].flags


def test_size_change_across_pages_is_not_marked(make_pdf):
    path = _two_pages(
        make_pdf,
        "cont-size",
        ["第一段开头的文字内容", "这一句话没有结束，接着下一页"],
        ["标题式的大字号续接"],
        p2_size=18,
    )
    doc = PdfParser("pypdfium2").parse(path)
    assert MARK not in doc.blocks[0].flags


def test_single_page_last_block_is_never_marked(make_pdf):
    path = make_pdf("single", _page(["单页末尾没有句号的文字"]))
    doc = PdfParser("pypdfium2").parse(path)
    assert all(MARK not in b.flags for b in doc.blocks)


def test_last_page_last_block_is_never_marked(make_pdf):
    path = _two_pages(
        make_pdf,
        "cont-last",
        ["第一页完整的句子。"],
        ["最后一页末尾没有句号的文字"],
    )
    doc = PdfParser("pypdfium2").parse(path)
    assert all(MARK not in b.flags for b in doc.blocks)


def test_existing_flags_are_kept_when_marking(make_pdf):
    path = _two_pages(
        make_pdf,
        "cont-keep",
        ["第一段开头的文字内容", "这一句话没有结束，接着下一页"],
        ["的内容继续"],
    )
    doc = PdfParser("pypdfium2").parse(path)
    assert doc.blocks[0].flags == [MARK]
    assert doc.blocks[0].order == 0
