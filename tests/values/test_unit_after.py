"""公共接口 values.units.unit_after（供 T7 使用）。

unit_after(text, pos, *, window=8) -> UnitSpan | None
- 从 pos 起跳过空白、半/全角括号、连字符，识别其后的单位（窗口 window 个字符内）。
- 返回 UnitSpan(start, end, unit)：start == pos；end 为单位段（含跳过的括号/空白）的终点；
  unit 为规范单位（表外写法原样）。text[start:end] 即原文中的单位段。
- 不改变私有函数 match_unit 的行为。
"""

import pytest

from citecell.values.units import UnitSpan, unit_after


@pytest.mark.parametrize(
    ("text", "surface", "unit"),
    [
        ("12（月）", "（月）", "月"),
        ("12(月)", "(月)", "月"),
        ("12 months", " months", "月"),
        ("12 个月", " 个月", "月"),
        ("12　个月", "　个月", "月"),  # 全角空格
        ("12-month", "-month", "月"),
        ("12 周", " 周", "周"),
        ("12个工作日", "个工作日", "工作日"),
        ("12天", "天", "天"),
        ("12 人次", " 人次", "人次"),  # 表外单位原样
    ],
)
def test_recognises_unit_after_separators(text, surface, unit):
    span = unit_after(text, 2)
    assert span == UnitSpan(start=2, end=2 + len(surface), unit=unit)
    assert text[span.start : span.end] == surface


def test_returns_none_when_no_unit():
    assert unit_after("12，共", 2) is None
    assert unit_after("12", 2) is None


def test_window_limits_search():
    assert unit_after("12很多很多很多很多个月", 2) is None
    assert unit_after("12 个月", 2, window=2) is None  # 窗口只够空白和「个」，不足以到达「月」


def test_position_in_middle_of_text():
    text = "期限12个月及3天"
    # "期限12个月及3天"：索引 2–3 为"12"，4–6 为"个月"，7 为"3"，8 为"天"
    span = unit_after(text, 4)
    assert span == UnitSpan(start=4, end=6, unit="月")
    assert text[span.start : span.end] == "个月"
    assert unit_after(text, 8) == UnitSpan(start=8, end=9, unit="天")
