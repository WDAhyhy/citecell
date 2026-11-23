"""表面形式生成：金额大写、中文数字、中文日期、万元、百分比、XLSX 单元格文本。

本模块由合成器独立实现，不 import 解析器或取值模块（金标准独立性，ADR-013）。
解析器的"金额→大写"往返测试应由另一位工人的实现与本模块互相验证。
"""

from __future__ import annotations

import datetime as _dt
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

_DIG_LOW = "零一二三四五六七八九"
_DIG_CAP = "零壹贰叁肆伍陆柒捌玖"
_UNIT_LOW = ("", "十", "百", "千")
_UNIT_CAP = ("", "拾", "佰", "仟")
_YEAR_DIG = "〇一二三四五六七八九"


def _group(n: int, cap: bool) -> str:
    """0 < n < 10000 的四位组，内部零压缩为一个"零"。"""
    dig = _DIG_CAP if cap else _DIG_LOW
    units = _UNIT_CAP if cap else _UNIT_LOW
    out: list[str] = []
    pending_zero = False
    for pos in (3, 2, 1, 0):
        d = (n // 10**pos) % 10
        if d == 0:
            pending_zero = True
            continue
        if pending_zero and out:
            out.append(dig[0])
        pending_zero = False
        if not cap and pos == 1 and d == 1 and not out:
            out.append(units[1])  # 小写口语："十""十五"，而非"一十"
        else:
            out.append(dig[d] + units[pos])
    return "".join(out)


def _rest(lo: int, threshold: int, cap: bool) -> str:
    """亿/万 之后的低位段：不足阈值时补一个"零"。"""
    if lo == 0:
        return ""
    prefix = "零" if lo < threshold else ""
    return prefix + cn_int(lo, cap)


def cn_int(n: int, cap: bool = False) -> str:
    """非负整数 → 中文数字。cap=True 为大写（壹贰叁…），用于金额。"""
    if n < 0:
        raise ValueError("cn_int 仅接受非负整数")
    if n == 0:
        return _DIG_CAP[0] if cap else _DIG_LOW[0]
    if n >= 10**8:
        hi, lo = divmod(n, 10**8)
        return cn_int(hi, cap) + "亿" + _rest(lo, 10**7, cap)
    if n >= 10**4:
        hi, lo = divmod(n, 10**4)
        return cn_int(hi, cap) + "万" + _rest(lo, 1000, cap)
    return _group(n, cap)


def amount_to_uppercase(amount: Decimal, suffix_zheng: bool = True) -> str:
    """金额 → 中文大写。

    suffix_zheng=True 时，无角分的金额末尾加"整"；有角分时永不加"整"。
    例：1200000→壹佰贰拾万元整；30000→叁万元整；105→壹佰零伍元整；
        10.05→壹拾元零伍分；1005.5→壹仟零伍元伍角。
    仅接受至多两位小数的金额。
    """
    if amount < 0:
        raise ValueError("金额不能为负")
    if amount != amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP):
        raise ValueError("金额最多两位小数")
    cents = int(amount.quantize(Decimal("0.01")) * 100)
    yuan, frac = divmod(cents, 100)
    jiao, fen = divmod(frac, 10)
    text = cn_int(yuan, cap=True) + "元"
    if frac == 0:
        return text + ("整" if suffix_zheng else "")
    if jiao == 0:
        return text + "零" + _DIG_CAP[fen] + "分"
    if fen == 0:
        return text + _DIG_CAP[jiao] + "角"
    return text + _DIG_CAP[jiao] + "角" + _DIG_CAP[fen] + "分"


def money_plain(amount: Decimal) -> str:
    """1200000 → '1,200,000.00元'。"""
    return f"{amount:,.2f}元"


def money_wan(amount: Decimal) -> str:
    """1200000 → '120万元'。要求为 10000 的整数倍。"""
    if amount % 10000 != 0 or amount < 10000:
        raise ValueError("万元表面形式要求为 10000 的整数倍")
    return f"{int(amount) // 10000}万元"


def cn_date(d: _dt.date) -> str:
    """日期 → '二〇二四年三月五日'（年份逐位中文数字，〇 为零）。"""
    year = "".join(_YEAR_DIG[int(c)] for c in f"{d.year:04d}")
    return f"{year}年{cn_int(d.month)}月{cn_int(d.day)}日"


def arabic_date(d: _dt.date) -> str:
    """日期 → '2024年3月5日'。"""
    return f"{d.year}年{d.month}月{d.day}日"


def percent_sign(value: Decimal) -> str:
    """15 → '15%'（整数百分比）。"""
    return f"{int(value)}%"


def percent_cn(value: Decimal) -> str:
    """15 → '百分之十五'。"""
    return f"百分之{cn_int(int(value))}"


def xlsx_cell_text(value: Any, number_format: str | None) -> str:
    """XLSX_TEXT_V1 的写出侧实现：与 T3 解析器的规则保持一致（由测试独立复核）。

    浮点用最短往返 repr 转 Decimal，输出无科学计数法；含 % 的格式先乘 100 并追加 "%"
    （0.15 + "0%" → "15%"）；日期（datetime）渲染为 ISO；字符串原样。
    """
    if isinstance(value, _dt.datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        raise TypeError("布尔单元格不进入 XLSX_TEXT_V1")
    if isinstance(value, int):
        d = Decimal(value)
    elif isinstance(value, float):
        d = Decimal(repr(value))
    else:
        raise TypeError(f"不支持的单元格类型: {type(value).__name__}")
    if number_format and "%" in number_format:
        return format(d.scaleb(2).normalize(), "f") + "%"
    return format(d.normalize(), "f")
