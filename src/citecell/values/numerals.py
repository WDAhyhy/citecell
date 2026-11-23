"""中文整数解析（小写、大写、"两"；亿/万/千百十）。

规则（均为显式约定，错误输入一律抛 ValueParseError，不做猜测）：
- 单个数字字符可解析：'〇'、'零' → 0，'一'/'壹' → 1，…，'两' → 2。
- 多字符必须是合法的位值写法：相邻两个数字字符非法（"一二" 拒绝）；
  单位必须严格递减（"十百" 拒绝）；数字与单位之间不允许缺位。
- 省略的 1：单位前没有数字时视为 1（"十五" = 15，"百" = 100）。
- 中间的 "零/〇" 为补位零：必须出现在单位之后或开头，且其后紧跟非零数字
  （"一百零五" = 105；"一千零五" = 1005；"一百零" 拒绝）。
- 口语简写一律拒绝（不猜）：单位（百/千/万/亿）之后的末尾单个数字（"一百二"、"二百五"、
  "一千二"、"三万五"、"一亿五"）以及 "亿"/"万" 之后的单个数字段都抛 ValueParseError，
  由校验器转为待人工复核。标准写法（"二十三"、"一百二十"、"一百零二"、"三万五千"）不受影响。
- "亿"、"万" 分段：每段内的值必须小于其单位（"一万一万" 拒绝）。
- 不支持小数（"三点五"）与分数，遇到即拒绝。
"""

from __future__ import annotations

from citecell.interfaces import ValueParseError
from citecell.values._common import CN_DIGITS, CN_NUMERAL_CHARS, CN_SMALL_UNITS


def _fail() -> ValueParseError:
    # 不回显输入，避免错误信息携带正文
    return ValueParseError("无法解析为中文整数")


def parse_cn_integer(text: str) -> int:
    """解析中文整数（不含小数、负号）。失败抛 ValueParseError。"""
    if not text or any(c not in CN_NUMERAL_CHARS for c in text):
        raise _fail()
    if len(text) == 1 and text in CN_DIGITS:
        return CN_DIGITS[text]
    if "亿" in text:
        head, _, tail = text.partition("亿")
        if not head or "亿" in tail:
            raise _fail()
        high = parse_cn_integer(head)
        if high == 0:
            raise _fail()
        return high * 10**8 + _tail_value(tail, 10**8)
    if "万" in text:
        head, _, tail = text.partition("万")
        if not head or "万" in tail:
            raise _fail()
        high = parse_cn_integer(head)
        if high == 0:
            raise _fail()
        return high * 10**4 + _tail_value(tail, 10**4)
    return _below_wan(text)


def _tail_value(tail: str, unit: int) -> int:
    """"亿"/"万" 之后的段。单个数字按口语尾数解释（见模块 docstring）。"""
    if tail == "":
        return 0
    if len(tail) == 1 and tail in CN_DIGITS:
        # 口语简写（"三万五"、"一亿五"）：拒绝
        raise _fail()
    value = parse_cn_integer(tail)
    if value >= unit:
        raise _fail()
    return value


def _below_wan(text: str) -> int:
    """不含"万/亿"的整数，取值范围 [0, 10000)。"""
    tokens: list[tuple[str, int]] = []
    for c in text:
        if c in CN_DIGITS:
            tokens.append(("d", CN_DIGITS[c]))
        elif c in CN_SMALL_UNITS:
            tokens.append(("u", CN_SMALL_UNITS[c]))
        else:  # 不应出现（万/亿 已在上层切分）
            raise _fail()
    if not tokens:
        raise _fail()
    if len(tokens) == 1 and tokens[0][0] == "d":
        return tokens[0][1]

    n = len(tokens)
    total = 0
    pending: int | None = None  # 等待单位的数字
    last_unit: int | None = None
    for i, (kind, v) in enumerate(tokens):
        if kind == "d":
            if v == 0:
                # 补位零：前面必须是单位或开头；后面必须紧跟非零数字
                if pending is not None or i + 1 >= n:
                    raise _fail()
                if i > 0 and tokens[i - 1][0] == "d":
                    raise _fail()
                next_kind, next_v = tokens[i + 1]
                if next_kind != "d" or next_v == 0:
                    raise _fail()
                continue
            if pending is not None:
                raise _fail()  # 相邻数字，如 "一二"
            pending = v
        else:
            if last_unit is not None and v >= last_unit:
                raise _fail()  # 单位未递减，如 "十百"
            total += (pending if pending is not None else 1) * v
            last_unit = v
            pending = None

    if pending is not None:
        if n >= 2 and tokens[-2][0] == "u" and tokens[-2][1] >= 100:
            # 口语简写（"一百二"=120、"二百五"）：拒绝，交由校验器转待人工复核
            raise _fail()
        total += pending  # 标准写法："二十三"、"十五"
    return total

