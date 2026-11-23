"""数字单位识别与规范化（ARCH §6.3 NUMBER 单位规则）。

设计说明（写在这里以便审查）：
- 「工作日」与「天」**不合并**：工作日不含周末，12 工作日与 12 天天数不可互换，
  合并会把真实冲突吞掉。
- 单位写在数字后约 8 个字符的窗口内；允许「个」、空格、全/半角括号、英文单词。
- 提及区间包含所识别的单位（与 test_find.py 中 surface == "12个月" 的既有断言一致）。
- 只有 spec.unit 存在时才捕获单位；未写明单位的一方不与另一方冲突。
"""

from decimal import Decimal

import pytest

from citecell.interfaces import ValueParseError
from citecell.models import KeySpec, NumberValue, ValueType
from citecell.values import ValueNormalizerV1
from citecell.values.units import canon_unit

N = ValueNormalizerV1()
DAY = KeySpec(key="期限", value_type=ValueType.NUMBER, unit="天")
MONTH = KeySpec(key="期限", value_type=ValueType.NUMBER, unit="月")


def _single(text: str, key: KeySpec) -> NumberValue:
    ms = N.find_mentions(text, key)
    assert len(ms) == 1, [m.surface for m in ms]
    return ms[0].value


# 写法 → 期望的规范单位。这些写法在修复前全部会漏掉或得到原样单位。
WIDENED = [
    ("12个月以内", "月"),
    ("12个工作日", "工作日"),
    ("12（月）", "月"),
    ("12(月)", "月"),
    ("12 months", "月"),
    ("12月份", "月"),
    ("12 个月", "月"),
]


@pytest.mark.parametrize(("text", "unit"), WIDENED)
def test_widened_unit_forms_are_recognised(text, unit):
    v = _single(text, DAY)
    assert v.unit == unit
    # 对照：spec.unit=天 时，除「天」类之外都必须是单位冲突，而不是与「12」一致
    if unit != "天":
        assert not N.equivalent(v, NumberValue(value=Decimal(12), unit="天"), DAY)


# 对照组：spec.unit=天，这些应与 12天 等价
CONTROL_DAY = [
    ("12天", "天"),
    ("12 days", "天"),
    ("12日", "天"),
    ("12自然日", "天"),
    ("12 个天", "天"),
]


@pytest.mark.parametrize(("text", "unit"), CONTROL_DAY)
def test_day_synonyms_are_equivalent_to_day(text, unit):
    v = _single(text, DAY)
    assert v.unit == unit
    assert N.equivalent(v, NumberValue(value=Decimal(12), unit="天"), DAY)


@pytest.mark.parametrize(
    ("text", "unit"),
    [("12 weeks", "周"), ("12星期", "周"), ("2 years", "年"), ("3年度", "年"), ("5小时", "小时"), ("5 hours", "小时")],
)
def test_other_canonical_units(text, unit):
    assert _single(text, DAY).unit == unit


def test_unknown_unit_kept_verbatim():
    assert _single("12件", DAY).unit == "件"


def test_unit_beyond_window_is_not_captured():
    v = _single("12很多很多很多很多个月", DAY)
    assert v.unit is None  # 超出窗口：不捕获，视为未写明单位，不冲突


def test_workday_and_day_are_not_interchangeable():
    wd = NumberValue(value=Decimal(12), unit="工作日")
    day = NumberValue(value=Decimal(12), unit="天")
    assert not N.equivalent(wd, day, DAY)
    assert N.equivalent(wd, NumberValue(value=Decimal("12.0"), unit="工作日"), DAY)


def test_month_and_day_conflict_and_missing_unit_does_not_conflict():
    assert not N.equivalent(NumberValue(value=Decimal(12), unit="月"), NumberValue(value=Decimal(12), unit="天"), DAY)
    assert N.equivalent(NumberValue(value=Decimal(12), unit=None), NumberValue(value=Decimal(12), unit="天"), DAY)


def test_equivalent_normalizes_raw_units_in_inputs():
    # 模型直接给出的未规范化单位，比较时也要先规范化
    assert N.equivalent(NumberValue(value=Decimal(12), unit="个月"), NumberValue(value=Decimal(12), unit="months"), MONTH)


@pytest.mark.parametrize(
    ("text", "unit"),
    [("12个工作日", "工作日"), ("12 months", "月"), ("12（月）", "月"), ("12天", "天")],
)
def test_parse_value_uses_same_unit_rules(text, unit):
    assert N.parse_value(text, DAY if unit != "月" else MONTH).unit == unit


def test_parse_value_rejects_trailing_context_and_numeral_units():
    with pytest.raises(ValueParseError):
        N.parse_value("12个月以内", DAY)  # 值原文应只含数值与单位
    with pytest.raises(ValueParseError):
        N.parse_value("三点五", DAY)


def test_canon_table_keys_are_stable():
    assert canon_unit("个月") == canon_unit("months") == canon_unit("月份") == "月"
    assert canon_unit("工作日") == canon_unit("个工作日") == "工作日"
    assert canon_unit("日历日") == "天"
