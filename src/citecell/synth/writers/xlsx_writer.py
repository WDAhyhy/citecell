"""XLSX 写出（openpyxl）并记账单元格位置。

记账：工作表 0、单元格 A1 地址、行列号与隐藏标记；char_start/char_end 为 None（整格）。
"""

from __future__ import annotations

import io

from openpyxl import Workbook
from openpyxl.utils import get_column_letter

from citecell.models import XlsxLocator
from citecell.synth.determinism import FIXED_DT, canonicalize_zip
from citecell.synth.ir import DocIR, Placement, Span


def write(ir: DocIR) -> tuple[bytes, list[Placement]]:
    """写出 XLSX，返回 (规范化后的文件字节, 按行列顺序排列的记账)。"""
    wb = Workbook()
    ws = wb.active
    ws.title = ir.sheet_name
    props = wb.properties
    props.creator = "citecell-synth"
    props.lastModifiedBy = "citecell-synth"
    props.title = "采购要素（合成样本）"
    props.created = FIXED_DT
    props.modified = FIXED_DT

    placements: list[Placement] = []
    for r_idx, row in enumerate(ir.rows, start=1):
        if row.hidden:
            ws.row_dimensions[r_idx].hidden = True
        for c_idx, cell in enumerate(row.cells, start=1):
            if cell is None:
                continue
            if isinstance(cell, Span):
                target = ws.cell(row=r_idx, column=c_idx, value=cell.cell_value)
                if cell.number_format:
                    target.number_format = cell.number_format
                loc = XlsxLocator(
                    sheet_index=0,
                    sheet_name=ir.sheet_name,
                    cell=f"{get_column_letter(c_idx)}{r_idx}",
                    row_number=r_idx,
                    column_number=c_idx,
                    hidden=row.hidden,
                    number_format=cell.number_format,
                )
                placements.append(Placement(cell.sid, loc, None, None, cell.surface))
            else:
                ws.cell(row=r_idx, column=c_idx, value=cell)

    buf = io.BytesIO()
    wb.save(buf)
    return canonicalize_zip(buf.getvalue()), placements
