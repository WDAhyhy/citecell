"""FactSampler：按 KeySet 与随机源采样一份事实表 {key: NormalizedValue | ABSENT}。

所有随机性只来自传入的 ``random.Random`` 实例。实体名由组件词表随机组合，全部为虚构内容。
"""

from __future__ import annotations

import datetime as _dt
import random
from decimal import Decimal
from typing import Any

from citecell.models import (
    BooleanValue,
    DateValue,
    DifficultyConfig,
    EnumValue,
    KeySet,
    KeySpec,
    MoneyValue,
    NormalizedValue,
    NumberValue,
    PercentValue,
    TextValue,
    ValueType,
)


class _Absent:
    """键在文档中缺失（金标准 expected=absent）。"""

    _instance: "_Absent | None" = None

    def __new__(cls) -> "_Absent":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "ABSENT"


ABSENT: Any = _Absent()

# 组件词表：仅用于拼接明显虚构的名称
NAME_PREFIX = ("云澜", "墨岚", "青禾", "远山", "白沙", "赤岭", "临川", "素湖")
NAME_MIDDLE = ("示范", "样例", "演练", "模拟", "试点")
NAME_SUFFIX = ("贸易有限公司", "装备有限公司", "物料中心", "供应链有限公司")
_DATE_BASE = _dt.date(2024, 1, 1)
_PERCENTS = (5, 10, 15, 20, 25, 30, 40, 50)
_MONTHS = (6, 12, 18, 24, 36, 48, 60)


def _fake_name(rng: random.Random) -> str:
    return rng.choice(NAME_PREFIX) + rng.choice(NAME_MIDDLE) + rng.choice(NAME_SUFFIX)


def sample_value(spec: KeySpec, rng: random.Random) -> NormalizedValue:
    """按键的值类型随机生成一个规范化真值。"""
    vt = spec.value_type
    if vt == ValueType.TEXT:
        if spec.key in ("甲方", "乙方"):
            return TextValue(text=_fake_name(rng))
        return TextValue(text=f"HT-{rng.randint(2024, 2026)}-{rng.randint(1, 9999):04d}")
    if vt == ValueType.MONEY:
        if rng.random() < 0.7:
            amount = Decimal(rng.randrange(3, 200) * 10000)  # 万元整数
        else:
            amount = Decimal(rng.randrange(1000, 2000000, 100))
        if rng.random() < 0.1:
            amount += Decimal(rng.choice(("0.05", "0.50", "0.10")))  # 含角分
        return MoneyValue(amount=amount, currency=spec.currency)
    if vt == ValueType.PERCENT:
        return PercentValue(value=Decimal(rng.choice(_PERCENTS)))
    if vt == ValueType.DATE:
        day = _DATE_BASE + _dt.timedelta(days=rng.randrange(0, 900))
        return DateValue(iso=day.isoformat(), precision="day")
    if vt == ValueType.NUMBER:
        return NumberValue(value=Decimal(rng.choice(_MONTHS)), unit=spec.unit)
    if vt == ValueType.ENUM:
        return EnumValue(value=rng.choice(spec.enum_options).value)
    if vt == ValueType.BOOLEAN:
        return BooleanValue(value=rng.random() < 0.5)
    raise ValueError(f"M1 不支持的值类型: {vt}")


def sample_facts(rng: random.Random, keyset: KeySet, diff: DifficultyConfig) -> dict[str, Any]:
    """采样事实表。missing_rate 决定键是否缺失（缺失记为 ABSENT）。键顺序与 KeySet 一致。"""
    facts: dict[str, Any] = {}
    for spec in keyset.keys:
        if rng.random() < diff.missing_rate:
            facts[spec.key] = ABSENT
        else:
            facts[spec.key] = sample_value(spec, rng)
    return facts


def _perturb(spec: KeySpec, truth: NormalizedValue, rng: random.Random) -> NormalizedValue:
    """生成一个"相似但错误"的候选值（调用方保证与真值不等价）。"""
    vt = spec.value_type
    if vt == ValueType.MONEY:
        return MoneyValue(amount=truth.amount + Decimal(10000 * rng.randint(1, 3)), currency=truth.currency)
    if vt == ValueType.PERCENT:
        return PercentValue(value=Decimal(rng.choice(_PERCENTS)))
    if vt == ValueType.DATE:
        day = _dt.date.fromisoformat(truth.iso) + _dt.timedelta(days=rng.choice([-30, -7, -1, 1, 7, 30]))
        return DateValue(iso=day.isoformat(), precision="day")
    if vt == ValueType.NUMBER:
        return NumberValue(value=truth.value + rng.randint(1, 12), unit=truth.unit)
    if vt == ValueType.ENUM:
        others = [o.value for o in spec.enum_options if o.value != truth.value]
        return EnumValue(value=rng.choice(others))
    if vt == ValueType.BOOLEAN:
        return BooleanValue(value=not truth.value)
    return sample_value(spec, rng)  # TEXT：重新采样名称或编号


def make_distractors(spec: KeySpec, truth: NormalizedValue, rng: random.Random, n: int) -> list[NormalizedValue]:
    """生成 n 个与真值不等价的干扰值。

    一般情况下干扰值两两互不相同。布尔键只有一个不等价取值，此时允许重复（每个干扰项仍与真值不等价）。
    """
    if spec.value_type == ValueType.BOOLEAN:
        return [_perturb(spec, truth, rng) for _ in range(n)]
    out: list[NormalizedValue] = []
    attempts = 0
    while len(out) < n:
        attempts += 1
        if attempts > 500:
            raise RuntimeError(f"无法为键 {spec.key!r} 生成 {n} 个不同的干扰值")
        cand = _perturb(spec, truth, rng)
        if cand != truth and cand not in out:
            out.append(cand)
    return out
