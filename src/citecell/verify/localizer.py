"""定位器 LocalizerV1（ARCH §6.4）：块内字符区间 → 原生定位与精度。

规则：
* PDF 且块带逐字片段（``Block.fragments``）：取与 ``[start, end)`` 相交的片段，
  按 ``(page_number, line)`` 分组，每组的框取并集，得到 ``PdfLocator.regions``
  （跨两行的引文恰好 2 个框）。
  - 所有相交片段都是单字符（``end - start == 1``）且完全包含于区间 → 精度 ``CHAR``；
  - 存在 span 级片段 → 精度 ``LINE``；部分相交的 span 级片段按字符数在水平方向线性插值。
* PDF 块有片段但区间与任何片段都不相交（例如只覆盖插入的空格）→ 退化为块框，精度 ``BLOCK``。
* PDF 块无片段 → 块框，精度 ``BLOCK``。
* DOCX 块 → 沿用块的 locator，精度 ``CHAR``（字符区间在 Evidence 中）。
* XLSX 块 → 沿用块的 locator，精度 ``CELL``。

页面尺寸：PdfLocator 的 page_width/page_height 取自块的 locator。块跨页时（片段页号与块
locator 页号不同），尺寸取自块的 locator 页，这是块级数据能提供的最佳近似；M1 的块不跨页，
该情形只在 Verifier 按页切分后出现。跨页区间由 Verifier 在调用前切分，本类遇到跨页区间会抛 ValueError。
"""

from __future__ import annotations

from citecell.models import (
    BBox,
    Block,
    DocxLocator,
    Locator,
    LocatorPrecision,
    PdfLocator,
    TextFragment,
    XlsxLocator,
)

LOCALIZER_VERSION = "localizer-v1"


class LocalizerV1:
    """满足 ``citecell.interfaces.Localizer`` 协议的定位器。"""

    version: str = LOCALIZER_VERSION

    def locate(self, block: Block, start: int, end: int) -> tuple[Locator, LocatorPrecision]:
        """返回 ``[start, end)`` 在 ``block`` 中的定位。调用方须保证 ``0 <= start < end <= len(block.text)``。"""
        if start < 0 or end <= start or end > len(block.text):
            raise ValueError("定位区间非法")
        loc = block.locator
        if isinstance(loc, PdfLocator):
            return self._locate_pdf(block, loc, start, end)
        if isinstance(loc, DocxLocator):
            return loc, LocatorPrecision.CHAR
        if isinstance(loc, XlsxLocator):
            return loc, LocatorPrecision.CELL
        raise TypeError("不支持的 locator 类型")  # pragma: no cover - 联合类型已穷尽

    # ------------------------------------------------------------------ PDF
    def _locate_pdf(
        self, block: Block, loc: PdfLocator, start: int, end: int
    ) -> tuple[Locator, LocatorPrecision]:
        block_fallback = (loc, LocatorPrecision.BLOCK)
        overlapping = sorted(
            (f for f in block.fragments if f.start < end and f.end > start),
            key=lambda f: (f.start, f.end),
        )
        if not overlapping:
            return block_fallback
        pages = {f.page_number for f in overlapping}
        if len(pages) != 1:
            # 调用方（Verifier）已按页切分；仍跨页说明调用方违反约定，显式报错而不是猜测。
            raise ValueError("定位区间跨越多页，应由调用方按页切分")
        page = pages.pop()

        precision = (
            LocatorPrecision.CHAR
            if all(f.end - f.start == 1 and f.start >= start and f.end <= end for f in overlapping)
            else LocatorPrecision.LINE
        )

        # 按 (页, 行) 分组，每组取框的并集；组的顺序按 (页, 行) 升序，保证确定性。
        groups: dict[tuple[int, int], BBox] = {}
        for f in overlapping:
            box = _clip_box(f, start, end)
            key = (f.page_number, f.line)
            groups[key] = box if key not in groups else _union(groups[key], box)
        regions = [groups[k] for k in sorted(groups)]
        return (
            PdfLocator(
                page_number=page,
                page_width=loc.page_width,
                page_height=loc.page_height,
                regions=regions,
            ),
            precision,
        )


def _clip_box(f: TextFragment, start: int, end: int) -> BBox:
    """片段与区间的交集对应的框。逐字片段直接取片段框；span 级片段按字符数线性插值（仅水平）。"""
    n = f.end - f.start
    if f.start >= start and f.end <= end:
        return f.bbox  # 完全包含：直接取片段框
    a = max(start, f.start) - f.start  # 相交部分在片段内的起始字符偏移
    b = min(end, f.end) - f.start  # 相交部分在片段内的结束字符偏移（开区间）
    b0 = f.bbox
    width = b0.x1 - b0.x0
    x0 = b0.x0 + width * a / n
    x1 = b0.x0 + width * b / n
    return BBox(x0=x0, y0=b0.y0, x1=x1, y1=b0.y1)


def _union(a: BBox, b: BBox) -> BBox:
    return BBox(
        x0=min(a.x0, b.x0),
        y0=min(a.y0, b.y0),
        x1=max(a.x1, b.x1),
        y1=max(a.y1, b.y1),
    )


__all__ = ["LOCALIZER_VERSION", "LocalizerV1"]
