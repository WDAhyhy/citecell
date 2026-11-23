"""ValueNormalizerV1：值类型系统（ARCH §6.3）的确定性实现。

- parse_value：模型给出的值原文 → 规范化值（失败抛 ValueParseError，错误信息不回显输入）。
- find_mentions：在一段文本中识别该类型的全部提及（原文坐标，按 start 升序，互不重叠）。
- equivalent：规范化值相等判定。

全程使用 Decimal；不依赖 citecell.synth。

单位匹配规则（NUMBER，仅当 spec.unit 存在时才捕获单位）：
- 数字之后紧跟可选的"个"加单位文字即捕获单位：`12个月` → 单位 "月"。
- spec.unit 完全匹配时单位即 spec.unit；否则捕获紧随其后的 1–2 个 CJK 字符
  （且其后不是 CJK 字符），例如 `12天` → 单位 "天"。
- 单位不在 equivalent 中判定：equivalent 只比较数值。UNIT_MISMATCH 应由 Verifier
  比较 `NumberValue.unit` 与 `spec.unit` 得出（见 T2 交付说明中的契约变更请求）。
- spec.unit 为空时 NumberValue.unit 恒为 None。

NUMBER 的查找：阿拉伯数字（可带 千/万/亿 倍数）与中文整数（必须含十百千万亿之一，
以免把"一般""二者"中的"一"误判为数字）。
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal

from citecell.interfaces import ValueParseError
from citecell.models import (
    BooleanValue,
    DateValue,
    EnumValue,
    KeySpec,
    MoneyValue,
    NormalizedValue,
    NumberValue,
    PercentValue,
    TextValue,
    ValueMention,
)
from citecell.values._common import (
    CN_DIGIT_CHARS,
    CN_NUMERAL_CHARS,
    CN_UNIT_CHARS,
    NUMERIC,
    SCALE,
    strict_numeric_tokens,
    to_decimal,
    width_fold,
)
from citecell.values.dates import find_date_spans, parse_date_text
from citecell.values.money import find_money_spans, money_value
from citecell.values.numerals import parse_cn_integer
from citecell.values.percent import find_percent_spans, parse_percent_text
from citecell.values.units import KNOWN_UNITS, canon_unit, match_cn_known_unit, match_unit

_NUMBER_FULL = re.compile(
    rf"^(?P<sign>[-负－])?(?P<body>{NUMERIC}|[{CN_NUMERAL_CHARS}]+)(?P<scale>[千万亿])?(?P<rest>.*)$",
    re.DOTALL,
)
_NUMBER_CN_RUN_FIND = re.compile(rf"(?<![0-9A-Za-z.,\-/])(?:-|负)?[{CN_NUMERAL_CHARS}]+")
_CN_SPEC_UNIT_AFTER = re.compile(r"\s*\(?\s*(?:个\s*)?{unit}\s*\)?")


def _fail(what: str) -> ValueParseError:
    return ValueParseError(f"无法解析为 {what} 值")


def _text_key(s: str) -> str:
    """枚举/布尔比较用的键（仅用于 parse，不用于定位，因此可 casefold）。"""
    return width_fold(s).strip().casefold()


def _resolve_longest(spans: list[tuple[int, int, object]]) -> list[tuple[int, int, object]]:
    """同一区域多个候选时保留更长者（如 "不含税" 优先于其中的 "含税"），结果按 start 升序。"""
    ordered = sorted(spans, key=lambda s: (-(s[1] - s[0]), s[0]))
    kept: list[tuple[int, int, object]] = []
    for start, end, payload in ordered:
        if any(start < k_end and k_start < end for k_start, k_end, _ in kept):
            continue
        kept.append((start, end, payload))
    kept.sort(key=lambda s: s[0])
    return kept


class ValueNormalizerV1:
    """值类型系统 v1。满足 interfaces.ValueNormalizer 协议。"""

    version: str = "values-v1"

    # ------------------------------------------------------------------ parse
    def parse_value(self, value_text: str, spec: KeySpec) -> NormalizedValue:
        s = value_text.strip()
        if not s:
            raise ValueParseError("值为空")
        vt = spec.value_type.value
        try:
            if vt == "text":
                return TextValue(text=s)
            if vt == "money":
                return money_value(s, spec.currency)
            if vt == "percent":
                return PercentValue(value=parse_percent_text(s))
            if vt == "date":
                return parse_date_text(s)
            if vt == "number":
                # 只有键声明了 unit 时才接受数字后的单位文字，否则 "三点五" 会被误判为 3 + 单位"点五"
                return _parse_number(s, unit_spec=spec.unit)
            if vt == "enum":
                return EnumValue(value=_match_option(s, spec, "enum"))
            if vt == "boolean":
                return _parse_boolean(s, spec)
        except ValueError as exc:  # pydantic 校验失败等
            if isinstance(exc, ValueParseError):
                raise
            raise _fail(vt) from exc
        raise _fail(vt)

    # ------------------------------------------------------------------- find
    def find_mentions(self, text: str, spec: KeySpec) -> list[ValueMention]:
        vt = spec.value_type.value
        if vt == "text":
            # 文本类型不做提及识别：由 T1 的子串匹配完成（见 ARCH §6.3）。
            return []
        if vt == "money":
            return [
                self._mention(text, s, e, money_value(text[s:e], spec.currency))
                for s, e in find_money_spans(text)
            ]
        if vt == "percent":
            return [
                self._mention(text, s, e, PercentValue(value=parse_percent_text(text[s:e])))
                for s, e in find_percent_spans(text)
            ]
        if vt == "date":
            return [self._mention(text, s, e, parse_date_text(text[s:e])) for s, e in find_date_spans(text)]
        if vt == "number":
            return self._find_numbers(text, spec)
        if vt == "enum":
            return self._find_options(text, spec, boolean=False)
        if vt == "boolean":
            return self._find_options(text, spec, boolean=True)
        return []

    # ------------------------------------------------------------- equivalent
    def equivalent(self, a: NormalizedValue, b: NormalizedValue, spec: KeySpec) -> bool:
        if a.type != b.type:
            return False
        if isinstance(a, NumberValue) and isinstance(b, NumberValue):
            if a.unit and b.unit and canon_unit(a.unit) != canon_unit(b.unit):
                return False  # 两边都写明单位且规范化后不同 → 冲突
            return a.value == b.value
        if isinstance(a, MoneyValue) and isinstance(b, MoneyValue):
            return a.amount == b.amount and a.currency == b.currency
        if isinstance(a, PercentValue) and isinstance(b, PercentValue):
            return a.value == b.value
        if isinstance(a, DateValue) and isinstance(b, DateValue):
            return a.iso == b.iso and a.precision == b.precision
        if isinstance(a, EnumValue) and isinstance(b, EnumValue):
            return a.value == b.value
        if isinstance(a, BooleanValue) and isinstance(b, BooleanValue):
            return a.value == b.value
        if isinstance(a, TextValue) and isinstance(b, TextValue):
            return _text_norm(a.text) == _text_norm(b.text)
        return False

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _mention(text: str, start: int, end: int, value: NormalizedValue) -> ValueMention:
        return ValueMention(value=value, start=start, end=end, surface=text[start:end])

    def _find_numbers(self, text: str, spec: KeySpec) -> list[ValueMention]:
        folded = width_fold(text)
        # 候选：(起点, 提及终点, 数值, 单位)
        cands: list[tuple[int, int, Decimal, str | None]] = []
        # 阿拉伯数字：先切出严格数字串（整串合法才算），再接可选的 千/万/亿 倍数与单位
        for start, num_end, ascii_num in strict_numeric_tokens(text):
            value = to_decimal(ascii_num)  # 千分位逗号在 to_decimal 中去除
            end = num_end
            if num_end < len(folded) and folded[num_end] in SCALE:
                value *= SCALE[folded[num_end]]
                end = num_end + 1
            end, unit = self._extend_unit(folded, end, spec.unit)
            cands.append((start, end, value, unit))
        # 中文数字串：需含数字且含单位字（如"十五"），或紧随已知单位（如"六个月""十个月"）。
        # 只含单位字的串（"千万"）或无单位的普通词（"一般""五金"）不是提及。
        for m in _NUMBER_CN_RUN_FIND.finditer(folded):
            run = m.group(0)
            body = run.lstrip("-负")
            known = match_cn_known_unit(folded, m.end())
            has_digit = any(ch in CN_DIGIT_CHARS for ch in body)
            has_unit_char = any(ch in CN_UNIT_CHARS for ch in body)
            if not (known or (has_digit and has_unit_char)):
                continue
            try:
                value = _parse_number(text[m.start() : m.end()]).value
            except ValueParseError:
                continue
            if known:
                end, unit = known
            else:
                end, unit = self._extend_cn_unit(folded, m.end(), spec.unit)
            cands.append((m.start(), end, value, unit))

        mentions: list[ValueMention] = []
        for start, end, value, unit in sorted(cands, key=lambda c: (c[0], -(c[1] - c[0]))):
            if mentions and start < mentions[-1].end:
                continue  # 区间重叠：保留先出现且较长者
            mentions.append(self._mention(text, start, end, NumberValue(value=value, unit=unit)))
        return mentions

    @staticmethod
    def _extend_unit(folded: str, end: int, spec_unit: str | None) -> tuple[int, str | None]:
        """阿拉伯数字：spec.unit 存在时，在窗口内识别紧随数字的单位并并入提及区间。返回 (新 end, 规范单位)。"""
        if not spec_unit:
            return end, None
        found = match_unit(folded, end)
        if found is None:
            return end, None
        return found

    @staticmethod
    def _extend_cn_unit(folded: str, end: int, spec_unit: str | None) -> tuple[int, str | None]:
        """中文数字：只接受与 spec.unit 完全相同的单位（不做通用单位猜测，避免"一元化"之类被当作单位）。"""
        if not spec_unit:
            return end, None
        m = re.compile(_CN_SPEC_UNIT_AFTER.pattern.format(unit=re.escape(spec_unit))).match(folded, end)
        if m is None:
            return end, None
        return m.end(), canon_unit(spec_unit)

    def _find_options(self, text: str, spec: KeySpec, *, boolean: bool) -> list[ValueMention]:
        if not spec.enum_options:
            return []
        folded = width_fold(text)
        spans: list[tuple[int, int, object]] = []
        for opt in spec.enum_options:
            for surface in [opt.value, *opt.aliases]:
                key = width_fold(surface).strip()
                if not key:
                    continue
                start = folded.find(key)
                while start != -1:
                    spans.append((start, start + len(key), opt.value))
                    start = folded.find(key, start + 1)
        mentions: list[ValueMention] = []
        for start, end, option_value in _resolve_longest(spans):
            value: NormalizedValue
            if boolean:
                value = BooleanValue(value=option_value == "true")
            else:
                value = EnumValue(value=str(option_value))
            mentions.append(self._mention(text, start, end, value))
        return mentions


# ---------------------------------------------------------------- module level
def _parse_number(s: str, unit_spec: str | None = None) -> NumberValue:
    """解析数字原文。unit_spec 为键声明的单位（None 表示键没有单位，此时任何后缀都是错误）。

    后缀单位只接受：表内规范单位（KNOWN_UNITS）或 unit_spec 本身。其余后缀（"一元化"中的"元化"、
    "五金"中的"金"）一律拒绝。
    """
    t = width_fold(s.strip())
    m = _NUMBER_FULL.match(t)
    if not m:
        raise _fail("number")
    body = m.group("body")
    if body[0] in "0123456789":
        value = to_decimal(body)
        if m.group("scale"):
            value *= SCALE[m.group("scale")]
    else:
        if m.group("scale"):  # 中文数字已含万/亿/千，不再接阿拉伯倍数
            raise _fail("number")
        value = Decimal(parse_cn_integer(body))
    if m.group("sign"):
        value = -value
    unit: str | None = None
    if m.group("rest").strip():
        if unit_spec is None:
            raise _fail("number")
        found = match_unit(t, m.start("rest"))
        if found is None or found[0] != len(t):
            raise _fail("number")  # 单位必须占满剩余文本
        if found[1] not in KNOWN_UNITS and found[1] != canon_unit(unit_spec):
            raise _fail("number")
        unit = found[1]
    return NumberValue(value=value, unit=unit)


def _match_option(s: str, spec: KeySpec, kind: str) -> str:
    if not spec.enum_options:
        raise _fail(kind)
    key = _text_key(s)
    for opt in spec.enum_options:
        if _text_key(opt.value) == key or any(_text_key(a) == key for a in opt.aliases):
            return opt.value
    raise _fail(kind)


def _parse_boolean(s: str, spec: KeySpec) -> BooleanValue:
    if spec.enum_options:
        return BooleanValue(value=_match_option(s, spec, "boolean") == "true")
    key = _text_key(s)
    if key in ("true", "false"):
        return BooleanValue(value=key == "true")
    raise _fail("boolean")


def _text_norm(s: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s)).casefold()
