"""金额解析与提及识别。结果统一换算为元（Decimal）。

支持的形式（M1）：
- 阿拉伯：`¥1,234.50`、`￥1 234.50`、`RMB1200`、`人民币1,200元`、`1,200元整`、`120万元`、`1.2亿元`
  （前缀 ¥/￥/RMB/人民币，或后缀 元/圆 至少出现一个，否则视为普通数字）。
- 中文大写：`人民币壹佰贰拾万元整`、`壹仟零伍元伍角`、`壹拾元零伍分`、`叁万元整`。
  必须以 元/圆 结尾；角、分为单个数字字符；"整/正" 可选。
- 不支持：只有 角/分 没有 元/圆 的写法（"伍角"），以及负金额。
"""

from __future__ import annotations

import re
from decimal import Decimal

from citecell.interfaces import ValueParseError
from citecell.models import MoneyValue
from citecell.values._common import (
    CN_DIGIT_CHARS,
    CN_DIGITS,
    CN_NUMERAL_CHARS,
    NUMERIC,
    NUMERIC_LOOSE,
    SCALE,
    to_decimal,
    width_fold,
)
from citecell.values.numerals import parse_cn_integer

_PREFIX = r"(?:人民币|RMB|¥)"

_CN_MONEY_FULL = re.compile(
    rf"^(?:{_PREFIX})?"
    rf"(?P<int>[{CN_NUMERAL_CHARS}]+)(?P<unit>[元圆])"
    rf"(?:(?P<jiao>零?[{CN_DIGIT_CHARS}])角)?"
    rf"(?:(?P<fen>零?[{CN_DIGIT_CHARS}])分)?"
    rf"(?P<z>[整正])?$"
)
# 带前缀：允许空格分组（"￥1 234.50"）；无前缀：严格数字，空白不分组。
_ARABIC_MONEY_PREFIXED = re.compile(
    rf"^(?P<pre>{_PREFIX})\s?(?P<num>{NUMERIC_LOOSE})(?P<scale>[千万亿])?(?P<suf>[元圆])?(?P<z>[整正])?$"
)
_ARABIC_MONEY_PLAIN = re.compile(rf"^(?P<num>{NUMERIC})(?P<scale>[千万亿])?(?P<suf>[元圆])?(?P<z>[整正])?$")

# 查找用的较宽正则（之后逐个用 parse 验证，失败则丢弃）
_CN_MONEY_FIND = re.compile(
    rf"(?:{_PREFIX})?[{CN_NUMERAL_CHARS}]+[元圆](?:零?[{CN_DIGIT_CHARS}]角)?(?:零?[{CN_DIGIT_CHARS}]分)?[整正]?"
)
_ARABIC_MONEY_FIND = re.compile(
    rf"(?<![0-9A-Za-z.,\-/])(?:(?:{_PREFIX})\s?{NUMERIC_LOOSE}|{NUMERIC})[千万亿]?[元圆]?[整正]?"
)


def parse_money_text(text: str) -> Decimal:
    """把金额原文解析为以元计的 Decimal。失败抛 ValueParseError。"""
    s = width_fold(text.strip())
    m = _CN_MONEY_FULL.match(s)
    if m:
        amount = Decimal(parse_cn_integer(m.group("int")))
        if m.group("jiao"):
            amount += Decimal(CN_DIGITS[m.group("jiao")[-1]]) / 10
        if m.group("fen"):
            amount += Decimal(CN_DIGITS[m.group("fen")[-1]]) / 100
        return amount

    m = _ARABIC_MONEY_PREFIXED.match(s) or _ARABIC_MONEY_PLAIN.match(s)
    if m:
        if not (m.groupdict().get("pre") or m.group("suf")):
            raise ValueParseError("缺少金额标记（¥/人民币/元）")
        if m.group("z") and not m.group("suf"):
            raise ValueParseError("整/正 只能跟在 元/圆 之后")
        amount = to_decimal(m.group("num"))
        if m.group("scale"):
            amount *= SCALE[m.group("scale")]
        return amount

    raise ValueParseError("无法解析为金额")


def money_value(text: str, currency: str) -> MoneyValue:
    return MoneyValue(amount=parse_money_text(text), currency=currency)


def find_money_spans(text: str) -> list[tuple[int, int]]:
    """返回 (start, end) 列表，已验证可解析，按 start 升序、互不重叠。"""
    folded = width_fold(text)
    candidates: list[tuple[int, int]] = []
    for pattern in (_CN_MONEY_FIND, _ARABIC_MONEY_FIND):
        for m in pattern.finditer(folded):
            try:
                parse_money_text(text[m.start() : m.end()])
            except ValueParseError:
                continue
            candidates.append((m.start(), m.end()))
    return _non_overlapping(candidates)


def _non_overlapping(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """按 start 升序；同起点取较长者；丢弃与已保留区间重叠的区间。"""
    result: list[tuple[int, int]] = []
    for start, end in sorted(spans, key=lambda s: (s[0], -(s[1] - s[0]))):
        if result and start < result[-1][1]:
            continue
        result.append((start, end))
    return result

