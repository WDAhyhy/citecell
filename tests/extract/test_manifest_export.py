"""清单与导出：哈希格式、无绝对路径、依赖版本、CSV/XLSX 形状与状态标记、人工修正追加写。"""

from __future__ import annotations

import asyncio
import csv
import json
import re
from datetime import datetime, timezone

from openpyxl import load_workbook

from citecell.backends import FakeBackend
from citecell.export import (
    append_correction,
    cell_text,
    evidence_table,
    format_value,
    load_corrections,
    values_table,
    write_csv,
    write_xlsx,
)
from citecell.extract import build_pipeline
from citecell.models import (
    Correction,
    DateValue,
    ExtractionRecord,
    Issue,
    IssueCode,
    MoneyValue,
    PercentValue,
    ReviewAction,
    RunConfig,
    Severity,
    Status,
)

HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _run_with(backend, keyset, config, paths, run_id="m"):
    return asyncio.run(build_pipeline(backend, config).run(paths, keyset, config, run_id=run_id))


def _wrong_quote_backend(block_id: str) -> FakeBackend:
    content = json.dumps(
        {"fields": [{"key": "合同编号", "evidence": [{"block_id": block_id, "quote": "HT-XXXX"}], "value": "HT-XXXX"}]},
        ensure_ascii=False,
    )
    return FakeBackend([content], repeat_last=True)


def test_manifest_hashes_and_no_absolute_paths(make_docx, parse, block_id_of, keyset_one, tmp_path):
    path = make_docx()
    backend = FakeBackend(["unused"], repeat_last=True)
    config = RunConfig(backend=backend.info(), max_semantic_retries=0)
    result = _run_with(backend, keyset_one, config, [path])
    manifest = result.manifest
    dumped = json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False)

    for field in (manifest.config_sha256, manifest.keyset_sha256, manifest.output_schema_sha256, manifest.prompt_template_sha256):
        assert HEX64.match(field), field
    for doc in manifest.documents:
        assert HEX64.match(doc.sha256)
    assert str(tmp_path) not in dumped
    assert "contract.docx" not in dumped  # record_filenames 默认关闭
    assert "pypdfium2" in manifest.dependencies
    assert manifest.dependencies["pypdfium2"]
    assert manifest.created_at.tzinfo is not None
    assert manifest.keyset_name == "t-one"


def test_values_and_evidence_tables(make_docx, parse, block_id_of, keyset_one, tmp_path):
    path = make_docx()
    doc = parse(path)
    bid = block_id_of(doc, "HT-2024-001")
    backend = _wrong_quote_backend(bid)
    config = RunConfig(backend=backend.info(), max_semantic_retries=1)
    result = _run_with(backend, keyset_one, config, [path])

    rows = values_table(result, keyset_one)
    assert rows[0] == ["文档", "合同编号"]
    assert len(rows) == 2
    assert rows[1][0] == result.documents[0].info.doc_id
    assert rows[1][1].startswith("【待复核】")  # 两轮都不通过 → 待复核，值仍可见但有标记

    ev = evidence_table(result)
    assert ev[0][:4] == ["文档", "键", "状态", "值"]
    assert len(ev[0]) == 13
    assert ev[1][2] == Status.NEEDS_REVIEW.value

    out_csv = tmp_path / "v.csv"
    write_csv(rows, out_csv)
    with out_csv.open(encoding="utf-8-sig") as fh:
        parsed_rows = list(csv.reader(fh))
    assert parsed_rows == rows

    out_xlsx = tmp_path / "r.xlsx"
    write_xlsx(result, keyset_one, out_xlsx)
    wb = load_workbook(out_xlsx)
    assert wb.sheetnames == ["值", "证据"]
    assert wb["值"]["B2"].value.startswith("【待复核】")


def test_cell_text_markers_and_not_found():
    failed = ExtractionRecord(
        doc_id="0123456789abcdef",
        key="k",
        status=Status.FAILED,
        issues=[Issue(code=IssueCode.BACKEND_ERROR, severity=Severity.ERROR, message="x")],
    )
    assert cell_text(failed) == "【失败】"
    missing = ExtractionRecord(doc_id="0123456789abcdef", key="k", status=Status.NOT_FOUND)
    assert cell_text(missing) == "（未找到）"


def test_value_display_formats():
    assert format_value(MoneyValue(amount=1200000)) == "1,200,000元"
    assert format_value(PercentValue(value=15)) == "15%"
    assert format_value(DateValue(iso="2024-03-05", precision="day")) == "2024-03-05"


def test_formula_injection_is_guarded(tmp_path):
    from citecell.export import _guard

    assert _guard("=HYPERLINK(\"x\")").startswith("'=")
    assert _guard("-1,200元") == "-1,200元"  # 数字样式保持原样
    assert _guard("普通文本") == "普通文本"


def test_corrections_are_appended_not_overwriting(tmp_path):
    log = tmp_path / "corrections.jsonl"
    now = datetime(2026, 11, 23, tzinfo=timezone.utc)
    first = Correction(
        correction_id="c1", run_id="r", doc_id="0123456789abcdef", key="合同编号",
        action=ReviewAction.REJECT, created_at=now,
    )
    second = Correction(
        correction_id="c2", run_id="r", doc_id="0123456789abcdef", key="合同编号",
        action=ReviewAction.CORRECT, value_text="HT-2024-001", created_at=now,
    )
    append_correction(log, first)
    append_correction(log, second)
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2  # 追加，不改写
    loaded = load_corrections(log)
    assert loaded[("0123456789abcdef", "合同编号")].action == ReviewAction.CORRECT
