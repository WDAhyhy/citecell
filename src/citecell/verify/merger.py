"""SimpleMerger（ARCH §6.5 合并部分）：同一 (文档, 键) 多条记录 → 一条记录。

合并规则（按优先级，确定性，只依赖输入顺序与整数/字符串比较）：

1. **grounded**：按规范化值等价分组（``ValueNormalizerV1.equivalent``）。
   - 只有一组：合并证据（按 块、区间、值区间 去重，保持首次出现顺序）；
     **所有** needs_review 记录（带值者）进入 ``alternatives``，即使值与采纳值等价，
     因为它们可能带有单位冲突等信息；其问题以 warning 级保留在合并记录中。
   - 多组（值不等价）：needs_review + ``CONFLICTING_CANDIDATES``，``value`` 为空，
     每组一条 ``alternatives``（证据为组内并集），needs_review 中带值的记录也进入 ``alternatives``。
   - not_found 与 failed 不改变 grounded 结果（"窗口未见到不代表没有"）；failed 的问题以 warning 记录。
2. **无 grounded，有 needs_review**：取第一条带值的 needs_review 为主记录（保留其证据与索引）；
   其余带不等价值的记录进入 ``alternatives``。若多个 needs_review 的值互不等价，
   结果为 needs_review + ``CONFLICTING_CANDIDATES``，``value`` 为空，全部带值记录进入 ``alternatives``。
3. **只有 failed 与 not_found 的组合**：只要存在 failed，结果为 failed（基础设施失败不能被 not_found 掩盖）；
   否则全部 not_found → not_found。

``producer_id`` 规则：合并记录的 producer_id 取**第一条被采纳（或主）记录**的值；
各候选的 producer_id 保留在 ``alternatives`` 中，因此溯源不丢失。

注意：合并记录上的问题若来自某条子记录，其 ``evidence_index`` 不再有效（证据列表已重组），
因此复制时清空；只有“主记录本身的证据列表被原样保留”时才保留索引。
"""

from __future__ import annotations

from collections.abc import Sequence

from citecell.models import (
    AlternativeValue,
    Evidence,
    ExtractionMethod,
    ExtractionRecord,
    Issue,
    IssueCode,
    KeySpec,
    NormalizedValue,
    Severity,
    Status,
)
from citecell.values import ValueNormalizerV1

MERGER_VERSION = "merger-simple-v1"


def _evidence_key(e: Evidence) -> tuple:
    return (e.block_id, e.char_start, e.char_end, e.value_char_start, e.value_char_end)


def _dedupe_evidence(items: Sequence[Evidence]) -> list[Evidence]:
    seen: set[tuple] = set()
    out: list[Evidence] = []
    for e in items:
        k = _evidence_key(e)
        if k not in seen:
            seen.add(k)
            out.append(e)
    return out


def _copy_issues(items: Sequence[Issue], *, severity: Severity | None = None) -> list[Issue]:
    """复制问题并清空 evidence_index（合并后索引失效）；按 (代码, 严重度, 说明) 去重，保持首次顺序。"""
    seen: set[tuple] = set()
    out: list[Issue] = []
    for i in items:
        sev = severity if severity is not None else i.severity
        k = (i.code, sev, i.message)
        if k in seen:
            continue
        seen.add(k)
        out.append(Issue(code=i.code, severity=sev, message=i.message, evidence_index=None))
    return out


def _dedupe_ids(items: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


class SimpleMerger:
    """满足 ``citecell.interfaces.Resolver`` 协议的 M1 合并器。确定性纯函数。"""

    version: str = MERGER_VERSION

    def __init__(self) -> None:
        self._values = ValueNormalizerV1()

    def resolve(self, doc_id: str, spec: KeySpec, records: Sequence[ExtractionRecord]) -> ExtractionRecord:
        if not records:
            raise ValueError("至少需要一条记录才能合并")
        for r in records:
            if r.doc_id != doc_id or r.key != spec.key:
                raise ValueError("记录与文档或键定义不一致")
            if r.status == Status.DERIVED:
                raise ValueError("M1 合并器不处理 derived 记录（由 M3 决策层处理）")

        attempt_ids = _dedupe_ids([a for r in records for a in r.attempt_ids])
        grounded = [r for r in records if r.status == Status.GROUNDED]
        review = [r for r in records if r.status == Status.NEEDS_REVIEW]
        failed = [r for r in records if r.status == Status.FAILED]

        if grounded:
            return self._merge_grounded(doc_id, spec, grounded, review, failed, attempt_ids)
        if review:
            return self._merge_review(doc_id, spec, review, failed, attempt_ids)
        if failed:
            issues = _copy_issues([i for r in failed for i in r.issues])
            return self._make(doc_id, spec, Status.FAILED, None, None, [], None, None, [], issues, attempt_ids)
        issues = _copy_issues([i for r in records for i in r.issues])
        return self._make(doc_id, spec, Status.NOT_FOUND, None, None, [], None, None, [], issues, attempt_ids)

    # ------------------------------------------------------------ grounded
    def _merge_grounded(
        self,
        doc_id: str,
        spec: KeySpec,
        grounded: list[ExtractionRecord],
        review: list[ExtractionRecord],
        failed: list[ExtractionRecord],
        attempt_ids: list[str],
    ) -> ExtractionRecord:
        groups = self._group_by_value(grounded, spec)
        warn_failed = _copy_issues([i for r in failed for i in r.issues], severity=Severity.WARNING)

        # 未被采纳的 needs_review 记录全部进入 alternatives，其问题降为 warning 保留在合并记录中。
        review_issues = _copy_issues([i for r in review for i in r.issues], severity=Severity.WARNING)
        review_alts = [self._record_alternative(r) for r in review if r.value is not None]

        if len(groups) > 1:
            alternatives = [self._group_alternative(g) for g in groups] + review_alts
            issues = [
                Issue(code=IssueCode.CONFLICTING_CANDIDATES, severity=Severity.ERROR, message="多个基于原文的值互不等价")
            ] + review_issues + warn_failed
            first = groups[0][0]
            return self._make(
                doc_id, spec, Status.NEEDS_REVIEW, None, None, [], first.method, first.producer_id,
                alternatives, issues, attempt_ids,
            )

        group = groups[0]
        chosen_value = group[0].value
        evidence = _dedupe_evidence([e for r in group for e in r.evidence])
        # 与采纳值等价的 needs_review 也进入 alternatives（不丢弃其单位冲突等问题）。
        alternatives = review_alts
        info = _copy_issues([i for r in group for i in r.issues if i.severity == Severity.INFO])
        issues = info + review_issues + warn_failed
        # 采纳值与待复核候选不等价或存在单位冲突：仍保持 grounded，但附 warning 级 CONFLICTING_CANDIDATES。
        conflict = any(
            r.value is not None
            and (
                not self._values.equivalent(r.value, chosen_value, spec)
                or any(i.code == IssueCode.UNIT_MISMATCH for i in r.issues)
            )
            for r in review
        )
        if conflict:
            issues.append(
                Issue(code=IssueCode.CONFLICTING_CANDIDATES, severity=Severity.WARNING, message="采纳值与待复核候选不一致（值或单位冲突）")
            )
        first = group[0]
        return self._make(
            doc_id, spec, Status.GROUNDED, chosen_value, first.value_text, evidence, first.method,
            first.producer_id, alternatives, issues, attempt_ids,
        )

    def _group_by_value(self, grounded: list[ExtractionRecord], spec: KeySpec) -> list[list[ExtractionRecord]]:
        """按值等价分组，组的顺序为首次出现顺序（确定性）。"""
        groups: list[list[ExtractionRecord]] = []
        for r in grounded:
            assert r.value is not None  # grounded 不变量
            for g in groups:
                assert g[0].value is not None
                if self._values.equivalent(g[0].value, r.value, spec):
                    g.append(r)
                    break
            else:
                groups.append([r])
        return groups

    # ------------------------------------------------------------ needs_review
    def _merge_review(
        self,
        doc_id: str,
        spec: KeySpec,
        review: list[ExtractionRecord],
        failed: list[ExtractionRecord],
        attempt_ids: list[str],
    ) -> ExtractionRecord:
        valued = [r for r in review if r.value is not None]
        primary = valued[0] if valued else review[0]
        warn_failed = _copy_issues([i for r in failed for i in r.issues], severity=Severity.WARNING)
        others = [r for r in review if r is not primary]

        conflict = any(
            not self._values.equivalent(primary.value, r.value, spec)
            for r in valued
            if primary.value is not None
        )
        if conflict:
            alternatives = [self._record_alternative(r) for r in valued]
            issues = [
                Issue(code=IssueCode.CONFLICTING_CANDIDATES, severity=Severity.ERROR, message="多个待复核的值互不等价")
            ] + warn_failed
            return self._make(
                doc_id, spec, Status.NEEDS_REVIEW, None, None, [], primary.method, primary.producer_id,
                alternatives, issues, attempt_ids,
            )

        alternatives = [
            self._record_alternative(r)
            for r in others
            if r.value is not None and (primary.value is None or not self._values.equivalent(primary.value, r.value, spec))
        ]
        # 主记录的问题保留索引（其证据列表原样保留）；其余子记录的问题清空索引。
        own = list(primary.issues)
        extra = _copy_issues([i for r in others for i in r.issues] + warn_failed)
        issues = own + [i for i in extra if (i.code, i.severity, i.message) not in {(j.code, j.severity, j.message) for j in own}]
        return self._make(
            doc_id, spec, Status.NEEDS_REVIEW, primary.value, primary.value_text, list(primary.evidence),
            primary.method, primary.producer_id, alternatives, issues, attempt_ids,
        )

    # ------------------------------------------------------------ 构造辅助
    @staticmethod
    def _group_alternative(group: list[ExtractionRecord]) -> AlternativeValue:
        first = group[0]
        return AlternativeValue(
            value_text=first.value_text,
            value=first.value,
            evidence=_dedupe_evidence([e for r in group for e in r.evidence]),
            issues=_copy_issues([i for r in group for i in r.issues]),
            producer_id=first.producer_id or "unknown",
        )

    @staticmethod
    def _record_alternative(r: ExtractionRecord) -> AlternativeValue:
        # 单条记录的证据列表原样保留，因此其问题索引仍然有效。
        return AlternativeValue(
            value_text=r.value_text,
            value=r.value,
            evidence=list(r.evidence),
            issues=list(r.issues),
            producer_id=r.producer_id or "unknown",
        )

    @staticmethod
    def _make(
        doc_id: str,
        spec: KeySpec,
        status: Status,
        value: NormalizedValue | None,
        value_text: str | None,
        evidence: list[Evidence],
        method: ExtractionMethod | None,
        producer_id: str | None,
        alternatives: list[AlternativeValue],
        issues: list[Issue],
        attempt_ids: list[str],
    ) -> ExtractionRecord:
        return ExtractionRecord(
            doc_id=doc_id,
            key=spec.key,
            status=status,
            value=value,
            value_text=value_text,
            evidence=evidence,
            method=method,
            producer_id=producer_id,
            alternatives=alternatives,
            issues=issues,
            attempt_ids=attempt_ids,
        )


__all__ = ["MERGER_VERSION", "SimpleMerger"]
