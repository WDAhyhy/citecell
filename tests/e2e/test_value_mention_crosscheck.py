"""T2 值提及识别的交叉检查（经公共接口 ValueNormalizerV1.find_mentions）。

* 正例：「24个月」「二十四个月」「十二个月」「十六个月」能被识别；
* 回归：单个汉字数字 + 个月（「六个月」「五个月」）也应被识别（曾为缺陷，T2 已修复，现为普通回归测试）。
"""

from __future__ import annotations

import pytest

from citecell.models import KeySet, ValueType
from citecell.values import ValueNormalizerV1


@pytest.fixture(scope="module")
def months_spec(keyset: KeySet):  # noqa: ANN001
    return next(k for k in keyset.keys if k.key == "质保期")


@pytest.mark.parametrize("text", ["24个月", "二十四个月", "十二个月", "十六个月", "六个月", "五个月", "七个月"])
def test_month_mentions_are_found(months_spec, text: str) -> None:  # noqa: ANN001
    assert months_spec.value_type is ValueType.NUMBER
    mentions = ValueNormalizerV1().find_mentions(text, months_spec)
    assert len(mentions) == 1, f"{text!r} 应识别出 1 个提及，实际 {len(mentions)}"
