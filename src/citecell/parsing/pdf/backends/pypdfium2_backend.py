"""PDF 字符后端：pypdfium2（PDFium，BSD-3-Clause 包装，默认后端，见 docs/VERIFICATION_T0.md）。

只负责产出字符流与页面信息，坐标换算到 PDF_COORDS_V1（CropBox 左上角为原点，y 向下）。
虚拟字符标记使用 ``FPDFText_IsGenerated``：换行符 ``\\r\\n`` 与自动插入的空格都会被标记。
几何使用 ``loose=True`` 的字体度量框：同一行的字符 y 范围一致，x 起点与绘制原点一致
（T4 实测：tight 框的 x0 比绘制原点偏移约 0.9pt，loose 框 x0 与原点相等）。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from citecell.models import BBox, PdfChar, PdfPageInfo
from citecell.interfaces import UnsupportedDocumentError

logger = logging.getLogger("citecell.parsing.pdf")

_HIGH_SURROGATE = range(0xD800, 0xDC00)


class Pypdfium2Backend:
    """实现 ``interfaces.PdfBackend``。"""

    name = "pypdfium2"

    def __init__(self) -> None:
        self.library_version = f"pypdfium2 {pdfium.version.PYPDFIUM_INFO} / pdfium {pdfium.version.PDFIUM_INFO}"
        self._doc: pdfium.PdfDocument | None = None

    def open(self, path: Path) -> None:
        try:
            self._doc = pdfium.PdfDocument(str(path))
        except pdfium.PdfiumError as exc:
            # 异常信息里可能含路径，统一替换为通用说明（隐私，ARCH §12）。
            raise UnsupportedDocumentError("PDF 无法打开（加密、损坏或格式不受支持）") from exc

    def _require(self) -> pdfium.PdfDocument:
        if self._doc is None:
            raise RuntimeError("backend not opened")
        return self._doc

    def pages(self) -> list[PdfPageInfo]:
        doc = self._require()
        out: list[PdfPageInfo] = []
        for i in range(len(doc)):
            page = doc[i]
            left, bottom, right, top = page.get_cropbox()
            out.append(
                PdfPageInfo(
                    page_number=i + 1,
                    width=float(right - left),
                    height=float(top - bottom),
                    rotation=int(page.get_rotation()),
                )
            )
        return out

    def iter_chars(self, page_number: int):
        doc = self._require()
        page = doc[page_number - 1]
        left, bottom, right, top = page.get_cropbox()
        textpage = page.get_textpage()
        try:
            n = textpage.count_chars()
            i = 0
            while i < n:
                text = textpage.get_text_range(i, 1)
                step = 1
                # PDFium 按 UTF-16 代码单元计数：非 BMP 字符以代理对出现，需要合并。
                # 未用真实非 BMP 字体验证（T4 测试未覆盖），见最终回复的已知缺陷。
                if text and ord(text[0]) in _HIGH_SURROGATE and i + 1 < n:
                    pair = text + textpage.get_text_range(i + 1, 1)
                    text = pair.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
                    step = 2
                generated = bool(pdfium_c.FPDFText_IsGenerated(textpage.raw, i))
                fx0, fbottom, fx1, ftop = textpage.get_charbox(i, loose=True)
                size = float(pdfium_c.FPDFText_GetFontSize(textpage.raw, i))
                bbox = BBox(
                    x0=float(fx0 - left),
                    y0=float(top - ftop),
                    x1=float(fx1 - left),
                    y1=float(top - fbottom),
                )
                yield PdfChar(
                    text=text,
                    bbox=bbox,
                    font_size=size if size > 0 else None,
                    generated=generated,
                )
                i += step
        finally:
            textpage.close()

    def close(self) -> None:
        if self._doc is not None:
            self._doc.close()
            self._doc = None
