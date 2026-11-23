"""导出的公式注入防护与表格形状。

恶意内容可以来自：文档名、键名、值、引文、声明块 ID、XLSX 工作表名、人工修正的说明与复核者。
所有写出的单元格都必须经过同一个出口（write_csv / write_xlsx）的防护。
"""

from __future__ import annotations

import asyncio
import csv
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from openpyxl import load_workbook

from citecell.backends import FakeBackend
from citecell.export import (
    correction_table,
    evidence_table,
    values_table,
    write_csv,
    write_xlsx,
)
from citecell.extract import build_pipeline
from citecell.models import (
    Correction,
    DocumentResult,
    Evidence,
    ExtractionRecord,
    Issue,
    IssueCode,
    KeySet,
    KeySpec,
    LocatorPrecision,
    MatchLevel,
    ReviewAction,
    RunConfig,
    RunResult,
    Severity,
    Status,
    TextValue,
    ValueType,
    XlsxLocator,
)

# 公式前缀：ASCII 与全角。制表符、回车开头也要防护。
FORMULA_START = set("=+-@＝＋－＠\t\r")
FILLER = "​‌‍⁠﻿　  "
# 真正的数字：允许原样输出。
NUMERIC_OK = re.compile(r"-?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?(%|元)?")

PAYLOADS = [
    "=1+1",
    '=HYPERLINK("http://x","c")',
    "+1+1",
    "-1+1",
    "@SUM(1)",
    "-5+cmd|' /C calc'!A0",
    "-(1+1)",
    "--1",
    "+-1",
    "\t=1+1",
    "\r=1+1",
    "​=1+1",
    "﻿=1+1",
    "　=1+1",
    " =1+1",
    "＝1+1",
    "＋1",
    "－1",
    "＠SUM(1)",
]


def _assert_safe(cell: str) -> None:
    if NUMERIC_OK.fullmatch(cell):
        return
    core = cell.lstrip(FILLER)
    assert cell[:1] not in ("\t", "\r"), repr(cell)
    assert core[:1] not in FORMULA_START, repr(cell)


def _build_result(make_docx, payload: str) -> tuple[RunResult, KeySet, dict]:
    path = make_docx()
    backend = FakeBackend(["unused"], repeat_last=True)
    config = RunConfig(backend=backend.info(), max_semantic_retries=0)
    base = asyncio.run(build_pipeline(backend, config).run([path], _keyset("合同编号"), config, run_id="inj"))

    key = payload if _valid_key(payload) else "键" + payload
    keyset = _keyset(key)
    info = base.documents[0].info.model_copy(update={"display_name": payload})
    doc_id = info.doc_id
    ev = Evidence(
        doc_id=doc_id,
        block_id="p1-b1",
        claimed_block_id=payload,
        quote=payload,
        match_level=MatchLevel.EXACT,
        char_start=0,
        char_end=len(payload),
        locator=XlsxLocator(
            sheet_index=0,
            sheet_name=payload,
            cell="B2",
            row_number=2,
            column_number=2,
        ),
        precision=LocatorPrecision.CHAR,
    )
    rec = ExtractionRecord(
        doc_id=doc_id,
        key=key,
        status=Status.NEEDS_REVIEW,
        value=TextValue(text=payload),
        value_text=payload,
        evidence=[ev],
        issues=[Issue(code=IssueCode.VALUE_UNPARSEABLE, severity=Severity.WARNING, message="x")],
    )
    # 第二个键：没有证据的记录（用于检查空证据行的列宽）。
    bare = ExtractionRecord(doc_id=doc_id, key="无证据键", status=Status.NOT_FOUND)
    doc = DocumentResult(info=info, records=[rec, bare])
    result = RunResult(manifest=base.manifest, documents=[doc])
    keyset = KeySet(
        name="inj",
        keys=[KeySpec(key=key, value_type=ValueType.TEXT), KeySpec(key="无证据键", value_type=ValueType.TEXT)],
    )
    corrections = {
        (doc_id, key): Correction(
            correction_id="c1",
            run_id="inj",
            doc_id=doc_id,
            key=key,
            action=ReviewAction.CORRECT,
            value_text=payload,
            reviewer=payload[:64] or "r",
            created_at=datetime(2026, 11, 23, tzinfo=timezone.utc),
            note=payload,
        )
    }
    return result, keyset, corrections


def _keyset(key: str) -> KeySet:
    return KeySet(name="inj", keys=[KeySpec(key=key, value_type=ValueType.TEXT)])


def _valid_key(payload: str) -> bool:
    try:
        KeySpec(key=payload, value_type=ValueType.TEXT)
    except ValueError:
        return False
    return True


def _all_tables(result, keyset, corrections, tmp_path: Path):
    """把全部表写到磁盘，返回 {名称: (写出路径, 行数据)}。"""
    tables = {
        "values": (tmp_path / "values.csv", values_table(result, keyset)),
        "evidence": (tmp_path / "evidence.csv", evidence_table(result)),
        "corrections": (tmp_path / "corrections.csv", correction_table(corrections)),
    }
    for path, rows in tables.values():
        write_csv(rows, path)
    return tables


@pytest.mark.parametrize("payload", PAYLOADS)
def test_csv_cells_never_start_with_formula(make_docx, payload, tmp_path):
    result, keyset, corrections = _build_result(make_docx, payload)
    tables = _all_tables(result, keyset, corrections, tmp_path)
    for name, (path, _rows) in tables.items():
        with path.open(encoding="utf-8-sig", newline="") as fh:
            read_back = list(csv.reader(fh))
        for row in read_back:
            for cell in row:
                _assert_safe(cell)
        assert read_back, name


@pytest.mark.parametrize("payload", PAYLOADS)
def test_xlsx_has_no_formulas_and_string_cells(make_docx, payload, tmp_path):
    result, keyset, corrections = _build_result(make_docx, payload)
    out = tmp_path / "r.xlsx"
    write_xlsx(result, keyset, out, corrections=corrections)

    with zipfile.ZipFile(out) as zf:
        for name in zf.namelist():
            if name.startswith("xl/worksheets/"):
                xml = zf.read(name)
                assert b"<f>" not in xml and b"<f " not in xml, name

    wb = load_workbook(out)
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str):
                    assert cell.data_type == "s", (ws.title, cell.coordinate)
                    _assert_safe(cell.value)


@pytest.mark.parametrize("payload", PAYLOADS)
def test_every_row_matches_header(make_docx, payload, tmp_path):
    result, keyset, corrections = _build_result(make_docx, payload)
    tables = _all_tables(result, keyset, corrections, tmp_path)
    for name, (_path, rows) in tables.items():
        header = rows[0]
        for row in rows:
            assert len(row) == len(header), (name, row)


def test_evidence_row_without_evidence_has_header_width(make_docx, tmp_path):
    result, keyset, corrections = _build_result(make_docx, "x")
    rows = evidence_table(result)
    widths = {len(r) for r in rows}
    assert widths == {len(rows[0])} == {13}


def test_numbers_are_not_modified(tmp_path):
    numbers = ["-1", "-1.5", "12,345", "50%", "120元", "-1,200元", "1200000元"]
    path = tmp_path / "n.csv"
    write_csv([["h"]] + [[n] for n in numbers], path)
    with path.open(encoding="utf-8-sig", newline="") as fh:
        read_back = [row[0] for row in csv.reader(fh)][1:]
    assert read_back == numbers


def test_guard_is_idempotent():
    from citecell.export import guard_cell

    for payload in PAYLOADS + ["普通文本", "'=already"]:
        once = guard_cell(payload)
        assert guard_cell(once) == once, payload
        assert not (once.startswith("''")), payload


def test_guard_does_not_touch_plain_text_or_numbers():
    from citecell.export import guard_cell

    for s in ["普通文本", "-1", "12,345", "50%", "120元"]:
        assert guard_cell(s) == s
