"""定位几何与命中判定（ARCHITECTURE.md §9「定位准确率」「证据支持率」）。

* PDF：同页，且存在一对框 ``IoU >= threshold``。
* DOCX：同段落（正文段落）或同单元格（表格），且字符区间相交；金标准未给出字符区间时，
  仅要求同容器。
* XLSX：同工作表、同单元格（行号与列号相同）。

本模块只做纯函数判定，不读取文档。
"""

from __future__ import annotations

from citecell.models import BBox, DocxLocator, Evidence, GoldMention, PdfLocator, XlsxLocator


def iou(a: BBox, b: BBox) -> float:
    """两个 PDF 框的交并比（IoU）。

    定义：``area(a ∩ b) / area(a ∪ b)``。完全重合为 1.0，不相交（含仅边界接触）为 0.0。
    若并集面积为 0（两框均为零面积），返回 0.0，避免除零；这种情况不计为命中。
    """
    ix = min(a.x1, b.x1) - max(a.x0, b.x0)
    iy = min(a.y1, b.y1) - max(a.y0, b.y0)
    inter = max(ix, 0.0) * max(iy, 0.0)
    area_a = (a.x1 - a.x0) * (a.y1 - a.y0)
    area_b = (b.x1 - b.x0) * (b.y1 - b.y0)
    union = area_a + area_b - inter
    if union <= 0.0:
        return 0.0
    return inter / union


def pdf_hit(pred: PdfLocator, gold: PdfLocator, *, iou_threshold: float) -> bool:
    """PDF 命中：同页，且某一对矩形的 IoU 不低于阈值。"""
    if pred.page_number != gold.page_number:
        return False
    return any(iou(p, g) >= iou_threshold for p in pred.regions for g in gold.regions)


def docx_container(loc: DocxLocator) -> tuple[object, ...]:
    """DOCX 定位的容器标识：单元格用 (table, row, col)，正文段落用 (paragraph,)。"""
    if loc.table_number is not None:
        return ("table", loc.table_number, loc.row_index, loc.col_index)
    return ("paragraph", loc.paragraph_number)


def _char_intersect(ps: int | None, pe: int | None, gs: int | None, ge: int | None) -> bool:
    # 金标准未给出字符区间：视为整个容器，只要预测有区间即相交。
    if gs is None or ge is None:
        return True
    if ps is None or pe is None:
        return False
    return max(ps, gs) < min(pe, ge)


def evidence_hits_mention(ev: Evidence, mention: GoldMention, *, iou_threshold: float) -> bool:
    """证据是否落在某个金标准提及（mention）上。格式不一致或证据无定位时返回 False。"""
    pl = ev.locator
    gl = mention.locator
    if pl is None or pl.format != gl.format:
        return False
    if isinstance(pl, PdfLocator) and isinstance(gl, PdfLocator):
        return pdf_hit(pl, gl, iou_threshold=iou_threshold)
    if isinstance(pl, DocxLocator) and isinstance(gl, DocxLocator):
        return docx_container(pl) == docx_container(gl) and _char_intersect(
            ev.char_start, ev.char_end, mention.char_start, mention.char_end
        )
    if isinstance(pl, XlsxLocator) and isinstance(gl, XlsxLocator):
        return (pl.sheet_name, pl.row_number, pl.column_number) == (
            gl.sheet_name,
            gl.row_number,
            gl.column_number,
        )
    return False


__all__ = ["docx_container", "evidence_hits_mention", "iou", "pdf_hit"]
