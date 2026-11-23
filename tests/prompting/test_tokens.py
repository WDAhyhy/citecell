from citecell.prompting.tokens import HeuristicTokenCounter, is_cjk


def _cjk_count(text: str) -> int:
    return sum(1 for ch in text if "一" <= ch <= "鿿")


def test_empty_is_zero():
    assert HeuristicTokenCounter().count("") == 0
    assert HeuristicTokenCounter().count("   ") == 0


def test_count_is_at_least_cjk_characters():
    counter = HeuristicTokenCounter()
    samples = [
        "合同金额为人民币一万二千元。",
        "混合 mixed 文本 with English 与 数字 12,000 元",
        "全角，标点！（括号）",
        "一二三四五六七八九十" * 7,
    ]
    for text in samples:
        assert counter.count(text) >= _cjk_count(text), text
        assert counter.count(text) >= sum(1 for ch in text if is_cjk(ch)), text


def test_monotonic_on_concatenation():
    counter = HeuristicTokenCounter()
    a, b = "付款日期为一月", "abcdefghij"
    assert counter.count(a + b) <= counter.count(a) + counter.count(b)
    assert counter.count(a + b) >= max(counter.count(a), counter.count(b))


def test_name_is_versioned():
    assert HeuristicTokenCounter.name == "heuristic_v1"
