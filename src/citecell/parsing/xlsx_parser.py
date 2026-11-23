"""XLSX 解析器（ARCH §5.4、§5.5；T0 结论见 docs/VERIFICATION_T0.md §2.5）。

约定：
* 一个非空单元格一块；合并区域只在左上角出块（``cell_range`` 记范围，``MERGED`` 标记）。
  被合并覆盖的单元格没有值（openpyxl 返回 MergedCell），不出块。
* 块 ID：``s{sheet_number}-{A1}``，sheet_number = sheet_index + 1，A1 为左上角地址。
  ``sheet_index`` 是工作簿中**全部**工作表的 0 起下标（含隐藏表），与是否纳入隐藏表无关，保证 ID 稳定。
* 隐藏工作表默认不纳入（``include_hidden_sheets=False``）；隐藏行/列默认纳入并标 ``HIDDEN``
  （``include_hidden_cells=False`` 时整体跳过）。
  openpyxl 把连续隐藏列存为区间 ``min..max``，本实现遍历区间而不是只看单个列字母。
* 公式：``data_only=False`` 取公式，``data_only=True`` 取缓存值。无缓存值 → text 为空串、
  标 ``FORMULA`` 与 ``FORMULA_NO_CACHED_VALUE``。
* XLSX_TEXT_V1：整数值浮点渲染为整数（25.0→25）；其他浮点用最短往返 repr 转 Decimal，``format(..., 'f')``
  输出（不出现科学计数法）；数字格式含 ``%`` 时乘 100 并加 ``%``；日期为 ISO，标 ``DATE_FROM_SERIAL``，
  ``raw_value`` 为序列号的 repr；布尔为 TRUE/FALSE；其余保持原值。``number_format`` 原样记录。
* 表头（M1 启发式）：已用区域内第一行非空行为表头；合并表头向其覆盖的各列传播。
  ``row_headers`` M1 不填（ARCH 未规定）。
* 行列下标 ``TableContext.row_index / col_index`` 相对已用区域左上角，0 起。
* 安全：zip 限制 + 拒绝 DTD/实体声明（见 ``common.check_zip_package``），不依赖 defusedxml 是否安装。

本模块只做解析，不做公式求值。
"""

from __future__ import annotations

import datetime as _dt
import logging
import math
from decimal import Decimal
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.cell.cell import MergedCell
from openpyxl.utils import column_index_from_string, get_column_letter
from openpyxl.utils.datetime import to_excel
from openpyxl.worksheet.formula import ArrayFormula
from openpyxl.worksheet.worksheet import Worksheet
from pydantic import ValidationError

from citecell.interfaces import UnsupportedDocumentError
from citecell.models import (
    Block,
    BlockFlag,
    BlockKind,
    DocFormat,
    DocumentInfo,
    ParsedDocument,
    TableContext,
    XlsxLocator,
)
from citecell.parsing.common import (
    DEFAULT_MAX_UNCOMPRESSED_BYTES,
    DEFAULT_MAX_ZIP_ENTRIES,
    check_zip_package,
    fingerprint,
)

logger = logging.getLogger("citecell.parsing")


def _float_text(value: float) -> str:
    """XLSX_TEXT_V1：浮点渲染。"""
    if not math.isfinite(value):
        return str(value)
    if value.is_integer():
        return str(int(value))
    return format(Decimal(repr(value)), "f")


def _percent_text(value: float) -> str:
    """XLSX_TEXT_V1：含 % 的数字格式，乘 100 后加 %。"""
    scaled = Decimal(repr(value)) * 100
    return format(scaled.normalize(), "f") + "%"


def _date_text(value: _dt.date | _dt.datetime) -> str:
    """ISO 8601：时间部分为零时输出 YYYY-MM-DD（ARCH §5.4），否则输出 datetime.isoformat()。"""
    if isinstance(value, _dt.datetime) and (value.hour or value.minute or value.second or value.microsecond):
        return value.isoformat()
    if isinstance(value, _dt.datetime):
        return value.date().isoformat()
    return value.isoformat()


class _CellRecord:
    __slots__ = ("row", "col", "text", "flags", "formula", "number_format", "raw_value", "cached")

    def __init__(self, row, col, text, flags, formula, number_format, raw_value, cached):
        self.row = row
        self.col = col
        self.text = text
        self.flags = flags
        self.formula = formula
        self.number_format = number_format
        self.raw_value = raw_value
        self.cached = cached


class XlsxParser:
    """XLSX 解析器，实现 ``citecell.interfaces.Parser``。"""

    name = "xlsx"
    version = "xlsx-v1"
    formats = frozenset({DocFormat.XLSX})

    def __init__(
        self,
        *,
        include_hidden_sheets: bool = False,
        include_hidden_cells: bool = True,
        max_block_chars: int = 800,
        max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
        max_zip_entries: int = DEFAULT_MAX_ZIP_ENTRIES,
    ) -> None:
        self._include_hidden_sheets = include_hidden_sheets
        self._include_hidden_cells = include_hidden_cells
        self._max_block_chars = max_block_chars
        self._max_uncompressed_bytes = max_uncompressed_bytes
        self._max_zip_entries = max_zip_entries

    def parse(self, path: Path, *, display_name: str | None = None) -> ParsedDocument:
        path = Path(path)
        fp = fingerprint(path)
        names = check_zip_package(
            path,
            doc_id=fp.doc_id,
            max_uncompressed_bytes=self._max_uncompressed_bytes,
            max_entries=self._max_zip_entries,
        )
        if "xl/workbook.xml" not in names:
            raise UnsupportedDocumentError(f"doc {fp.doc_id}: 不是 Excel 工作簿（缺少 xl/workbook.xml）")
        try:
            wb_formula = load_workbook(str(path), data_only=False)
            wb_cached = load_workbook(str(path), data_only=True)
        except Exception as exc:
            logger.warning("xlsx 打开失败 doc_id=%s exc=%s", fp.doc_id, type(exc).__name__)
            raise UnsupportedDocumentError(f"doc {fp.doc_id}: 无法读取 Excel 工作簿") from None

        try:
            blocks, warnings, sheet_names = self._build(wb_formula, wb_cached, fp.doc_id)
        except ValidationError:
            logger.warning("xlsx 块构造校验失败 doc_id=%s", fp.doc_id)
            raise UnsupportedDocumentError(f"doc {fp.doc_id}: 块结构校验失败") from None

        info = DocumentInfo(
            doc_id=fp.doc_id,
            sha256=fp.sha256,
            format=DocFormat.XLSX,
            size_bytes=fp.size_bytes,
            display_name=display_name,
            sheet_names=sheet_names,
            parser_name=self.name,
            parser_version=self.version,
            warnings=warnings,
        )
        return ParsedDocument(info=info, blocks=blocks)

    # ------------------------------------------------------------------
    def _build(self, wb_formula, wb_cached, doc_id: str):
        blocks: list[Block] = []
        warnings: list[str] = []
        sheet_names = list(wb_formula.sheetnames)
        hidden_sheets = 0
        formula_no_cache = 0
        long_count = 0

        for sheet_index, name in enumerate(sheet_names):
            ws = wb_formula[name]
            sheet_hidden = ws.sheet_state != "visible"
            if sheet_hidden and not self._include_hidden_sheets:
                hidden_sheets += 1
                continue
            if not isinstance(ws, Worksheet):  # 图表工作表没有单元格
                continue
            ws_cached = wb_cached[name]
            records, header_row, covers, merged_tl, n_nocache = self._sheet_records(ws, ws_cached, sheet_hidden)
            formula_no_cache += n_nocache
            table_id = f"s{sheet_index + 1}"
            min_row, min_col = ws.min_row, ws.min_column

            header_text_by_col: dict[int, str] = {}
            if header_row is not None:
                for col in range(min_col, ws.max_column + 1):
                    origin = covers.get((header_row, col), (header_row, col))
                    rec = records.get(origin)
                    if rec is not None and rec.text:
                        header_text_by_col[col] = rec.text

            for (row, col) in sorted(records):
                rec = records[(row, col)]
                cell_ref = f"{get_column_letter(col)}{row}"
                cell_range = merged_tl.get((row, col))
                is_header = header_row is not None and row == header_row
                column_headers: list[str] = []
                if not is_header and header_row is not None and col in header_text_by_col:
                    column_headers = [header_text_by_col[col]]
                text = rec.text
                if len(text) > self._max_block_chars:
                    long_count += 1
                blocks.append(
                    Block(
                        doc_id=doc_id,
                        block_id=f"{table_id}-{cell_ref}",
                        kind=BlockKind.TABLE_CELL,
                        text=text,
                        order=len(blocks),
                        table=TableContext(
                            table_id=table_id,
                            row_index=row - min_row,
                            col_index=col - min_col,
                            column_headers=column_headers,
                            is_header=is_header,
                        ),
                        locator=XlsxLocator(
                            sheet_index=sheet_index,
                            sheet_name=name,
                            cell=cell_ref,
                            cell_range=cell_range,
                            row_number=row,
                            column_number=col,
                            hidden=(BlockFlag.HIDDEN in rec.flags),
                            formula=rec.formula,
                            number_format=rec.number_format,
                            raw_value=rec.raw_value,
                        ),
                        flags=rec.flags,
                    )
                )

        if hidden_sheets:
            warnings.append(f"已排除 {hidden_sheets} 个隐藏工作表（include_hidden_sheets=False）")
        if formula_no_cache:
            warnings.append(f"{formula_no_cache} 个公式单元格无缓存值（未经 Excel/LibreOffice 重算），text 为空")
        if long_count:
            warnings.append(f"{long_count} 个块超过 max_block_chars={self._max_block_chars}（未切分）")
        return blocks, warnings, sheet_names

    def _sheet_records(self, ws, ws_cached, sheet_hidden: bool):
        """读取一个工作表的非空单元格记录。

        返回 (records, header_row, covers, merged_tl, n_no_cache)：
        * records: {(row, col): _CellRecord}，仅含应出块的单元格（已应用隐藏规则）
        * covers: {(row, col): (top_row, top_col)}，合并区域内每个位置 → 左上角
        * merged_tl: {(row, col): "B1:C1"}，仅合并左上角（范围多于一格时）
        """
        hidden_rows = {idx for idx, dim in ws.row_dimensions.items() if dim.hidden}
        hidden_cols: set[int] = set()
        for key, dim in ws.column_dimensions.items():
            if dim.hidden:
                lo = dim.min or column_index_from_string(key)
                hi = dim.max or lo
                hidden_cols.update(range(lo, hi + 1))

        covers: dict[tuple[int, int], tuple[int, int]] = {}
        merged_tl: dict[tuple[int, int], str] = {}
        for cr in ws.merged_cells.ranges:
            for r in range(cr.min_row, cr.max_row + 1):
                for c in range(cr.min_col, cr.max_col + 1):
                    covers[(r, c)] = (cr.min_row, cr.min_col)
            if cr.min_row != cr.max_row or cr.min_col != cr.max_col:
                merged_tl[(cr.min_row, cr.min_col)] = cr.coord

        records: dict[tuple[int, int], _CellRecord] = {}
        n_nocache = 0
        if ws.max_row < ws.min_row or ws.max_column < ws.min_column:
            return records, None, covers, merged_tl, n_nocache

        region = dict(min_row=ws.min_row, max_row=ws.max_row, min_col=ws.min_column, max_col=ws.max_column)
        cached_rows = ws_cached.iter_rows(**region)
        for row_cells, row_cached in zip(ws.iter_rows(**region), cached_rows):
            for cell, cached_cell in zip(row_cells, row_cached):
                if isinstance(cell, MergedCell):
                    continue
                row, col = cell.row, cell.column
                row_hidden = row in hidden_rows
                col_hidden = col in hidden_cols
                cell_hidden = row_hidden or col_hidden
                if cell_hidden and not self._include_hidden_cells:
                    continue
                rec = self._render_cell(cell, cached_cell, ws_cached.parent.epoch, cell_hidden or sheet_hidden)
                if rec is None:
                    continue
                if rec.formula is not None and rec.cached is None:
                    n_nocache += 1
                if (row, col) in merged_tl:
                    rec.flags.append(BlockFlag.MERGED)
                records[(row, col)] = rec

        header_row = None
        for row, _col in sorted(records):
            if records[(row, _col)].text:
                header_row = row
                break
        return records, header_row, covers, merged_tl, n_nocache

    def _render_cell(self, cell, cached_cell, epoch, hidden: bool) -> _CellRecord | None:
        value = cell.value
        formula: str | None = None
        if cell.data_type == "f" or isinstance(value, ArrayFormula):
            formula = value.text if isinstance(value, ArrayFormula) else str(value)
            cached = cached_cell.value
        else:
            cached = value
            if cached is None or (isinstance(cached, str) and cached == ""):
                return None

        number_format = cell.number_format
        flags: list[BlockFlag] = []
        if hidden:
            flags.append(BlockFlag.HIDDEN)
        if formula is not None:
            flags.append(BlockFlag.FORMULA)

        raw_value: str | None = None
        if cached is None:
            text = ""
            flags.append(BlockFlag.FORMULA_NO_CACHED_VALUE)
        elif isinstance(cached, bool):
            text = "TRUE" if cached else "FALSE"
        elif isinstance(cached, (_dt.datetime, _dt.date)):
            text = _date_text(cached)
            flags.append(BlockFlag.DATE_FROM_SERIAL)
            try:
                raw_value = repr(float(to_excel(cached, epoch)))
            except (TypeError, ValueError):
                raw_value = None
        elif isinstance(cached, _dt.time):
            text = cached.isoformat()
        elif isinstance(cached, (int, float)) and "%" in (number_format or ""):
            text = _percent_text(float(cached))
        elif isinstance(cached, float):
            text = _float_text(cached)
        elif isinstance(cached, int):
            text = str(cached)
        else:
            text = str(cached)

        if formula is None and text == "":
            return None
        return _CellRecord(
            row=cell.row,
            col=cell.column,
            text=text,
            flags=flags,
            formula=formula,
            number_format=number_format,
            raw_value=raw_value,
            cached=cached,
        )
