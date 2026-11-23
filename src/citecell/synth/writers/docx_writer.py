"""DOCX 写出（python-docx）并记账原生位置。

记账规则（金标准来源，ADR-013）：
* 正文顶层元素按文档顺序计数 → body_index（段落与表格交错，0 起）；
* 正文顶层段落计数 → paragraph_number（1 起，含标题段）；
* 表格计数 → table_number（1 起），单元格给 row_index / col_index（0 起，含表头行）；
* char_start / char_end 为该段落（或单元格唯一段落）文本内的码点区间，半开。
"""

from __future__ import annotations

import io
from typing import Callable

from docx import Document
from docx.table import _Cell
from docx.text.paragraph import Paragraph

from citecell.models import DocxLocator
from citecell.synth.determinism import FIXED_DT, canonicalize_zip
from citecell.synth.ir import DocIR, ParaIR, Placement, Seg, Span, TableIR


def _fill(par: Paragraph, segs: list[Seg], locator: Callable[[], DocxLocator], out: list[Placement]) -> None:
    """把段列表写入段落，并逐个记下值出现的字符区间。"""
    pos = 0
    for seg in segs:
        if isinstance(seg, str):
            if seg:
                par.add_run(seg)
                pos += len(seg)
            continue
        assert isinstance(seg, Span)
        par.add_run(seg.surface)
        end = pos + len(seg.surface)
        out.append(Placement(seg.sid, locator(), pos, end, seg.surface))
        pos = end


def _reset_cell(cell: _Cell) -> Paragraph:
    """保留单元格的第一个段落，删除其余段落（合并单元格后 python-docx 可能留下空段落）。"""
    extra = cell.paragraphs[1:]
    for p in extra:
        p._element.getparent().remove(p._element)
    return cell.paragraphs[0]


def write(ir: DocIR) -> tuple[bytes, list[Placement]]:
    """写出 DOCX，返回 (规范化后的文件字节, 按文档顺序排列的记账)。"""
    doc = Document()
    props = doc.core_properties
    props.author = "citecell-synth"
    props.last_modified_by = "citecell-synth"
    props.title = "采购合同（合成样本）"
    props.created = FIXED_DT
    props.modified = FIXED_DT
    props.revision = 1

    placements: list[Placement] = []
    body_index = 0
    para_no = 0
    table_no = 0

    for blk in ir.blocks:
        if isinstance(blk, ParaIR):
            para_no += 1
            par = doc.add_paragraph(style=f"Heading {blk.heading_level}" if blk.heading_level else None)
            style_name = par.style.name
            bi, pn = body_index, para_no
            _fill(
                par,
                blk.segments,
                lambda bi=bi, pn=pn, sn=style_name: DocxLocator(body_index=bi, paragraph_number=pn, style_name=sn),
                placements,
            )
            body_index += 1
        else:
            assert isinstance(blk, TableIR)
            table_no += 1
            n_rows = len(blk.rows)
            n_cols = len(blk.rows[0])
            table = doc.add_table(rows=n_rows, cols=n_cols)
            table.style = "Table Grid"
            for r0, c0, r1, c1 in blk.merges:
                table.cell(r0, c0).merge(table.cell(r1, c1))
            covered: set[tuple[int, int]] = set()
            for r0, c0, r1, c1 in blk.merges:
                for r in range(r0, r1 + 1):
                    for c in range(c0, c1 + 1):
                        if (r, c) != (r0, c0):
                            covered.add((r, c))
            bi, tn = body_index, table_no
            for r, row in enumerate(blk.rows):
                for c, segs in enumerate(row):
                    if (r, c) in covered:
                        continue
                    par = _reset_cell(table.cell(r, c))
                    rr, cc = r, c
                    _fill(
                        par,
                        segs,
                        lambda bi=bi, tn=tn, rr=rr, cc=cc: DocxLocator(
                            body_index=bi, table_number=tn, row_index=rr, col_index=cc, style_name="Normal"
                        ),
                        placements,
                    )
            body_index += 1

    buf = io.BytesIO()
    doc.save(buf)
    return canonicalize_zip(buf.getvalue()), placements
