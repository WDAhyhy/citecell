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
NUMERIC = r"(?:[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)"
"""严格数字：千分位仅用逗号，第一组 1–3 位、之后每组恰好 3 位；小数点最多一个。空白不分组。"""

# 仅用于「带货币前缀」的金额（¥/￥/RMB/人民币 之后）：允许空格分组，如 "￥1 234.50"。
NUMERIC_LOOSE = r"(?:[0-9]{1,3}(?:[, ][0-9]{3})+(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)"

SCALE: dict[str, Decimal] = {"千": Decimal(1000), "万": Decimal(10**4), "亿": Decimal(10**8)}

_RUN_RE = re.compile(r"[0-9](?:[0-9,.]*[0-9])?")
_STRICT_RE = re.compile(r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?")


def is_strict_numeric(s: str) -> bool:
    """s 是否为合法的严格数字（ASCII，逗号千分位，至多一个小数点）。"""
    return _STRICT_RE.fullmatch(s) is not None


def strict_numeric_tokens(text: str) -> list[tuple[int, int, str]]:
    """扫描文本中的严格数字候选。返回 (start, end, ascii)，坐标为原文坐标（长度与折叠文本一致）。

    规则：
    - 候选为以数字开头、结尾的最大串（数字、`,`、`.`）。全角逗号 `，` 仅当「前段 1–3 位、后段恰好
      3 位」时并入千分位，否则在此切开（"5，5" 为两个数字）。
    - 每个候选整体必须合法，否则整串都不是提及（"12.5.3"、"1,2345" 不产生任何提及）。
    - 空白从不分组（"1 200" 为两个数字）。
    """
    folded = width_fold(text)
    out: list[tuple[int, int, str]] = []
    for m in _RUN_RE.finditer(folded):
        s, e = m.span()
        cuts: list[int] = []
        for i in range(s, e):
            if folded[i] != "," or text[i] != "，":
                continue
            j = i - 1
            while j >= s and folded[j].isdigit():
                j -= 1
            left = folded[j + 1 : i]
            k = i + 1
            while k < e and folded[k].isdigit():
                k += 1
            right = folded[i + 1 : k]
            # 左段：自上一个分隔符（或串首）起的数字；右段：到下一个分隔符（或串尾）的数字
            if not (1 <= len(left) <= 3 and len(right) == 3):
                cuts.append(i)
        bounds_start = [s] + [c + 1 for c in cuts]
        bounds_end = cuts + [e]
        for ps, pe in zip(bounds_start, bounds_end):
            piece = folded[ps:pe]
            if is_strict_numeric(piece):
                out.append((ps, pe, piece))
    return out


def to_decimal(numeric: str) -> Decimal:
    """把 NUMERIC 片段（可含千分位逗号/空格）转换为 Decimal。"""
    return Decimal(numeric.replace(",", "").replace(" ", ""))
