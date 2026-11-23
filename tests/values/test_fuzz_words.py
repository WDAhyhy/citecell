"""模糊测试：没有数字含义的词，解析必须抛 ValueParseError（第三轮 2）。

- 固定词表：审查者报告与常见歧义词。
- 随机词：random.Random(0) 生成 2000 个「含至少一个数字字符 + 至少一个非数字填充字符」的串。
  填充字符集中不含任何单位/货币/比例标记（元角分圆整正%之个月天年周时日），因此这些串
  在没有额外上下文时不应有数字含义；若解析成功即为缺陷。
- 类型：NUMBER（无单位）、NUMBER（unit=天）、MONEY、PERCENT。
"""

import random

import pytest

from citecell.interfaces import ValueParseError
from citecell.models import KeySpec, ValueType
from citecell.values import ValueNormalizerV1

N = ValueNormalizerV1()
KEYS = {
    "number": KeySpec(key="n", value_type=ValueType.NUMBER),
    "number_unit": KeySpec(key="n", value_type=ValueType.NUMBER, unit="天"),
    "money": KeySpec(key="m", value_type=ValueType.MONEY),
    "percent": KeySpec(key="p", value_type=ValueType.PERCENT),
}

# 注意：单独的「千万」是合法数字（1e7，如「千万富翁」），不在此列；「千万不要」中的「千万」无数字含义。
FIXED_WORDS = [
    "一般", "一些", "万一", "千万不要", "五金", "六合", "十分", "百分百", "一元化",
    "万元户", "统一", "第一", "一次性付款", "一个人", "一日三餐", "一时兴起", "二者", "十足",
    "百分之百的人", "千万别", "五湖四海", "三心二意", "一定", "两岸", "十全十美",
]

NUM_CHARS = "一二三四五六七八九十百千万亿零〇两"
FILLER_CHARS = "不要化金般些统人户好的我是你这那都就很也还又再把被让给在与和及或等"


def _generated_words() -> list[str]:
    rng = random.Random(0)
    words: list[str] = []
    pool = NUM_CHARS + FILLER_CHARS
    while len(words) < 2000:
        s = "".join(rng.choice(pool) for _ in range(rng.randint(2, 4)))
        if any(c in NUM_CHARS for c in s) and any(c in FILLER_CHARS for c in s):
            words.append(s)
    return words


ALL_WORDS = FIXED_WORDS + _generated_words()


@pytest.mark.parametrize("kind", list(KEYS))
@pytest.mark.parametrize("word", ALL_WORDS)
def test_words_without_numeric_meaning_do_not_parse(kind, word):
    with pytest.raises(ValueParseError):
        N.parse_value(word, KEYS[kind])


@pytest.mark.parametrize("word", FIXED_WORDS)
def test_fixed_words_produce_no_number_mentions(word):
    # 只对固定词表断言查找结果：随机串中可能有合法的数字（如「十万」在无单位时是 100000）
    assert N.find_mentions(word, KEYS["number"]) == [], word
