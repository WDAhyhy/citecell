"""引文匹配 QuoteMatcher 的实现（规则见 docs/ARCHITECTURE.md §6.2 步骤 4b–4e）。

匹配顺序：EXACT → NORMALIZED →（可选）FUZZY。只返回最高命中等级的全部命中，按 start 升序。

约定与取舍（均为本实现的决定，已在 README/测试中说明）：
- 重叠出现也计为命中（``"aa"`` 在 ``"aaa"`` 中命中 start=0 与 start=1）。
- NORMALIZED 命中的边界必须落在原文码点边界上：若命中切开了某个原文码点的 NFKC 展开
  （例如用 ``"2"`` 命中 ``㎡`` 展开出的 ``m2`` 中的 ``2``），该命中被丢弃，
  否则映射回的原文区间会比命中串更宽，且再归一化后不等于命中串。
- FUZZY 使用 Sellers 算法（子串编辑距离，起点自由），在归一化文本上进行；
  超过长度上限（原文 5000、引文 500 码点，可配置）直接返回空；
  编辑距离上限为 ``floor(len(引文归一化) * fuzzy_max_ratio)``，上限为 0 时不返回 FUZZY。
  FUZZY 命中互不重叠，按（距离、起点、终点）贪心选取。score = 1 - 距离 / 引文归一化长度。
"""

from __future__ import annotations

from citecell.models import MatchLevel, NormalizedText, QuoteMatch
from citecell.textnorm.normalize import Normalizer

VERSION = "matcher-v1"

DEFAULT_FUZZY_MAX_TEXT = 5000
DEFAULT_FUZZY_MAX_QUOTE = 500
DEFAULT_FUZZY_MAX_RATIO = 0.2


def _all_occurrences(hay: str, needle: str) -> list[int]:
    """返回 needle 在 hay 中所有（含重叠）出现的起点，升序。needle 为空时返回空列表。"""
    if not needle:
        return []
    out: list[int] = []
    pos = hay.find(needle)
    while pos != -1:
        out.append(pos)
        pos = hay.find(needle, pos + 1)
    return out


def _aligned(nt: NormalizedText, s: int, e: int) -> bool:
    """归一化区间 [s, e) 的两端是否都落在原文码点边界上。"""
    im = nt.index_map
    if s > 0 and im[s - 1] == im[s]:
        return False
    if e < len(im) and im[e - 1] == im[e]:
        return False
    return True


def _sellers(pattern: str, text: str) -> tuple[list[int], list[int]]:
    """Sellers 子串编辑距离。返回最后一行的 (距离, 起点) 列表，下标为终点（0..len(text)）。

    同距离时取较早的起点，保证确定性。
    """
    n = len(text)
    prev_d = [0] * (n + 1)  # 空模式对任意位置距离为 0（起点自由）
    prev_s = list(range(n + 1))
    for i in range(1, len(pattern) + 1):
        pc = pattern[i - 1]
        cur_d = [i] + [0] * n
        cur_s = [0] * (n + 1)
        for j in range(1, n + 1):
            best_d = prev_d[j - 1] + (0 if pc == text[j - 1] else 1)
            best_s = prev_s[j - 1]
            d = prev_d[j] + 1  # 模式字符缺失
            if d < best_d or (d == best_d and prev_s[j] < best_s):
                best_d, best_s = d, prev_s[j]
            d = cur_d[j - 1] + 1  # 文本字符多余
            if d < best_d or (d == best_d and cur_s[j - 1] < best_s):
                best_d, best_s = d, cur_s[j - 1]
            cur_d[j] = best_d
            cur_s[j] = best_s
        prev_d, prev_s = cur_d, cur_s
    return prev_d, prev_s


class Matcher:
    """满足 ``citecell.interfaces.QuoteMatcher`` 协议的引文匹配器。"""

    version: str = VERSION

    def __init__(
        self,
        *,
        fuzzy_max_text: int = DEFAULT_FUZZY_MAX_TEXT,
        fuzzy_max_quote: int = DEFAULT_FUZZY_MAX_QUOTE,
        fuzzy_max_ratio: float = DEFAULT_FUZZY_MAX_RATIO,
    ) -> None:
        self._norm = Normalizer()
        self._fuzzy_max_text = fuzzy_max_text
        self._fuzzy_max_quote = fuzzy_max_quote
        self._fuzzy_max_ratio = fuzzy_max_ratio

    def find(self, quote: str, text: str, *, allow_fuzzy: bool = False) -> list[QuoteMatch]:
        q = quote.strip()
        if not q or not text:
            return []

        # EXACT：逐字子串（仅去除引文首尾空白）
        exact = _all_occurrences(text, q)
        if exact:
            return [QuoteMatch(level=MatchLevel.EXACT, start=s, end=s + len(q)) for s in exact]

        # NORMALIZED：归一化后子串，且不切开原文码点
        nt = self._norm.normalize(text)
        nq = self._norm.normalize(q).text
        if not nq:
            return []
        normalized: list[QuoteMatch] = []
        for s in _all_occurrences(nt.text, nq):
            e = s + len(nq)
            if not _aligned(nt, s, e):
                continue
            os_, oe = nt.to_original_span(s, e)
            normalized.append(QuoteMatch(level=MatchLevel.NORMALIZED, start=os_, end=oe))
        if normalized:
            return normalized

        if allow_fuzzy:
            return self._fuzzy(q, text, nt, nq)
        return []

    def _fuzzy(self, q: str, text: str, nt: NormalizedText, nq: str) -> list[QuoteMatch]:
        if len(text) > self._fuzzy_max_text or len(q) > self._fuzzy_max_quote:
            return []
        m = len(nq)
        max_dist = int(m * self._fuzzy_max_ratio)
        if max_dist <= 0:
            return []

        dist, starts = _sellers(nq, nt.text)
        candidates: list[tuple[int, int, int]] = []  # (距离, 起点, 终点)
        for j in range(1, len(nt.text) + 1):
            d, s = dist[j], starts[j]
            if d <= max_dist and s < j and _aligned(nt, s, j):
                candidates.append((d, s, j))
        candidates.sort()

        accepted: list[tuple[int, int, int]] = []
        for d, s, j in candidates:
            if any(s < aj and as_ < j for _, as_, aj in accepted):
                continue
            accepted.append((d, s, j))
        accepted.sort(key=lambda c: c[1])

        out: list[QuoteMatch] = []
        for d, s, j in accepted:
            os_, oe = nt.to_original_span(s, j)
            score = 1.0 - d / m
            score = min(1.0, max(0.0, score))
            out.append(QuoteMatch(level=MatchLevel.FUZZY, start=os_, end=oe, score=score))
        return out
