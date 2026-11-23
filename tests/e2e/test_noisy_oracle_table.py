"""验收 3：NoisyOracle 逐类型。

每次只把一种 InjectedErrorType 的注入比例设为 1，max_semantic_retries=0（只看首轮行为），
然后按下面的预期表断言 Verifier/流水线对**被注入的键**的行为。只检查被注入的键；
未注入的键由验收 2 覆盖。每个用例同时断言该类型确实被注入过（否则测试是空的）。

预期表（依据 ARCH §6.2、§6.5；「引文唯一」指解析后全文该引文只命中一次）：

| 注入类型 | 被注入键的期望行为 |
|---|---|
| WRONG_BLOCK_ID | 引文唯一 → RELOCATED（claimed_block_id 记录原值）且 grounded；引文多处命中 → QUOTE_AMBIGUOUS，非 grounded |
| PARAPHRASED_QUOTE | 非 grounded（needs_review；QUOTE_FUZZY_ONLY / QUOTE_NOT_IN_BLOCK） |
| VALUE_NOT_IN_QUOTE | 非 grounded（needs_review；VALUE_NOT_IN_QUOTE） |
| MISSING_KEY | 非 grounded；输出中缺键，重试耗尽后 failed（KEY_MISSING_IN_OUTPUT），不得被记为 not_found |
| HALLUCINATED_VALUE | 非 grounded（值不在引文中） |
| DISTRACTOR_VALUE | **静默错误**：grounded 但值与金标准不等价；若值不可解析则 needs_review（VALUE_UNPARSEABLE） |
| CROSS_BLOCK_QUOTE | grounded 时必须带 QUOTE_CROSS_BLOCK；否则 needs_review |
| MALFORMED_JSON | 整次输出非法：该文档所有键 failed 或 needs_review（OUTPUT_SCHEMA_INVALID），不得 grounded |

纯数字金额单元格（XLSX，已知局限）可能额外带 VALUE_UNPARSEABLE 而成为 needs_review，表中允许。
"""

from __future__ import annotations

import pytest

from citecell.backends import NoisyOracleBackend
from citecell.models import (
    InjectedErrorType,
    IssueCode,
    KeySet,
    RunResult,
    Status,
    ValueType,
)
from citecell.values import ValueNormalizerV1

UNPARSEABLE = IssueCode.VALUE_UNPARSEABLE


def _codes(rec) -> set[IssueCode]:  # noqa: ANN001
    return {i.code for i in rec.issues}


def _bare_amount_limitation(rec, specs) -> bool:  # noqa: ANN001
    """已知局限：金额键的纯数字单元格因 VALUE_UNPARSEABLE 被判 needs_review（待表头单位修复）。"""
    return specs[rec.key].value_type is ValueType.MONEY and IssueCode.VALUE_UNPARSEABLE in _codes(rec)


def _injected_records(etype: InjectedErrorType, backend: NoisyOracleBackend, result: RunResult, corpus):  # noqa: ANN001
    """返回 [(gold_doc, gold_record, record, injected_doc_result)] —— 仅被注入 etype 的键。"""
    keys = {(i.doc_id, i.key) for i in backend.injections if i.error_type is etype and i.key is not None}
    out = []
    for dr in result.documents:
        gold = corpus.gold_by_sha[dr.info.sha256]
        gold_rec = {r.key: r for r in gold.records}
        for rec in dr.records:
            if (dr.info.doc_id, rec.key) in keys:
                out.append((gold, gold_rec[rec.key], rec, dr))
    return out


def _assert_injected(etype: InjectedErrorType, backend: NoisyOracleBackend) -> None:
    n = sum(1 for i in backend.injections if i.error_type is etype and i.key is not None)
    if etype is InjectedErrorType.MALFORMED_JSON:
        n = sum(1 for i in backend.injections if i.error_type is etype)
    assert n > 0, f"{etype.value} 在语料中从未被注入，测试为空"


def test_wrong_block_id_relocates_unique_quotes_only(corpus, keyset, noisy_runs) -> None:  # noqa: ANN001
    etype = InjectedErrorType.WRONG_BLOCK_ID
    backend, result = noisy_runs(etype)
    _assert_injected(etype, backend)
    specs = {k.key: k for k in keyset.keys}
    relocated = ambiguous = 0
    problems: list[str] = []
    for gold, gold_rec, rec, dr in _injected_records(etype, backend, result, corpus):
        codes = _codes(rec)
        if IssueCode.QUOTE_RELOCATED in codes:
            relocated += 1
            if rec.status != Status.GROUNDED and not _bare_amount_limitation(rec, specs):
                problems.append(f"{rec.key}: RELOCATED 但 {rec.status.value}")
            if not any(ev.claimed_block_id and ev.claimed_block_id != ev.block_id for ev in rec.evidence):
                problems.append(f"{rec.key}: RELOCATED 但未保留 claimed_block_id")
        elif IssueCode.QUOTE_AMBIGUOUS in codes:
            ambiguous += 1
            if rec.status == Status.GROUNDED:
                problems.append(f"{rec.key}: 引文多处命中却 grounded")
    assert relocated > 0, "应至少有一个唯一引文被重定位"
    assert not problems, "\n".join(problems[:20])


@pytest.mark.parametrize(
    ("etype", "expected_codes"),
    [
        (InjectedErrorType.PARAPHRASED_QUOTE, {IssueCode.QUOTE_FUZZY_ONLY, IssueCode.QUOTE_NOT_IN_BLOCK}),
        (InjectedErrorType.VALUE_NOT_IN_QUOTE, {IssueCode.VALUE_NOT_IN_QUOTE}),
        (InjectedErrorType.HALLUCINATED_VALUE, {IssueCode.VALUE_NOT_IN_QUOTE, IssueCode.VALUE_UNPARSEABLE, IssueCode.ENUM_OUT_OF_RANGE, IssueCode.EMPTY_EVIDENCE_WITH_VALUE}),
    ],
    ids=lambda x: x.value if isinstance(x, InjectedErrorType) else "",
)
def test_injected_quote_or_value_errors_are_not_grounded(corpus, keyset, noisy_runs, etype, expected_codes) -> None:  # noqa: ANN001
    backend, result = noisy_runs(etype)
    _assert_injected(etype, backend)
    rows = _injected_records(etype, backend, result, corpus)
    problems = [
        f"{rec.key}: grounded（应被拦下）"
        for _, _, rec, _ in rows
        if rec.status == Status.GROUNDED
    ]
    no_expected = [
        f"{rec.key}: 状态 {rec.status.value} 未包含预期问题码 {sorted(c.value for c in expected_codes)}，实际 {sorted(c.value for c in _codes(rec))}"
        for _, _, rec, _ in rows
        if rec.status != Status.GROUNDED and not (_codes(rec) & expected_codes)
    ]
    assert rows, "没有被注入的键"
    assert not problems, "\n".join(problems[:20])
    # 未被拦下的理由必须与预期表一致（允许其他码并存，但至少要含一个预期码）。
    assert not no_expected, "\n".join(no_expected[:20])


def test_missing_key_is_failed_not_not_found(corpus, keyset, noisy_runs) -> None:  # noqa: ANN001
    etype = InjectedErrorType.MISSING_KEY
    backend, result = noisy_runs(etype)
    _assert_injected(etype, backend)
    rows = _injected_records(etype, backend, result, corpus)
    assert rows
    problems = [
        f"{rec.key}: 实际 {rec.status.value}"
        for _, _, rec, _ in rows
        if not (rec.status == Status.FAILED and IssueCode.KEY_MISSING_IN_OUTPUT in _codes(rec))
    ]
    assert not problems, "\n".join(problems[:20])


def test_distractor_value_is_silent_wrong_grounding(corpus, keyset, noisy_runs) -> None:  # noqa: ANN001
    """DISTRACTOR_VALUE：引文与值都真实存在于原文，因此 grounded，但值错误（静默错误，ADR-001 的已知边界）。"""
    etype = InjectedErrorType.DISTRACTOR_VALUE
    backend, result = noisy_runs(etype)
    _assert_injected(etype, backend)
    specs = {k.key: k for k in keyset.keys}
    vn = ValueNormalizerV1()
    rows = _injected_records(etype, backend, result, corpus)
    assert rows
    silent = flagged = 0
    problems: list[str] = []
    for gold, gold_rec, rec, _ in rows:
        if rec.status == Status.GROUNDED:
            silent += 1
            if rec.value is not None and gold_rec.value is not None and vn.equivalent(rec.value, gold_rec.value, specs[rec.key]):
                problems.append(f"{rec.key}: grounded 但值与金标准等价（应是干扰值）")
        elif rec.status == Status.NEEDS_REVIEW:
            # 校验器拦下（如值无法解析、否定语境、值不在引文中）：不是静默错误，合格。
            flagged += 1
        else:
            problems.append(f"{rec.key}: 实际 {rec.status.value}，既非静默 grounded 也非 needs_review")
    assert silent > 0, "应观察到静默错误（grounded 但值错误）"
    assert silent + flagged == len(rows)
    assert not problems, "\n".join(problems[:20])


def test_cross_block_quote_is_marked(corpus, keyset, noisy_runs) -> None:  # noqa: ANN001
    etype = InjectedErrorType.CROSS_BLOCK_QUOTE
    backend, result = noisy_runs(etype)
    _assert_injected(etype, backend)
    rows = _injected_records(etype, backend, result, corpus)
    assert rows
    problems = [
        f"{rec.key}: grounded 但未标记 QUOTE_CROSS_BLOCK"
        for _, _, rec, _ in rows
        if rec.status == Status.GROUNDED and IssueCode.QUOTE_CROSS_BLOCK not in _codes(rec)
    ]
    assert not problems, "\n".join(problems[:20])


def test_malformed_json_fails_whole_window(corpus, keyset, noisy_runs) -> None:  # noqa: ANN001
    etype = InjectedErrorType.MALFORMED_JSON
    backend, result = noisy_runs(etype)
    _assert_injected(etype, backend)
    problems: list[str] = []
    n = 0
    for dr in result.documents:
        for rec in dr.records:
            n += 1
            if rec.status == Status.GROUNDED:
                problems.append(f"{dr.info.doc_id}/{rec.key}: 非法输出却 grounded")
            if rec.status not in (Status.FAILED, Status.NEEDS_REVIEW):
                problems.append(f"{dr.info.doc_id}/{rec.key}: 实际 {rec.status.value}")
    assert n > 0
    assert not problems, "\n".join(problems[:20])
