"""不带量级的单个汉字数字 + 已知单位的提及（六个月、三天、一年）。

规则（写在测试里以便审查）：
- 只有紧跟「已知单位」时才算提及；已知单位限定为：个月、天、工作日、年、周、星期、小时，
  以及它们的「个」前缀形式。裸「月」「日」「时」不作为单字数字的单位：
  「三月」「一日三餐」「一时」常为月份或其他意义，不能当作数量。
- 「一年级」中的「年」后接「级」，视为非单位。
- 多字数字的提及优先：「十六个月」只产生一个提及（16 月），不再拆出「六个月」。
- 提及区间包含单位字符（与 test_find.py / test_units.py 一致）。
- 「第六个月」（序数）当前会产生提及（6 月）；T7 的「第」序数守卫负责排除。本测试记录该现状。
"""

from decimal import Decimal

import pytest

from citecell.models import KeySpec, NumberValue, ValueType
from citecell.values import ValueNormalizerV1

N = ValueNormalizerV1()
NUM = KeySpec(key="期限", value_type=ValueType.NUMBER)
NUM_DAY = KeySpec(key="期限", value_type=ValueType.NUMBER, unit="天")


def _mentions(text: str, key: KeySpec = NUM):
    return N.find_mentions(text, key)


@pytest.mark.parametrize(
    ("text", "surface", "value", "unit"),
    [
        ("六个月", "六个月", "6", "月"),
        ("三天", "三天", "3", "天"),
        ("一年", "一年", "1", "年"),
        ("一周", "一周", "1", "周"),
        ("两小时", "两小时", "2", "小时"),
        ("一个工作日", "一个工作日", "1", "工作日"),
        ("合同期限一年后终止", "一年", "1", "年"),
        ("共一天", "一天", "1", "天"),
    ],
)
def test_single_digit_with_known_unit_is_a_mention(text, surface, value, unit):
    ms = _mentions(text)
    assert len(ms) == 1, [m.surface for m in ms]
    assert ms[0].surface == surface
    assert ms[0].value == NumberValue(value=Decimal(value), unit=unit)
    assert text[ms[0].start : ms[0].end] == ms[0].surface


@pytest.mark.parametrize(
    "text",
    [
        "统一",
        "一些",
        "一次性付款",
        "第一",
        "一个人",
        "一日三餐",
        "一时兴起",
        "三月",  # 裸「月」不作单字数字的单位（可能是月份）
        "一年级",  # 「年」后接「级」：非单位
    ],
)
def test_ordinary_words_are_not_mentions(text):
    assert _mentions(text) == [], text


@pytest.mark.parametrize(
    ("text", "surface", "value", "unit"),
    [
        ("十六个月", "十六个月", "16", "月"),
        ("二十六个月", "二十六个月", "26", "月"),
        ("三百六十五天", "三百六十五天", "365", "天"),
        ("一百零五天", "一百零五天", "105", "天"),
    ],
)
def test_multi_char_numeral_is_one_mention_not_split(text, surface, value, unit):
    ms = _mentions(text)
    assert len(ms) == 1, [m.surface for m in ms]
    assert ms[0].surface == surface
    assert ms[0].value == NumberValue(value=Decimal(value), unit=unit)


def test_multi_char_numeral_with_spec_unit_keeps_single_mention():
    ms = _mentions("十六个月", KeySpec(key="期限", value_type=ValueType.NUMBER, unit="天"))
    assert [m.value for m in ms] == [NumberValue(value=Decimal(16), unit="月")]


def test_colloquial_shorthand_still_rejected():
    assert _mentions("一百二") == []
    assert _mentions("三万五") == []


def test_ordinal_month_current_behaviour_is_recorded():
    """现状：「第六个月」产生 6 月的提及（序数守卫在 T7）。若将来 T2 改为排除，请同步更新。"""
    ms = _mentions("第六个月")
    assert [m.surface for m in ms] == ["六个月"]
    assert ms[0].value == NumberValue(value=Decimal(6), unit="月")


def test_mixed_arabic_and_cn_mentions_do_not_overlap():
    ms = _mentions("共12个月及六个月")
    spans = [(m.start, m.end) for m in ms]
    assert all(a_end <= b_start for (_, a_end), (b_start, _) in zip(spans, spans[1:]))
    assert [m.surface for m in ms] == ["12", "六个月"]
