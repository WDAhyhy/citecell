"""百分比解析。约定：返回值 15 表示 15%（不是 0.15）。

- 识别：`15%`、`15 %`、`15％`、`百分之十五`、`百分之15`。
- 不识别：`0.15`（无百分号，不当作百分比）。
"""

from __future__ import annotations

import re
from decimal import Decimal

from citecell.interfaces import ValueParseError
from citecell.values._common import CN_NUMERAL_CHARS, NUMERIC, to_decimal, width_fold
from citecell.values.numerals import parse_cn_integer

_PERCENT_FULL = re.compile(rf"^(?P<sign>-)?(?P<num>{NUMERIC}|[{CN_NUMERAL_CHARS}]+)\s?%$")
_PERCENT_WORD_FULL = re.compile(rf"^百分之(?P<num>{NUMERIC}|[{CN_NUMERAL_CHARS}]+)$")

_PERCENT_FIND = re.compile(rf"(?<![0-9A-Za-z.\-/])(?:-)?{NUMERIC}\s?%|百分之(?:{NUMERIC}|[{CN_NUMERAL_CHARS}]+)")


def _num_to_decimal(num: str) -> Decimal:
    if num[0] in "0123456789":
        return to_decimal(num)
    return Decimal(parse_cn_integer(num))


def parse_percent_text(text: str) -> Decimal:
    s = width_fold(text.strip())
    m = _PERCENT_FULL.match(s)
    if m:
        value = _num_to_decimal(m.group("num"))
        return -value if m.group("sign") else value
    m = _PERCENT_WORD_FULL.match(s)
    if m:
        return _num_to_decimal(m.group("num"))
    raise ValueParseError("无法解析为百分比")


def find_percent_spans(text: str) -> list[tuple[int, int]]:
    folded = width_fold(text)
    result: list[tuple[int, int]] = []
    for m in _PERCENT_FIND.finditer(folded):
        try:
            parse_percent_text(text[m.start() : m.end()])
        except ValueParseError:
            continue
        result.append((m.start(), m.end()))
    return result
