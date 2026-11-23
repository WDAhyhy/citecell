"""QuoteMatcher 单元测试（ARCHITECTURE §6.2 步骤 4b–4e 所需的匹配等级）。"""

from citecell.interfaces import QuoteMatcher
from citecell.models import MatchLevel
from citecell.textnorm import Matcher


def _slice(text: str, m) -> str:
    return text[m.start : m.end]


def test_normalized_hit_for_full_width_colon_comma_and_newline_space():
    text = "合同金额：\n1,200 元"
    hits = Matcher().find("合同金额:1,200元", text)
    assert len(hits) == 1
    assert hits[0].level == MatchLevel.NORMALIZED
    assert hits[0].end == len(text)  # 区间覆盖到"元"
    assert _slice(text, hits[0]) == text


def test_normalized_hit_covers_square_meter_symbol():
    text = "面积50㎡"
    hits = Matcher().find("50m2", text)
    assert len(hits) == 1
    assert hits[0].level == MatchLevel.NORMALIZED
    assert _slice(text, hits[0]) == "50㎡"


def test_decimal_point_and_thousands_separator_do_not_cross_match():
    assert Matcher().find("125", "１２.５") == []
    assert Matcher().find("1200", "1,200") == []
    assert Matcher().find("12.5", "１２.５")[0].level == MatchLevel.NORMALIZED


def test_exact_hit_suppresses_normalized_and_hits_sorted_by_start():
    text = "金额1 金额1 金额1"
    hits = Matcher().find("金额1", text)
    assert [h.level for h in hits] == [MatchLevel.EXACT] * 3
    assert [h.start for h in hits] == sorted(h.start for h in hits)
    assert [h.start for h in hits] == [0, 4, 8]  # 每段 "金额1 " 占 4 个码点


def test_exact_preferred_over_normalized_when_both_possible():
    text = "ABC abc"
    hits = Matcher().find("abc", text)
    assert len(hits) == 1
    assert hits[0].level == MatchLevel.EXACT
    assert hits[0].start == 4


def test_quote_outer_whitespace_is_stripped_before_matching():
    hits = Matcher().find("  合同  ", "前缀合同后缀")
    assert [h.level for h in hits] == [MatchLevel.EXACT]
    assert _slice("前缀合同后缀", hits[0]) == "合同"


def test_fuzzy_disabled_by_default_and_enabled_with_bounded_score():
    text = "合同总金额为壹拾万元"
    quote = "合同总金额为拾万元"  # 缺一个字，编辑距离 1
    assert Matcher().find(quote, text) == []
    hits = Matcher().find(quote, text, allow_fuzzy=True)
    assert hits, "应返回 FUZZY 命中"
    assert all(h.level == MatchLevel.FUZZY for h in hits)
    assert all(0.0 <= h.score <= 1.0 for h in hits)
    assert all(h.start < h.end for h in hits)


def test_fuzzy_hits_are_non_overlapping_and_sorted():
    text = "甲乙丙丁戊甲乙丙丁己" * 2
    hits = Matcher().find("甲乙丙丁庚", text, allow_fuzzy=True)
    starts = [h.start for h in hits]
    assert starts == sorted(starts)
    for a, b in zip(hits, hits[1:]):
        assert a.end <= b.start


def test_fuzzy_length_limits_return_empty():
    text = "合同总金额为壹拾万元"
    quote = "合同总金额为拾万元"
    assert Matcher(fuzzy_max_text=5).find(quote, text, allow_fuzzy=True) == []
    assert Matcher(fuzzy_max_quote=3).find(quote, text, allow_fuzzy=True) == []


def test_fuzzy_zero_budget_for_short_quote_returns_empty():
    # 归一化引文 4 码点，ratio 0.2 → 编辑距离上限 0，不返回 FUZZY
    assert Matcher().find("甲乙丙丁", "甲乙丁", allow_fuzzy=True) == []


def test_no_hit_returns_empty_list():
    assert Matcher().find("不存在的引文", "完全无关的文本") == []
    assert Matcher().find("", "文本") == []
    assert Matcher().find("   ", "文本") == []


def test_overlapping_occurrences_all_reported():
    hits = Matcher().find("aa", "aaa")
    assert [h.start for h in hits] == [0, 1]


def test_normalized_hit_that_splits_expansion_is_rejected():
    # 引文 "2" 在归一化文本中是 "m2" 的后半，切开了 ㎡ 的展开：拒绝该命中
    assert Matcher().find("2", "面积50㎡") == []
    # 引文 "50m" 同样切开展开
    assert Matcher().find("50m", "面积50㎡") == []


def test_protocol_conformance():
    assert isinstance(Matcher(), QuoteMatcher)
    assert Matcher.version == "matcher-v1"
