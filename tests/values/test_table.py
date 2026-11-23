"""T2 验收表（docs/TASKS.md T2）逐行参数化，外加少量边界用例。"""

from decimal import Decimal

import pytest

from citecell.interfaces import ValueParseError
from citecell.models import (
    DateValue,
    KeySpec,
    MoneyValue,
    NumberValue,
    PercentValue,
    ValueType,
)
from citecell.values import ValueNormalizerV1

N = ValueNormalizerV1()


def spec(vt: ValueType, **kw) -> KeySpec:
    return KeySpec(key="k", value_type=vt, **kw)


MONEY = spec(ValueType.MONEY)
NUMBER = spec(ValueType.NUMBER)
PERCENT = spec(ValueType.PERCENT)
DATE = spec(ValueType.DATE)


@pytest.mark.parametrize(
    ("text", "key", "expected"),
    [
        ("人民币壹佰贰拾万元整", MONEY, MoneyValue(amount=Decimal("1200000"))),
        ("壹仟零伍元伍角", MONEY, MoneyValue(amount=Decimal("1005.5"))),
        ("叁万元整", MONEY, MoneyValue(amount=Decimal("30000"))),
        ("壹拾元零伍分", MONEY, MoneyValue(amount=Decimal("10.05"))),
        ("¥1,234.50", MONEY, MoneyValue(amount=Decimal("1234.5"))),
        ("￥1 234.50", MONEY, MoneyValue(amount=Decimal("1234.5"))),
        ("１２３４.５元", MONEY, MoneyValue(amount=Decimal("1234.5"))),
        ("120万元", MONEY, MoneyValue(amount=Decimal("1200000"))),
        ("1.2亿元", MONEY, MoneyValue(amount=Decimal("120000000"))),
        ("一百零五", NUMBER, NumberValue(value=Decimal("105"))),
        ("两千", NUMBER, NumberValue(value=Decimal("2000"))),
        ("十五", NUMBER, NumberValue(value=Decimal("15"))),
        ("-3.5", NUMBER, NumberValue(value=Decimal("-3.5"))),
        ("负3.5", NUMBER, NumberValue(value=Decimal("-3.5"))),
        ("15%", PERCENT, PercentValue(value=Decimal("15"))),
        ("15 ％", PERCENT, PercentValue(value=Decimal("15"))),
        ("百分之十五", PERCENT, PercentValue(value=Decimal("15"))),
        ("2024年3月5日", DATE, DateValue(iso="2024-03-05", precision="day")),
        ("2024-03-05", DATE, DateValue(iso="2024-03-05", precision="day")),
        ("2024/3/5", DATE, DateValue(iso="2024-03-05", precision="day")),
        ("2024.3.5", DATE, DateValue(iso="2024-03-05", precision="day")),
        ("二〇二四年三月五日", DATE, DateValue(iso="2024-03-05", precision="day")),
        ("2024年3月", DATE, DateValue(iso="2024-03", precision="month")),
        ("2024年", DATE, DateValue(iso="2024", precision="year")),
    ],
)
def test_acceptance_table_parses(text, key, expected):
    assert N.parse_value(text, key) == expected


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("0.15", PERCENT),  # 无百分号不当作百分比
        ("03/05/2024", DATE),  # 年份不在首位
        ("24年3月5日", DATE),  # 两位年
        ("2024年2月30日", DATE),  # 非法日期
    ],
)
def test_acceptance_table_rejects(text, key):
    with pytest.raises(ValueParseError):
        N.parse_value(text, key)


def test_error_message_does_not_echo_long_input():
    long_text = "壹" * 80
    with pytest.raises(ValueParseError) as exc:
        N.parse_value(long_text, MONEY)
    assert long_text not in str(exc.value)
    assert len(str(exc.value)) < 50


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("伍角", MONEY),  # 只有角没有元：不支持
        ("壹元零", MONEY),  # 尾部补位零
        ("1,2,3元", MONEY),
        ("一二", NUMBER),  # 相邻数字
        ("十百", NUMBER),  # 单位未递减
        ("一百零", NUMBER),  # 尾部补位零
        ("三点五", NUMBER),  # 不支持小数的中文写法；无 unit 的键不得把"点五"当单位
        ("", NUMBER),
    ],
)
def test_invalid_inputs_raise(text, key):
    with pytest.raises(ValueParseError):
        N.parse_value(text, key)


def test_money_currency_from_spec():
    usd = spec(ValueType.MONEY, currency="USD")
    assert N.parse_value("100元", usd).currency == "USD"


def test_arabic_money_requires_marker():
    # 没有 ¥/人民币/元 标记的普通数字不是金额
    with pytest.raises(ValueParseError):
        N.parse_value("1234.5", MONEY)


def test_number_with_unit_from_spec():
    key = spec(ValueType.NUMBER, unit="月")
    assert N.parse_value("12个月", key) == NumberValue(value=Decimal("12"), unit="月")
    assert N.parse_value("12 个月", key) == NumberValue(value=Decimal("12"), unit="月")
    assert N.parse_value("12", key) == NumberValue(value=Decimal("12"), unit=None)
    # 单位不同不在解析阶段报错：返回原单位，由 Verifier 判 UNIT_MISMATCH
    assert N.parse_value("12天", key) == NumberValue(value=Decimal("12"), unit="天")


def test_number_without_unit_spec_rejects_unit_text():
    with pytest.raises(ValueParseError):
        N.parse_value("12个月", spec(ValueType.NUMBER))


def test_text_value_roundtrip():
    key = spec(ValueType.TEXT)
    assert N.parse_value("  甲方  ", key).text == "甲方"
    with pytest.raises(ValueParseError):
        N.parse_value("   ", key)
