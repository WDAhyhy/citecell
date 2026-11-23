"""PDF 写出（reportlab）并记账原生位置。

* 版面由 ``citecell.synth.pdf_typeset`` 自行排版（字宽、折行、分页），本模块只负责绘制；
* 中文使用 reportlab 内置 CID 字体 STSong-Light，**不嵌入、不下载字体**（见 docs/VERIFICATION_T0.md §2.1）；
* 绘制时 reportlab 原点在左下，记账（金标准）统一换算到 PDF_COORDS_V1（页面左上为原点、y 向下）；
* 字节确定性：Canvas 使用 invariant=1（不写入当前时间、固定文件 ID），并固定元数据；
* 金标准位置来自本模块的记账（ADR-013）：每个值出现按 (页, 行) 给出一个 BBox，跨行的值有多框，
  跨页的值拆为多条 Placement（每页一条）。char_start / char_end 为 None（PDF 按框定位）。
"""

from __future__ import annotations

import io
import logging
from collections.abc import Sequence

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen.canvas import Canvas

from citecell.models import BBox, PdfLocator
from citecell.synth.ir import DocIR, ParaIR, Placement, TableIR
from citecell.synth.pdf_typeset import A4_HEIGHT, A4_WIDTH, FontFace, Glyph, TypesetOptions, TypesetPage, typeset

log = logging.getLogger("citecell.synth")

FONT_NAME = "STSong-Light"
DOC_TITLE = "采购合同（合成样本）"
DOC_AUTHOR = "citecell-synth"


def _face() -> FontFace:
    if FONT_NAME not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(FONT_NAME))
    font = pdfmetrics.getFont(FONT_NAME)

    def width(ch: str, size: float) -> float:
        return pdfmetrics.stringWidth(ch, FONT_NAME, size)

    return FontFace(name=FONT_NAME, ascent=float(font.face.ascent), descent=float(font.face.descent), width=width)


def _render(pages: Sequence[TypesetPage], opts: TypesetOptions) -> bytes:
    buf = io.BytesIO()
    canvas = Canvas(buf, pagesize=(opts.page_width, opts.page_height), invariant=1, pageCompression=0)
    canvas.setTitle(DOC_TITLE)
    canvas.setAuthor(DOC_AUTHOR)
    canvas.setCreator(DOC_AUTHOR)
    canvas.setSubject("")
    h = opts.page_height
    for page in pages:
        canvas.setLineWidth(0.5)
        for x0, top, x1, bottom in page.rects:
            # 左上原点 → 左下原点：矩形左下角 y = H - bottom
            canvas.rect(x0, h - bottom, x1 - x0, bottom - top, stroke=1, fill=0)
        for g in page.glyphs:
            if g.ch == " ":  # 空格只占推进宽度，不绘制
                continue
            canvas.setFont(FONT_NAME, g.size)
            canvas.drawString(g.x0, h - g.baseline, g.ch)
        canvas.showPage()
    canvas.save()
    return buf.getvalue()


def _placements(pages: Sequence[TypesetPage], opts: TypesetOptions) -> list[Placement]:
    out: list[Placement] = []
    for page in pages:
        by_sid: dict[int, list[Glyph]] = {}
        for g in page.glyphs:  # dict 保持首次出现顺序
            if g.sid is not None:
                by_sid.setdefault(g.sid, []).append(g)
        for sid, glyphs in by_sid.items():
            by_line: dict[int, list[Glyph]] = {}
            for g in glyphs:
                by_line.setdefault(g.line_no, []).append(g)
            regions = [
                BBox(
                    x0=min(g.x0 for g in lg),
                    y0=min(g.top for g in lg),
                    x1=max(g.x1 for g in lg),
                    y1=max(g.bottom for g in lg),
                )
                for lg in by_line.values()
            ]
            locator = PdfLocator(
                page_number=page.number,
                page_width=opts.page_width,
                page_height=opts.page_height,
                regions=regions,
            )
            out.append(
                Placement(
                    sid=sid,
                    locator=locator,
                    char_start=None,
                    char_end=None,
                    surface_text="".join(g.ch for g in glyphs),
                )
            )
    return out


def write(ir: DocIR, *, options: TypesetOptions | None = None) -> tuple[bytes, list[Placement]]:
    """写出 PDF，返回 (字节, 记账)。ir.blocks 为 DOCX 形状的块（ParaIR / TableIR）。

    ``options`` 仅供测试使用（例如缩窄页宽以制造折行）；默认 A4、页边距 72pt。
    """
    if ir.fmt not in ("docx", "pdf"):
        raise ValueError(f"PDF 写出器不支持 IR 格式 {ir.fmt!r}")
    for blk in ir.blocks:
        if not isinstance(blk, (ParaIR, TableIR)):
            raise TypeError(f"未知块类型: {type(blk).__name__}")
    opts = options or TypesetOptions(page_width=A4_WIDTH, page_height=A4_HEIGHT)
    pages = typeset(ir.blocks, _face(), opts)
    data = _render(pages, opts)
    placements = _placements(pages, opts)
    log.info("pdf written pages=%d mentions=%d", len(pages), len(placements))
    return data, placements
