"""Writer 注册表：按格式字符串懒加载写出函数。

每个写出模块暴露 ``write(ir: DocIR) -> tuple[bytes, list[Placement]]``。
pdf 条目指向 T5b 的 pdf_writer（仅在无法导入时报 NotImplementedError）。
"""

from __future__ import annotations

import importlib
from typing import Callable

from citecell.synth.ir import DocIR, Placement

WriteFn = Callable[[DocIR], tuple[bytes, list[Placement]]]

_MODULES = {
    "docx": "citecell.synth.writers.docx_writer",
    "xlsx": "citecell.synth.writers.xlsx_writer",
    "pdf": "citecell.synth.writers.pdf_writer",  # T5b
}


def get_writer(fmt: str) -> WriteFn:
    if fmt not in _MODULES:
        raise ValueError(f"未知写出格式: {fmt!r}")
    try:
        module = importlib.import_module(_MODULES[fmt])
    except ImportError as exc:
        raise NotImplementedError(f"格式 {fmt!r} 的写出器未就绪（PDF 由 T5b 提供）") from exc
    return module.write
