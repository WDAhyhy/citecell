"""PDF 后端按名字懒加载。默认导入路径不 import 任何 PDF 库（见 ARCH §5.2）。

* ``pypdfium2``（默认）、``pdfminer``（交叉校验）。
* ``pymupdf``：AGPL 可选扩展，M1 未实现；明确报错，不做任何导入。
"""

from __future__ import annotations

import importlib

from citecell.interfaces import PdfBackend

_BACKENDS: dict[str, str] = {
    "pypdfium2": "citecell.parsing.pdf.backends.pypdfium2_backend:Pypdfium2Backend",
    "pdfminer": "citecell.parsing.pdf.backends.pdfminer_backend:PdfminerBackend",
}


class PdfBackendNotImplementedError(NotImplementedError):
    """请求的后端在 M1 中未实现（例如 pymupdf）。"""


def available_backends() -> tuple[str, ...]:
    return tuple(sorted(_BACKENDS))


def load_backend(name: str) -> PdfBackend:
    """按名字创建后端实例。未知名称抛 ValueError；pymupdf 抛 PdfBackendNotImplementedError。"""
    if name == "pymupdf":
        raise PdfBackendNotImplementedError(
            "pymupdf 后端为 AGPL 可选扩展，M1 未实现（不得进入默认安装与 exe，见 ARCH §5.2）"
        )
    target = _BACKENDS.get(name)
    if target is None:
        raise ValueError(f"未知 PDF 后端: {name!r}；可用: {', '.join(available_backends())}")
    module_name, class_name = target.split(":")
    module = importlib.import_module(module_name)
    return getattr(module, class_name)()
