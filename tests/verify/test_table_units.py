"""表格纯数字金额的单位来源测试（表头单位 / 单元格数字格式）。

文档在测试中用 openpyxl / python-docx 即时生成，并经 XlsxParser / DocxParser 解析，
因此列表头与数字格式来自真实解析器输出。
"""

from __future__ import annotations

from pathlib import Path

import docx
import openpyxl
import pytest

from citecell.models import (
    Candidate,
    EvidenceClaim,
    ExtractionMethod,
    IssueCode,
    KeySpec,
    Status,
    ValueType,
)
from citecell.parsing import DocxParser, XlsxParser
from citecell.verify import Verifier

MONEY = KeySpec(key="合同金额", value_type=ValueType.MONEY)


def _xlsx(tmp: Path, cells: dict[str, object], formats: dict[str, str] | None = None):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "S1"
    for ref, v in cells.items():
        ws[ref] = v
    for ref, fmt in (formats or {}).items():
        ws[ref].number_format = fmt
    path = tmp / "t.xlsx"
    wb.save(path)
    return XlsxParser().parse(path)


def _docx(tmp: Path, header_rows: list[str], value: str):
    d = docx.Document()
    t = d.add_table(rows=len(header_rows) + 1, cols=1)
    for r, h in enumerate(header_rows):
        t.cell(r, 0).text = h
    t.cell(len(header_rows), 0).text = value
    path = tmp / "t.docx"
    d.save(path)
    return DocxParser().parse(path)


def _verify(doc, block_id: str, quote: str, value: str, spec: KeySpec = MONEY):
    c = Candidate(
        doc_id=doc.info.doc_id,
        key=spec.key,
        value_text=value,
        evidence=[EvidenceClaim(block_id=block_id, quote=quote)],
        method=ExtractionMethod.LLM,
        producer_id="llm:test",
    )
    return Verifier().verify(c, doc, spec)


def _codes(rec) -> set[IssueCode]:
    return {i.code for i in rec.issues}


def _text_block(doc, text: str) -> str:
    return next(b.block_id for b in doc.blocks if b.text == text)


# ---------------------------------------------------------------------------
# 正例
# ---------------------------------------------------------------------------


def test_header_yuan_unit_grounds_bare_number(tmp_path):
    doc = _xlsx(tmp_path, {"A1": "合同金额（元）", "A2": "1345200"})
    rec = _verify(doc, "s1-A2", "1345200", "1345200")
    assert rec.status == Status.GROUNDED
    assert rec.value.amount == 1345200
    assert any(i.code == IssueCode.UNIT_FROM_HEADER and "列表头" in i.message for i in rec.issues)


def test_header_wan_yuan_multiplies_by_1e4(tmp_path):
    doc = _xlsx(tmp_path, {"A1": "金额（万元）", "A2": "130"})
    rec = _verify(doc, "s1-A2", "130", "130")
    assert rec.status == Status.GROUNDED
    assert rec.value.amount == 1300000


def test_number_format_yuan_symbol_grounds_when_no_header_unit(tmp_path):
    doc = _xlsx(tmp_path, {"B1": "合同金额", "B2": 1345200}, formats={"B2": "¥#,##0.00"})
    rec = _verify(doc, "s1-B2", "1345200", "1345200")
    assert rec.status == Status.GROUNDED
    assert rec.value.amount == 1345200
    assert any(i.code == IssueCode.UNIT_FROM_HEADER and "数字格式" in i.message for i in rec.issues)


def test_header_wins_over_number_format(tmp_path):
    doc = _xlsx(tmp_path, {"B1": "金额（万元）", "B2": 130}, formats={"B2": "¥#,##0.00"})
    rec = _verify(doc, "s1-B2", "130", "130")
    assert rec.status == Status.GROUNDED
    assert rec.value.amount == 1300000


def test_multi_level_header_scanned(tmp_path):
    doc = _xlsx(tmp_path, {"A1": "合同金额", "A2": "（万元）", "A3": "130"})
    rec = _verify(doc, "s1-A3", "130", "130")
    assert rec.status == Status.GROUNDED
    assert rec.value.amount == 1300000


def test_docx_table_header_unit_grounds(tmp_path):
    doc = _docx(tmp_path, ["金额（万元）"], "130")
    rec = _verify(doc, _text_block(doc, "130"), "130", "130")
    assert rec.status == Status.GROUNDED
    assert rec.value.amount == 1300000


def test_value_with_own_unit_ignores_header(tmp_path):
    doc = _xlsx(tmp_path, {"A1": "合同金额（元）", "A2": "120万元"})
    rec = _verify(doc, "s1-A2", "120万元", "120万元")
    assert rec.status == Status.GROUNDED
    assert rec.value.amount == 1200000


# ---------------------------------------------------------------------------
# 反例：不得猜测
# ---------------------------------------------------------------------------


def test_conflicting_header_units_stay_unparseable(tmp_path):
    doc = _xlsx(tmp_path, {"A1": "金额（元）", "A2": "金额（万元）", "A3": "130"})
    rec = _verify(doc, "s1-A3", "130", "130")
    assert rec.status == Status.NEEDS_REVIEW
    assert IssueCode.VALUE_UNPARSEABLE in _codes(rec)


def test_no_unit_clue_stays_unparseable(tmp_path):
    doc = _xlsx(tmp_path, {"A1": "金额", "A2": "1345200"})
    rec = _verify(doc, "s1-A2", "1345200", "1345200")
    assert rec.status == Status.NEEDS_REVIEW
    assert IssueCode.VALUE_UNPARSEABLE in _codes(rec)


def test_truncated_bare_number_rejected_when_cell_is_longer(tmp_path):
    doc = _xlsx(tmp_path, {"A1": "合同金额（元）", "A2": "1345200"})
    rec = _verify(doc, "s1-A2", "345200", "345200")
    assert rec.status == Status.NEEDS_REVIEW


def test_truncated_small_number_rejected(tmp_path):
    doc = _xlsx(tmp_path, {"A1": "金额（万元）", "A2": "15"})
    rec = _verify(doc, "s1-A2", "5", "5")
    assert rec.status == Status.NEEDS_REVIEW


def test_header_unit_does_not_override_value_unit_mismatch_guard(tmp_path):
    """值自带单位时，表头单位被忽略：120万元 不因表头“元”被改写为 120 元。"""
    doc = _xlsx(tmp_path, {"A1": "金额（元）", "A2": "120万元"})
    rec = _verify(doc, "s1-A2", "120万元", "120万元")
    assert rec.value.amount == 1200000


def test_unit_bearing_data_cell_above_is_not_a_header(tmp_path):
    """同列上方的数据单元格（含数字，如“120万元”）不得充当表头提供单位。"""
    doc = _xlsx(tmp_path, {"A1": "金额", "A2": "120万元", "A3": "130"})
    rec = _verify(doc, "s1-A3", "130", "130")
    assert rec.status == Status.NEEDS_REVIEW
    assert IssueCode.VALUE_UNPARSEABLE in _codes(rec)
