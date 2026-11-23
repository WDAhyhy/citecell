"""T8 测试的文档与配置工厂。所有测试文档在运行时由代码构造，不使用真实文档。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from citecell.models import (
    BackendInfo,
    Block,
    BlockFlag,
    BlockKind,
    DocFormat,
    DocumentInfo,
    DocxLocator,
    KeySpec,
    ParsedDocument,
    RunConfig,
    TableContext,
    XlsxLocator,
)

DOC_ID = "abababababababab"
SHEET = "订单"


def make_run_config(**overrides) -> RunConfig:
    backend = BackendInfo(backend_id="fake", provider="fake", model="fake-model")
    return RunConfig(backend=backend, **overrides)


def make_key(name: str, description: str | None = None, aliases: tuple[str, ...] = ()) -> KeySpec:
    return KeySpec(key=name, description=description, aliases=list(aliases))


def para(
    n: int,
    text: str,
    *,
    kind: BlockKind = BlockKind.PARAGRAPH,
    section_path: list[str] | None = None,
) -> Block:
    """正文块，ID 为 p{n}-b1（order 由 build_doc 统一赋值）。"""
    return Block(
        doc_id=DOC_ID,
        block_id=f"p{n}-b1",
        kind=kind,
        text=text,
        order=0,
        section_path=list(section_path or []),
        locator=DocxLocator(body_index=n - 1, paragraph_number=n),
    )


def cell(
    col_letter: str,
    row_number: int,
    text: str,
    *,
    table_id: str = "s1",
    row_index: int,
    col_index: int,
    column_headers: tuple[str, ...] = (),
    row_headers: tuple[str, ...] = (),
    is_header: bool = False,
    hidden: bool = False,
) -> Block:
    flags = [BlockFlag.HIDDEN] if hidden else []
    return Block(
        doc_id=DOC_ID,
        block_id=f"{table_id}-{col_letter}{row_number}",
        kind=BlockKind.TABLE_CELL,
        text=text,
        order=0,
        section_path=[],
        table=TableContext(
            table_id=table_id,
            row_index=row_index,
            col_index=col_index,
            column_headers=list(column_headers),
            row_headers=list(row_headers),
            is_header=is_header,
        ),
        locator=XlsxLocator(
            sheet_index=0,
            sheet_name=SHEET,
            cell=f"{col_letter}{row_number}",
            row_number=row_number,
            column_number=col_index + 1,
            hidden=hidden,
        ),
        flags=flags,
    )


def build_doc(blocks: list[Block], *, sheet_names: list[str] | None = None) -> ParsedDocument:
    """按列表顺序赋值 order，并构造 ParsedDocument。"""
    ordered = [b.model_copy(update={"order": i}) for i, b in enumerate(blocks)]
    info = DocumentInfo(
        doc_id=DOC_ID,
        sha256="ab" * 32,
        format=DocFormat.XLSX if sheet_names else DocFormat.DOCX,
        size_bytes=0,
        parser_name="test",
        parser_version="0",
        sheet_names=sheet_names,
    )
    return ParsedDocument(info=info, blocks=ordered)


@pytest.fixture
def t8():
    """测试工厂命名空间（importlib 模式下测试文件不能直接 import conftest）。"""
    return SimpleNamespace(
        DOC_ID=DOC_ID,
        SHEET=SHEET,
        make_run_config=make_run_config,
        make_key=make_key,
        para=para,
        cell=cell,
        build_doc=build_doc,
    )
