"""日期解析与提及识别（不含时间）。

支持（M1，ARCH §6.3）：
- 中文：`2024年3月5日`、`2024年3月`、`2024年`；月、日可为阿拉伯或中文数字（`十二月`、`十五日`、`号` 同 `日`）。
  年必须是 4 个数字字符，阿拉伯或中文逐位（`二〇二四年`）。
- 分隔符：`2024-03-05`、`2024/3/5`、`2024.3.5`，以及 `2024-03`、`2024/3`（月精度）。
  分隔符在一个日期内必须一致；`.` 形式必须包含日（否则与小数 2024.3 混淆）。
- 拒绝：`03/05/2024`（年份不在首位）、`24年3月5日`（两位年）、`2024年2月30日`（非法日期）。
"""

from __future__ import annotations

import datetime as _dt
import re

from citecell.interfaces import ValueParseError
from citecell.models import DateValue
from citecell.values._common import CN_DIGITS, width_fold
from citecell.values.numerals import parse_cn_integer

_CN_YEAR_DIGITS = "〇零一二三四五六七八九"
_YEAR = rf"(?:[0-9]{{4}}|[{_CN_YEAR_DIGITS}]{{4}})"
_MD = r"(?:[0-9]{1,2}|[一二三四五六七八九十零〇壹贰叁肆伍陆柒捌玖拾两]{1,3})"

_CN_DATE_FULL = re.compile(
    rf"^(?P<y>{_YEAR})年(?:(?P<m>{_MD})月(?:(?P<d>{_MD})[日号])?)?$"
)
_SEP_DATE_FULL = re.compile(r"^(?P<y>[0-9]{4})(?P<sep>[-/.])(?P<m>[0-9]{1,2})(?:(?P=sep)(?P<d>[0-9]{1,2}))?$")

# 查找用（之后用 parse 验证）
_CN_DATE_FIND = re.compile(rf"(?<![0-9A-Za-z]){_YEAR}年(?:{_MD}月(?:{_MD}[日号])?)?")
_SEP_DATE_FIND = re.compile(r"(?<![0-9A-Za-z.\-/])[0-9]{4}([-/.])[0-9]{1,2}(?:\1[0-9]{1,2})?(?![0-9])")


def _year(text: str) -> int:
    if len(text) == 4 and all(c in CN_DIGITS for c in text):
        return int("".join(str(CN_DIGITS[c]) for c in text))
    if len(text) == 4 and text.isdigit() and text.isascii():
        return int(text)
    raise ValueParseError("年份必须为 4 位")


def _month_or_day(text: str) -> int:
    if text.isascii() and text.isdigit():
        return int(text)
    return parse_cn_integer(text)


def _build(year: int, month: int | None, day: int | None) -> DateValue:
    try:
        if month is None:
            _dt.date(year, 1, 1)
            return DateValue(iso=f"{year:04d}", precision="year")
        if day is None:
            _dt.date(year, month, 1)
            return DateValue(iso=f"{year:04d}-{month:02d}", precision="month")
        _dt.date(year, month, day)
        return DateValue(iso=f"{year:04d}-{month:02d}-{day:02d}", precision="day")
    except (ValueError, TypeError) as exc:  # 非法日期（含 pydantic ValidationError）
        raise ValueParseError("非法日期") from exc


def parse_date_text(text: str) -> DateValue:
    """解析日期原文。失败抛 ValueParseError（不回显输入）。"""
    s = width_fold(text.strip())

    m = _CN_DATE_FULL.match(s)
    if m:
        year = _year(m.group("y"))
        month = _month_or_day(m.group("m")) if m.group("m") else None
        day = _month_or_day(m.group("d")) if m.group("d") else None
        if day is not None and month is None:
            raise ValueParseError("日需要与月一起出现")
        return _build(year, month, day)

    m = _SEP_DATE_FULL.match(s)
    if m:
        if m.group("sep") == "." and m.group("d") is None:
            raise ValueParseError("点分隔的日期必须包含日")
        year = int(m.group("y"))
        month = int(m.group("m"))
        day = int(m.group("d")) if m.group("d") else None
        return _build(year, month, day)

    raise ValueParseError("无法解析为日期")


def find_date_spans(text: str) -> list[tuple[int, int]]:
    """返回已验证可解析的日期区间，按 start 升序、互不重叠。"""
    folded = width_fold(text)
    spans: list[tuple[int, int]] = []
    for pattern in (_CN_DATE_FIND, _SEP_DATE_FIND):
        for m in pattern.finditer(folded):
            try:
                parse_date_text(text[m.start() : m.end()])
            except ValueParseError:
                continue
            spans.append((m.start(), m.end()))
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    result: list[tuple[int, int]] = []
    for start, end in spans:
        if result and start < result[-1][1]:
            continue
        result.append((start, end))
    return result
