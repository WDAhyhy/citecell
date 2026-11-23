"""文本归一化与引文匹配（T1，规则见 docs/ARCHITECTURE.md §6.1 / §6.2）。"""

from citecell.textnorm.match import Matcher
from citecell.textnorm.normalize import VERSION as NORMALIZER_VERSION
from citecell.textnorm.normalize import Normalizer

__all__ = ["Matcher", "Normalizer", "NORMALIZER_VERSION"]
