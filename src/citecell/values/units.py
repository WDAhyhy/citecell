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
from typing import NamedTuple

from citecell.values._common import CN_NUMERAL_CHARS, width_fold

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


# 中文数字后的「已知单位」（用于单字数字与中文数字串的提及识别）。
# 比 UNIT_CANON 更严：裸「月」「日」「时」不列入（「三月」「一日三餐」「一时」有其他意义）；
# 「年」后接「级」视为非单位。
_CN_KNOWN_RE = re.compile(
    r"\s*\(?\s*(?:"
    r"(?P<pre>个)\s*(?P<w1>月份|月|天|工作日|年|周|星期|小时)"
    r"|(?P<w2>天|工作日|年|周|星期|小时)(?!级)"
    r")\s*\)?"
)


def match_cn_known_unit(folded: str, pos: int) -> tuple[int, str] | None:
    """中文数字之后的已知单位（窗口内）。返回 (结束位置, 规范单位)，否则 None。"""
    m = _CN_KNOWN_RE.match(folded[pos : pos + UNIT_WINDOW])
    if not m:
        return None
    word = m.group("w1") or m.group("w2")
    return pos + m.end(), canon_unit(word)


# 表内规范单位集合：parse_value 只接受这些单位（或键声明的单位本身），
# 以免「一元化」「五金」中的汉字被当作单位。
KNOWN_UNITS: frozenset[str] = frozenset(UNIT_CANON.values())


class UnitSpan(NamedTuple):
    """unit_after 的结果。start/end 为原文坐标（与折叠文本等长）；unit 为规范单位。"""

    start: int
    end: int
    unit: str


def unit_after(text: str, pos: int, *, window: int = UNIT_WINDOW) -> UnitSpan | None:
    """识别 text[pos:] 中紧随其后的单位。

    - 从 pos 起跳过空白、半角/全角括号、连字符，然后识别单位（表内写法或 1–2 个汉字的表外写法）。
    - 搜索范围为 pos 起 window 个字符（默认 8）。
    - 返回 UnitSpan(start=pos, end=单位段终点, unit=规范单位)；text[start:end] 即原文中的单位段
      （包含跳过的括号和空白）。未识别、或只识别到「个」等无意义前缀时返回 None。
    - 只识别单位本身，不判断单位与数值是否匹配；冲突判定由调用方（Verifier）完成。
    - 本函数不改变私有函数 match_unit 的行为。
    """
    folded = width_fold(text)
    limit = min(len(folded), pos + window)
    j = pos
    while j < limit and folded[j] in " ()-":
        j += 1
    if j >= limit:
        return None
    found = match_unit(folded[:limit], j)
    if found is None:
        return None
    end, unit = found
    if not unit:  # 只有「个」等前缀，没有单位
        return None
    return UnitSpan(start=pos, end=end, unit=unit)
