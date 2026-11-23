"""文件解析层（T3 负责 DOCX / XLSX 与注册表；PDF 由 T4 提供，按字符串路径懒加载）。

默认导入路径不 import 任何 PDF 库，也不 import fitz / pymupdf。
"""

from citecell.parsing.common import (
    DefaultParserRegistry,
    build_default_registry,
    check_zip_package,
    fingerprint,
)
from citecell.parsing.docx_parser import DocxParser
from citecell.parsing.xlsx_parser import XlsxParser

__all__ = [
    "DefaultParserRegistry",
    "DocxParser",
    "XlsxParser",
    "build_default_registry",
    "check_zip_package",
    "fingerprint",
]
