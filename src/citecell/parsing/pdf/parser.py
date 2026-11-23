"""PDF 解析器：文件字节 → ParsedDocument。实现 ``interfaces.Parser``。

流程：读取字节 → SHA-256 得 doc_id → 后端逐页取字符 → PdfLayout 分块 → 文档级检查。
* 没有任何可见字符 → ``UnsupportedDocumentError("no text layer")``。
* 页面 /Rotate≠0 → 文档级警告（M1 不支持旋转页，坐标未换算）。
* 私有区/替换字符占比超过阈值 → 文档级 ``TEXT_LAYER_UNRELIABLE`` 警告。
* 日志只记录计数与 doc_id，不记录正文或路径。
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from citecell.interfaces import PdfBackend, UnsupportedDocumentError
from citecell.models import DocFormat, DocumentInfo, ParsedDocument
from citecell.parsing.pdf.backends import load_backend
from citecell.parsing.pdf.layout import (
    DEFAULT_MAX_BLOCK_CHARS,
    SUSPECT_RATIO,
    PdfLayoutV1,
    is_suspect_char,
)

logger = logging.getLogger("citecell.parsing.pdf")

PARSER_VERSION = "0.1.0"
DEFAULT_BACKEND = "pypdfium2"


class PdfParser:
    """实现 ``interfaces.Parser``。``backend`` 对应 ``RunConfig.pdf_backend``。"""

    name = "citecell-pdf"
    version = PARSER_VERSION
    formats = frozenset({DocFormat.PDF})

    def __init__(
        self,
        backend: str = DEFAULT_BACKEND,
        *,
        max_block_chars: int = DEFAULT_MAX_BLOCK_CHARS,
        suspect_ratio: float = SUSPECT_RATIO,
    ) -> None:
        self.backend_name = backend
        self.layout = PdfLayoutV1(max_block_chars=max_block_chars, suspect_ratio=suspect_ratio)
        self.suspect_ratio = suspect_ratio

    def parse(self, path: Path, *, display_name: str | None = None) -> ParsedDocument:
        path = Path(path)
        data = path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        doc_id = sha[:16]

        backend: PdfBackend = load_backend(self.backend_name)
        try:
            backend.open(path)
            pages = backend.pages()
            if not pages:
                raise UnsupportedDocumentError("no pages")
            chars_by_page = {p.page_number: list(backend.iter_chars(p.page_number)) for p in pages}
        finally:
            backend.close()

        warnings: list[str] = []
        rotated = [p.page_number for p in pages if p.rotation % 360 != 0]
        if rotated:
            warnings.append(f"PAGE_ROTATION_UNSUPPORTED: pages={rotated}")

        visible = 0
        suspect = 0
        for chars in chars_by_page.values():
            for c in chars:
                if c.generated or not c.text or c.text.isspace():
                    continue
                visible += 1
                if any(is_suspect_char(ch) for ch in c.text):
                    suspect += 1
        if visible == 0:
            raise UnsupportedDocumentError("no text layer")
        if suspect / visible > self.suspect_ratio:
            warnings.append(f"TEXT_LAYER_UNRELIABLE: suspect_ratio={suspect / visible:.3f}")

        blocks = self.layout.build_blocks(doc_id, pages, chars_by_page)
        display = display_name[:255] if display_name else None
        info = DocumentInfo(
            doc_id=doc_id,
            sha256=sha,
            format=DocFormat.PDF,
            size_bytes=len(data),
            display_name=display,
            page_count=len(pages),
            sheet_names=None,
            parser_name=self.name,
            parser_version=f"{self.version}+{self.layout.version}/{backend.library_version}",
            warnings=warnings,
        )
        logger.info("pdf parsed doc_id=%s pages=%d blocks=%d", doc_id, len(pages), len(blocks))
        return ParsedDocument(info=info, blocks=blocks)
