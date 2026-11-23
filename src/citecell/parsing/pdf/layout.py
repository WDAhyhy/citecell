"""与后端无关的 PDF 版面分析：PdfChar 流 → 行 → 视觉段落 → Block（ARCH §5.1、§5.2）。

版面规则（均为启发式，单栏、无表格恢复；已知局限见 tests 与最终回复）：

1. 字符按竖向重叠（≥ 较小框高的 50%）聚为行；行内按 x 排序。
2. 虚拟字符：``\\r``/``\\n`` 直接丢弃（行边界由几何决定）；虚拟空格与真实空格统一折叠为
   一个半角空格，且只出现在同一行内两个可见字符之间（不在行首行尾）。
3. 行间拼接 LINE_JOIN_V1：前后两侧均为 CJK 字符或 CJK 标点时直接相连，否则插入一个半角空格
   （前后已有空白时不重复插入）。插入的空格没有 TextFragment。
4. 分段（三条规则，任一成立即另起段）：
   * 行距 > 1.25 × 本页中位行距（段间距）；
   * 字号变化超过 10%（标题与正文的分界）；
   * 首行缩进 ≥ 1.5 em，且上一行明显未满行（右端距栏右缘 ≥ 4 em）。
5. 标题：段落字号 ≥ 本页正文中位字号 × 1.15 的段落为 HEADING，section_path 由标题栈维护。
6. 段落不跨页（PdfLocator 只能是单页）。超过 max_block_chars 的段落按句末标点切分，
   每段标记 SPLIT_FROM_LONG_BLOCK；没有句末标点时按空白切分，再不行则硬切。
7. 私有区字符或 U+FFFD 占比超过 5% 的块标记 TEXT_LAYER_SUSPECT。
"""

from __future__ import annotations

import statistics
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from citecell.models import (
    BBox,
    Block,
    BlockFlag,
    BlockKind,
    PdfChar,
    PdfLocator,
    PdfPageInfo,
    TextFragment,
)

LAYOUT_VERSION = "LAYOUT_V1"
JOIN_RULE = "LINE_JOIN_V1"
DEFAULT_MAX_BLOCK_CHARS = 800
SUSPECT_RATIO = 0.05

_LINE_OVERLAP = 0.5
_PITCH_BREAK = 1.25
_SIZE_BREAK = 0.10
_INDENT_EM = 1.5
_SHORT_EM = 4.0
_HEADING_RATIO = 1.15
_SENT_END = frozenset("。！？；!?;…")
#: 跨页续接：句末标点与右括号类（以这些字符结束的块不视为被截断）。
CONT_TERMINAL = frozenset("。！？；：.!?;:」』”）)】》]}")
#: 下一页第一块的起点 y0 须位于页高的前 15% 内（页面上部边距区）。
CONT_TOP_FRACTION = 0.15
#: 两块字号差的容许值（pt）；超出视为版式变化，不标记。
CONT_SIZE_TOL_PT = 1.0


# ---------------------------------------------------------------------------
# 字符分类
# ---------------------------------------------------------------------------


def is_cjk(ch: str) -> bool:
    """CJK 字符或 CJK 标点（LINE_JOIN_V1 判定用）。"""
    cp = ord(ch)
    return (
        0x2E80 <= cp <= 0x2FDF
        or 0x3000 <= cp <= 0x303F
        or 0x3040 <= cp <= 0x30FF
        or 0x3100 <= cp <= 0x31FF
        or 0x3200 <= cp <= 0x33FF
        or 0x3400 <= cp <= 0x4DBF
        or 0x4E00 <= cp <= 0x9FFF
        or 0xF900 <= cp <= 0xFAFF
        or 0xFE30 <= cp <= 0xFE4F
        or 0xFF00 <= cp <= 0xFFEF
        or 0x20000 <= cp <= 0x2FA1F
    )


def is_suspect_char(ch: str) -> bool:
    """私有区、替换字符与 Unicode 非字符（U+FFFE/U+FFFF）：文本层可能损坏（ARCH §5.2 失败场景）。

    注：pypdfium2 在某些行尾连字符之后输出 U+FFFE（pdfminer 输出 '-'，T4 实测），
    这里按可疑处理，不猜测原字符。
    """
    cp = ord(ch)
    return 0xE000 <= cp <= 0xF8FF or 0xF0000 <= cp <= 0xFFFFD or cp in (0xFFFD, 0xFFFE, 0xFFFF)


def _is_dropped_control(text: str) -> bool:
    """控制字符（Cc，非空白）整体丢弃；多码点文本只看首字符。"""
    return unicodedata.category(text[0]) == "Cc" and not text.isspace()


# ---------------------------------------------------------------------------
# 内部结构
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Glyph:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    size: float
    space: bool

    @property
    def mid_y(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass(slots=True)
class _Line:
    top: float
    bottom: float
    glyphs: list[_Glyph] = field(default_factory=list)
    # 以下由 _finish_line 填充
    text: str = ""
    frags: list[tuple[int, int, BBox]] = field(default_factory=list)
    left: float = 0.0
    right: float = 0.0
    size: float = 0.0

    @property
    def mid_y(self) -> float:
        return (self.top + self.bottom) / 2


def _glyphs(chars: Sequence[PdfChar]) -> list[_Glyph]:
    out: list[_Glyph] = []
    for c in chars:
        t = c.text
        if not t or t in ("\r", "\n") or _is_dropped_control(t):
            continue
        b = c.bbox
        h = b.y1 - b.y0
        size = c.font_size if c.font_size and c.font_size > 0 else max(h, 1.0)
        if t.isspace():
            out.append(_Glyph(" ", b.x0, b.y0, b.x1, b.y1, size, True))
            continue
        y0, y1 = b.y0, b.y1
        if h <= 0:  # 退化框：用字号补齐，避免无法参与行聚类
            y1 = y0 + size
        out.append(_Glyph(t, b.x0, y0, b.x1, y1, size, False))
    return out


def group_lines(glyphs: Sequence[_Glyph]) -> list[_Line]:
    """按竖向重叠聚行，再把空白字形挂到最近的行上（不产生文本，只提供空格位置）。"""
    visible = sorted((g for g in glyphs if not g.space), key=lambda g: (g.mid_y, g.x0))
    lines: list[_Line] = []
    for g in visible:
        h = g.y1 - g.y0
        best: _Line | None = None
        best_ratio = 0.0
        for ln in lines:
            lh = ln.bottom - ln.top
            denom = min(h, lh)
            if denom <= 0:
                continue
            inter = min(g.y1, ln.bottom) - max(g.y0, ln.top)
            ratio = inter / denom
            if ratio >= _LINE_OVERLAP and ratio > best_ratio:
                best, best_ratio = ln, ratio
        if best is None:
            lines.append(_Line(top=g.y0, bottom=g.y1, glyphs=[g]))
        else:
            best.glyphs.append(g)
            best.top = min(best.top, g.y0)
            best.bottom = max(best.bottom, g.y1)
    for g in glyphs:
        if not g.space:
            continue
        best = None
        best_d = float("inf")
        for ln in lines:
            d = max(ln.top - g.mid_y, 0.0, g.mid_y - ln.bottom)
            if d < best_d:
                best, best_d = ln, d
        if best is not None and best_d <= 0.5 * (best.bottom - best.top):
            best.glyphs.append(g)
    for ln in lines:
        _finish_line(ln)
    lines.sort(key=lambda ln: (ln.top, ln.left))
    return lines


def _finish_line(ln: _Line) -> None:
    ln.glyphs.sort(key=lambda g: (g.x0, g.x1))
    parts: list[str] = []
    frags: list[tuple[int, int, BBox]] = []
    pos = 0
    pending_space = False
    sizes: list[float] = []
    xs0: list[float] = []
    xs1: list[float] = []
    for g in ln.glyphs:
        if g.space:
            if parts:  # 行首空白丢弃；连续空白折叠
                pending_space = True
            continue
        if pending_space:
            parts.append(" ")
            pos += 1
            pending_space = False
        start = pos
        parts.append(g.text)
        pos += len(g.text)
        frags.append((start, pos, BBox(x0=g.x0, y0=g.y0, x1=g.x1, y1=g.y1)))
        sizes.append(g.size)
        xs0.append(g.x0)
        xs1.append(g.x1)
    ln.text = "".join(parts)
    ln.frags = frags
    ln.left = min(xs0) if xs0 else 0.0
    ln.right = max(xs1) if xs1 else 0.0
    ln.size = statistics.median(sizes) if sizes else 0.0
    ln.glyphs = []  # 释放


def join_line_texts(
    pieces: Sequence[tuple[str, Sequence[tuple[int, int, BBox]]]],
) -> tuple[str, list[tuple[int, int, int, BBox]]]:
    """LINE_JOIN_V1 拼接。返回 (文本, [(start, end, line_index, bbox)])。"""
    text = ""
    out: list[tuple[int, int, int, BBox]] = []
    for i, (lt, frags) in enumerate(pieces):
        if not lt:
            continue
        if text and i > 0:
            prev = text[-1]
            if not prev.isspace() and not lt[0].isspace() and not (is_cjk(prev) and is_cjk(lt[0])):
                text += " "
        offset = len(text)
        text += lt
        for s, e, bbox in frags:
            out.append((offset + s, offset + e, i, bbox))
    return text, out


# ---------------------------------------------------------------------------
# 分段与切分
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Para:
    lines: list[_Line]
    size: float


def _short(line: _Line, col_right: float) -> bool:
    return line.right < col_right - _SHORT_EM * line.size


def split_paragraphs(lines: Sequence[_Line]) -> list[_Para]:
    """单页单栏分段。lines 须按阅读顺序（自上而下）排列。"""
    if not lines:
        return []
    gaps = sorted(b.mid_y - a.mid_y for a, b in zip(lines, lines[1:]) if b.mid_y > a.mid_y)
    if len(gaps) >= 3:
        # 行距取下四分位：段间距会把中位数拉大（短页上尤其明显），下四分位更接近正常行距。
        pitch = gaps[(len(gaps) - 1) // 4]
    else:
        # 行数太少无法估计行距：退化为字号的 1.3 倍（常见正文行距）。
        pitch = 1.3 * statistics.median(x.size for x in lines) if lines[0].size else None
    col_left = sorted(ln.left for ln in lines)[len(lines) // 10]
    col_right = max(ln.right for ln in lines)

    paras: list[_Para] = []
    cur: list[_Line] = [lines[0]]
    for prev, ln in zip(lines, lines[1:]):
        brk = False
        gap = ln.mid_y - prev.mid_y
        if pitch is not None and gap > _PITCH_BREAK * pitch:
            brk = True
        if prev.size and abs(ln.size - prev.size) > _SIZE_BREAK * prev.size:
            brk = True
        if ln.left - col_left >= _INDENT_EM * ln.size and _short(prev, col_right):
            brk = True
        if brk:
            paras.append(_Para(cur, statistics.median(x.size for x in cur)))
            cur = [ln]
        else:
            cur.append(ln)
    paras.append(_Para(cur, statistics.median(x.size for x in cur)))
    return paras


def split_long_text(text: str, max_chars: int) -> list[tuple[int, int]]:
    """返回 [(start, end)] 半开区间；每段长度 ≤ max_chars；段首不含空白。"""
    if len(text) <= max_chars:
        return [(0, len(text))]
    pieces: list[tuple[int, int]] = []
    start = 0
    n = len(text)
    while n - start > max_chars:
        window_end = start + max_chars
        cut = -1
        # 1) 最靠后的句末标点（切在标点之后）
        for p in range(window_end, start, -1):
            if text[p - 1] in _SENT_END:
                cut = p
                break
        if cut > start:
            end, nxt = cut, cut
        else:
            # 2) 最靠后的空白（切在空白处，空白不保留）
            for p in range(window_end, start, -1):
                if text[p - 1].isspace():
                    cut = p
                    break
            if cut > start:
                end, nxt = cut - 1, cut
            else:
                # 3) 硬切
                end, nxt = window_end, window_end
        pieces.append((start, end))
        start = nxt
        while start < n and text[start].isspace():
            start += 1
    if start < n:
        pieces.append((start, n))
    return pieces


# ---------------------------------------------------------------------------
# 版面类（实现 interfaces.PdfLayout）
# ---------------------------------------------------------------------------


class PdfLayoutV1:
    """与后端无关的版面分析。``build_blocks`` 返回按阅读顺序、order 连续的 Block 列表。"""

    version = f"{LAYOUT_VERSION}+{JOIN_RULE}"

    def __init__(self, max_block_chars: int = DEFAULT_MAX_BLOCK_CHARS, suspect_ratio: float = SUSPECT_RATIO) -> None:
        if max_block_chars < 1:
            raise ValueError("max_block_chars 必须为正")
        self.max_block_chars = max_block_chars
        self.suspect_ratio = suspect_ratio

    def build_blocks(
        self,
        doc_id: str,
        pages: Sequence[PdfPageInfo],
        chars_by_page: Mapping[int, Sequence[PdfChar]],
    ) -> list[Block]:
        blocks: list[Block] = []
        block_pages: list[int] = []
        block_sizes: list[float] = []
        heading_stack: list[tuple[float, str]] = []
        for page in pages:
            glyphs = _glyphs(chars_by_page.get(page.page_number, ()))
            lines = group_lines(glyphs)
            visible_sizes = [g.size for g in glyphs if not g.space]
            body_size = statistics.median(visible_sizes) if visible_sizes else 0.0
            k = 0
            for para in split_paragraphs(lines):
                para_text, para_frags = join_line_texts(
                    [(ln.text, [(s, e, b) for (s, e, b) in ln.frags]) for ln in para.lines]
                )
                if not para_text.strip():
                    continue
                is_heading = body_size > 0 and para.size >= _HEADING_RATIO * body_size
                pieces = split_long_text(para_text, self.max_block_chars)
                split = len(pieces) > 1
                section_path = [t for _, t in heading_stack]
                if is_heading:
                    heading_stack[:] = [(s, t) for s, t in heading_stack if s > para.size]
                    heading_stack.append((para.size, para_text[: min(len(para_text), 200)]))
                for s, e in pieces:
                    ptext = para_text[s:e]
                    if not ptext.strip():
                        continue
                    frags = _clip_fragments(para_frags, s, e, page.page_number)
                    if not frags:
                        continue
                    k += 1
                    flags: list[BlockFlag] = []
                    if split:
                        flags.append(BlockFlag.SPLIT_FROM_LONG_BLOCK)
                    bad = sum(1 for ch in ptext if is_suspect_char(ch))
                    visible_n = sum(1 for ch in ptext if not ch.isspace())
                    if visible_n and bad / visible_n > self.suspect_ratio:
                        flags.append(BlockFlag.TEXT_LAYER_SUSPECT)
                    region = BBox(
                        x0=min(f.bbox.x0 for f in frags),
                        y0=min(f.bbox.y0 for f in frags),
                        x1=max(f.bbox.x1 for f in frags),
                        y1=max(f.bbox.y1 for f in frags),
                    )
                    block_pages.append(page.page_number)
                    block_sizes.append(para.size)
                    blocks.append(
                        Block(
                            doc_id=doc_id,
                            block_id=f"p{page.page_number}-b{k}",
                            kind=BlockKind.HEADING if is_heading else BlockKind.PARAGRAPH,
                            text=ptext,
                            order=len(blocks),
                            section_path=list(section_path),
                            locator=PdfLocator(
                                page_number=page.page_number,
                                page_width=page.width,
                                page_height=page.height,
                                regions=[region],
                            ),
                            fragments=frags,
                            flags=flags,
                        )
                    )
        return mark_page_continuations(blocks, block_pages, block_sizes, pages)


def mark_page_continuations(
    blocks: list[Block],
    block_pages: Sequence[int],
    block_sizes: Sequence[float],
    pages: Sequence[PdfPageInfo],
) -> list[Block]:
    """跨页续接标记 CONTINUED_ON_NEXT_PAGE（保守：宁可漏标，不要错标）。

    同时满足才标记当前页的最后一个块：
    1. 其最后一个非空白字符不是句末标点或右括号类（CONT_TERMINAL）；
    2. 下一页存在块，且下一页第一个块的起点 y0 位于下一页上部 CONT_TOP_FRACTION 之内；
    3. 两块字号差 ≤ CONT_SIZE_TOL_PT。
    """
    height_of = {p.page_number: p.height for p in pages}
    last_idx: dict[int, int] = {}
    first_idx: dict[int, int] = {}
    for i, pg in enumerate(block_pages):
        last_idx[pg] = i
        first_idx.setdefault(pg, i)
    out = list(blocks)
    for pg in sorted(last_idx):
        nxt = pg + 1
        if nxt not in first_idx:
            continue
        i = last_idx[pg]
        j = first_idx[nxt]
        cur = out[i]
        stripped = cur.text.rstrip()
        if not stripped or stripped[-1] in CONT_TERMINAL:
            continue
        nxt_top = out[j].locator.regions[0].y0
        if nxt_top > CONT_TOP_FRACTION * height_of[nxt]:
            continue
        if abs(block_sizes[i] - block_sizes[j]) > CONT_SIZE_TOL_PT:
            continue
        out[i] = cur.model_copy(update={"flags": [*cur.flags, BlockFlag.CONTINUED_ON_NEXT_PAGE]})
    return out


def _clip_fragments(
    frags: Sequence[tuple[int, int, int, BBox]],
    s: int,
    e: int,
    page_number: int,
) -> list[TextFragment]:
    out: list[TextFragment] = []
    for fs, fe, line, bbox in frags:
        a, b = max(fs, s), min(fe, e)
        if b > a:
            out.append(
                TextFragment(
                    start=a - s,
                    end=b - s,
                    page_number=page_number,
                    line=line,
                    bbox=bbox,
                )
            )
    return out
