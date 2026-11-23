"""解析器注册表与导入卫生测试（TASKS.md T3 验收）。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from citecell.interfaces import ParserRegistry, UnsupportedDocumentError
from citecell.parsing import DocxParser, XlsxParser, build_default_registry
from citecell.parsing.common import DefaultParserRegistry, fingerprint

SRC_DIR = Path(__file__).resolve().parents[3] / "src"


def test_registry_protocol_and_dispatch(make_docx, make_xlsx):
    reg = build_default_registry()
    assert isinstance(reg, ParserRegistry)
    assert isinstance(reg.for_path(make_docx(lambda d: d.add_paragraph("x"))), DocxParser)
    assert isinstance(reg.for_path(make_xlsx()), XlsxParser)


def test_unknown_extension_is_unsupported(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("hello", encoding="utf-8")
    with pytest.raises(UnsupportedDocumentError):
        build_default_registry().for_path(f)


def test_extension_content_mismatch_is_an_error(tmp_path, make_xlsx):
    xlsx = make_xlsx()
    disguised = tmp_path / "disguised.docx"
    disguised.write_bytes(xlsx.read_bytes())
    with pytest.raises(UnsupportedDocumentError):
        build_default_registry().for_path(disguised)


def test_pdf_magic_mismatch_is_an_error(tmp_path):
    f = tmp_path / "fake.pdf"
    f.write_bytes(b"PK\x03\x04 not really pdf")
    with pytest.raises(UnsupportedDocumentError):
        build_default_registry().for_path(f)


def test_pdf_without_implementation_is_unsupported(tmp_path):
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4\n%%EOF\n")
    reg = DefaultParserRegistry(pdf_parser_path="citecell.parsing.no_such_module:PdfParser")
    with pytest.raises(UnsupportedDocumentError):
        reg.for_path(f)


def test_pdf_missing_class_is_unsupported(tmp_path):
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4\n%%EOF\n")
    reg = DefaultParserRegistry(pdf_parser_path="citecell.parsing.common:NoSuchClass")
    with pytest.raises(UnsupportedDocumentError):
        reg.for_path(f)


def test_fingerprint_matches_sha256_prefix(tmp_path):
    import hashlib

    f = tmp_path / "bytes.bin"
    f.write_bytes(b"abc" * 1000)
    fp = fingerprint(f)
    assert fp.doc_id == hashlib.sha256(b"abc" * 1000).hexdigest()[:16]
    assert fp.size_bytes == 3000


def test_import_does_not_load_fitz_or_pymupdf_or_pdf_backends():
    code = (
        "import sys, citecell.parsing;"
        "bad = [m for m in ('fitz', 'pymupdf', 'citecell.parsing.pdf') if m in sys.modules];"
        "assert not bad, bad"
    )
    env = dict(os.environ, PYTHONPATH=str(SRC_DIR))
    proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
