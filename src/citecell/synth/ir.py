"""合成器中间表示（DocIR）。

Realizer 产出 DocIR；Writer 逐个写出并记账（Placement），GoldBuilder 由记账结果生成金标准。
Writer 的记账是金标准位置的唯一来源，被测解析器不参与（ADR-013）。

PDF 写出（T5b）应提供同名入口 ``write(ir: DocIR) -> tuple[bytes, list[Placement]]``；
若需要不同的版面信息，由 T5b 在 pdf_typeset.py 中扩展，不改动本文件的既有字段。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Union

from pydantic import BaseModel


@dataclass(frozen=True)
class Span:
    """文档中一处值的出现（值或干扰值）。``sid`` 在单个文档内唯一。

    surface：文档中逐字出现的文本（DOCX 为段落/单元格片段；XLSX 为单元格的 XLSX_TEXT_V1 文本）。
    cell_value / number_format：仅 XLSX 使用，写入单元格的原始 Python 值与数字格式。
    """

    sid: int
    key: str
    role: str  # "value" | "distractor"
    surface: str
    cell_value: Any = None
    number_format: str | None = None
    tags: tuple[str, ...] = ()


Seg = Union[str, Span]
"""段内片段：普通文本或一处值出现。"""


@dataclass
class ParaIR:
    segments: list[Seg]
    heading_level: int = 0  # 0 表示正文段落


@dataclass
class TableIR:
    """表格：rows[r][c] 为该单元格的段列表（单段落）。merges 为 (r0, c0, r1, c1)，含端点。"""

    rows: list[list[list[Seg]]]
    merges: list[tuple[int, int, int, int]] = field(default_factory=list)


@dataclass
class SheetRow:
    cells: list[Any]  # 每格：None、普通 Python 值，或 Span
    hidden: bool = False


@dataclass
class DocIR:
    fmt: str  # "docx" | "xlsx"
    layout: str  # fixed | perturbed | free
    blocks: list[ParaIR | TableIR] = field(default_factory=list)  # DOCX
    sheet_name: str = "采购要素"  # XLSX
    rows: list[SheetRow] = field(default_factory=list)  # XLSX，第 0 行为表头
    spans: list[Span] = field(default_factory=list)  # 所有值出现，按创建顺序


@dataclass(frozen=True)
class Placement:
    """Writer 记账的一处出现：原生定位 + 字符区间（DOCX）+ 文本。"""

    sid: int
    locator: BaseModel
    char_start: int | None
    char_end: int | None
    surface_text: str


def block_signature(ir: DocIR) -> tuple:
    """块结构签名：用于比较不同 layout 的文档结构是否不同。"""
    if ir.fmt == "xlsx":
        return ("sheet", len(ir.rows))
    sig: list[tuple] = []
    for blk in ir.blocks:
        if isinstance(blk, ParaIR):
            sig.append(("p", blk.heading_level))
        else:
            sig.append(("t", len(blk.rows[0]) if blk.rows else 0))
    return tuple(sig)
