"""上下文规划：决定每次模型调用送哪些块、问哪些键（ARCHITECTURE §6.5）。

流程：

1. ``prefilter="auto"`` 且整文档渲染后不超过预算 → 单窗口含全部块；键按 KeySet 顺序，
   仅受 ``max_keys_per_window`` 拆分。
2. 否则（``prefilter="always"`` 或文档超预算）按键选块：
   * 字符二元组 BM25（键名 + 说明 + 别名为查询）取高分块作为核心（至多 ``CORE_TOP_K`` 个）；
   * 核心块的扩展：±1 邻块、所在章节标题、所在表格的表头块（第二梯队：列或行的同伴单元格）；
   * 没有任何命中的键：按文档顺序取前缀（超出预算则标 ``truncated``），不静默丢弃；
   * 预算内贪心装填；装不下的块被跳过并标 ``truncated``；
3. 按 KeySet 顺序逐键装箱：仅当新键的选块与当前窗口的块高度重叠（≥50%）、键数未超过
   ``max_keys_per_window``、合并后仍在预算内时才合并，否则开新窗口。
4. 窗口内块按文档顺序；用渲染后的真实文本再估算一次，超出预算则从窗口尾部（最后加入、优先级最低的块）移除。

全部过程确定：无随机数、集合只用于成员判断、迭代顺序显式排序。
"""

from __future__ import annotations

import logging
import math
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from citecell.models import (
    Block,
    BlockKind,
    ContextWindow,
    KeySpec,
    ParsedDocument,
    RunConfig,
)
from citecell.prompting.renderer import render_block_line, render_document_lines
from citecell.prompting.tokens import HeuristicTokenCounter

logger = logging.getLogger("citecell.prompting")


def _terms(text: str) -> Counter[str]:
    """字符二元组（单字符段保留为一元组）。按空白切分段落，段之间不生成跨段二元组。"""
    counts: Counter[str] = Counter()
    norm = unicodedata.normalize("NFKC", text or "").lower()
    for seg in norm.split():
        if len(seg) == 1:
            counts[seg] += 1
        else:
            for i in range(len(seg) - 1):
                counts[seg[i : i + 2]] += 1
    return counts


def _query_terms(spec: KeySpec) -> list[str]:
    terms: set[str] = set()
    terms.update(_terms(spec.key).keys())
    if spec.description:
        terms.update(_terms(spec.description).keys())
    for alias in spec.aliases:
        terms.update(_terms(alias).keys())
    return sorted(terms)


def _dedupe(seq: Sequence[int]) -> list[int]:
    seen: set[int] = set()
    out: list[int] = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


@dataclass
class _BlockIndex:
    blocks: Sequence[Block]
    table_members: dict[str, list[int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for i, b in enumerate(self.blocks):
            if b.table is not None:
                self.table_members.setdefault(b.table.table_id, []).append(i)

    def expansions(self, i: int) -> tuple[list[int], list[int]]:
        """返回 (第一梯队, 第二梯队) 扩展块下标，均不含 i 本身。"""
        blocks = self.blocks
        b = blocks[i]
        tier1: list[int] = []
        tier2: list[int] = []
        if i - 1 >= 0:
            tier1.append(i - 1)
        if i + 1 < len(blocks):
            tier1.append(i + 1)
        for name in b.section_path:
            for j in range(i - 1, -1, -1):
                h = blocks[j]
                if h.kind == BlockKind.HEADING and h.text.strip() == name.strip():
                    tier1.append(j)
                    break
        if b.table is not None:
            members = self.table_members.get(b.table.table_id, [])
            for j in members:
                if j != i and blocks[j].table is not None and blocks[j].table.is_header:
                    tier1.append(j)
            t = b.table
            if t.is_header:
                tier2.extend(
                    j for j in members if j != i and blocks[j].table is not None and blocks[j].table.col_index == t.col_index
                )
            else:
                tier2.extend(
                    j
                    for j in members
                    if j != i
                    and blocks[j].table is not None
                    and blocks[j].table.row_index == t.row_index
                    and blocks[j].table.col_index == 0
                )
        return [j for j in _dedupe(tier1) if j != i], [j for j in _dedupe(tier2) if j != i]


class DefaultContextPlanner:
    """默认规划器：整文档直送，否则字符二元组 BM25 按键选块。"""

    version = "planner_v1"
    CORE_TOP_K = 8
    BM25_K1 = 1.2
    BM25_B = 0.75
    MIN_MERGE_OVERLAP = 0.5

    def __init__(self, counter: HeuristicTokenCounter | None = None) -> None:
        self._counter = counter or HeuristicTokenCounter()

    @property
    def token_counter_name(self) -> str:
        return self._counter.name

    # -- 公共接口 ---------------------------------------------------------

    def plan(
        self,
        doc: ParsedDocument,
        keys: Sequence[KeySpec],
        *,
        budget_tokens: int,
        config: RunConfig | None = None,
    ) -> list[ContextWindow]:
        """规划窗口。``config`` 为 None 时等同于默认 RunConfig（auto / cell_lines / 不限键数）。"""
        if budget_tokens < 1:
            raise ValueError("budget_tokens 必须为正")
        key_list = list(keys)
        if not key_list:
            raise ValueError("至少需要一个键")
        if len({k.key for k in key_list}) != len(key_list):
            raise ValueError("键名重复")

        prefilter = config.prefilter if config is not None else "auto"
        table_style = config.table_style if config is not None else "cell_lines"
        max_keys = config.max_keys_per_window if config is not None else None

        blocks = doc.blocks
        if not blocks:
            return []

        if prefilter == "auto":
            full_tokens = self._counter.count("\n".join(render_document_lines(doc, blocks, table_style)))
            if full_tokens <= budget_tokens:
                all_ids = [b.block_id for b in blocks]
                return self._chunk_keys_single(key_list, max_keys, all_ids, full_tokens, doc.info.doc_id)

        return self._plan_by_keys(doc, key_list, budget_tokens, table_style, max_keys)

    # -- 整文档直送 -------------------------------------------------------

    @staticmethod
    def _chunk_keys_single(
        keys: Sequence[KeySpec],
        max_keys: int | None,
        all_ids: list[str],
        tokens: int,
        doc_id: str,
    ) -> list[ContextWindow]:
        size = len(keys) if max_keys is None else max_keys
        out: list[ContextWindow] = []
        for start in range(0, len(keys), size):
            chunk = keys[start : start + size]
            out.append(
                ContextWindow(
                    window_id=f"w{len(out) + 1:03d}",
                    doc_id=doc_id,
                    block_ids=list(all_ids),
                    keys=[k.key for k in chunk],
                    estimated_tokens=tokens,
                    truncated=False,
                )
            )
        return out

    # -- 按键选块 ---------------------------------------------------------

    def _plan_by_keys(
        self,
        doc: ParsedDocument,
        keys: Sequence[KeySpec],
        budget: int,
        table_style: str,
        max_keys: int | None,
    ) -> list[ContextWindow]:
        blocks = doc.blocks
        n = len(blocks)
        costs = [self._counter.count(render_block_line(b, table_style)) for b in blocks]
        index = _BlockIndex(blocks)

        doc_terms = [_terms(b.text) for b in blocks]
        doc_len = [sum(c.values()) for c in doc_terms]
        avg_len = (sum(doc_len) / n) if n else 1.0
        postings: dict[str, list[tuple[int, int]]] = {}
        for i, c in enumerate(doc_terms):
            for t, tf in c.items():
                postings.setdefault(t, []).append((i, tf))

        selections: list[tuple[str, list[int], bool]] = []
        for spec in keys:
            scores = self._bm25(spec, n, doc_len, avg_len, postings)
            ordered, truncated = self._select_for_key(spec.key, scores, index, costs, budget)
            selections.append((spec.key, ordered, truncated))

        return self._pack(doc, selections, costs, budget, table_style, max_keys)

    def _bm25(
        self,
        spec: KeySpec,
        n: int,
        doc_len: list[int],
        avg_len: float,
        postings: dict[str, list[tuple[int, int]]],
    ) -> list[float]:
        scores = [0.0] * n
        k1, b = self.BM25_K1, self.BM25_B
        for term in _query_terms(spec):
            plist = postings.get(term)
            if not plist:
                continue
            df = len(plist)
            idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
            for i, tf in plist:
                denom = tf + k1 * (1.0 - b + b * doc_len[i] / max(avg_len, 1e-9))
                scores[i] += idf * tf * (k1 + 1.0) / denom
        return scores

    def _select_for_key(
        self,
        key: str,
        scores: list[float],
        index: _BlockIndex,
        costs: list[int],
        budget: int,
    ) -> tuple[list[int], bool]:
        n = len(scores)
        positives = [i for i in range(n) if scores[i] > 0.0]
        if positives:
            cores = sorted(positives, key=lambda i: (-scores[i], i))[: self.CORE_TOP_K]
            tier1: list[int] = []
            tier2: list[int] = []
            for c in cores:
                t1, t2 = index.expansions(c)
                tier1.extend(t1)
                tier2.extend(t2)
            priority = _dedupe(list(cores) + tier1 + tier2)
        else:
            # 没有任何命中：按文档顺序取前缀，超出预算时标记截断，不静默丢弃。
            priority = list(range(n))

        chosen: list[int] = []
        used = 0
        truncated = False
        for i in priority:
            if used + costs[i] <= budget:
                chosen.append(i)
                used += costs[i]
            else:
                truncated = True
        if not chosen:
            raise ValueError(f"预算 {budget} 小于键 {key} 的任一候选块，无法规划")
        return chosen, truncated

    def _pack(
        self,
        doc: ParsedDocument,
        selections: list[tuple[str, list[int], bool]],
        costs: list[int],
        budget: int,
        table_style: str,
        max_keys: int | None,
    ) -> list[ContextWindow]:
        blocks = doc.blocks
        groups: list[dict] = []
        cur: dict | None = None
        for name, ordered, truncated in selections:
            sel = set(ordered)
            if cur is not None and (max_keys is None or len(cur["keys"]) < max_keys):
                overlap = len(sel & cur["U"]) / len(sel)
                extra = sum(costs[i] for i in sel - cur["U"])
                if overlap >= self.MIN_MERGE_OVERLAP and cur["cost"] + extra <= budget:
                    cur["keys"].append(name)
                    for i in ordered:
                        if i not in cur["U"]:
                            cur["U"].add(i)
                            cur["added"].append(i)
                    cur["cost"] += extra
                    cur["truncated"] = cur["truncated"] or truncated
                    continue
            if cur is not None:
                groups.append(cur)
            cur = {
                "keys": [name],
                "U": set(ordered),
                "added": list(ordered),
                "cost": sum(costs[i] for i in ordered),
                "truncated": truncated,
            }
        if cur is not None:
            groups.append(cur)

        windows: list[ContextWindow] = []
        for seq, g in enumerate(groups, start=1):
            exact_lines = render_document_lines(doc, [blocks[i] for i in sorted(g["U"])], table_style)
            exact = self._counter.count("\n".join(exact_lines))
            trunc = g["truncated"]
            while exact > budget and g["added"]:
                dropped = g["added"].pop()
                g["U"].discard(dropped)
                trunc = True
                exact_lines = render_document_lines(doc, [blocks[i] for i in sorted(g["U"])], table_style)
                exact = self._counter.count("\n".join(exact_lines))
            if exact > budget:
                raise ValueError(f"预算 {budget} 小于窗口 w{seq:03d} 的最小渲染长度")
            windows.append(
                ContextWindow(
                    window_id=f"w{seq:03d}",
                    doc_id=doc.info.doc_id,
                    block_ids=[blocks[i].block_id for i in sorted(g["U"])],
                    keys=list(g["keys"]),
                    estimated_tokens=exact,
                    truncated=trunc,
                )
            )
        logger.debug("规划完成：%d 个键 → %d 个窗口", len(selections), len(windows))
        return windows


__all__ = ["DefaultContextPlanner"]
