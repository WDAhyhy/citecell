"""导出（ARCH §2、§11）：results.json 为权威输出；CSV / XLSX 为派生视图。

* ``results.json``：规范 JSON（``citecell.canonical``）。
* ``values``（CSV 与 XLSX 的第一个工作表）：行 = 文档，列 = 键，单元格 = 显示值。
  ``needs_review`` 与 ``failed`` 单元格带明确前缀标记（``【待复核】`` / ``【失败】``），
  ``not_found`` 写为 ``（未找到）``。
* ``evidence``（第二个文件或工作表）：每条证据一行，给出块、匹配级别、定位与引文。
* 人工修正（ADR-010）：``Correction`` 以 JSONL 追加写入；导出时只生成单独的修正表，
  绝不改写机器结果。

公式注入防护只在一个出口统一执行：``write_csv`` 与 ``write_xlsx`` 写入每个文本单元格前调用
``guard_cell``。表格构造函数不做防护，因此新增的列不会漏掉。
"""

from __future__ import annotations

import csv
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from citecell.canonical import canonical_json
from citecell.models import (
    BooleanValue,
    Correction,
    DateValue,
    DocxLocator,
    EnumValue,
    Evidence,
    ExtractionRecord,
    KeySet,
    MoneyValue,
    NormalizedValue,
    NumberValue,
    PdfLocator,
    PercentValue,
    RunResult,
    Status,
    TextValue,
    XlsxLocator,
)

# 可能被电子表格解释为公式的开头字符（ASCII 与全角），以及制表符、回车。
_FORMULA_START = frozenset("=+-@＝＋－＠")
_CONTROL_START = ("\t", "\r")
# 电子表格可能忽略的前导填充字符：零宽字符、BOM、全角空格、不换行空格、ASCII 空格。
_LEADING_FILLER = "​‌‍⁠﻿　  "
# 真正的数字（千分位、小数、百分号、"元"）原样输出，不加防护。
_REAL_NUMBER = re.compile(r"-?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?(%|元)?")


def guard_cell(text: str) -> str:
    """公式注入防护（幂等）。

    * 纯数字白名单原样返回；
    * 去掉前导填充字符后以 ``= + - @``（或全角等价字符）开头，或以制表符/回车开头，
      则在前面加单引号；
    * 已经带单引号的内容不再加第二个（只有公式型开头才加，而 ``'`` 本身不在该集合中）。
    """
    if not text or _REAL_NUMBER.fullmatch(text):
        return text
    core = text.lstrip(_LEADING_FILLER)
    if text.startswith(_CONTROL_START) or (core and core[0] in _FORMULA_START):
        return "'" + text
    return text


def format_value(value: NormalizedValue | None) -> str:
    """规范化值的显示文本（用于表格，不是可再解析的格式）。"""
    if value is None:
        return ""
    if isinstance(value, TextValue):
        return value.text
    if isinstance(value, NumberValue):
        base = format(value.value, ",f")
        return f"{base} {value.unit}" if value.unit else base
    if isinstance(value, MoneyValue):
        amount = format(value.amount, ",f")
        return f"{amount}元" if value.currency == "CNY" else f"{amount} {value.currency}"
    if isinstance(value, PercentValue):
        return f"{format(value.value, 'f')}%"
    if isinstance(value, DateValue):
        return value.iso
    if isinstance(value, EnumValue):
        return value.value
    if isinstance(value, BooleanValue):
        return "是" if value.value else "否"
    return str(value.type)  # pragma: no cover - 未知类型


def cell_text(rec: ExtractionRecord) -> str:
    """values 表中的单元格文本（机器结果，带状态标记）。"""
    shown = format_value(rec.value)
    if rec.status in (Status.GROUNDED, Status.DERIVED):
        return shown
    if rec.status == Status.NOT_FOUND:
        return "（未找到）"
    if rec.status == Status.NEEDS_REVIEW:
        text = shown or rec.value_text or ""
        return f"【待复核】{text}" if text else "【待复核】"
    return "【失败】"


def describe_locator(ev: Evidence) -> str:
    loc = ev.locator
    if loc is None:
        return ""
    if isinstance(loc, PdfLocator):
        return f"第{loc.page_number}页"
    if isinstance(loc, DocxLocator):
        if loc.table_number is not None:
            row = (loc.row_index or 0) + 1
            col = (loc.col_index or 0) + 1
            return f"表{loc.table_number}第{row}行第{col}列"
        return f"段落{loc.paragraph_number}"
    if isinstance(loc, XlsxLocator):
        return f"{loc.sheet_name}!{loc.cell}"
    return ""  # pragma: no cover


VALUE_HEADER_DOC = "文档"


def values_table(result: RunResult, keyset: KeySet) -> list[list[str]]:
    """第一个表：表头 + 每个文档一行（未防护，由写出层统一处理）。"""
    header = [VALUE_HEADER_DOC] + [k.key for k in keyset.keys]
    rows = [header]
    for doc in result.documents:
        by_key = {r.key: r for r in doc.records}
        name = doc.info.display_name or doc.info.doc_id
        row = [name]
        for k in keyset.keys:
            rec = by_key.get(k.key)
            row.append(cell_text(rec) if rec is not None else "")
        rows.append(row)
    return rows


EVIDENCE_HEADER = [
    "文档",
    "键",
    "状态",
    "值",
    "块ID",
    "声明块ID",
    "匹配级别",
    "定位",
    "引文",
    "值区间起",
    "值区间止",
    "尝试ID",
    "问题代码",
]


def evidence_table(result: RunResult) -> list[list[str]]:
    """第二个表：每条证据一行；没有证据的记录保留一行（证据列为空），以免丢失状态。"""
    rows = [list(EVIDENCE_HEADER)]
    width = len(EVIDENCE_HEADER)
    for doc in result.documents:
        for rec in doc.records:
            codes = ";".join(sorted({i.code.value for i in rec.issues}))
            attempts = ";".join(rec.attempt_ids)
            base = [doc.info.doc_id, rec.key, rec.status.value, format_value(rec.value)]
            if not rec.evidence:
                # 列数 = 表头列数：4 个基础列 + 7 个证据列（空）+ 尝试 + 问题代码。
                rows.append(base + [""] * 7 + [attempts, codes])
                continue
            for ev in rec.evidence:
                row = base + [
                    ev.block_id,
                    ev.claimed_block_id or "",
                    ev.match_level.value,
                    describe_locator(ev),
                    ev.quote,
                    "" if ev.value_char_start is None else str(ev.value_char_start),
                    "" if ev.value_char_end is None else str(ev.value_char_end),
                    attempts,
                    codes,
                ]
                rows.append(row)
    assert all(len(r) == width for r in rows)
    return rows


CORRECTION_HEADER = ["文档", "键", "动作", "值", "复核者", "时间", "备注"]


def correction_table(corrections: Mapping[tuple[str, str], Correction]) -> list[list[str]]:
    rows = [list(CORRECTION_HEADER)]
    for (doc_id, key), c in sorted(corrections.items()):
        value = c.value_text or format_value(c.value)
        rows.append([doc_id, key, c.action.value, value, c.reviewer, c.created_at.isoformat(), c.note or ""])
    return rows


def write_results_json(result: RunResult, path: Path) -> None:
    path.write_text(canonical_json(result), encoding="utf-8")


def write_csv(rows: Sequence[Sequence[str]], path: Path) -> None:
    """写出 CSV。所有文本单元格经 ``guard_cell``（唯一出口）。utf-8-sig 便于 Excel 打开中文。"""
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        csv.writer(fh).writerows([[guard_cell(c) for c in row] for row in rows])


def write_xlsx(
    result: RunResult,
    keyset: KeySet,
    path: Path,
    *,
    corrections: Mapping[tuple[str, str], Correction] | None = None,
) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import PatternFill

    wb = Workbook()
    ws_values = wb.active
    ws_values.title = "值"
    review_fill = PatternFill("solid", fgColor="FFF2CC")
    failed_fill = PatternFill("solid", fgColor="F4CCCC")

    _write_rows(ws_values, values_table(result, keyset))
    # 按状态着色：需要按原记录回查状态。
    for r_idx, doc in enumerate(result.documents, start=2):
        by_key = {r.key: r for r in doc.records}
        for c_idx, k in enumerate(keyset.keys, start=2):
            rec = by_key.get(k.key)
            if rec is None:
                continue
            if rec.status == Status.NEEDS_REVIEW:
                ws_values.cell(row=r_idx, column=c_idx).fill = review_fill
            elif rec.status == Status.FAILED:
                ws_values.cell(row=r_idx, column=c_idx).fill = failed_fill

    _write_rows(wb.create_sheet("证据"), evidence_table(result))
    if corrections:
        _write_rows(wb.create_sheet("人工修正"), correction_table(corrections))
    wb.save(path)


def _write_rows(ws: Any, rows: Sequence[Sequence[str]]) -> None:
    """写出 XLSX 行。文本经 ``guard_cell``（唯一出口），并全部显式设为字符串类型，
    使 openpyxl 不把任何内容识别为公式。"""
    for row in rows:
        ws.append([guard_cell(c) if isinstance(c, str) else c for c in row])
    for row in ws.iter_rows():
        for cell in row:
            if isinstance(cell.value, str):
                cell.data_type = "s"


# ---------------------------------------------------------------- 人工修正（追加写）
def append_correction(path: Path, correction: Correction) -> None:
    """追加一条人工复核动作（JSONL，每行一条规范 JSON）。不修改已有行。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(canonical_json(correction) + "\n")


def load_corrections(path: Path) -> dict[tuple[str, str], Correction]:
    """读取修正日志；同一 (文档, 键) 取文件中最后出现的一条（追加顺序即时间顺序）。"""
    out: dict[tuple[str, str], Correction] = {}
    if not path.exists():
        return out
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            c = Correction.model_validate_json(line)
            out[(c.doc_id, c.key)] = c
    return out


__all__ = [
    "append_correction",
    "cell_text",
    "correction_table",
    "describe_locator",
    "evidence_table",
    "format_value",
    "guard_cell",
    "load_corrections",
    "values_table",
    "write_csv",
    "write_results_json",
    "write_xlsx",
]
