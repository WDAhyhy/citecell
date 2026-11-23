"""解析层公共工具（T3）。

* 文件指纹：``doc_id = sha256[:16]``，流式计算，不把整文件读入内存。
* zip 容器检查（DOCX / XLSX 共用）：条目数上限；解压总量按**实际读取字节**计量（不信任 zip 头里的声明大小）；
  XML 部件中出现 ``<!DOCTYPE`` / ``<!ENTITY`` 声明即拒绝（不展开实体，不依赖 defusedxml 是否安装）。
* ``DefaultParserRegistry``：按扩展名 + 容器内部成员（魔数）识别。扩展名与内容不符 → ``UnsupportedDocumentError``
  （选择"报错"而非"按内容静默改判"，以免把误命名的文件悄悄当作另一种格式解析）。
  PDF 解析器按字符串路径懒加载（T4 实现）；加载失败统一转为 ``UnsupportedDocumentError``。

日志只写 doc_id 与异常类型名，不写路径、正文、引文或提示词。
"""

from __future__ import annotations

import hashlib
import importlib
import logging
import re
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from citecell.interfaces import Parser, UnsupportedDocumentError
from citecell.models import DocFormat

logger = logging.getLogger("citecell.parsing")

DEFAULT_MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
"""zip 容器解压后总字节数上限（实际读取计量）。"""

DEFAULT_MAX_ZIP_ENTRIES = 5000
"""zip 条目数上限。"""

PDF_PARSER_PATH = "citecell.parsing.pdf.parser:PdfParser"
"""PDF 解析器的懒加载路径（T4 实现）。注册表不在模块导入时 import 它。"""

_CHUNK = 1 << 20
_XML_SUFFIXES = (".xml", ".rels", ".vml")
_FORBIDDEN_XML = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)
_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"


@dataclass(frozen=True)
class FileFingerprint:
    doc_id: str
    sha256: str
    size_bytes: int


def fingerprint(path: Path) -> FileFingerprint:
    """流式计算文件 SHA-256。doc_id 为其前 16 位十六进制。"""
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
    digest = h.hexdigest()
    return FileFingerprint(doc_id=digest[:16], sha256=digest, size_bytes=size)


def read_head(path: Path, n: int = 8) -> bytes:
    with open(path, "rb") as fh:
        return fh.read(n)


def check_zip_package(
    path: Path,
    *,
    doc_id: str,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
    max_entries: int = DEFAULT_MAX_ZIP_ENTRIES,
) -> frozenset[str]:
    """校验 zip 容器并返回成员名集合。

    失败统一抛 ``UnsupportedDocumentError``，消息只含 doc_id，不含路径。
    """
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        raise UnsupportedDocumentError(f"doc {doc_id}: 不是有效的 zip 容器") from None
    with zf:
        infos = zf.infolist()
        if len(infos) > max_entries:
            raise UnsupportedDocumentError(f"doc {doc_id}: zip 条目数超过上限 {max_entries}")
        total = 0
        for info in infos:
            if info.file_size > max_uncompressed_bytes:
                raise UnsupportedDocumentError(f"doc {doc_id}: 单个部件声明大小超过解压上限")
            scan_xml = info.filename.lower().endswith(_XML_SUFFIXES)
            xml_parts: list[bytes] = []
            try:
                with zf.open(info) as fh:
                    while True:
                        chunk = fh.read(_CHUNK)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_uncompressed_bytes:
                            raise UnsupportedDocumentError(f"doc {doc_id}: 解压后总大小超过上限")
                        if scan_xml:
                            xml_parts.append(chunk)
            except UnsupportedDocumentError:
                raise
            except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError, EOFError, ValueError):
                raise UnsupportedDocumentError(f"doc {doc_id}: zip 部件损坏或压缩方式不受支持") from None
            if scan_xml and _FORBIDDEN_XML.search(b"".join(xml_parts)):
                # 拒绝而非展开：DTD / 实体声明在本项目中没有合法用途（ARCH §5.4 安全约定）。
                raise UnsupportedDocumentError(f"doc {doc_id}: XML 含 DTD/实体声明，已拒绝")
        return frozenset(i.filename for i in infos)


def zip_member_names(path: Path, *, doc_id: str) -> frozenset[str]:
    """仅读取目录（用于识别类型，不做解压检查）。"""
    try:
        with zipfile.ZipFile(path) as zf:
            return frozenset(zf.namelist())
    except (zipfile.BadZipFile, OSError):
        raise UnsupportedDocumentError(f"doc {doc_id}: 不是有效的 zip 容器") from None


class DefaultParserRegistry:
    """按扩展名 + 魔数选择解析器。

    * ``.pdf``：必须以 ``%PDF-`` 开头；解析器由 ``pdf_parser_path`` 懒加载，``pdf_options`` 作为关键字参数传入。
    * ``.docx``：容器内须有 ``word/document.xml``。
    * ``.xlsx``：容器内须有 ``xl/workbook.xml``。
    * 其余扩展名，或扩展名与内容不符 → ``UnsupportedDocumentError``。
    """

    def __init__(
        self,
        *,
        include_hidden_sheets: bool = False,
        include_hidden_cells: bool = True,
        max_block_chars: int = 800,
        pdf_parser_path: str = PDF_PARSER_PATH,
        pdf_options: Mapping[str, Any] | None = None,
        max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
        max_zip_entries: int = DEFAULT_MAX_ZIP_ENTRIES,
    ) -> None:
        self._include_hidden_sheets = include_hidden_sheets
        self._include_hidden_cells = include_hidden_cells
        self._max_block_chars = max_block_chars
        self._pdf_parser_path = pdf_parser_path
        self._pdf_options = dict(pdf_options or {})
        self._max_uncompressed_bytes = max_uncompressed_bytes
        self._max_zip_entries = max_zip_entries
        self._cache: dict[str, Parser] = {}

    def for_path(self, path: Path) -> Parser:
        path = Path(path)
        ext = path.suffix.lower()
        if ext == ".pdf":
            if read_head(path, len(_PDF_MAGIC)) != _PDF_MAGIC:
                raise UnsupportedDocumentError("扩展名为 .pdf 但内容不是 PDF")
            return self._get("pdf")
        if ext in (".docx", ".xlsx"):
            if read_head(path, len(_ZIP_MAGIC)) != _ZIP_MAGIC:
                raise UnsupportedDocumentError(f"扩展名为 {ext} 但内容不是 zip 容器")
            names = zip_member_names(path, doc_id="unknown")
            required = "word/document.xml" if ext == ".docx" else "xl/workbook.xml"
            if required not in names:
                raise UnsupportedDocumentError(f"扩展名为 {ext} 但容器内容与之不符")
            return self._get(ext[1:])
        raise UnsupportedDocumentError(f"不支持的文件类型: {ext or '(无扩展名)'}")

    def _get(self, kind: str) -> Parser:
        if kind in self._cache:
            return self._cache[kind]
        parser: Parser
        if kind == "docx":
            from citecell.parsing.docx_parser import DocxParser

            parser = DocxParser(
                max_block_chars=self._max_block_chars,
                max_uncompressed_bytes=self._max_uncompressed_bytes,
                max_zip_entries=self._max_zip_entries,
            )
        elif kind == "xlsx":
            from citecell.parsing.xlsx_parser import XlsxParser

            parser = XlsxParser(
                include_hidden_sheets=self._include_hidden_sheets,
                include_hidden_cells=self._include_hidden_cells,
                max_block_chars=self._max_block_chars,
                max_uncompressed_bytes=self._max_uncompressed_bytes,
                max_zip_entries=self._max_zip_entries,
            )
        elif kind == "pdf":
            parser = self._load_pdf_parser()
        else:  # pragma: no cover - 内部调用保证不会到达
            raise UnsupportedDocumentError("未知解析器类型")
        self._cache[kind] = parser
        return parser

    def _load_pdf_parser(self) -> Parser:
        module_name, _, attr = self._pdf_parser_path.partition(":")
        try:
            module = importlib.import_module(module_name)
            cls = getattr(module, attr)
        except (ImportError, AttributeError) as exc:
            # T4 未完成或其依赖未安装：PDF 输入明确拒绝，而不是崩溃。
            logger.warning("PDF 解析器不可用: %s", type(exc).__name__)
            raise UnsupportedDocumentError("PDF 解析器不可用（未安装或尚未实现）") from None
        return cls(**self._pdf_options)


def build_default_registry(**kwargs: Any) -> DefaultParserRegistry:
    """构造默认注册表。关键字参数见 ``DefaultParserRegistry``。"""
    return DefaultParserRegistry(**kwargs)


def format_of_extension(path: Path) -> DocFormat | None:
    """仅根据扩展名给出格式（不做内容校验），供上层做快速分类。"""
    return {
        ".pdf": DocFormat.PDF,
        ".docx": DocFormat.DOCX,
        ".xlsx": DocFormat.XLSX,
    }.get(Path(path).suffix.lower())
