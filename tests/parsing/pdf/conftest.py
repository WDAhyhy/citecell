"""T4 测试夹具：运行时用 reportlab 即时生成 PDF（不依赖 T5 代码，不提交二进制样例）。

中文使用内置 CID 字体 STSong-Light（未嵌入）；T0 已验证 pypdfium2 与 pdfminer.six 都能提取。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

CJK_FONT = "STSong-Light"
pdfmetrics.registerFont(UnicodeCIDFont(CJK_FONT))

PAGE_W, PAGE_H = A4  # 595.27..., 841.88...


def build_pdf(path: Path, draw: Callable[[canvas.Canvas], None], *, rotate: int = 0, pagesize=A4,
              extra_pages: Callable[[canvas.Canvas], None] | None = None) -> Path:
    c = canvas.Canvas(str(path), pagesize=pagesize)
    if rotate:
        c.setPageRotation(rotate)
    draw(c)
    c.showPage()
    if extra_pages is not None:
        extra_pages(c)
        c.showPage()
    c.save()
    return path


@pytest.fixture
def make_pdf(tmp_path: Path):
    """返回 (name, draw, **kw) -> Path 的工厂；文件写入 pytest 临时目录。"""

    def _make(name: str, draw: Callable[[canvas.Canvas], None], **kw) -> Path:
        return build_pdf(tmp_path / f"{name}.pdf", draw, **kw)

    return _make
