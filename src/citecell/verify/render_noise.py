"""渲染噪声剥离（ARCH §6.2 步骤 4a）。

提示词渲染（ARCH §6.6）会在每个块前加上 ``[块编号]`` 或 ``[块编号 | 列头 | …]`` 前缀。
模型有时会把这些前缀连同正文一起抄进引文，例如 ``[s1-B3 | 单价] 25.00``。
本模块只剥离**形如块编号的方括号前缀**，不做其他猜测：

* 只匹配以 BLOCK_ID 形式开头的方括号（``p3-b12``、``para17``、``t2-r3-c1``、``s1-B3``），
  方括号内可跟 ``| …`` 的列头说明。普通方括号文字（如 ``[附件]``）不会被剥离。
* 行模式渲染的 ``[s1-A3] 螺丝M6 | [s1-B3] 25.00`` 中间的前缀也会被剥离；
  剥离后的 `` | `` 分隔符保留（跨单元格引文本来就无法在单一块中命中）。

调用方（Verifier）应当**先用原始引文匹配，失败后才使用剥离后的引文**，
以免误删原文中恰好形如块编号的文字。
"""

from __future__ import annotations

import re

#: 与 ``citecell.models.BLOCK_ID_PATTERN`` 一致的块编号子模式（去掉首尾锚点）。
_BLOCK_TOKEN = r"(?:p[1-9]\d*-b[1-9]\d*|para[1-9]\d*|t[1-9]\d*-r[1-9]\d*-c[1-9]\d*|s[1-9]\d*-[A-Z]{1,3}[1-9]\d*)"

#: 渲染前缀：``[块编号]`` 或 ``[块编号 | 列头 …]``，之后可跟空白。
NOISE_PREFIX = re.compile(rf"\[{_BLOCK_TOKEN}(?:\s*\|[^\[\]\n]*)?\]\s*")


def strip_render_noise(quote: str) -> str:
    """返回剥离渲染前缀后的引文（首尾空白已去除）。不含噪声时原样返回去空白后的串。

    >>> strip_render_noise("[s1-B3 | 单价] 25.00")
    '25.00'
    """
    return NOISE_PREFIX.sub("", quote).strip()


def has_render_noise(quote: str) -> bool:
    """引文中是否含有可剥离的渲染前缀。"""
    return NOISE_PREFIX.search(quote) is not None


__all__ = ["NOISE_PREFIX", "has_render_noise", "strip_render_noise"]
