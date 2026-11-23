"""find_mentions / equivalent / 协议一致性。"""

import random
from decimal import Decimal

import pytest

from citecell.interfaces import ValueNormalizer
from citecell.models import (
    BooleanValue,
    DateValue,
    EnumOption,
    EnumValue,
    KeySpec,
    MoneyValue,
    NumberValue,
    PercentValue,
    ValueType,
)
from citecell.values import ValueNormalizerV1

N = ValueNormalizerV1()


def test_satisfies_value_normalizer_protocol():
    assert isinstance(N, ValueNormalizer)


def test_spec_money_example_finds_two_mentions_with_exact_spans():
    text = "合同总价为人民币壹佰贰拾万元整（¥1,200,000.00）"
    key = KeySpec(key="总价", value_type=ValueType.MONEY)
    ms = N.find_mentions(text, key)
    assert len(ms) == 2
    assert [m.value.amount for m in ms] == [Decimal("1200000"), Decimal("1200000.00")]
    for m in ms:
        assert text[m.start : m.end] == m.surface
    assert ms[0].surface == "人民币壹佰贰拾万元整"
    assert ms[1].surface == "¥1,200,000.00"
    assert ms[0].start < ms[1].start


def test_money_mentions_require_marker():
    key = KeySpec(key="金额", value_type=ValueType.MONEY)
    assert N.find_mentions("编号 1200 共 3 项", key) == []


def test_percent_mentions_include_chinese_form():
    key = KeySpec(key="比例", value_type=ValueType.PERCENT)
    ms = N.find_mentions("增长15%，占比百分之十五；系数0.15不算", key)
    assert [m.surface for m in ms] == ["15%", "百分之十五"]
    assert all(m.value == PercentValue(value=Decimal("15")) for m in ms)


def test_date_mentions_skip_ambiguous_forms():
    key = KeySpec(key="日期", value_type=ValueType.DATE)
    text = "签署于2024年3月5日，有效期至2025-03，旧格式03/05/2024不算"
    ms = N.find_mentions(text, key)
    assert [m.value for m in ms] == [
        DateValue(iso="2024-03-05", precision="day"),
        DateValue(iso="2025-03", precision="month"),
    ]


def test_number_cn_run_requires_unit_character():
    key = KeySpec(key="数量", value_type=ValueType.NUMBER)
    ms = N.find_mentions("一般为一百零五套", key)
    assert [m.value for m in ms] == [NumberValue(value=Decimal("105"))]
    assert ms[0].surface == "一百零五"


def test_number_unit_capture_matches_spec_unit():
    key = KeySpec(key="期限", value_type=ValueType.NUMBER, unit="月")
    ms = N.find_mentions("服务期12个月，试用3天", key)
    assert ms[0].surface == "12个月"
    assert ms[0].value == NumberValue(value=Decimal("12"), unit="月")
    # 不同单位：仍识别为提及，单位为"天"，由 Verifier 判 UNIT_MISMATCH
    assert ms[1].surface == "3天"
    assert ms[1].value == NumberValue(value=Decimal("3"), unit="天")


def test_number_without_spec_unit_has_no_unit():
    key = KeySpec(key="数量", value_type=ValueType.NUMBER)
    ms = N.find_mentions("共12个月", key)
    assert ms[0].value == NumberValue(value=Decimal("12"), unit=None)
    assert ms[0].surface == "12"


def test_enum_alias_maps_to_canonical_value():
    key = KeySpec(
        key="当事人",
        value_type=ValueType.ENUM,
        enum_options=[EnumOption(value="甲方", aliases=["买方"]), EnumOption(value="乙方", aliases=["卖方"])],
    )
    ms = N.find_mentions("买方及卖方签字", key)
    assert [m.value for m in ms] == [EnumValue(value="甲方"), EnumValue(value="乙方")]
    assert N.parse_value("买方", key) == EnumValue(value="甲方")
    with pytest.raises(Exception):
        N.parse_value("丙方", key)


def test_boolean_longest_alias_wins_no_overlap():
    key = KeySpec(
        key="含税",
        value_type=ValueType.BOOLEAN,
        enum_options=[EnumOption(value="true", aliases=["含税"]), EnumOption(value="false", aliases=["不含税"])],
    )
    ms = N.find_mentions("报价不含税", key)
    assert [m.value for m in ms] == [BooleanValue(value=False)]
    assert ms[0].surface == "不含税"
    assert N.parse_value("含税", key) == BooleanValue(value=True)


def test_text_type_has_no_mention_detection():
    assert N.find_mentions("任意文本", KeySpec(key="名称", value_type=ValueType.TEXT)) == []


def test_equivalence_rules():
    money = KeySpec(key="金额", value_type=ValueType.MONEY)
    assert N.equivalent(MoneyValue(amount=Decimal(25)), MoneyValue(amount=Decimal("25.00")), money)
    assert not N.equivalent(MoneyValue(amount=Decimal(25)), MoneyValue(amount=Decimal(25), currency="USD"), money)
    d_month = DateValue(iso="2024-03", precision="month")
    d_day = DateValue(iso="2024-03-01", precision="day")
    assert not N.equivalent(d_month, d_day, KeySpec(key="d", value_type=ValueType.DATE))
    num = KeySpec(key="n", value_type=ValueType.NUMBER, unit="月")
    assert N.equivalent(NumberValue(value=Decimal(12), unit="月"), NumberValue(value=Decimal("12.0")), num)
    assert not N.equivalent(MoneyValue(amount=Decimal(1)), NumberValue(value=Decimal(1)), money)


def _random_text(rng: random.Random) -> str:
    pool = "壹贰叁肆伍陆柒捌玖拾佰仟万亿元角分整零一二三十百千 ,.%年月日0123456789¥￥人民币百分之负-/"
    return "".join(rng.choice(pool) for _ in range(rng.randrange(0, 40)))


@pytest.mark.parametrize(
    "key",
    [
        KeySpec(key="m", value_type=ValueType.MONEY),
        KeySpec(key="p", value_type=ValueType.PERCENT),
        KeySpec(key="d", value_type=ValueType.DATE),
        KeySpec(key="n", value_type=ValueType.NUMBER, unit="月"),
    ],
)
def test_property_mentions_are_ordered_disjoint_and_exact(key):
    """随机串上：不抛异常；区间与 surface 一致；按 start 升序且互不重叠；每个提及可被其规范值再次解析。"""
    rng = random.Random(0)
    for _ in range(1000):
        text = _random_text(rng)
        ms = N.find_mentions(text, key)
        last_end = -1
        for m in ms:
            assert 0 <= m.start < m.end <= len(text)
            assert text[m.start : m.end] == m.surface
            assert m.start >= last_end
            last_end = m.end
            assert N.equivalent(m.value, m.value, key)


def test_colloquial_shorthand_is_not_returned_as_mention():
    """口语简写不得产生 120/35000 等提及；标准写法仍须被识别。"""
    num = KeySpec(key="数量", value_type=ValueType.NUMBER)
    assert N.find_mentions("共一百二，另有二百五套", num) == []
    ms = N.find_mentions("共一百二十套，另一百零二套", num)
    assert [m.value for m in ms] == [NumberValue(value=Decimal("120")), NumberValue(value=Decimal("102"))]
    assert [m.surface for m in ms] == ["一百二十", "一百零二"]
