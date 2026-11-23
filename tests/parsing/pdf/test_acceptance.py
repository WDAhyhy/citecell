"""T4 验收：块数与段落数、块文本、逐字片段覆盖、几何误差、确定性。

PDF 在测试中由 reportlab 即时生成；坐标以 reportlab 绘制点为真值。
"""

from __future__ import annotations

import pytest
from reportlab.lib.pagesizes import A4

from citecell.interfaces import Parser
from citecell.models import BlockKind
from citecell.parsing.pdf import PdfParser

CJK_FONT = "STSong-Light"  # 与 conftest.py 中注册的字体一致（pytest importlib 模式下不用相对导入）
PAGE_H = A4[1]

TITLE = "第一章 总体情况"
LINE_A1 = "这是第一段的第一行文字，用来测试换行后的段落拼接是否正确，"
LINE_A2 = "并且第二行末尾没有句号"
SRC_B = "营收 Revenue 增长 12.5%，2024年。"  # 中英交界在源文本中已有空格

# 绘制点（reportlab 坐标，原点左下）
X0 = 72.0
Y_TITLE, Y_A1, Y_A2, Y_B = 760.0, 720.0, 704.0, 660.0


def draw_acceptance(c):
    c.setFont(CJK_FONT, 18)
    c.drawString(X0, Y_TITLE, TITLE)
    c.setFont(CJK_FONT, 12)
    c.drawString(X0, Y_A1, LINE_A1)
    c.drawString(X0, Y_A2, LINE_A2)
    c.drawString(X0, Y_B, SRC_B)


@pytest.fixture
def acceptance_doc(make_pdf):
    path = make_pdf("acceptance", draw_acceptance)
    return path


@pytest.mark.parametrize("backend", ["pypdfium2", "pdfminer"])
def test_block_count_and_text_match_source_paragraphs(acceptance_doc, backend):
    doc = PdfParser(backend).parse(acceptance_doc)
    assert [b.block_id for b in doc.blocks] == ["p1-b1", "p1-b2", "p1-b3"]
    assert [b.kind for b in doc.blocks] == [BlockKind.HEADING, BlockKind.PARAGRAPH, BlockKind.PARAGRAPH]
    # 中文跨行处无空格；源文本中的中英交界空格保留。
    assert doc.blocks[0].text == TITLE
    assert doc.blocks[1].text == LINE_A1 + LINE_A2
    assert doc.blocks[2].text == SRC_B


def test_parser_satisfies_interface_protocol():
    assert isinstance(PdfParser(), Parser)


def test_every_non_space_char_is_covered_by_a_fragment(acceptance_doc):
    doc = PdfParser("pypdfium2").parse(acceptance_doc)
    for block in doc.blocks:
        covered = set()
        for f in block.fragments:
            covered.update(range(f.start, f.end))
        for i, ch in enumerate(block.text):
            if not ch.isspace():
                assert i in covered, f"{block.block_id} 第 {i} 个字符无片段"


def test_fragment_boxes_inside_page_and_geometry_within_tolerance(acceptance_doc):
    """几何误差：每行首字符的框左缘与绘制 x 的偏差 ≤ 0.5pt；基线落在字框内；行序自上而下。"""
    X_TOL = 0.5
    doc = PdfParser("pypdfium2").parse(acceptance_doc)
    page_w = doc.blocks[0].locator.page_width
    page_h = doc.blocks[0].locator.page_height
    assert page_h == pytest.approx(PAGE_H, abs=1e-3)

    expected = {
        "p1-b1": [(X0, Y_TITLE)],
        "p1-b2": [(X0, Y_A1), (X0, Y_A2)],
        "p1-b3": [(X0, Y_B)],
    }
    max_x_err = 0.0
    line_tops: dict[str, list[float]] = {}
    for block in doc.blocks:
        for f in block.fragments:
            b = f.bbox
            assert 0.0 <= b.x0 and b.x1 <= page_w + 1e-6
            assert 0.0 <= b.y0 and b.y1 <= page_h + 1e-6
        firsts = {}
        for f in block.fragments:
            firsts.setdefault(f.line, f)
        assert sorted(firsts) == list(range(len(firsts)))
        line_tops[block.block_id] = [firsts[i].bbox.y0 for i in sorted(firsts)]
        for (x, y_pdf), line in zip(expected[block.block_id], sorted(firsts)):
            f = firsts[line]
            max_x_err = max(max_x_err, abs(f.bbox.x0 - x))
            baseline_top = PAGE_H - y_pdf
            assert f.bbox.y0 <= baseline_top <= f.bbox.y1, (block.block_id, line)
    assert max_x_err <= X_TOL, f"x 误差 {max_x_err:.3f}pt 超过容差 {X_TOL}pt"
    # 第一行 y0 小于最后一行（左上原点，y 向下）
    assert line_tops["p1-b2"][0] < line_tops["p1-b2"][-1]


def test_repeated_parse_gives_identical_json(acceptance_doc):
    p = PdfParser("pypdfium2")
    a = p.parse(acceptance_doc).model_dump_json()
    b = p.parse(acceptance_doc).model_dump_json()
    assert a == b


def test_doc_id_is_file_hash_prefix(acceptance_doc):
    import hashlib

    doc = PdfParser().parse(acceptance_doc)
    sha = hashlib.sha256(acceptance_doc.read_bytes()).hexdigest()
    assert doc.info.sha256 == sha
    assert doc.info.doc_id == sha[:16]
    assert doc.info.display_name is None  # 默认不记录文件名（隐私）
