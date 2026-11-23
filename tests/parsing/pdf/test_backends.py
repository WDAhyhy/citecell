"""T4 后端测试：虚拟字符（空格型）样本、两后端交叉校验、不支持的文档、旋转页、许可证隔离。

虚拟字符的结论来自实测（见 tests 中注释与最终回复）；未覆盖的情形不在这里声称结论。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.lib.pdfencrypt import StandardEncryption
from reportlab.pdfgen import canvas

from citecell.interfaces import PdfBackend, UnsupportedDocumentError
from citecell.parsing.pdf import PdfParser
from citecell.parsing.pdf.backends import PdfBackendNotImplementedError, load_backend

CJK_FONT = "STSong-Light"
SRC = Path(__file__).resolve().parents[3] / "src"


def _chars(path: Path, backend: str):
    b = load_backend(backend)
    b.open(path)
    try:
        return list(b.iter_chars(1))
    finally:
        b.close()


def _text(path: Path, backend: str) -> str:
    return "".join(c.text for c in _chars(path, backend) if not (c.generated and c.text in "\r\n"))


# ---------------------------------------------------------------------------
# 空格型虚拟字符（T0 未覆盖的情形）
# ---------------------------------------------------------------------------


def _draw_gap(c):
    c.setFont("Helvetica", 12)
    c.drawString(72, 750, "Alpha")
    c.drawString(200, 750, "Beta")  # 源文件中没有空格，只有渲染间距


def _draw_columns(c):
    c.setFont(CJK_FONT, 12)
    c.drawString(72, 700, "左栏文字")
    c.drawString(330, 700, "右栏文字")  # 两栏同一基线


def _draw_kern(c):
    c.setFont("Helvetica", 12)
    t = c.beginText(72, 700)
    t.textOut("Gamma")
    t.moveCursor(12, 0)
    t.textOut("Delta")
    c.drawText(t)


def _draw_tight(c):
    c.setFont("Helvetica", 12)
    for i in range(3):
        c.drawString(72, 700 - 12 * i, f"Line {i} tight leading")


def _draw_wide(c):
    c.setFont("Helvetica", 12)
    for i in range(3):
        c.drawString(72, 700 - 30 * i, f"Line {i} wide leading")


def _draw_track(c):
    c.setFont("Helvetica", 12)
    t = c.beginText(72, 700)
    t.setCharSpace(3)
    t.textLine("Tracked letters spacing")
    c.drawText(t)


def _draw_wordspace(c):
    c.setFont("Helvetica", 12)
    t = c.beginText(72, 700)
    t.setWordSpace(8)
    t.textLine("Word spacing eight here")
    c.drawText(t)


def _draw_table(c):
    c.setFont("Helvetica", 12)
    c.drawString(72, 700, "Name")
    c.drawString(250, 700, "Value")
    c.drawString(400, 700, "Unit")
    c.drawString(72, 680, "Revenue")
    c.drawString(250, 680, "12.5")
    c.drawString(400, 680, "pct")


def _gen_counts(path: Path):
    chars = _chars(path, "pypdfium2")
    sp = sum(1 for c in chars if c.generated and c.text == " ")
    br = sum(1 for c in chars if c.generated and c.text in ("\r", "\n"))
    return sp, br


@pytest.mark.parametrize(
    "draw, expect_generated_space",
    [
        (_draw_gap, True),
        (_draw_columns, True),
        (_draw_kern, True),
        (_draw_table, True),
        (_draw_tight, False),
        (_draw_wide, False),
        (_draw_track, False),
        (_draw_wordspace, False),
    ],
    ids=["gap", "two-columns", "kern-moveCursor", "table-row", "tight-leading", "wide-leading", "charspace", "wordspace"],
)
def test_pdfium_generated_space_observed_by_sample(make_pdf, draw, expect_generated_space):
    """实测（pypdfium2）：渲染间距较大的同行字符会得到一个 generated 空格；行距、字距、词距变化不产生。"""
    path = make_pdf("gen", draw)
    sp, _ = _gen_counts(path)
    assert (sp > 0) == expect_generated_space


def test_generated_space_is_in_block_text_as_single_space(make_pdf):
    path = make_pdf("gap2", _draw_gap)
    doc = PdfParser("pypdfium2").parse(path)
    assert [b.text for b in doc.blocks] == ["Alpha Beta"]


def test_line_breaks_of_separate_lines_are_generated_and_not_in_text(make_pdf):
    path = make_pdf("tight2", _draw_tight)
    sp, br = _gen_counts(path)
    assert br >= 2  # 行末 \r\n 被标记为 generated（实测每行两个）
    doc = PdfParser("pypdfium2").parse(path)
    assert "\r" not in "".join(b.text for b in doc.blocks)
    assert "\n" not in "".join(b.text for b in doc.blocks)
    assert doc.blocks[0].text == "Line 0 tight leading Line 1 tight leading Line 2 tight leading"


def test_known_limitation_two_columns_on_one_baseline_merge_into_one_line(make_pdf):
    """已知缺陷：M1 为单栏版面，同一基线的两栏会被合并为一行。此测试锁定该行为，不代表正确分栏。"""
    path = make_pdf("cols", _draw_columns)
    doc = PdfParser("pypdfium2").parse(path)
    assert [b.text for b in doc.blocks] == ["左栏文字 右栏文字"]


# ---------------------------------------------------------------------------
# 两后端交叉校验（pdfminer 不产生虚拟空格，因此只比较无此情形的样本）
# ---------------------------------------------------------------------------


def test_crosscheck_block_text_equal_between_backends(make_pdf):
    def draw(c):
        c.setFont(CJK_FONT, 18)
        c.drawString(72, 760, "第一章 总体情况")
        c.setFont(CJK_FONT, 12)
        c.drawString(72, 720, "这是第一段的第一行文字，用来测试换行后的段落拼接是否正确，")
        c.drawString(72, 704, "并且第二行末尾没有句号")
        c.drawString(72, 660, "营收 Revenue 增长 12.5%，2024年。")

    path = make_pdf("cross", draw)
    a = PdfParser("pypdfium2").parse(path)
    b = PdfParser("pdfminer").parse(path)
    assert [x.text for x in a.blocks] == [x.text for x in b.blocks]
    assert [x.kind for x in a.blocks] == [x.kind for x in b.blocks]


def test_crosscheck_known_difference_on_rendered_gap(make_pdf):
    """已知差异（已实测）：pypdfium2 插入虚拟空格，pdfminer 不插入，文本不同。"""
    path = make_pdf("cross-gap", _draw_gap)
    assert _text(path, "pypdfium2") == "Alpha Beta"
    assert _text(path, "pdfminer") == "AlphaBeta"


def test_pdfminer_glyph_boxes_are_in_page_coordinates(make_pdf):
    path = make_pdf("pm-box", _draw_gap)
    chars = [c for c in _chars(path, "pdfminer") if c.text == "A"]
    assert chars
    b = chars[0].bbox
    assert b.x0 == pytest.approx(72.0, abs=0.5)
    assert 0 <= b.y0 <= b.y1 <= A4[1]


# ---------------------------------------------------------------------------
# 不支持的文档、旋转页、许可证隔离
# ---------------------------------------------------------------------------


def test_graphics_only_pdf_is_unsupported(make_pdf):
    def draw(c):
        c.rect(72, 600, 200, 100, fill=1)

    with pytest.raises(UnsupportedDocumentError, match="no text layer"):
        PdfParser().parse(make_pdf("graphics", draw))


def test_garbage_bytes_are_unsupported(tmp_path):
    p = tmp_path / "junk.pdf"
    p.write_bytes(b"this is not a pdf at all\x00\x01")
    with pytest.raises(UnsupportedDocumentError):
        PdfParser().parse(p)


def test_encrypted_pdf_is_unsupported(tmp_path):
    p = tmp_path / "enc.pdf"
    c = canvas.Canvas(str(p), pagesize=A4, encrypt=StandardEncryption("userpw", "ownerpw"))
    c.setFont("Helvetica", 12)
    c.drawString(72, 700, "secret")
    c.save()
    with pytest.raises(UnsupportedDocumentError):
        PdfParser().parse(p)


def test_rotated_page_gives_document_warning(make_pdf):
    def draw(c):
        c.setFont("Helvetica", 12)
        c.drawString(72, 700, "Rotated page text")

    path = make_pdf("rot", draw, rotate=90)
    doc = PdfParser("pypdfium2").parse(path)
    assert any(w.startswith("PAGE_ROTATION_UNSUPPORTED") for w in doc.info.warnings)


def test_pymupdf_backend_is_explicitly_not_implemented():
    with pytest.raises(PdfBackendNotImplementedError, match="AGPL"):
        load_backend("pymupdf")


def test_unknown_backend_name_is_value_error():
    with pytest.raises(ValueError):
        load_backend("nope")


def test_backend_protocol_conformance():
    assert isinstance(load_backend("pypdfium2"), PdfBackend)
    assert isinstance(load_backend("pdfminer"), PdfBackend)


def test_default_import_path_does_not_load_fitz_or_pdf_backends():
    code = (
        "import sys; import citecell.parsing.pdf; "
        "bad = {'fitz', 'pymupdf', 'pypdfium2', 'pdfminer'} & set(sys.modules); "
        "assert not bad, bad; print('ok')"
    )
    env = dict(os.environ, PYTHONPATH=str(SRC))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"


def test_pymupdf_is_not_installed_in_environment():
    import importlib.util

    assert importlib.util.find_spec("fitz") is None
    assert importlib.util.find_spec("pymupdf") is None


# ---------------------------------------------------------------------------
# 已知缺陷（锁定当前行为，不代表正确）
# ---------------------------------------------------------------------------


def test_known_limitation_uniform_single_line_paragraphs_merge(make_pdf):
    """已知缺陷：页内 ≥3 个行距且段间距与行距相同（每段仅一行）时，无法区分段落，全部合并为一块。"""

    def draw(c):
        c.setFont(CJK_FONT, 12)
        for i in range(4):
            c.drawString(72, 700 - 40 * i, f"第{i + 1}段只有一行。")

    doc = PdfParser("pypdfium2").parse(make_pdf("uniform", draw))
    assert [b.text for b in doc.blocks] == ["第1段只有一行。第2段只有一行。第3段只有一行。第4段只有一行。"]


def test_known_limitation_line_end_hyphen_marker_from_pdfium(make_pdf):
    """已知差异/缺陷：pypdfium2 在行尾连字符后（且之后还有续行）输出 U+FFFE，pdfminer 输出 '-'。
    U+FFFE 按可疑字符计入占比，但本块占比低于 5% 阈值，因此不会被标记 TEXT_LAYER_SUSPECT。"""

    def draw(c):
        c.setFont("Helvetica", 12)
        c.drawString(72, 700, "The inter-")
        c.drawString(72, 684, "national market grew.")

    path = make_pdf("hyphen", draw)
    doc = PdfParser("pypdfium2").parse(path)
    assert "￾" in doc.blocks[0].text
    assert "-" not in doc.blocks[0].text
    assert _text(path, "pdfminer").replace("\r", "").replace("\n", "").startswith("The inter-national")
