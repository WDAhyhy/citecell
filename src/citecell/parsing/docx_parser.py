"""DOCX 解析器（ARCH §5.3、§5.5；T0 结论见 docs/VERIFICATION_T0.md §2.4）。

* 按 body 子元素顺序交错遍历段落（``w:p``）与表格（``w:tbl``），``body_index`` 为该顺序下标（包括空段落）。
* 正文段落：``para{n}``，n 为顶层段落序号（含空段落，保证 ID 稳定）；空白段落不出块。
* 标题：优先 styleId（``Heading1``）与内部名（``heading 1``），中文显示名（``标题 1``）仅作补充。
  section_path 维护标题栈；标题块自身的 section_path 为其上级标题路径（不含自身）。
* 表格：一格一块，``t{m}-r{r}-c{c}``（ID 为 1 起，``DocxLocator.row_index/col_index`` 为 0 起）。
  合并单元格：python-docx 对合并区域会重复返回同一底层 ``w:tc``，按底层元素去重，仅在左上角出块，
  并记录 ``row_span`` / ``col_span``。嵌套表格按行优先拼入外层单元格，标 ``NESTED_TABLE_FLATTENED``。
  同一行内的单元格以 ``\\t`` 连接，行之间以 ``\\n`` 连接。
* 表头（M1 启发式，与 XLSX 一致）：第一行为表头，``column_headers`` 取其在该列覆盖的文本；ARCH 未规定 DOCX 的表头语义，属于本实现选择。
* 不纳入：批注、页眉页脚、文本框、脚注。含修订（``w:ins`` / ``w:del``）或文本框时给文档级警告。
* 解析前校验 zip 容器（条目数、实际解压量、拒绝 DTD/实体声明）。

构造参数：``max_block_chars``（超长块只警告，不切分：块 ID 模式不允许子块后缀，见交付说明）；
zip 限制参数。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from pydantic import ValidationError

from citecell.interfaces import UnsupportedDocumentError
from citecell.models import (
    Block,
    BlockFlag,
    BlockKind,
    DocFormat,
    DocumentInfo,
    DocxLocator,
    ParsedDocument,
    TableContext,
)
from citecell.parsing.common import (
    DEFAULT_MAX_UNCOMPRESSED_BYTES,
    DEFAULT_MAX_ZIP_ENTRIES,
    check_zip_package,
    fingerprint,
)

logger = logging.getLogger("citecell.parsing")

_HEADING_ID = re.compile(r"^heading([1-9])$", re.IGNORECASE)
_HEADING_INTERNAL = re.compile(r"^heading ([1-9])$", re.IGNORECASE)
_HEADING_UI_CN = re.compile(r"^标题 ?([1-9])$")
_STYLE_NAME_MAX = 200


def _heading_level(paragraph: Paragraph) -> int | None:
    """返回标题层级（1–9），非标题返回 None。"""
    try:
        style = paragraph.style
    except Exception:  # 样式缺失时按普通段落处理
        return None
    if style is None:
        return None
    style_id = style.style_id or ""
    m = _HEADING_ID.match(style_id)
    if m:
        return int(m.group(1))
    internal = style.element.name_val or ""
    m = _HEADING_INTERNAL.match(internal)
    if m:
        return int(m.group(1))
    m = _HEADING_UI_CN.match(style.name or "")
    if m:
        return int(m.group(1))
    return None


def _style_name(paragraph: Paragraph) -> str | None:
    try:
        style = paragraph.style
    except Exception:
        return None
    if style is None or not style.name:
        return None
    return style.name[:_STYLE_NAME_MAX]


def _iter_table_cells(table: Table):
    """按行优先遍历表格的底层单元格（去重后）。

    产出 ``(row, col, cell, row_span, col_span)``；只在合并区域的左上角产出一次。
    行列下标为网格下标（0 起），与 python-docx ``row.cells`` 的位置一致。
    """
    grid = [list(row.cells) for row in table.rows]
    keys = [[cell._tc for cell in row] for row in grid]
    seen: set = set()
    for r, row in enumerate(grid):
        for c, cell in enumerate(row):
            tc = keys[r][c]
            if tc in seen:
                continue
            seen.add(tc)
            col_span = 1
            while c + col_span < len(row) and keys[r][c + col_span] == tc:
                col_span += 1
            row_span = 1
            while r + row_span < len(keys) and c < len(keys[r + row_span]) and keys[r + row_span][c] == tc:
                row_span += 1
            yield r, c, cell, row_span, col_span


def _cell_content(cell) -> tuple[str, bool]:
    """单元格文本（段落以 \\n 连接；嵌套表格按行优先拼接）。返回 (文本, 是否含嵌套表格)。"""
    parts: list[str] = []
    nested = False
    for child in cell._tc.iterchildren():
        if child.tag == qn("w:p"):
            text = Paragraph(child, cell).text
            if text:
                parts.append(text)
        elif child.tag == qn("w:tbl"):
            nested = True
            text = _table_text(Table(child, cell))
            if text:
                parts.append(text)
    return "\n".join(parts), nested


def _table_text(table: Table) -> str:
    """嵌套表格的扁平化文本：行内 \\t，行间 \\n。"""
    rows: dict[int, list[str]] = {}
    for r, _c, cell, _rs, _cs in _iter_table_cells(table):
        text, _ = _cell_content(cell)
        rows.setdefault(r, []).append(text)
    return "\n".join("\t".join(cells) for _r, cells in sorted(rows.items()))


class DocxParser:
    """DOCX 解析器，实现 ``citecell.interfaces.Parser``。"""

    name = "docx"
    version = "docx-v1"
    formats = frozenset({DocFormat.DOCX})

    def __init__(
        self,
        *,
        max_block_chars: int = 800,
        max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
        max_zip_entries: int = DEFAULT_MAX_ZIP_ENTRIES,
    ) -> None:
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
        if "word/document.xml" not in names:
            raise UnsupportedDocumentError(f"doc {fp.doc_id}: 不是 Word 文档（缺少 word/document.xml）")
        try:
            document = Document(str(path))
        except Exception as exc:  # 损坏/不支持的内容：统一转换，不泄露路径
            logger.warning("docx 打开失败 doc_id=%s exc=%s", fp.doc_id, type(exc).__name__)
            raise UnsupportedDocumentError(f"doc {fp.doc_id}: 无法读取 Word 文档") from None

        body = document.element.body
        warnings: list[str] = []
        if next(body.iter(qn("w:ins"), qn("w:del")), None) is not None:
            warnings.append("文档含修订痕迹（w:ins/w:del）；解析取当前可见文本，修订处理未核实")
        if next(body.iter(qn("w:txbxContent")), None) is not None:
            warnings.append("文档含文本框；文本框内容未纳入")

        try:
            blocks, long_count = self._build_blocks(document, fp.doc_id)
        except ValidationError:
            # pydantic 错误信息会回显输入值（正文），这里丢弃细节。
            logger.warning("docx 块构造校验失败 doc_id=%s", fp.doc_id)
            raise UnsupportedDocumentError(f"doc {fp.doc_id}: 块结构校验失败") from None

        if long_count:
            warnings.append(
                f"{long_count} 个块超过 max_block_chars={self._max_block_chars}（未切分；块 ID 不支持子块）"
            )

        info = DocumentInfo(
            doc_id=fp.doc_id,
            sha256=fp.sha256,
            format=DocFormat.DOCX,
            size_bytes=fp.size_bytes,
            display_name=display_name,
            parser_name=self.name,
            parser_version=self.version,
            warnings=warnings,
        )
        return ParsedDocument(info=info, blocks=blocks)

    # ------------------------------------------------------------------
    def _build_blocks(self, document, doc_id: str) -> tuple[list[Block], int]:
        body = document.element.body
        body_parent = document._body
        blocks: list[Block] = []
        long_count = 0
        headings: list[tuple[int, str]] = []
        body_index = 0
        para_no = 0
        table_no = 0

        def add(**kwargs) -> None:
            nonlocal long_count
            text = kwargs["text"]
            if len(text) > self._max_block_chars:
                long_count += 1
            blocks.append(Block(doc_id=doc_id, order=len(blocks), **kwargs))

        for child in body.iterchildren():
            if child.tag == qn("w:p"):
                para = Paragraph(child, body_parent)
                para_no += 1
                text = para.text
                if text.strip():
                    level = _heading_level(para)
                    path_now = [t for _lvl, t in headings]
                    if level is not None:
                        while headings and headings[-1][0] >= level:
                            headings.pop()
                        path_now = [t for _lvl, t in headings]
                        headings.append((level, text))
                    add(
                        block_id=f"para{para_no}",
                        kind=BlockKind.HEADING if level is not None else BlockKind.PARAGRAPH,
                        text=text,
                        section_path=path_now,
                        locator=DocxLocator(
                            body_index=body_index,
                            paragraph_number=para_no,
                            style_name=_style_name(para),
                        ),
                    )
                body_index += 1
            elif child.tag == qn("w:tbl"):
                table_no += 1
                table = Table(child, body_parent)
                self._add_table(table, table_no, body_index, [t for _lvl, t in headings], add)
                body_index += 1
        return blocks, long_count

    def _add_table(self, table: Table, table_no: int, body_index: int, section_path: list[str], add) -> None:
        entries = []
        texts: dict[tuple[int, int], str] = {}
        covers: dict[tuple[int, int], tuple[int, int]] = {}
        for r, c, cell, row_span, col_span in _iter_table_cells(table):
            text, nested = _cell_content(cell)
            texts[(r, c)] = text
            for dr in range(row_span):
                for dc in range(col_span):
                    covers[(r + dr, c + dc)] = (r, c)
            entries.append((r, c, text, nested, row_span, col_span))

        def header_for_col(col: int) -> str:
            origin = covers.get((0, col))
            return texts.get(origin, "") if origin else ""

        for r, c, text, nested, row_span, col_span in entries:
            if not text.strip():
                continue
            flags: list[BlockFlag] = []
            if nested:
                flags.append(BlockFlag.NESTED_TABLE_FLATTENED)
            if row_span > 1 or col_span > 1:
                flags.append(BlockFlag.MERGED)
            header = header_for_col(c)
            column_headers = [header] if (r > 0 and header) else []
            add(
                block_id=f"t{table_no}-r{r + 1}-c{c + 1}",
                kind=BlockKind.TABLE_CELL,
                text=text,
                section_path=section_path,
                table=TableContext(
                    table_id=f"t{table_no}",
                    row_index=r,
                    col_index=c,
                    column_headers=column_headers,
                    is_header=(r == 0),
                ),
                locator=DocxLocator(
                    body_index=body_index,
                    table_number=table_no,
                    row_index=r,
                    col_index=c,
                    row_span=row_span,
                    col_span=col_span,
                ),
                flags=flags,
            )
