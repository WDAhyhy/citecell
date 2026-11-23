"""数字词法严格化（第三轮）。

规则：
- 候选数字串 = 以数字开头和结尾的最大串（数字、`,`、`.`，全角 `，` `．` 已折叠为半角）。
- 全角逗号 `，` 仅当「前段 1–3 位数字、后段恰好 3 位数字」时作为千分位并入；否则在此切开
  （"5，5" 是两个数字）。
- 整个候选串必须是合法数字才算提及；非法则整串都不是提及，不取其中一段（"12.5.3" 不得取出 12.5）。
- 合法数字：千分位第一组 1–3 位、之后每组恰好 3 位；小数点最多一个。
- 空白不是千分位：「1 200」「010 123 456」「12 500 6000」是多个数字。
  旧行为（"1 200" → 1200，NUMERIC 允许空格分组）已废止；这是第三轮的明确要求。
  例外：货币前缀金额（"￥1 234.50"）保留空格分组，因为 T2 验收表要求它为 1234.5，
  且前缀 ¥/￥/RMB/人民币 使其不会与表格相邻列混淆；无前缀时空白一律不分组。
"""

from decimal import Decimal

import pytest

from citecell.interfaces import ValueParseError
from citecell.models import KeySpec, MoneyValue, NumberValue, ValueType
from citecell.values import ValueNormalizerV1

N = ValueNormalizerV1()
NUM = KeySpec(key="n", value_type=ValueType.NUMBER)
MONEY = KeySpec(key="m", value_type=ValueType.MONEY)


@pytest.mark.parametrize(
    ("text", "surfaces", "values"),
    [
        ("1,234,567.89", ["1,234,567.89"], ["1234567.89"]),
        ("12.5", ["12.5"], ["12.5"]),
        ("1,2345", [], []),
        ("12,34", [], []),
        ("1,23,456", [], []),
        ("12.5.3", [], []),
        ("1.2.3", [], []),
        ("1 200", ["1", "200"], ["1", "200"]),
        ("010 123 456", ["010", "123", "456"], ["10", "123", "456"]),
        ("12 500 6000", ["12", "500", "6000"], ["12", "500", "6000"]),
        ("5，5", ["5", "5"], ["5", "5"]),  # 第二个 5 不得漏检
        ("12，34", ["12", "34"], ["12", "34"]),
        ("1，234", ["1，234"], ["1234"]),  # 全角千分位
        ("1,234，567", ["1,234，567"], ["1234567"]),
    ],
)
def test_find_uses_whole_numeric_token(text, surfaces, values):
    ms = N.find_mentions(text, NUM)
    assert [m.surface for m in ms] == surfaces
    assert [m.value.value for m in ms] == [Decimal(v) for v in values]
    for m in ms:
        assert text[m.start : m.end] == m.surface


@pytest.mark.parametrize("text", ["1,2345", "12,34", "1,23,456", "12.5.3", "1.2.3", "1 200", "010 123 456", "12 500 6000", "5，5"])
def test_parse_rejects_illegal_or_multi_numeric_strings(text):
    with pytest.raises(ValueParseError):
        N.parse_value(text, NUM)


def test_parse_accepts_standard_grouping_and_decimal():
    assert N.parse_value("1,234,567.89", NUM) == NumberValue(value=Decimal("1234567.89"))
    assert N.parse_value("12.5", NUM) == NumberValue(value=Decimal("12.5"))
    assert N.parse_value("1，234", NUM) == NumberValue(value=Decimal("1234"))


def test_money_prefixed_space_grouping_kept_for_acceptance_table():
    assert N.parse_value("￥1 234.50", MONEY) == MoneyValue(amount=Decimal("1234.5"))


def test_money_unprefixed_space_grouping_rejected():
    with pytest.raises(ValueParseError):
        N.parse_value("1 234.50元", MONEY)
