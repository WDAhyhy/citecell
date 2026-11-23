"""DOCX 解析器测试（TASKS.md T3 验收；ARCH §5.3、§5.5）。"""

from __future__ import annotations

import hashlib

import pytest
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from citecell.interfaces import Parser, UnsupportedDocumentError
from citecell.models import BlockFlag, BlockKind, DocxLocator
from citecell.parsing.docx_parser import DocxParser


def build_structure(doc):
    doc.add_heading("合同条款", level=1)
    doc.add_paragraph("第一段")
    doc.add_paragraph("第二段")
    doc.add_paragraph("第三段")
    t = doc.add_table(rows=3, cols=3)
    t.cell(0, 0).merge(t.cell(0, 1))
    t.cell(0, 0).text = "项目"
    t.cell(0, 2).text = "值"
    for r in (1, 2):
        for c in range(3):
            t.cell(r, c).text = f"r{r}c{c}"
    doc.add_paragraph("表后段落")


def test_body_order_ids_and_kinds(make_docx):
    path = make_docx(build_structure)
    parsed = DocxParser().parse(path)
    ids = [b.block_id for b in parsed.blocks]
    assert ids == [
        "para1",
        "para2",
        "para3",
        "para4",
        "t1-r1-c1",
        "t1-r1-c3",
        "t1-r2-c1",
        "t1-r2-c2",
        "t1-r2-c3",
        "t1-r3-c1",
        "t1-r3-c2",
        "t1-r3-c3",
        "para5",
    ]
    assert [b.order for b in parsed.blocks] == list(range(len(parsed.blocks)))
    assert parsed.blocks[0].kind == BlockKind.HEADING
    assert parsed.blocks[0].text == "合同条款"
    assert parsed.blocks[0].section_path == []
    for b in parsed.blocks[1:]:
        assert b.section_path == ["合同条款"], b.block_id
    # 正文段落 body_index 与表格 body_index 按文档顺序递增（段落与表格交错）
    body_indices = [b.locator.body_index for b in parsed.blocks]
    assert body_indices == sorted(body_indices)


def test_merged_cell_emitted_once_with_span(make_docx):
    path = make_docx(build_structure)
    parsed = DocxParser().parse(path)
    by_id = parsed.block_map()
    assert "t1-r1-c2" not in by_id
    merged = by_id["t1-r1-c1"]
    assert merged.kind == BlockKind.TABLE_CELL
    assert merged.text == "项目"
    assert merged.locator.col_span == 2
    assert merged.locator.row_span == 1
    assert BlockFlag.MERGED in merged.flags
    assert by_id["t1-r1-c3"].text == "值"
    assert by_id["t1-r1-c3"].locator.col_index == 2


def test_table_headers_propagate_over_merged_cells(make_docx):
    path = make_docx(build_structure)
    by_id = DocxParser().parse(path).block_map()
    assert by_id["t1-r2-c1"].table.column_headers == ["项目"]
    assert by_id["t1-r2-c2"].table.column_headers == ["项目"]  # 合并表头覆盖第 2 列
    assert by_id["t1-r2-c3"].table.column_headers == ["值"]
    assert by_id["t1-r1-c1"].table.is_header is True
    assert by_id["t1-r2-c1"].table.is_header is False


def test_all_locators_round_trip_validation(make_docx):
    path = make_docx(build_structure)
    parsed = DocxParser().parse(path)
    for b in parsed.blocks:
        DocxLocator.model_validate(b.locator.model_dump())  # 校验器通过


def test_deterministic_and_doc_id_is_sha256_prefix(make_docx):
    path = make_docx(build_structure)
    p = DocxParser()
    first = p.parse(path).model_dump_json()
    second = p.parse(path).model_dump_json()
    assert first == second
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    parsed = p.parse(path)
    assert parsed.info.doc_id == digest[:16]
    assert parsed.info.sha256 == digest
    assert parsed.info.display_name is None
    assert parsed.info.parser_name == "docx"


def test_display_name_only_when_passed(make_docx):
    path = make_docx(build_structure)
    assert DocxParser().parse(path, display_name="合同.docx").info.display_name == "合同.docx"


def test_nested_table_is_flattened_and_flagged(make_docx):
    def builder(doc):
        t = doc.add_table(rows=1, cols=1)
        cell = t.cell(0, 0)
        cell.text = "外层"
        inner = cell.add_table(rows=2, cols=2)
        inner.cell(0, 0).text = "甲"
        inner.cell(0, 1).text = "乙"
        inner.cell(1, 0).text = "丙"
        inner.cell(1, 1).text = "丁"

    parsed = DocxParser().parse(make_docx(builder))
    block = parsed.block_map()["t1-r1-c1"]
    assert BlockFlag.NESTED_TABLE_FLATTENED in block.flags
    assert block.text == "外层\n甲\t乙\n丙\t丁"


def test_heading_detection_by_style_id_and_internal_name(make_docx):
    def builder(doc):
        # 中文显示名（补充识别）
        st = doc.styles.add_style("标题 2", WD_STYLE_TYPE.PARAGRAPH)
        st.style_id = "Biaoti2"
        doc.add_paragraph("章")
        doc.add_paragraph("一级", style="Heading 1")
        p2 = doc.add_paragraph("二级文本", style=st)
        assert p2.text == "二级文本"
        # 样式 id 与内部名不同：内部名 heading 3 也应被识别
        st3 = doc.styles.add_style("Custom Three", WD_STYLE_TYPE.PARAGRAPH)
        st3.style_id = "XYZ"
        st3.element.name_val = "heading 3"
        doc.add_paragraph("三级", style=st3)

    parsed = DocxParser().parse(make_docx(builder))
    kinds = {b.text: b.kind for b in parsed.blocks}
    assert kinds["一级"] == BlockKind.HEADING
    assert kinds["二级文本"] == BlockKind.HEADING
    assert kinds["三级"] == BlockKind.HEADING
    assert kinds["章"] == BlockKind.PARAGRAPH


def test_empty_paragraph_keeps_numbering_but_emits_no_block(make_docx):
    def builder(doc):
        doc.add_paragraph("")
        doc.add_paragraph("有内容")

    parsed = DocxParser().parse(make_docx(builder))
    assert [b.block_id for b in parsed.blocks] == ["para2"]


def test_revision_marks_produce_document_warning(make_docx):
    def builder(doc):
        p = doc.add_paragraph("修订前")
        ins = OxmlElement("w:ins")
        ins.set(qn("w:id"), "1")
        ins.set(qn("w:author"), "tester")
        p._p.append(ins)

    parsed = DocxParser().parse(make_docx(builder))
    assert any("修订" in w for w in parsed.info.warnings)


def test_rejects_entity_declarations_without_expansion(tmp_path, make_docx, zipkit):
    base = make_docx(lambda doc: doc.add_paragraph("正文"))
    bomb = b'<!DOCTYPE w:document [<!ENTITY e "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA">]>'

    def inject(data: bytes) -> bytes:
        decl_end = data.index(b"?>") + 2
        return data[:decl_end] + bomb + data[decl_end:]

    evil = zipkit.rewrite_member(base, tmp_path / "evil.docx", "word/document.xml", inject)
    with pytest.raises(UnsupportedDocumentError, match="DTD"):
        DocxParser().parse(evil)


def test_zip_limits_are_enforced(make_docx):
    path = make_docx(build_structure)
    with pytest.raises(UnsupportedDocumentError):
        DocxParser(max_uncompressed_bytes=1000).parse(path)
    with pytest.raises(UnsupportedDocumentError):
        DocxParser(max_zip_entries=2).parse(path)


def test_non_zip_and_wrong_container_are_rejected(tmp_path):
    junk = tmp_path / "junk.docx"
    junk.write_bytes(b"not a zip at all")
    with pytest.raises(UnsupportedDocumentError):
        DocxParser().parse(junk)


def test_parser_satisfies_protocol():
    assert isinstance(DocxParser(), Parser)
    assert DocxParser.name == "docx"
