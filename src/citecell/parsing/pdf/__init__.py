"""PDF 解析子包（T4）：后端（backends/）+ 与后端无关的版面（layout.py）+ 解析器（parser.py）。

默认导入路径不 import 任何 PDF 后端库；后端在 ``PdfParser`` 首次使用时按名字懒加载。
"""

from citecell.parsing.pdf.layout import JOIN_RULE, LAYOUT_VERSION, PdfLayoutV1
from citecell.parsing.pdf.parser import DEFAULT_BACKEND, PdfParser

__all__ = ["DEFAULT_BACKEND", "JOIN_RULE", "LAYOUT_VERSION", "PdfLayoutV1", "PdfParser"]
