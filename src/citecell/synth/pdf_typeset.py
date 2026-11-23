"""PDF 自排版：字宽、折行、分页与表格版式，并输出逐字几何记账。

本模块不依赖 reportlab：字体度量由调用方以 FontFace 注入，绘制与坐标换算到 reportlab
原点（左下）在 pdf_writer 中完成。几何统一采用 PDF_COORDS_V1 的约定：页面左上为原点、
x 向右、y 向下、单位 pt（与 models.BBox 一致）。

记账规则（金标准来源，ADR-013）：
* 每个字符输出一个 Glyph：推进框 [x0, x1]，字形框 [top, bottom]（由字体 ascent/descent 给出），基线 baseline；
* 字符按绘制顺序（即 PDF 内容流顺序）排列，同一行的字符共享 line_no；
* 不调用任何解析器。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Sequence

from citecell.synth.ir import ParaIR, Seg, Span, TableIR

log = logging.getLogger("citecell.synth")

A4_WIDTH = 595.2755905511812
A4_HEIGHT = 841.8897637795277

WidthFn = Callable[[str, float], float]
"""(单个字符, 字号 pt) -> 推进宽度 pt。"""


@dataclass(frozen=True)
class FontFace:
    name: str
    ascent: float  # 字形框上沿，每千单位，正值
    descent: float  # 字形框下沿，每千单位，负值
    width: WidthFn


@dataclass(frozen=True)
class TypesetOptions:
    page_width: float = A4_WIDTH
    page_height: float = A4_HEIGHT
    margin: float = 72.0
    body_size: float = 11.0
    heading1_size: float = 18.0
    heading2_size: float = 14.0
    line_factor: float = 1.6  # 行距 = 字号 × line_factor
    para_gap: float = 6.0
    cell_pad: float = 4.0
    table_gap: float = 8.0

    def heading_size(self, level: int) -> float:
        return self.heading1_size if level == 1 else self.heading2_size


@dataclass(frozen=True)
class Glyph:
    page: int
    ch: str
    x0: float  # 推进框左
    x1: float  # 推进框右
    top: float  # 字形框上沿
    bottom: float  # 字形框下沿
    baseline: float  # 基线
    size: float
    line_no: int  # 文档内行序号（同一行共享）
    sid: int | None  # 所属值出现（Span.sid），普通文本为 None


@dataclass
class TypesetPage:
    number: int
    glyphs: list[Glyph]
    rects: list[tuple[float, float, float, float]]  # 表格单元格框 (x0, top, x1, bottom)


@dataclass(frozen=True)
class _Item:
    ch: str
    w: float
    sid: int | None


_NO_BREAK_BEFORE = frozenset("，。、；：！？）》」』】,.;:!?)]}%")
_NO_BREAK_AFTER = frozenset("（《「『【([{¥$")


def _is_word(ch: str) -> bool:
    return ch.isascii() and (ch.isalnum() or ch in "-._/#@")


def _break_ok(prev: str, cur: str) -> bool:
    """两个相邻字符之间是否允许折行。空格归前一个字符（不在空格前折行）。"""
    if cur == " ":
        return False
    if prev == " ":
        return True
    if cur in _NO_BREAK_BEFORE or prev in _NO_BREAK_AFTER:
        return False
    return not (_is_word(prev) and _is_word(cur))


def _last_break(seq: Sequence[_Item]) -> int:
    for i in range(len(seq) - 1, 0, -1):
        if _break_ok(seq[i - 1].ch, seq[i].ch):
            return i
    return -1


def wrap(items: Sequence[_Item], avail: float) -> list[list[_Item]]:
    """贪心折行。不丢弃任何字符（包括空格），保证各行拼接后等于输入序列。

    优先在允许折行处断开；无可断处时在字符间强制断开（如超长的英文串）。
    """
    lines: list[list[_Item]] = []
    cur: list[_Item] = []
    cur_w = 0.0
    for it in items:
        while cur and cur_w + it.w > avail:
            brk = _last_break(cur)
            head = cur[:brk] if brk > 0 else cur
            tail = cur[brk:] if brk > 0 else []
            lines.append(head)
            cur = tail
            cur_w = sum(i.w for i in cur)
        cur.append(it)
        cur_w += it.w
    if cur:
        lines.append(cur)
    return lines


class _Typesetter:
    def __init__(self, face: FontFace, opts: TypesetOptions) -> None:
        self.face = face
        self.o = opts
        self.pages: list[TypesetPage] = []
        self._page: TypesetPage = self._new_page_obj()
        self._y = opts.margin
        self._line_no = 0
        self._bottom = opts.page_height - opts.margin

    # ------------------------------------------------------------ 基础度量
    def _new_page_obj(self) -> TypesetPage:
        return TypesetPage(number=len(self.pages) + 1, glyphs=[], rects=[])

    def _new_page(self) -> None:
        self.pages.append(self._page)
        self._page = self._new_page_obj()
        self._y = self.o.margin

    def _line_metrics(self, size: float) -> tuple[float, float, float]:
        """返回 (字形框上沿到基线, 基线到字形框下沿, 行高)。"""
        asc = self.face.ascent * size / 1000.0
        desc = -self.face.descent * size / 1000.0
        return asc, desc, size * self.o.line_factor

    def _items(self, segs: Sequence[Seg], size: float) -> list[_Item]:
        items: list[_Item] = []
        for seg in segs:
            if isinstance(seg, str):
                for ch in seg:
                    items.append(self._item(ch, size, None))
            else:
                assert isinstance(seg, Span)
                for ch in seg.surface:
                    items.append(self._item(ch, size, seg.sid))
        return items

    def _item(self, ch: str, size: float, sid: int | None) -> _Item:
        if ch in "\r\n\t":
            raise ValueError(f"PDF 写出不支持控制字符 {ch!r}（IR 文本应为单行）")
        return _Item(ch, self.face.width(ch, size), sid)

    def _emit_line(self, items: Sequence[_Item], x: float, line_top: float, size: float) -> None:
        asc, desc, lh = self._line_metrics(size)
        half = (lh - (asc + desc)) / 2.0
        baseline = line_top + half + asc
        top = line_top + half
        bottom = baseline + desc
        self._line_no += 1
        for it in items:
            self._page.glyphs.append(
                Glyph(
                    page=self._page.number,
                    ch=it.ch,
                    x0=x,
                    x1=x + it.w,
                    top=top,
                    bottom=bottom,
                    baseline=baseline,
                    size=size,
                    line_no=self._line_no,
                    sid=it.sid,
                )
            )
            x += it.w

    # ------------------------------------------------------------ 块
    def paragraph(self, blk: ParaIR) -> None:
        o = self.o
        size = o.heading_size(blk.heading_level) if blk.heading_level else o.body_size
        if blk.heading_level:
            self._y += o.para_gap
        _, _, lh = self._line_metrics(size)
        avail = o.page_width - 2 * o.margin
        lines = wrap(self._items(blk.segments, size), avail)
        for line in lines:
            if self._y + lh > self._bottom:
                self._new_page()
            self._emit_line(line, o.margin, self._y, size)
            self._y += lh
        self._y += o.para_gap

    def table(self, tbl: TableIR) -> None:
        o = self.o
        size = o.body_size
        _, _, lh = self._line_metrics(size)
        pad = o.cell_pad
        n_rows = len(tbl.rows)
        n_cols = len(tbl.rows[0])
        colw = (o.page_width - 2 * o.margin) / n_cols
        col_x = [o.margin + i * colw for i in range(n_cols + 1)]

        anchors = {(r0, c0): (r1, c1) for r0, c0, r1, c1 in tbl.merges}
        covered: set[tuple[int, int]] = set()
        for r0, c0, r1, c1 in tbl.merges:
            for r in range(r0, r1 + 1):
                for c in range(c0, c1 + 1):
                    if (r, c) != (r0, c0):
                        covered.add((r, c))
        spans: list[tuple[int, int, int, int]] = []
        for r in range(n_rows):
            for c in range(n_cols):
                if (r, c) in covered:
                    continue
                r1, c1 = anchors.get((r, c), (r, c))
                spans.append((r, c, r1, c1))

        cell_lines: dict[tuple[int, int], list[list[_Item]]] = {}
        row_h = [lh + 2 * pad] * n_rows
        for r0, c0, r1, c1 in spans:
            w = col_x[c1 + 1] - col_x[c0]
            lines = wrap(self._items(tbl.rows[r0][c0], size), w - 2 * pad)
            cell_lines[(r0, c0)] = lines
            need = max(len(lines), 1) * lh + 2 * pad
            if r1 == r0:
                row_h[r0] = max(row_h[r0], need)
        for r0, c0, r1, c1 in spans:  # 纵向合并：不足时加在最后一行
            if r1 > r0:
                need = max(len(cell_lines[(r0, c0)]), 1) * lh + 2 * pad
                total = sum(row_h[r0 : r1 + 1])
                if need > total:
                    row_h[r1] += need - total

        # 纵向合并连通的行组作为整体，不跨页拆分
        end_of = list(range(n_rows))
        for r0, _, r1, _ in spans:
            if r1 > r0:
                for r in range(r0, r1 + 1):
                    end_of[r] = max(end_of[r], r1)
        groups: list[tuple[int, int]] = []
        r = 0
        while r < n_rows:
            g_end = r
            k = r
            while k <= g_end:
                g_end = max(g_end, end_of[k])
                k += 1
            groups.append((r, g_end))
            r = g_end + 1

        for ga, gb in groups:
            gh = sum(row_h[ga : gb + 1])
            if self._y + gh > self._bottom and self._y > o.margin:
                self._new_page()
            group_top = self._y
            for r0, c0, r1, c1 in spans:
                if not (ga <= r0 <= gb):
                    continue
                x0 = col_x[c0]
                x1 = col_x[c1 + 1]
                top = group_top + sum(row_h[ga:r0])
                bottom = top + sum(row_h[r0 : r1 + 1])
                self._page.rects.append((x0, top, x1, bottom))
                for i, line in enumerate(cell_lines[(r0, c0)]):
                    self._emit_line(line, x0 + pad, top + pad + i * lh, size)
            self._y = group_top + gh
        self._y += o.table_gap

    def run(self, blocks: Sequence[ParaIR | TableIR]) -> list[TypesetPage]:
        for blk in blocks:
            if isinstance(blk, ParaIR):
                self.paragraph(blk)
            elif isinstance(blk, TableIR):
                self.table(blk)
            else:  # pragma: no cover - 类型由 DocIR 保证
                raise TypeError(f"未知块类型: {type(blk).__name__}")
        self.pages.append(self._page)
        return self.pages


def typeset(blocks: Sequence[ParaIR | TableIR], face: FontFace, opts: TypesetOptions) -> list[TypesetPage]:
    """把 DocIR 块排版为页面，返回按页序排列的逐字几何。"""
    pages = _Typesetter(face, opts).run(blocks)
    log.info("pdf typeset pages=%d glyphs=%d", len(pages), sum(len(p.glyphs) for p in pages))
    return pages
