"""规范 JSON 与 sha256 工具（ARCH §11）。

所有哈希都基于这里的规范序列化：``sort_keys=True, ensure_ascii=False,
separators=(",", ":")``，Decimal 序列化为字符串，NaN/Infinity 不允许。
被 T6（请求哈希、档位配置哈希）与 T10（清单、配置哈希）共用。
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

from pydantic import BaseModel


def _default(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, (set, frozenset)):
        raise TypeError("集合没有确定的顺序，不能进入规范 JSON；请先转为排序后的列表")
    raise TypeError(f"无法规范化的类型: {type(obj).__name__}")


def canonical_json(obj: Any) -> str:
    """返回规范 JSON 字符串。Pydantic 模型按 ``model_dump(mode="json")`` 处理。"""
    if isinstance(obj, BaseModel):
        obj = obj.model_dump(mode="json")
    return json.dumps(
        obj,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
        default=_default,
    )


def canonical_sha256(obj: Any) -> str:
    """规范 JSON 的 sha256 十六进制摘要（小写，64 位）。"""
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()
