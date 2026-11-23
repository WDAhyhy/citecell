"""预测与金标准对齐：键为 ``(doc_sha256, key)``（ARCHITECTURE.md §9）。

对齐规则（不一致时直接报错，绝不静默丢弃）：
* 结果中的每份文档都必须有金标准；无金标准的文档 → ``ValueError``。
* 金标准中的文档若结果里没有 → 计入 ``n_missing``（指针集中下载失败或哈希不符时的跳过）。
* 对齐文档的键集合必须与金标准键集合完全相同，且都属于 KeySet。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from citecell.models import (
    DocFormat,
    DocumentResult,
    ExtractionRecord,
    GoldDocument,
    GoldRecord,
    KeySet,
    KeySpec,
    RunResult,
)


@dataclass(frozen=True, slots=True)
class AlignedRecord:
    """一个（文档 × 键）单元格的预测与金标准配对。"""

    doc_sha256: str
    doc_format: DocFormat
    key: str
    spec: KeySpec
    pred: ExtractionRecord
    gold: GoldRecord

    @property
    def gold_present(self) -> bool:
        """金标准中该键存在（present 或 derived）。"""
        return self.gold.expected in ("present", "derived")


@dataclass(frozen=True, slots=True)
class Alignment:
    records: list[AlignedRecord]
    docs: list[DocumentResult]
    gold_docs: list[GoldDocument]
    n_missing: int


def align(result: RunResult, gold: Sequence[GoldDocument], keyset: KeySet) -> Alignment:
    specs = {k.key: k for k in keyset.keys}

    pred_by_sha: dict[str, DocumentResult] = {}
    for dr in result.documents:
        sha = dr.info.sha256
        if sha in pred_by_sha:
            raise ValueError("结果中同一文档出现多次")
        pred_by_sha[sha] = dr

    gold_by_sha: dict[str, GoldDocument] = {}
    for g in gold:
        if g.doc_sha256 in gold_by_sha:
            raise ValueError("金标准中同一文档出现多次")
        gold_by_sha[g.doc_sha256] = g

    unlabeled = set(pred_by_sha) - set(gold_by_sha)
    if unlabeled:
        raise ValueError(f"结果中有 {len(unlabeled)} 份文档没有金标准，无法评测")

    records: list[AlignedRecord] = []
    docs: list[DocumentResult] = []
    gold_docs: list[GoldDocument] = []
    n_missing = 0
    for sha in sorted(gold_by_sha):
        g = gold_by_sha[sha]
        dr = pred_by_sha.get(sha)
        if dr is None:
            n_missing += 1
            continue
        gold_keys = [r.key for r in g.records]
        if len(set(gold_keys)) != len(gold_keys):
            raise ValueError("金标准中同一文档的键重复")
        unknown = set(gold_keys) - set(specs)
        if unknown:
            raise ValueError(f"金标准包含 KeySet 中不存在的键：{sorted(unknown)}")
        pred_by_key = {r.key: r for r in dr.records}
        if set(pred_by_key) != set(gold_keys):
            raise ValueError("文档的预测键集合与金标准不一致")
        for gr in sorted(g.records, key=lambda r: r.key):
            records.append(
                AlignedRecord(
                    doc_sha256=sha,
                    doc_format=g.format,
                    key=gr.key,
                    spec=specs[gr.key],
                    pred=pred_by_key[gr.key],
                    gold=gr,
                )
            )
        docs.append(dr)
        gold_docs.append(g)
    return Alignment(records=records, docs=docs, gold_docs=gold_docs, n_missing=n_missing)


__all__ = ["AlignedRecord", "Alignment", "align"]
