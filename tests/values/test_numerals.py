"""中文整数解析：正例、反例、与参考生成器的往返属性测试。

注意：这里的 _ref_* 生成器是本测试文件内的参考实现，不是 T5a（synth）的独立实现。
ARCH §6.3 要求与合成器的"金额→大写"生成函数做往返测试；那一项需等 T5a 与 T11 落地后
补跑（本任务不 import citecell.synth）。
"""

import random
from decimal import Decimal

import pytest

from citecell.interfaces import ValueParseError
from citecell.values.numerals import parse_cn_integer

DIG = "零一二三四五六七八九"


def _ref_below_wan(n: int) -> str:
    """0 < n < 10000 的小写中文，按标准读法（补位零、省略前导一十）。"""
    units = ["", "十", "百", "千"]
    digits = [int(c) for c in f"{n:04d}"]
    out = ""
    zero_pending = False
    for i, d in enumerate(digits):
        pos = 3 - i
        if d == 0:
            zero_pending = True
            continue
        if zero_pending and out:
            out += "零"
        zero_pending = False
        if d == 1 and pos == 1 and not out:
            out += "十"
        else:
            out += DIG[d] + units[pos]
    return out


def _ref_below_yi(n: int) -> str:
    """0 <= n < 1e8。"""
    if n == 0:
        return "零"
    hi, lo = divmod(n, 10**4)
    if hi == 0:
        return _ref_below_wan(lo)
    out = _ref_below_wan(hi) + "万"
    if lo:
        if lo < 1000:
            out += "零"
        out += _ref_below_wan(lo)
    return out


def _ref_int(n: int) -> str:
    hi, lo = divmod(n, 10**8)
    if hi == 0:
        return _ref_below_yi(lo)
    out = _ref_below_yi(hi) + "亿"
    if lo:
        if lo < 10**7:
            out += "零"
        out += _ref_below_yi(lo)
    return out


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("〇", 0), ("零", 0), ("一", 1), ("壹", 1), ("两", 2), ("九", 9),
        ("十", 10), ("十五", 15), ("二十三", 23), ("一百", 100),
        ("一百二十", 120), ("一百二十三", 123), ("一百零二", 102),
        ("一百零五", 105), ("一千零五", 1005), ("两千", 2000), ("壹万贰仟", 12000),
        ("壹佰贰拾万", 1200000), ("一百二十万", 1200000),
        ("三万五千", 35000), ("一万零五", 10005), ("一万零五十", 10050),
        ("一亿", 10**8), ("一亿零五", 10**8 + 5), ("一亿五千万", 15 * 10**7), ("一万亿", 10**12),
        ("五千万", 5 * 10**7), ("一亿五千万", 15 * 10**7),
    ],
)
def test_positive_examples(text, expected):
    assert parse_cn_integer(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "", "一二", "十十", "十百", "一百零", "〇〇", "零亿", "亿", "万一万", "一万一万",
        "一亿一亿", "abc", "三点五", "一百零〇五", "一零五", "亿五",
        # 口语简写：拒绝（不猜）
        "一百二", "一千二", "二百五", "一百二十三五", "一千二百五", "三万五", "一亿五", "一万二",
    ],
)
def test_negative_examples(text):
    with pytest.raises(ValueParseError):
        parse_cn_integer(text)


def test_roundtrip_random_integers_are_exact():
    rng = random.Random(0)
    samples = [rng.randrange(0, 10**8) for _ in range(1000)]
    samples += [rng.randrange(1, 10**4) for _ in range(300)]  # 小数，含大量补位零
    samples += [10**k for k in range(8)] + [10**k + 1 for k in range(8)]
    for n in samples:
        text = _ref_int(n)
        assert parse_cn_integer(text) == n, (n, text)


def test_money_cents_roundtrip_random():
    """对随机金额（分为单位）生成大写形式再解析，必须逐分相等。"""
    from citecell.values.normalizer import ValueNormalizerV1
    from citecell.models import KeySpec, ValueType

    rng = random.Random(1)
    key = KeySpec(key="k", value_type=ValueType.MONEY)
    n = ValueNormalizerV1()
    for _ in range(500):
        yuan = rng.randrange(1, 10**8)
        jiao = rng.randrange(0, 10)
        fen = rng.randrange(0, 10)
        text = f"{_ref_int(yuan)}元"
        if jiao:
            text += f"{DIG[jiao]}角"
        if fen:
            text += f"{'零' if not jiao else ''}{DIG[fen]}分"
        expected = Decimal(yuan) + Decimal(jiao) / 10 + Decimal(fen) / 100
        assert n.parse_value(text, key).amount == expected, text
