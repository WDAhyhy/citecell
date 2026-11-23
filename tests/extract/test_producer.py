"""LLMCandidateProducer 的重试与失败语义（通过 Pipeline 驱动，验证端到端的记录状态）。"""

from __future__ import annotations

import asyncio
import json

from citecell.backends import FakeBackend
from citecell.extract import build_pipeline
from citecell.interfaces import BackendError
from citecell.models import AttemptOutcome, IssueCode, RunConfig, Status


def answer(fields: list[dict]) -> str:
    return json.dumps({"fields": fields}, ensure_ascii=False)


def field(key: str, block_id: str, quote: str, value: str | None) -> dict:
    evidence = [] if value is None else [{"block_id": block_id, "quote": quote}]
    return {"key": key, "evidence": evidence, "value": value}


def _run(backend, keyset, config, path):
    pipeline = build_pipeline(backend, config)
    return asyncio.run(pipeline.run([path], keyset, config, run_id="t-run"))


def _config(backend, **kw) -> RunConfig:
    return RunConfig(backend=backend.info(), **kw)


def test_second_round_fixes_wrong_quote(make_docx, parse, block_id_of, keyset_one):
    path = make_docx()
    bid = block_id_of(parse(path), "HT-2024-001")
    backend = FakeBackend(
        [
            answer([field("合同编号", bid, "HT-2024-OOO", "HT-2024-OOO")]),  # 首轮：引文不在块中
            answer([field("合同编号", bid, "HT-2024-001", "HT-2024-001")]),  # 第二轮：正确
        ]
    )
    result = _run(backend, keyset_one, _config(backend), path)
    doc = result.documents[0]
    rec = doc.records[0]

    assert rec.status == Status.GROUNDED
    assert rec.value_text == "HT-2024-001"
    assert len(doc.attempts) == 2
    first, second = doc.attempts
    assert second.retry_of == first.attempt_id
    assert first.retry_of is None
    # 单元格的 attempt_ids 记录产生过候选的全部尝试（审计用），被否决的首轮也保留。
    assert rec.attempt_ids == [first.attempt_id, second.attempt_id]
    # 第二轮只重问失败的键
    assert second.keys == ["合同编号"]


def test_two_wrong_rounds_end_as_needs_review(make_docx, parse, block_id_of, keyset_one):
    path = make_docx()
    bid = block_id_of(parse(path), "HT-2024-001")
    wrong = answer([field("合同编号", bid, "HT-2024-OOO", "HT-2024-OOO")])
    backend = FakeBackend([wrong], repeat_last=True)
    result = _run(backend, keyset_one, _config(backend, max_semantic_retries=2), path)
    doc = result.documents[0]

    assert doc.records[0].status == Status.NEEDS_REVIEW
    assert len(doc.attempts) == 3  # 首轮 + 2 次语义重试
    assert doc.attempts[1].retry_of == doc.attempts[0].attempt_id
    assert doc.attempts[2].retry_of == doc.attempts[1].attempt_id


def test_no_semantic_retry_when_disabled(make_docx, parse, block_id_of, keyset_one):
    path = make_docx()
    bid = block_id_of(parse(path), "HT-2024-001")
    wrong = answer([field("合同编号", bid, "HT-2024-OOO", "HT-2024-OOO")])
    backend = FakeBackend([wrong], repeat_last=True)
    result = _run(backend, keyset_one, _config(backend, max_semantic_retries=0), path)
    doc = result.documents[0]

    assert doc.records[0].status == Status.NEEDS_REVIEW
    assert len(doc.attempts) == 1


def test_backend_error_marks_window_keys_failed(make_docx, keyset_three):
    path = make_docx()
    backend = FakeBackend([BackendError("连接被拒绝")])
    result = _run(backend, keyset_three, _config(backend), path)
    doc = result.documents[0]

    assert [r.key for r in doc.records] == ["合同编号", "甲方", "合同金额"]
    for rec in doc.records:
        assert rec.status == Status.FAILED
        codes = {i.code for i in rec.issues}
        assert IssueCode.BACKEND_ERROR in codes
    assert len(doc.attempts) == 1
    assert doc.attempts[0].outcome == AttemptOutcome.TRANSPORT_ERROR


def test_invalid_output_always_fails_with_schema_code(make_docx, keyset_one):
    path = make_docx()
    backend = FakeBackend(["这不是 JSON"], repeat_last=True)
    result = _run(backend, keyset_one, _config(backend, max_semantic_retries=2), path)
    doc = result.documents[0]
    rec = doc.records[0]

    assert rec.status == Status.FAILED
    assert IssueCode.OUTPUT_SCHEMA_INVALID in {i.code for i in rec.issues}
    assert all(a.outcome == AttemptOutcome.SCHEMA_INVALID for a in doc.attempts)
    assert len(doc.attempts) == 3


def test_key_missing_every_round_fails(make_docx, keyset_one):
    path = make_docx()
    backend = FakeBackend([answer([])], repeat_last=True)
    result = _run(backend, keyset_one, _config(backend, max_semantic_retries=1), path)
    rec = result.documents[0].records[0]

    assert rec.status == Status.FAILED
    assert IssueCode.KEY_MISSING_IN_OUTPUT in {i.code for i in rec.issues}


def test_missing_key_recovered_on_retry(make_docx, parse, block_id_of, keyset_one):
    path = make_docx()
    bid = block_id_of(parse(path), "HT-2024-001")
    backend = FakeBackend(
        [
            answer([]),  # 首轮漏键
            answer([field("合同编号", bid, "HT-2024-001", "HT-2024-001")]),
        ]
    )
    result = _run(backend, keyset_one, _config(backend), path)
    rec = result.documents[0].records[0]

    assert rec.status == Status.GROUNDED
    assert len(result.documents[0].attempts) == 2


def test_not_found_is_not_retried(make_docx, keyset_one):
    path = make_docx()
    backend = FakeBackend([answer([field("合同编号", "p1-b1", "", None)])], repeat_last=True)
    result = _run(backend, keyset_one, _config(backend), path)
    doc = result.documents[0]

    assert doc.records[0].status == Status.NOT_FOUND
    assert len(doc.attempts) == 1
