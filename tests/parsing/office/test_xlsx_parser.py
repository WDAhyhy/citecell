"""XLSX 解析器测试（TASKS.md T3 验收；ARCH §5.4、§5.5）。"""

from __future__ import annotations

import datetime as dt
import hashlib

import pytest

from citecell.interfaces import Parser, UnsupportedDocumentError
from citecell.models import BlockFlag, BlockKind
from citecell.parsing.xlsx_parser import XlsxParser


def build_sample(wb):
    ws = wb.active
    ws.title = "数据"
    ws["A1"] = "名称"
    ws["B1"] = "金额"
    ws.merge_cells("B1:C1")
    ws["A2"] = "甲"
    ws["B2"] = 25.0
    ws["C2"] = 0.15
    ws["C2"].number_format = "0%"
    ws["A3"] = "乙"
    ws["B3"] = dt.datetime(2024, 3, 5)
    ws["B3"].number_format = "yyyy-mm-dd"
    ws["A4"] = "丙"
    ws["B4"] = "=B2*2"  # openpyxl 不写缓存值
    ws["A5"] = "隐藏行"
    ws.row_dimensions[5].hidden = True
    ws["A6"] = "第六行"
    ws["D2"] = "隐藏列D"
    ws.column_dimensions["D"].hidden = True
    ws["E2"] = "区间E"
    ws["F2"] = "区间F"
    ws["G2"] = "区间G"
    # 连续隐藏列以区间 min..max 存储（E..G），以单字母键查询会漏判
    ws.column_dimensions["E"].min = 5
    ws.column_dimensions["E"].max = 7
    ws.column_dimensions["E"].hidden = True
    hidden = wb.create_sheet("隐藏表")
    hidden["A1"] = "机密模板"
    hidden.sheet_state = "hidden"


def test_cell_blocks_text_flags_and_locators(make_xlsx):
    parsed = XlsxParser().parse(make_xlsx(build_sample))
    by_id = parsed.block_map()

    assert by_id["s1-A1"].text == "名称"
    assert by_id["s1-A1"].table.is_header is True
    assert by_id["s1-A1"].table.column_headers == []

    # 合并区域只在左上角出块
    assert "s1-C1" not in by_id
    merged = by_id["s1-B1"]
    assert merged.locator.cell_range == "B1:C1"
    assert BlockFlag.MERGED in merged.flags

    # 合并表头向覆盖的各列传播
    assert by_id["s1-B2"].table.column_headers == ["金额"]
    assert by_id["s1-C2"].table.column_headers == ["金额"]
    assert by_id["s1-A2"].table.column_headers == ["名称"]

    # 0.15 配 0% → 15%
    assert by_id["s1-C2"].text == "15%"
    assert by_id["s1-C2"].locator.number_format == "0%"

    # 整数值浮点 → 整数
    assert by_id["s1-B2"].text == "25"

    # 公式无缓存值
    formula = by_id["s1-B4"]
    assert formula.text == ""
    assert BlockFlag.FORMULA_NO_CACHED_VALUE in formula.flags
    assert BlockFlag.FORMULA in formula.flags
    assert formula.locator.formula == "=B2*2"
    assert formula.locator.formula.startswith("=")

    # 日期
    date = by_id["s1-B3"]
    assert date.text == "2024-03-05"
    assert BlockFlag.DATE_FROM_SERIAL in date.flags
    assert date.locator.raw_value == "45356.0"

    # 所有单元格块都是 table_cell 且 table 不为空
    for b in parsed.blocks:
        assert b.kind == BlockKind.TABLE_CELL
        assert b.table is not None


def test_empty_cells_emit_no_block(make_xlsx):
    parsed = XlsxParser().parse(make_xlsx(build_sample))
    assert "s1-B5" not in parsed.block_map()
    assert "s1-A7" not in parsed.block_map()


def test_hidden_rows_and_columns_are_flagged(make_xlsx):
    by_id = XlsxParser().parse(make_xlsx(build_sample)).block_map()
    assert BlockFlag.HIDDEN in by_id["s1-A5"].flags  # 隐藏行
    assert by_id["s1-A5"].locator.hidden is True
    assert BlockFlag.HIDDEN in by_id["s1-D2"].flags  # 单列隐藏
    for cell in ("s1-E2", "s1-F2", "s1-G2"):  # 连续隐藏区间 E..G
        assert BlockFlag.HIDDEN in by_id[cell].flags, cell
    assert BlockFlag.HIDDEN not in by_id["s1-A6"].flags


def test_hidden_cells_can_be_excluded(make_xlsx):
    parsed = XlsxParser(include_hidden_cells=False).parse(make_xlsx(build_sample))
    by_id = parsed.block_map()
    for cell in ("s1-A5", "s1-D2", "s1-E2", "s1-F2", "s1-G2"):
        assert cell not in by_id, cell
    assert "s1-A6" in by_id


def test_hidden_sheet_excluded_by_default_and_flagged_when_included(make_xlsx):
    path = make_xlsx(build_sample)
    default = XlsxParser().parse(path)
    assert not any(b.locator.sheet_name == "隐藏表" for b in default.blocks)
    assert any("隐藏工作表" in w for w in default.info.warnings)
    assert default.info.sheet_names == ["数据", "隐藏表"]

    included = XlsxParser(include_hidden_sheets=True).parse(path)
    secret = included.block_map()["s2-A1"]
    assert secret.text == "机密模板"
    assert secret.locator.sheet_index == 1  # 下标含隐藏表，与是否纳入无关
    assert BlockFlag.HIDDEN in secret.flags


def test_formula_with_cached_value_is_rendered(tmp_path, make_xlsx, zipkit):
    base = make_xlsx(build_sample)
    path = zipkit.rewrite_member(
        base,
        tmp_path / "cached.xlsx",
        "xl/worksheets/sheet1.xml",
        lambda data: zipkit.inject_cached_value(data, "B4", "50"),
    )
    cell = XlsxParser().parse(path).block_map()["s1-B4"]
    assert cell.text == "50"
    assert BlockFlag.FORMULA in cell.flags
    assert BlockFlag.FORMULA_NO_CACHED_VALUE not in cell.flags


@pytest.mark.parametrize(
    "value, expected",
    [
        (25.0, "25"),
        # openpyxl 写入时只保留约 15 位有效数字，0.1+0.2 在文件中即为 0.3
        (0.1 + 0.2, "0.3"),
        (1e-05, "0.00001"),
        (1.5e20, "150000000000000000000"),
        (-3.5, "-3.5"),
        (12, "12"),
        (True, "TRUE"),
    ],
)
def test_xlsx_text_v1_numeric_rendering(make_xlsx, value, expected):
    def builder(wb):
        wb.active["A1"] = value

    cell = XlsxParser().parse(make_xlsx(builder)).block_map()["s1-A1"]
    assert cell.text == expected
    assert "e" not in cell.text.lower() or cell.text.lower() in ("true", "false")


def test_percent_format_applies_to_integers_too(make_xlsx):
    def builder(wb):
        wb.active["A1"] = 15
        wb.active["A1"].number_format = "0%"

    assert XlsxParser().parse(make_xlsx(builder)).block_map()["s1-A1"].text == "1500%"


def test_date_with_time_keeps_time(make_xlsx):
    def builder(wb):
        wb.active["A1"] = dt.datetime(2024, 3, 5, 10, 30)

    cell = XlsxParser().parse(make_xlsx(builder)).block_map()["s1-A1"]
    assert cell.text == "2024-03-05T10:30:00"
    assert BlockFlag.DATE_FROM_SERIAL in cell.flags


def test_deterministic_and_doc_id(make_xlsx):
    path = make_xlsx(build_sample)
    p = XlsxParser()
    assert p.parse(path).model_dump_json() == p.parse(path).model_dump_json()
    parsed = p.parse(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert parsed.info.doc_id == digest[:16]
    assert parsed.info.display_name is None


def test_rejects_entity_declarations_without_expansion(tmp_path, make_xlsx, zipkit):
    base = make_xlsx(build_sample)
    bomb = b'<!DOCTYPE worksheet [<!ENTITY e "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA">]>'

    def inject(data: bytes) -> bytes:
        # openpyxl 输出的工作表 XML 没有 <?xml?> 声明，DOCTYPE 直接放在根元素之前即可
        return bomb + data

    evil = zipkit.rewrite_member(base, tmp_path / "evil.xlsx", "xl/worksheets/sheet1.xml", inject)
    with pytest.raises(UnsupportedDocumentError, match="DTD"):
        XlsxParser().parse(evil)


def test_zip_limits_are_enforced(make_xlsx):
    path = make_xlsx(build_sample)
    with pytest.raises(UnsupportedDocumentError):
        XlsxParser(max_uncompressed_bytes=1000).parse(path)
    with pytest.raises(UnsupportedDocumentError):
        XlsxParser(max_zip_entries=2).parse(path)


def test_long_block_is_warned_not_split(make_xlsx):
    def builder(wb):
        wb.active["A1"] = "长" * 50

    parsed = XlsxParser(max_block_chars=10).parse(make_xlsx(builder))
    assert len(parsed.blocks) == 1
    assert any("max_block_chars" in w for w in parsed.info.warnings)


def test_parser_satisfies_protocol():
    assert isinstance(XlsxParser(), Parser)
    assert XlsxParser.name == "xlsx"
