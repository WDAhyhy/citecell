"""验收 4：T2 值解析与 T5a 合成器表面形式的交叉往返。

* 用 random.Random(0) 生成 1000 个金额（0.01 到 10^10，两位小数）；合成器的中文大写
  ``synth.amount_to_uppercase(x)`` 经 ``values`` 解析后必须等于 x；
* 合成器产出的日期表面形式（中文日期、阿拉伯数字日期、ISO 形式）都能被解析回原值。

只使用公共接口：citecell.synth.surface、citecell.values.ValueNormalizerV1、金额/日期的 KeySpec 取自 KeySet。
"""

from __future__ import annotations

import datetime as dt
import random
from decimal import Decimal

from citecell.models import KeySet, MoneyValue, DateValue, ValueType
from citecell.synth.surface import amount_to_uppercase, arabic_date, cn_date
from citecell.values import ValueNormalizerV1

N_AMOUNTS = 1000
N_DATES = 500


def _spec(keyset: KeySet, value_type: ValueType):  # noqa: ANN201
    for spec in keyset.keys:
        if spec.value_type is value_type:
            return spec
    raise AssertionError(f"KeySet 中没有 {value_type.value} 类型的键")


def test_uppercase_amounts_roundtrip(keyset: KeySet) -> None:
    spec = _spec(keyset, ValueType.MONEY)
    vn = ValueNormalizerV1()
    rnd = random.Random(0)
    failures: list[str] = []
    for _ in range(N_AMOUNTS):
        cents = rnd.randint(1, 10**12)  # 0.01 .. 10_000_000_000.00
        x = (Decimal(cents) / Decimal(100)).quantize(Decimal("0.01"))
        text = amount_to_uppercase(x)
        parsed = vn.parse_value(text, spec)
        if not isinstance(parsed, MoneyValue) or Decimal(str(parsed.amount)) != x:
            failures.append(f"{x} -> {text} -> {parsed!r}")
    assert not failures, f"{len(failures)} 个金额往返失败：\n" + "\n".join(failures[:20])


def test_date_surface_forms_roundtrip(keyset: KeySet) -> None:
    spec = _spec(keyset, ValueType.DATE)
    vn = ValueNormalizerV1()
    rnd = random.Random(0)
    failures: list[str] = []
    base = dt.date(2000, 1, 1)
    for _ in range(N_DATES):
        d = base + dt.timedelta(days=rnd.randint(0, 25_000))
        for surface in (cn_date(d), arabic_date(d), d.isoformat()):
            parsed = vn.parse_value(surface, spec)
            if not isinstance(parsed, DateValue) or parsed.iso != d.isoformat() or parsed.precision != "day":
                failures.append(f"{d.isoformat()} -> {surface} -> {parsed!r}")
    assert not failures, f"{len(failures)} 个日期表面形式往返失败：\n" + "\n".join(failures[:20])
