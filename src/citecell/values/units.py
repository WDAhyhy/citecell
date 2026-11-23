"""NUMBER 单位：规范化表与识别窗口。

规则：
- 单位写在数字之后，窗口为 UNIT_WINDOW 个字符（折叠为半角后计）。
- 允许中间有「个」、空格、括号（半角 `()` 或全角 `（）`，折叠后相同）。
- 英文单位按整词匹配（不区分大小写）。
- 表内写法归一化为规范形式；表外的 1–2 个汉字保持原样（不含中文数字字符）。

「工作日」与「天」刻意不合并：工作日不含周末，二者天数不可互换。
"""

from __future__ import annotations

import re

from citecell.values._common import CN_NUMERAL_CHARS

UNIT_WINDOW = 8

# 原始写法 → 规范单位。键不含前缀「个」（识别时会去掉）。
UNIT_CANON: dict[str, str] = {
    # 月
    "月": "月",
    "月份": "月",
    "month": "月",
    "months": "月",
    # 天（自然日）
    "天": "天",
    "日": "天",
    "自然日": "天",
    "日历日": "天",
    "day": "天",
    "days": "天",
    # 工作日：与「天」区分，见模块 docstring
    "工作日": "工作日",
    # 年
    "年": "年",
    "年度": "年",
    "year": "年",
    "years": "年",
    # 周
    "周": "周",
    "星期": "周",
    "week": "周",
    "weeks": "周",
    # 小时
    "小时": "小时",
    "时": "小时",
    "hour": "小时",
    "hours": "小时",
}

_WORDS = sorted(UNIT_CANON, key=len, reverse=True)
_TABLE_RE = re.compile(
    r"\s*\(?\s*(?:个\s*)?(?P<word>"
    + "|".join(re.escape(w) for w in _WORDS)
    + r")(?![A-Za-z])\s*\)?",
    re.IGNORECASE,
)
_GENERIC_RE = re.compile(r"\s*\(?\s*(?:个\s*)?(?P<raw>[一-鿿]{1,2})(?![一-鿿])\s*\)?")


def canon_unit(raw: str) -> str:
    """把单位原文归一化到规范形式；表外写法原样返回（去掉前缀「个」与首尾空白）。"""
    key = raw.strip().removeprefix("个").strip()
    return UNIT_CANON.get(key.casefold(), key)


def match_unit(folded: str, pos: int) -> tuple[int, str] | None:
    """在 folded[pos:] 的窗口内识别单位。返回 (结束位置, 规范单位)，未识别返回 None。

    folded 必须是已 width_fold 的文本（长度与原文一致，因此位置可直接用于原文切片）。
    """
    window = folded[pos : pos + UNIT_WINDOW]
    m = _TABLE_RE.match(window)
    if m:
        return pos + m.end(), canon_unit(m.group("word"))
    m = _GENERIC_RE.match(window)
    if m and not any(ch in CN_NUMERAL_CHARS for ch in m.group("raw")):
        return pos + m.end(), canon_unit(m.group("raw"))
    return None
