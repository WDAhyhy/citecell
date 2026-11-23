"""DOCX / XLSX 解析测试的共用夹具：全部在运行时生成文件，不提交二进制样例（TASKS.md §0-4）。"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Callable
from pathlib import Path

import openpyxl
import pytest
from docx import Document


@pytest.fixture
def make_docx(tmp_path: Path) -> Callable[..., Path]:
    def _make(builder: Callable[[Document], None] | None = None, name: str = "sample.docx") -> Path:
        doc = Document()
        if builder is not None:
            builder(doc)
        path = tmp_path / name
        doc.save(path)
        return path

    return _make


@pytest.fixture
def make_xlsx(tmp_path: Path) -> Callable[..., Path]:
    def _make(builder: Callable[[openpyxl.Workbook], None] | None = None, name: str = "sample.xlsx") -> Path:
        wb = openpyxl.Workbook()
        if builder is not None:
            builder(wb)
        path = tmp_path / name
        wb.save(path)
        return path

    return _make


def rewrite_member(src: Path, dst: Path, member: str, transform: Callable[[bytes], bytes]) -> Path:
    """复制 zip，并对一个成员应用 transform（测试用，例如注入 XML 或补写缓存值）。"""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if info.filename == member:
                data = transform(data)
            zout.writestr(info.filename, data)
    return dst


def inject_cached_value(data: bytes, cell_ref: str, value: str) -> bytes:
    """为公式单元格写入缓存值 <v>（openpyxl 不会写缓存值）。"""
    pattern = re.compile(rb'(<c r="' + cell_ref.encode() + rb'"[^>]*>)(<f>[^<]*</f>)(<v\s*/>|<v></v>)?')
    new, n = pattern.subn(lambda m: m.group(1) + m.group(2) + f"<v>{value}</v>".encode(), data, count=1)
    assert n == 1, f"未找到公式单元格 {cell_ref}"
    return new


@pytest.fixture
def zipkit():
    """以夹具形式暴露 zip 改写工具（importlib 导入模式下测试文件不能直接 import conftest）。"""
    from types import SimpleNamespace

    return SimpleNamespace(rewrite_member=rewrite_member, inject_cached_value=inject_cached_value)
