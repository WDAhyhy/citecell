"""定位几何与命中判定（ARCHITECTURE.md §9「定位准确率」「证据支持率」）。

* PDF：同页，且存在一对框 ``IoU >= threshold``。
* DOCX：同段落（正文段落）或同单元格（表格），且字符区间相交；金标准未给出字符区间时，
  仅要求同容器。
* XLSX：同工作表、同单元格（行号与列号相同）。

本模块只做纯函数判定，不读取文档。
"""

from __future__ import annotations

from citecell.models import BBox, Block, DocxLocator, Evidence, GoldMention, PdfLocator, XlsxLocator

BASIS_VALUE = "value"
"""值口径：PDF 用值区间还原的框；DOCX 用值字符区间；XLSX 为单元格（单元格即值）。"""
BASIS_QUOTE = "quote"
"""引文口径（回退）：PDF 用整段引文区域；DOCX 用引文字符区间。"""
BASIS_NONE = "none"
"""证据无定位，不适用。"""


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


def _union(a: BBox, b: BBox) -> BBox:
    return BBox(x0=min(a.x0, b.x0), y0=min(a.y0, b.y0), x1=max(a.x1, b.x1), y1=max(a.y1, b.y1))


def value_regions(block: Block, vstart: int, vend: int) -> list[tuple[int, BBox]] | None:
    """由解析块的逐字符片段还原值区间的框：与 [vstart, vend) 相交的片段按（页, 行）合并为一个框。

    返回按（页, 行）排序的 ``(page_number, bbox)`` 列表；没有相交片段（含块无片段）时返回 None，
    调用方据此回退到引文口径。片段精度为 span/line 时，框会覆盖整个 span（比值宽），这是已知局限。
    """
    frags = [f for f in block.fragments if f.start < vend and f.end > vstart]
    if not frags:
        return None
    groups: dict[tuple[int, int], BBox] = {}
    for f in sorted(frags, key=lambda f: (f.page_number, f.line, f.start)):
        key = (f.page_number, f.line)
        groups[key] = f.bbox if key not in groups else _union(groups[key], f.bbox)
    return [(page, groups[(page, line)]) for page, line in sorted(groups)]


def pdf_value_hit(regions: list[tuple[int, BBox]], gold: PdfLocator, *, iou_threshold: float) -> bool:
    """值区间框命中：与金标准提及同页，且某一对框的 IoU 不低于阈值。"""
    return any(
        page == gold.page_number and iou(box, g) >= iou_threshold
        for page, box in regions
        for g in gold.regions
    )


def localization_basis(ev: Evidence, block: Block | None) -> str:
    """定位口径：PDF 有值区间且块有相交片段 → value，否则 quote；DOCX 有值区间 → value，否则 quote；
    XLSX → value（单元格）；无定位 → none。"""
    loc = ev.locator
    if loc is None:
        return BASIS_NONE
    if isinstance(loc, XlsxLocator):
        return BASIS_VALUE
    if ev.value_char_start is None or ev.value_char_end is None:
        return BASIS_QUOTE
    if isinstance(loc, PdfLocator):
        if block is not None and value_regions(block, ev.value_char_start, ev.value_char_end) is not None:
            return BASIS_VALUE
        return BASIS_QUOTE
    return BASIS_VALUE


def localization_hit(
    ev: Evidence, mention: GoldMention, *, block: Block | None, iou_threshold: float
) -> tuple[bool, str]:
    """定位命中（ARCH §9「定位准确率」）。返回 (是否命中, 实际使用的口径)。

    * PDF 值口径：值区间框与金标准框同页且 IoU ≥ 阈值；回退（无块/无值区间/无片段）：引文区域框。
    * DOCX：同段落或单元格；值口径为值字符区间与金标准字符区间相交，无值区间时回退为引文字符区间。
    * XLSX：同单元格。
    """
    basis = localization_basis(ev, block)
    pl = ev.locator
    gl = mention.locator
    if pl is None or pl.format != gl.format:
        return False, basis
    if isinstance(pl, PdfLocator) and isinstance(gl, PdfLocator):
        if basis == BASIS_VALUE and block is not None:
            regions = value_regions(block, ev.value_char_start, ev.value_char_end)  # type: ignore[arg-type]
            assert regions is not None
            return pdf_value_hit(regions, gl, iou_threshold=iou_threshold), basis
        return pdf_hit(pl, gl, iou_threshold=iou_threshold), basis
    if isinstance(pl, DocxLocator) and isinstance(gl, DocxLocator):
        if docx_container(pl) != docx_container(gl):
            return False, basis
        if basis == BASIS_VALUE:
            return _char_intersect(ev.value_char_start, ev.value_char_end, mention.char_start, mention.char_end), basis
        return _char_intersect(ev.char_start, ev.char_end, mention.char_start, mention.char_end), basis
    if isinstance(pl, XlsxLocator) and isinstance(gl, XlsxLocator):
        same = (pl.sheet_name, pl.row_number, pl.column_number) == (gl.sheet_name, gl.row_number, gl.column_number)
        return same, basis
    return False, basis


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
