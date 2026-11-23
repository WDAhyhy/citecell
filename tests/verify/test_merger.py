"""T7 验收 15：SimpleMerger 合并规则（ARCH §6.5）。

记录由 Verifier 真实产生（而不是手工拼装），以保证输入满足契约不变量。
"""

from __future__ import annotations

from citecell.models import (
    Block,
    BlockKind,
    Candidate,
    DocFormat,
    DocumentInfo,
    DocxLocator,
    EvidenceClaim,
    ExtractionMethod,
    IssueCode,
    KeySpec,
    ParsedDocument,
    Status,
    ValueType,
)
from citecell.verify import SimpleMerger, Verifier

DOC_ID = "0123456789abcdef"
KEY = "合同金额"
SPEC = KeySpec(key=KEY, value_type=ValueType.MONEY)


def _doc(texts: list[str]) -> ParsedDocument:
    info = DocumentInfo(
        doc_id=DOC_ID,
        sha256=DOC_ID + "0" * 48,
        format=DocFormat.DOCX,
        size_bytes=1,
        parser_name="test",
        parser_version="0",
    )
    blocks = [
        Block(
            doc_id=DOC_ID,
            block_id=f"para{i + 1}",
            kind=BlockKind.PARAGRAPH,
            text=t,
            order=i,
            locator=DocxLocator(body_index=i, paragraph_number=i + 1),
        )
        for i, t in enumerate(texts)
    ]
    return ParsedDocument(info=info, blocks=blocks)


def _rec(doc: ParsedDocument, claims: list[tuple[str, str]], value: str | None):
    c = Candidate(
        doc_id=DOC_ID,
        key=KEY,
        value_text=value,
        evidence=[EvidenceClaim(block_id=b, quote=q) for b, q in claims],
        method=ExtractionMethod.LLM,
        producer_id="llm:test",
    )
    return Verifier().verify(c, doc, SPEC)


def _codes(rec) -> set[IssueCode]:
    return {i.code for i in rec.issues}


def test_acc_15_same_value_evidence_is_merged_and_deduplicated():
    doc = _doc(["合同金额为1,200元。", "金额合计1,200元。"])
    a = _rec(doc, [("para1", "合同金额为1,200元")], "1,200元")
    b = _rec(doc, [("para2", "金额合计1,200元")], "1,200元")
    c = _rec(doc, [("para1", "合同金额为1,200元")], "1,200元")  # 与 a 完全相同，应被去重
    out = SimpleMerger().resolve(DOC_ID, SPEC, [a, b, c])
    assert out.status == Status.GROUNDED
    assert len(out.evidence) == 2
    assert {e.block_id for e in out.evidence} == {"para1", "para2"}


def test_acc_15_two_unequal_grounded_values_conflict_and_both_alternatives():
    doc = _doc(["合同金额为1,200元。", "合同金额为1,300元。"])
    a = _rec(doc, [("para1", "合同金额为1,200元")], "1,200元")
    b = _rec(doc, [("para2", "合同金额为1,300元")], "1,300元")
    assert a.status == Status.GROUNDED and b.status == Status.GROUNDED
    out = SimpleMerger().resolve(DOC_ID, SPEC, [a, b])
    assert out.status == Status.NEEDS_REVIEW
    assert IssueCode.CONFLICTING_CANDIDATES in _codes(out)
    assert out.value is None
    assert len(out.alternatives) == 2
    amounts = sorted(str(alt.value.amount) for alt in out.alternatives)
    assert amounts == ["1200", "1300"]


def test_acc_15_grounded_plus_not_found_is_grounded():
    doc = _doc(["合同金额为1,200元。", "其他条款。"])
    g = _rec(doc, [("para1", "合同金额为1,200元")], "1,200元")
    nf = _rec(doc, [], None)
    assert nf.status == Status.NOT_FOUND
    out = SimpleMerger().resolve(DOC_ID, SPEC, [nf, g])
    assert out.status == Status.GROUNDED
    assert out.value == g.value


def test_acc_15_all_not_found_is_not_found():
    doc = _doc(["其他条款。", "无关内容。"])
    a = _rec(doc, [], None)
    b = _rec(doc, [], None)
    out = SimpleMerger().resolve(DOC_ID, SPEC, [a, b])
    assert out.status == Status.NOT_FOUND
    assert out.value is None and out.evidence == []


def test_acc_15_needs_review_value_is_alternative_when_grounded_wins():
    doc = _doc(["合同金额为1,200元。", "合同金额为1,300元。"])
    g = _rec(doc, [("para1", "合同金额为1,200元")], "1,200元")
    # 值 1300 在引文 "合同金额为1,200元" 中找不到 → needs_review(VALUE_NOT_IN_QUOTE)
    y = _rec(doc, [("para1", "合同金额为1,200元")], "1,300元")
    assert y.status == Status.NEEDS_REVIEW and y.value is not None
    out = SimpleMerger().resolve(DOC_ID, SPEC, [y, g])
    assert out.status == Status.GROUNDED
    assert out.value == g.value
    assert len(out.alternatives) == 1
    assert out.alternatives[0].value == y.value


def test_failed_is_not_masked_by_not_found():
    from citecell.models import ExtractionRecord, Issue, Severity

    doc = _doc(["其他条款。"])
    nf = _rec(doc, [], None)
    failed = ExtractionRecord(
        doc_id=DOC_ID,
        key=KEY,
        status=Status.FAILED,
        method=ExtractionMethod.LLM,
        producer_id="llm:test",
        issues=[Issue(code=IssueCode.BACKEND_TIMEOUT, severity=Severity.ERROR, message="超时")],
    )
    out = SimpleMerger().resolve(DOC_ID, SPEC, [nf, failed])
    assert out.status == Status.FAILED


def test_merge_is_deterministic():
    doc = _doc(["合同金额为1,200元。", "合同金额为1,300元。"])
    a = _rec(doc, [("para1", "合同金额为1,200元")], "1,200元")
    b = _rec(doc, [("para2", "合同金额为1,300元")], "1,300元")
    m = SimpleMerger()
    assert m.resolve(DOC_ID, SPEC, [a, b]).model_dump() == m.resolve(DOC_ID, SPEC, [a, b]).model_dump()


def test_empty_input_rejected():
    import pytest

    with pytest.raises(ValueError):
        SimpleMerger().resolve(DOC_ID, SPEC, [])


