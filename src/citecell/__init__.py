"""CiteCell: 可溯源的多文档键值抽取系统。

本包在里程碑 1 阶段只包含接口契约（models.py / interfaces.py），
具体实现由后续任务（见 docs/TASKS.md）补齐。
"""

from citecell.models import CONTRACT_VERSION

__all__ = ["CONTRACT_VERSION", "__version__"]

__version__ = "0.1.0"
