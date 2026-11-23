"""校验、定位与合并（T7，规则见 docs/ARCHITECTURE.md §6.2 / §6.4 / §6.5）。

导出：
* ``Verifier``：确定性校验器（Candidate → ExtractionRecord）。
* ``LocalizerV1``：块内字符区间 → PDF 框 / DOCX / XLSX 定位。
* ``SimpleMerger``：同一 (文档, 键) 的多条记录合并。
* ``RETRYABLE_ISSUE_CODES``：可重试的问题代码集合（供 T10 使用）。
* ``strip_render_noise``：渲染前缀剥离（纯函数）。

默认导入路径不依赖任何 PDF 后端，也不导入 PyMuPDF。
"""

from citecell.verify.localizer import LocalizerV1
from citecell.verify.merger import SimpleMerger
from citecell.verify.render_noise import strip_render_noise
from citecell.verify.retryable import RETRYABLE_ISSUE_CODES
from citecell.verify.verifier import Verifier

__all__ = [
    "RETRYABLE_ISSUE_CODES",
    "LocalizerV1",
    "SimpleMerger",
    "Verifier",
    "strip_render_noise",
]
