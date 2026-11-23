"""Token 估算（启发式，宁高勿低）。

系数未经真实 tokenizer 校准（本任务未引入任何 tokenizer 依赖，也未能在沙箱中核实目标模型的分词结果）：

* 每个 CJK 字符（汉字、CJK 标点、全角字符）计 ``CJK_TOKENS_PER_CHAR = 1.0`` 个 token；
* 其余非空白字符按每 ``LATIN_CHARS_PER_TOKEN = 3`` 个字符向上取整计 1 个 token（常见英文分词约 4 字符/token，取 3 偏保守）。

保守性保证：``count(text) >= CJK 字符数``。真实 tokenizer 可用后应在 M1 实测中校准并记录系数，
再把 ``name`` 改为新版本号。
"""

from __future__ import annotations

import math

CJK_TOKENS_PER_CHAR = 1.0
LATIN_CHARS_PER_TOKEN = 3.0


def is_cjk(ch: str) -> bool:
    """判断字符是否属于 CJK 统一表意文字、扩展区、CJK 标点或全角字符区。"""
    o = ord(ch)
    return (
        0x3000 <= o <= 0x303F  # CJK 标点
        or 0x3400 <= o <= 0x4DBF  # 扩展 A
        or 0x4E00 <= o <= 0x9FFF  # 基本区
        or 0xF900 <= o <= 0xFAFF  # 兼容表意
        or 0xFF00 <= o <= 0xFFEF  # 全角 ASCII / 标点
        or 0x20000 <= o <= 0x2EBEF  # 扩展 B–F
    )


class HeuristicTokenCounter:
    """默认启发式 token 计数器。见模块文档中的系数说明。"""

    name = "heuristic_v1"

    def count(self, text: str) -> int:
        if not text:
            return 0
        cjk = 0
        other = 0
        for ch in text:
            if ch.isspace():
                continue
            if is_cjk(ch):
                cjk += 1
            else:
                other += 1
        return math.ceil(cjk * CJK_TOKENS_PER_CHAR) + math.ceil(other / LATIN_CHARS_PER_TOKEN)


__all__ = ["HeuristicTokenCounter", "is_cjk", "CJK_TOKENS_PER_CHAR", "LATIN_CHARS_PER_TOKEN"]
