"""偏移保持归一化 NORMALIZER_V1（规则见 docs/ARCHITECTURE.md §6.1）。

逐码点处理原文，产出归一化文本与 ``index_map``（归一化码点 → 原文码点下标）。
规则要点：
1. 空白、零宽字符、软连字符：删除（产出 0 个码点）；
2. 单码点 NFKC（全角 ASCII→半角、①→1、㎡→m2 等，1 → 多时每个产出码点都映射回同一原文下标）；
   NFKC 结果中的空白（如 U+3000 → 空格）随即删除；
3. 标点只映射不删除（禁止删除，否则 ``12.5`` 与 ``125`` 相等）；
4. 拉丁字母 ``casefold()``。

本模块为纯标准库实现，且不对任何输入抛异常。
"""

from __future__ import annotations

import unicodedata

from citecell.models import NormalizedText

VERSION = "normalizer-v1"

# 第 1 步：直接删除的码点（空白另由 str.isspace 判定）。
_DELETE = frozenset("​‌‍﻿­")

# 第 3 步：标点映射表（只映射，不删除）。
_PUNCT_MAP: dict[str, str] = {
    # 双引号类
    "“": '"',  # “
    "”": '"',  # ”
    "„": '"',  # „
    # 单引号类
    "‘": "'",  # ‘
    "’": "'",  # ’
    # 顿号、句号
    "、": ",",  # 、
    "。": ".",  # 。
    # 方括号类
    "【": "[",  # 【
    "】": "]",  # 】
    "〔": "[",  # 〔
    "〕": "]",  # 〕
    # 各类横线 → "-"
    "‐": "-",  # 连字符
    "‑": "-",  # 不间断连字符
    "‒": "-",  # 数字破折号
    "–": "-",  # 短破折号
    "—": "-",  # 长破折号
    "―": "-",  # 水平线
    "−": "-",  # 减号
    "﹘": "-",  # 小横线
    "﹣": "-",  # 小号连字符
    # 波浪号 → "~"
    "〜": "~",  # 〜
    "～": "~",  # ～
}


def _is_latin(ch: str) -> bool:
    if ch.isascii():
        return ch.isalpha()
    return unicodedata.name(ch, "").startswith("LATIN")


class Normalizer:
    """NORMALIZER_V1 的实现，满足 ``citecell.interfaces.TextNormalizer`` 协议。"""

    version: str = VERSION

    def normalize(self, text: str) -> NormalizedText:
        out: list[str] = []
        index_map: list[int] = []
        for i, ch in enumerate(text):
            if ch.isspace() or ch in _DELETE:
                continue
            for c0 in unicodedata.normalize("NFKC", ch):
                if c0.isspace() or c0 in _DELETE:
                    continue
                c1 = _PUNCT_MAP.get(c0, c0)
                if _is_latin(c1):
                    c1 = c1.casefold()
                for oc in c1:
                    out.append(oc)
                    index_map.append(i)
        return NormalizedText(original=text, text="".join(out), index_map=tuple(index_map))
