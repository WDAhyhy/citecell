"""快照测试：cell_lines / row_lines 的渲染结果与写死的期望字符串完全一致。"""

from citecell.prompting.renderer import render_document_lines


def _fixture_doc(t8):
    blocks = [
        t8.para(1, "第一章 采购", kind=_heading(), section_path=[]),
        t8.para(2, "合同金额为人民币 12,000 元。", section_path=["第一章 采购"]),
        # 表头行（row_index 0）
        t8.cell("A", 1, "产品", row_index=0, col_index=0, is_header=True),
        t8.cell("B", 1, "单价", row_index=0, col_index=1, is_header=True),
        t8.cell("C", 1, "备注", row_index=0, col_index=2, is_header=True),
        # 数据行（row_index 1）
        t8.cell("A", 2, "螺丝M6", row_index=1, col_index=0, column_headers=("产品",)),
        t8.cell("B", 2, "25.00", row_index=1, col_index=1, column_headers=("单价",), row_headers=("螺丝M6",)),
        t8.cell(
            "C",
            2,
            "隐藏备注",
            row_index=1,
            col_index=2,
            column_headers=("备注",),
            row_headers=("螺丝M6",),
            hidden=True,
        ),
    ]
    return t8.build_doc(blocks, sheet_names=[t8.SHEET])


def _heading():
    from citecell.models import BlockKind

    return BlockKind.HEADING


EXPECTED_CELL_LINES = [
    "[p1-b1] # 第一章 采购",
    "[p2-b1] 合同金额为人民币 12,000 元。",
    "[s1-A1] 产品",
    "[s1-B1] 单价",
    "[s1-C1] 备注",
    "[s1-A2 | 产品] 螺丝M6",
    "[s1-B2 | 单价 | 螺丝M6] 25.00",
    "[s1-C2 (隐藏) | 备注 | 螺丝M6] 隐藏备注",
]

EXPECTED_ROW_LINES = [
    "[p1-b1] # 第一章 采购",
    "[p2-b1] 合同金额为人民币 12,000 元。",
    "表 s1「订单」 列: A=产品 | B=单价 | C=备注",
    "[s1-A1] 产品 | [s1-B1] 单价 | [s1-C1] 备注",
    "[s1-A2] 螺丝M6 | [s1-B2] 25.00 | [s1-C2 (隐藏)] 隐藏备注",
]


def test_cell_lines_snapshot(t8):
    doc = _fixture_doc(t8)
    assert render_document_lines(doc, doc.blocks, "cell_lines") == EXPECTED_CELL_LINES


def test_row_lines_snapshot(t8):
    doc = _fixture_doc(t8)
    assert render_document_lines(doc, doc.blocks, "row_lines") == EXPECTED_ROW_LINES


def test_newlines_are_flattened(t8):
    doc = t8.build_doc([t8.para(1, "第一行\n第二行\r\n第三行")])
    lines = render_document_lines(doc, doc.blocks, "cell_lines")
    assert lines == ["[p1-b1] 第一行 第二行  第三行"]


def test_unknown_table_style_rejected(t8):
    doc = _fixture_doc(t8)
    import pytest

    with pytest.raises(ValueError):
        render_document_lines(doc, doc.blocks, "grid")
