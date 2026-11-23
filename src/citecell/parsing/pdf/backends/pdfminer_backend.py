"""PDF 字符后端：pdfminer.six（MIT）。定位为交叉校验后端，不是默认后端。

使用 ``laparams=None`` 取得原始 LTChar 流（不做 pdfminer 自身的分行/分词），
因此不会产生 LTAnno 虚拟字符；``generated`` 恒为 False。
这意味着两后端在"源文件中没有空格、但渲染间距很大"的文本上会不一致（pypdfium2 会插入
虚拟空格，pdfminer 不会）；交叉校验测试只使用不含此情形的样本。
"""

from __future__ import annotations

from pathlib import Path

from pdfminer.converter import PDFPageAggregator
from pdfminer.layout import LTChar, LTContainer
from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
from pdfminer.pdfpage import PDFPage
from pdfminer.pdftypes import PDFException
from pdfminer.psparser import PSException

from citecell.interfaces import UnsupportedDocumentError
from citecell.models import BBox, PdfChar, PdfPageInfo


class PdfminerBackend:
    """实现 ``interfaces.PdfBackend``。"""

    name = "pdfminer"

    def __init__(self) -> None:
        import pdfminer

        self.library_version = f"pdfminer.six {getattr(pdfminer, '__version__', 'unknown')}"
        self._fp = None
        self._pages: list = []  # pdfminer PDFPage 对象（含 cropbox/rotate）

    def open(self, path: Path) -> None:
        try:
            self._fp = open(path, "rb")
            self._pages = list(PDFPage.get_pages(self._fp))
        except (PDFException, PSException, OSError, ValueError, IndexError, KeyError) as exc:
            self.close()
            raise UnsupportedDocumentError("PDF 无法打开（加密、损坏或格式不受支持）") from exc

    def pages(self) -> list[PdfPageInfo]:
        out: list[PdfPageInfo] = []
        for i, page in enumerate(self._pages):
            left, bottom, right, top = page.cropbox
            out.append(
                PdfPageInfo(
                    page_number=i + 1,
                    width=float(right - left),
                    height=float(top - bottom),
                    rotation=int(page.rotate or 0) % 360,
                )
            )
        return out

    def iter_chars(self, page_number: int):
        page = self._pages[page_number - 1]
        left, bottom, right, top = page.cropbox
        rsrc = PDFResourceManager()
        device = PDFPageAggregator(rsrc, laparams=None)
        interp = PDFPageInterpreter(rsrc, device)
        interp.process_page(page)
        layout = device.get_result()

        def walk(obj):
            if isinstance(obj, LTChar):
                yield obj
            elif isinstance(obj, LTContainer):
                for child in obj:
                    yield from walk(child)

        for ch in walk(layout):
            x0, y0, x1, y1 = ch.bbox
            size = float(ch.size) if ch.size else None
            yield PdfChar(
                text=ch.get_text(),
                bbox=BBox(x0=float(x0 - left), y0=float(top - y1), x1=float(x1 - left), y1=float(top - y0)),
                font_size=size if size and size > 0 else None,
                font_name=str(ch.fontname) if ch.fontname else None,
                generated=False,
            )

    def close(self) -> None:
        if self._fp is not None:
            self._fp.close()
            self._fp = None
        self._pages = []
