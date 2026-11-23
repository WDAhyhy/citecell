"""值类型子包内共享的字符表与正则片段（纯标准库）。"""

from __future__ import annotations

import re
from decimal import Decimal

# 全角 → 半角的逐字符映射（长度保持不变，保证 find_mentions 的原文坐标不漂移）。
# 不做 NFKC：NFKC 会改变长度（如 "㎡" 展开），破坏 start/end。
_WIDTH_TABLE: dict[int, int | str] = {c: c - 0xFEE0 for c in range(0xFF01, 0xFF5F)}
_WIDTH_TABLE[0x3000] = 0x20  # 全角空格
_WIDTH_TABLE[0xFFE5] = "¥"  # ￥ → ¥（单字符对单字符）


def width_fold(text: str) -> str:
    """全角 ASCII 与全角空格折叠为半角；长度保持不变。"""
    return text.translate(_WIDTH_TABLE)


# 中文数字字符
CN_DIGITS: dict[str, int] = {
    "〇": 0, "零": 0,
    "一": 1, "壹": 1,
    "二": 2, "贰": 2, "两": 2,
    "三": 3, "叁": 3,
    "四": 4, "肆": 4,
    "五": 5, "伍": 5,
    "六": 6, "陆": 6,
    "七": 7, "柒": 7,
    "八": 8, "捌": 8,
    "九": 9, "玖": 9,
}
CN_SMALL_UNITS: dict[str, int] = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
CN_BIG_UNITS: dict[str, int] = {"万": 10**4, "亿": 10**8}
CN_UNIT_CHARS = "".join(CN_SMALL_UNITS) + "".join(CN_BIG_UNITS)
CN_DIGIT_CHARS = "".join(CN_DIGITS)
CN_NUMERAL_CHARS = CN_DIGIT_CHARS + "".join(CN_SMALL_UNITS) + "".join(CN_BIG_UNITS)

# 阿拉伯数字（只用 [0-9]，不用 \d，避免混入其他 Unicode 数字）。
# 千分位允许逗号或单个空格；小数点只接受 "."。
NUMERIC = r"(?:[0-9]{1,3}(?:[, ][0-9]{3})+(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)"

SCALE: dict[str, Decimal] = {"千": Decimal(1000), "万": Decimal(10**4), "亿": Decimal(10**8)}

def to_decimal(numeric: str) -> Decimal:
    """把 NUMERIC 片段（可含千分位逗号/空格）转换为 Decimal。"""
    return Decimal(numeric.replace(",", "").replace(" ", ""))
