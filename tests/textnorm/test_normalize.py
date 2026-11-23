"""NORMALIZER_V1 单元测试与属性测试（ARCHITECTURE §6.1）。全部数据在运行时生成。"""

import random

from citecell.interfaces import TextNormalizer
from citecell.textnorm import Normalizer


def _norm(s: str) -> str:
    return Normalizer().normalize(s).text


def test_fullwidth_and_ideographic_space_normalized_with_span_back_to_original():
    nt = Normalizer().normalize("ＡＢＣ　１２３")
    assert nt.text == "abc123"
    assert nt.to_original_span(0, len(nt.text)) == (0, 7)
    # 单个字符的回映射：'b' 对应原文第 1 个码点
    assert nt.to_original_span(1, 2) == (1, 2)


def test_punctuation_is_not_deleted_decimal_and_thousands_separators_kept():
    assert _norm("１２.５") == "12.5"
    assert _norm("125") == "125"
    assert _norm("１２.５") != _norm("125")
    assert _norm("1,200") == "1,200"
    assert _norm("1,200") != _norm("1200")


def test_whitespace_zero_width_and_soft_hyphen_deleted():
    assert _norm("a​b­c　d\n\te‌f‍g﻿h") == "abcdefgh"


def test_square_meter_and_circled_digit_expand_with_shared_origin_index():
    nt = Normalizer().normalize("面积50㎡")
    assert nt.text == "面积50m2"
    # ㎡ 展开出的两个码点都映射回原文同一下标
    assert nt.index_map[-1] == nt.index_map[-2] == 4
    assert nt.to_original_span(2, 6) == (2, 5)
    assert _norm("①") == "1"


def test_punctuation_mapping_table():
    src = "“a”‘b’、。【】〔〕——～〜"
    assert _norm(src) == "\"a\"'b',.[][]--~~"


def test_latin_casefold_only():
    assert _norm("ABC合同Ab") == "abc合同ab"


def test_colon_and_comma_full_width_map_to_ascii():
    assert _norm("合同金额：\n1,200 元") == "合同金额:1,200元"


def test_protocol_conformance():
    assert isinstance(Normalizer(), TextNormalizer)
    assert Normalizer.version == "normalizer-v1"


_POOL = (
    "中文汉字测试合同金额"
    "ＡＢＣａｂｃ１２３．，：；"
    " 　\n\t"
    "，。、；：！？“”‘’（）【】《》"
    ".,:;!?()[]\"'-"
    "0123456789"
    "㎡①－～〜—"
)


def test_property_roundtrip_of_random_intervals_on_aligned_boundaries():
    rng = random.Random(0)
    checked = 0
    for _ in range(1000):
        length = rng.randint(1, 40)
        s = "".join(rng.choice(_POOL) for _ in range(length))
        nt = Normalizer().normalize(s)
        if not nt.text:
            continue
        im = nt.index_map
        n = len(nt.text)
        ns = rng.randint(0, n - 1)
        ne = rng.randint(ns + 1, n)
        # 仅测试不切开 NFKC 展开的区间；切开的区间由 matcher 拒绝（见 test_match.py）
        if ns > 0 and im[ns - 1] == im[ns]:
            continue
        if ne < n and im[ne - 1] == im[ne]:
            continue
        os_, oe = nt.to_original_span(ns, ne)
        assert Normalizer().normalize(s[os_:oe]).text == nt.text[ns:ne]
        checked += 1
    # 确保属性测试不是空转
    assert checked > 300


def test_normalizer_never_raises_and_is_deterministic():
    rng = random.Random(1)
    for _ in range(300):
        s = "".join(rng.choice(_POOL + "\ud800\x00\U0001F600") for _ in range(rng.randint(0, 30)))
        a = Normalizer().normalize(s)
        b = Normalizer().normalize(s)
        assert a == b
        assert len(a.index_map) == len(a.text)
