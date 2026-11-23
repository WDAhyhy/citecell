"""表面形式生成的单元测试（金额大写、中文日期、万元、百分比、XLSX_TEXT_V1 写出侧）。"""

import datetime as dt
from decimal import Decimal

import pytest

from citecell.synth.surface import (
    amount_to_uppercase,
    cn_date,
    cn_int,
    money_wan,
    percent_cn,
    xlsx_cell_text,
)


@pytest.mark.parametrize(
    "amount, expected",
    [
        (Decimal("1200000"), "壹佰贰拾万元整"),
        (Decimal("30000"), "叁万元整"),
        (Decimal("105"), "壹佰零伍元整"),
        (Decimal("10.05"), "壹拾元零伍分"),
        (Decimal("1005.5"), "壹仟零伍元伍角"),
    ],
)
def test_uppercase_examples_from_spec(amount, expected):
    assert amount_to_uppercase(amount, suffix_zheng=True) == expected


def test_uppercase_suffix_style_is_a_parameter():
    assert amount_to_uppercase(Decimal("105"), suffix_zheng=True) == "壹佰零伍元整"
    assert amount_to_uppercase(Decimal("105"), suffix_zheng=False) == "壹佰零伍元"
    assert amount_to_uppercase(Decimal("1200000"), suffix_zheng=False) == "壹佰贰拾万元"


def test_uppercase_rejects_sub_cent_precision():
    with pytest.raises(ValueError):
        amount_to_uppercase(Decimal("1.005"))


def test_uppercase_zero_and_zero_jiao():
    assert amount_to_uppercase(Decimal("0")) == "零元整"
    assert amount_to_uppercase(Decimal("100.50")) == "壹佰元伍角"


@pytest.mark.parametrize(
    "n, expected",
    [(10, "十"), (12, "十二"), (20, "二十"), (36, "三十六"), (105, "一百零五"),
     (110, "一百一十"), (10005, "一万零五"), (100000000, "一亿")],
)
def test_lowercase_numerals(n, expected):
    assert cn_int(n) == expected


def test_chinese_date_with_zero_year_digit():
    assert cn_date(dt.date(2024, 3, 5)) == "二〇二四年三月五日"
    assert cn_date(dt.date(2025, 10, 12)) == "二〇二五年十月十二日"


def test_wan_and_percent_surfaces():
    assert money_wan(Decimal("1200000")) == "120万元"
    with pytest.raises(ValueError):
        money_wan(Decimal("1005"))
    assert percent_cn(Decimal("15")) == "百分之十五"


def test_xlsx_text_v1_writer_side_rules():
    assert xlsx_cell_text(1200000, None) == "1200000"
    assert xlsx_cell_text(10.05, None) == "10.05"
    assert xlsx_cell_text(0.3, "0%") == "30%"
    assert xlsx_cell_text(dt.datetime(2024, 3, 5), "yyyy-mm-dd") == "2024-03-05"
    assert xlsx_cell_text("含税", None) == "含税"
