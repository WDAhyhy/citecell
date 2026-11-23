"""XLSX 写出（openpyxl）并记账单元格位置。

记账：工作表序号（主表为 0，附加表依次递增）、单元格 A1 地址、行列号与隐藏标记；
char_start/char_end 为 None（整格）。
"""

from __future__ import annotations

import io

from openpyxl import Workbook
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from citecell.models import XlsxLocator
from citecell.synth.determinism import FIXED_DT, canonicalize_zip
from citecell.synth.ir import DocIR, Placement, SheetRow, Span


def _write_sheet(ws: Worksheet, sheet_index: int, name: str, rows: list[SheetRow], out: list[Placement]) -> None:
    for r_idx, row in enumerate(rows, start=1):
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
                    sheet_index=sheet_index,
                    sheet_name=name,
                    cell=f"{get_column_letter(c_idx)}{r_idx}",
                    row_number=r_idx,
                    column_number=c_idx,
                    hidden=row.hidden,
                    number_format=cell.number_format,
                )
                out.append(Placement(cell.sid, loc, None, None, cell.surface))
            else:
                ws.cell(row=r_idx, column=c_idx, value=cell)


def write(ir: DocIR) -> tuple[bytes, list[Placement]]:
    """写出 XLSX，返回 (规范化后的文件字节, 按工作表与行列顺序排列的记账)。"""
    wb = Workbook()
    props = wb.properties
    props.creator = "citecell-synth"
    props.lastModifiedBy = "citecell-synth"
    props.title = "采购要素（合成样本）"
    props.created = FIXED_DT
    props.modified = FIXED_DT

    sheets = [(ir.sheet_name, ir.rows)] + [(s.name, s.rows) for s in ir.extra_sheets]
    placements: list[Placement] = []
    for idx, (name, rows) in enumerate(sheets):
        if idx == 0:
            ws = wb.active
            ws.title = name
        else:
            ws = wb.create_sheet(title=name)
        _write_sheet(ws, idx, name, rows, placements)

    buf = io.BytesIO()
    wb.save(buf)
    return canonicalize_zip(buf.getvalue()), placements
