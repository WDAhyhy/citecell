"""IoU 与三种格式的命中判定（ARCH §9）。"""

from __future__ import annotations

import pytest

from citecell.eval.geometry import evidence_hits_mention, iou, pdf_hit
from citecell.models import BBox, DocxLocator, GoldMention, PdfLocator, XlsxLocator

from .conftest import evidence


def _box(x0, y0, x1, y1) -> BBox:
    return BBox(x0=x0, y0=y0, x1=x1, y1=y1)


def test_iou_identical_is_one():
    assert iou(_box(0, 0, 10, 10), _box(0, 0, 10, 10)) == 1.0


def test_iou_disjoint_is_zero():
    assert iou(_box(0, 0, 10, 10), _box(20, 20, 30, 30)) == 0.0


def test_iou_touching_edges_is_zero():
    assert iou(_box(0, 0, 10, 10), _box(10, 0, 20, 10)) == 0.0


def test_iou_half_overlap_known_answer():
    # a=[0,2]×[0,2] 面积 4；b=[1,3]×[0,2] 面积 4；交 [1,2]×[0,2] 面积 2；并 6 → 1/3
    assert iou(_box(0, 0, 2, 2), _box(1, 0, 3, 2)) == pytest.approx(1 / 3)


def test_iou_is_symmetric():
    a, b = _box(0, 0, 4, 3), _box(2, 1, 7, 5)
    assert iou(a, b) == pytest.approx(iou(b, a))


def test_iou_zero_area_does_not_divide_by_zero():
    assert iou(_box(1, 1, 1, 1), _box(1, 1, 1, 1)) == 0.0


def _pdf(page: int, *boxes: BBox) -> PdfLocator:
    return PdfLocator(page_number=page, page_width=612, page_height=792, regions=list(boxes))


def test_pdf_hit_requires_same_page():
    box = _box(0, 0, 10, 10)
    assert pdf_hit(_pdf(1, box), _pdf(1, box), iou_threshold=0.5)
    assert not pdf_hit(_pdf(1, box), _pdf(2, box), iou_threshold=0.5)


def test_pdf_hit_threshold():
    a = _box(0, 0, 2, 2)
    b = _box(1, 0, 3, 2)  # IoU = 1/3
    assert not pdf_hit(_pdf(1, a), _pdf(1, b), iou_threshold=0.5)
    assert pdf_hit(_pdf(1, a), _pdf(1, b), iou_threshold=0.3)


def _mention(locator, cs=None, ce=None) -> GoldMention:
    return GoldMention(locator=locator, char_start=cs, char_end=ce, surface_text="x")


def test_docx_hit_same_paragraph_and_intersecting_chars():
    ev = evidence("d" * 16, DocxLocator(body_index=0, paragraph_number=1), "abcdefghij")
    ev = ev.model_copy(update={"char_start": 0, "char_end": 10})
    hit = _mention(DocxLocator(body_index=0, paragraph_number=1), 5, 8)
    assert evidence_hits_mention(ev, hit, iou_threshold=0.5)


def test_docx_miss_when_char_intervals_do_not_intersect():
    ev = evidence("d" * 16, DocxLocator(body_index=0, paragraph_number=1), "abcdefghij")
    ev = ev.model_copy(update={"char_start": 0, "char_end": 4})
    miss = _mention(DocxLocator(body_index=0, paragraph_number=1), 6, 9)
    assert not evidence_hits_mention(ev, miss, iou_threshold=0.5)


def test_docx_miss_for_different_paragraph():
    ev = evidence("d" * 16, DocxLocator(body_index=0, paragraph_number=1), "abc")
    other = _mention(DocxLocator(body_index=1, paragraph_number=2), 0, 3)
    assert not evidence_hits_mention(ev, other, iou_threshold=0.5)


def test_docx_table_cell_container():
    cell = DocxLocator(body_index=3, table_number=1, row_index=2, col_index=4)
    ev = evidence("d" * 16, cell, "123")
    same = _mention(DocxLocator(body_index=3, table_number=1, row_index=2, col_index=4), 0, 3)
    other_cell = _mention(DocxLocator(body_index=3, table_number=1, row_index=2, col_index=5), 0, 3)
    assert evidence_hits_mention(ev, same, iou_threshold=0.5)
    assert not evidence_hits_mention(ev, other_cell, iou_threshold=0.5)


def test_docx_gold_without_char_span_matches_whole_container():
    ev = evidence("d" * 16, DocxLocator(body_index=0, paragraph_number=1), "abc")
    whole = _mention(DocxLocator(body_index=0, paragraph_number=1), None, None)
    assert evidence_hits_mention(ev, whole, iou_threshold=0.5)


def test_xlsx_hit_same_cell_only():
    def xl(cell: str, row: int, col: int, sheet: str = "项目") -> XlsxLocator:
        return XlsxLocator(sheet_index=0, sheet_name=sheet, cell=cell, row_number=row, column_number=col)

    ev = evidence("d" * 16, xl("B3", 3, 2), "1200")
    assert evidence_hits_mention(ev, _mention(xl("B3", 3, 2)), iou_threshold=0.5)
    assert not evidence_hits_mention(ev, _mention(xl("C3", 3, 3)), iou_threshold=0.5)
    assert not evidence_hits_mention(ev, _mention(xl("B3", 3, 2, sheet="其他")), iou_threshold=0.5)


def test_format_mismatch_never_hits():
    ev = evidence("d" * 16, _pdf(1, _box(0, 0, 10, 10)), "x")
    docx_mention = _mention(DocxLocator(body_index=0, paragraph_number=1))
    assert not evidence_hits_mention(ev, docx_mention, iou_threshold=0.5)


def test_unlocated_evidence_never_hits():
    ev = evidence("d" * 16, _pdf(1, _box(0, 0, 10, 10)), "x").model_copy(update={"locator": None})
    assert not evidence_hits_mention(ev, _mention(_pdf(1, _box(0, 0, 10, 10))), iou_threshold=0.5)
