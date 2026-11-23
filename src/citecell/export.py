"""导出（ARCH §2、§11）：results.json 为权威输出；CSV / XLSX 为派生视图。

* ``results.json``：规范 JSON（``citecell.canonical``）。
* ``values``（CSV 与 XLSX 的第一个工作表）：行 = 文档，列 = 键，单元格 = 显示值。
  ``needs_review`` 与 ``failed`` 单元格带明确前缀标记（``【待复核】`` / ``【失败】``），
  ``not_found`` 写为 ``（未找到）``。
* ``evidence``（第二个文件或工作表）：每条证据一行，给出块、匹配级别、定位与引文。
* 人工修正（ADR-010）：``Correction`` 以 JSONL 追加写入；导出时只生成单独的修正表，
  绝不改写机器结果。

CSV 与 XLSX 中以 ``= + - @`` 开头的文本（非数字）会加前缀 ``'`` 以防公式注入。
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

_FORMULA_PREFIX = ("=", "+", "-", "@", "\t", "\r")
_NUMERIC = re.compile(r"^[+-]?[\d,]*\.?\d+(%|元)?$")

VALUE_HEADER_DOC = "文档"


def _guard(text: str) -> str:
    """防公式注入：可疑前缀且不是纯数字时加单引号。"""
    if text.startswith(_FORMULA_PREFIX) and not _NUMERIC.match(text):
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


def values_table(result: RunResult, keyset: KeySet) -> list[list[str]]:
    """第一个表：表头 + 每个文档一行。"""
    header = [VALUE_HEADER_DOC] + [k.key for k in keyset.keys]
    rows = [header]
    for doc in result.documents:
        by_key = {r.key: r for r in doc.records}
        name = doc.info.display_name or doc.info.doc_id
        row = [_guard(name)]
        for k in keyset.keys:
            rec = by_key.get(k.key)
            row.append(_guard(cell_text(rec)) if rec is not None else "")
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
    """第二个表：每条证据一行；没有证据的记录也保留一行（证据列为空），以免丢失状态。"""
    rows = [list(EVIDENCE_HEADER)]
    for doc in result.documents:
        for rec in doc.records:
            codes = ";".join(sorted({i.code.value for i in rec.issues}))
            attempts = ";".join(rec.attempt_ids)
            base = [doc.info.doc_id, rec.key, rec.status.value, format_value(rec.value)]
            if not rec.evidence:
                rows.append(_guard_row(base + ["", "", "", "", "", "", "", "", attempts, codes]))
                continue
            for ev in rec.evidence:
                rows.append(
                    _guard_row(
                        base
                        + [
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
                    )
                )
    return rows


def _guard_row(row: list[str]) -> list[str]:
    return [_guard(c) if i in (0, 1, 3, 8) else c for i, c in enumerate(row)]


def correction_table(corrections: Mapping[tuple[str, str], Correction]) -> list[list[str]]:
    rows = [["文档", "键", "动作", "值", "复核者", "时间", "备注"]]
    for (doc_id, key), c in sorted(corrections.items()):
        value = c.value_text or format_value(c.value)
        rows.append(
            [
                _guard(doc_id),
                _guard(key),
                c.action.value,
                _guard(value),
                c.reviewer,
                c.created_at.isoformat(),
                _guard(c.note or ""),
            ]
        )
    return rows


def write_results_json(result: RunResult, path: Path) -> None:
    path.write_text(canonical_json(result), encoding="utf-8")


def write_csv(rows: Sequence[Sequence[str]], path: Path) -> None:
    # utf-8-sig：Excel 直接打开中文不乱码。
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        csv.writer(fh).writerows(rows)


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

    value_rows = values_table(result, keyset)
    _write_rows(ws_values, value_rows)
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
    for row in rows:
        ws.append(list(row))
    # 所有单元格强制按文本存储，避免被当作公式。
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
    "load_corrections",
    "values_table",
    "write_csv",
    "write_results_json",
    "write_xlsx",
]
