"""OracleBackend / NoisyOracleBackend / FakeBackend。

夹具在运行时手工构造（两段正文 + 一个干扰值 + 一个缺失键），不使用真实文档。
"""

from __future__ import annotations

import asyncio

import pytest

from citecell.backends import FakeBackend, NoisyOracleBackend, OracleBackend
from citecell.interfaces import BackendError
from citecell.models import (
    Block,
    BlockKind,
    ChatMessage,
    ChatRequest,
    DataSource,
    DocFormat,
    DocumentInfo,
    DocxLocator,
    GoldDocument,
    GoldMention,
    GoldRecord,
    InjectedErrorType,
    LLMExtractionOutput,
    ParsedDocument,
    RequestContext,
    TextValue,
)

SHA = "ab" * 32
DOC_ID = SHA[:16]

B1 = "原报价为¥90,000.00，本项目名称为示例采购项目。"
B2 = "合同金额为¥100,000.00，付款方式为分期。"
B3 = "验收日期为2026年3月1日。"
SURFACE_AMOUNT = "¥100,000.00"
SURFACE_DATE = "2026年3月1日"
SURFACE_DISTRACTOR = "¥90,000.00"

ALL_BLOCKS = ["p1-b1", "p1-b2", "p1-b3"]
KEYS = ["合同金额", "验收日期", "保证金"]


def make_parsed(sha: str = SHA) -> ParsedDocument:
    info = DocumentInfo(
        doc_id=sha[:16],
        sha256=sha,
        format=DocFormat.DOCX,
        size_bytes=1234,
        parser_name="test-parser",
        parser_version="0.0.0",
    )
    blocks = [
        Block(
            doc_id=sha[:16],
            block_id=f"p1-b{i + 1}",
            kind=BlockKind.PARAGRAPH,
            text=t,
            order=i,
            locator=DocxLocator(body_index=i, paragraph_number=i + 1),
        )
        for i, t in enumerate([B1, B2, B3])
    ]
    return ParsedDocument(info=info, blocks=blocks)


def make_gold() -> GoldDocument:
    records = [
        GoldRecord(
            key="合同金额",
            expected="present",
            value=TextValue(text=SURFACE_AMOUNT),
            mentions=[
                GoldMention(locator=DocxLocator(body_index=1, paragraph_number=2), surface_text=SURFACE_AMOUNT)
            ],
            distractors=[
                GoldMention(locator=DocxLocator(body_index=0, paragraph_number=1), surface_text=SURFACE_DISTRACTOR)
            ],
        ),
        GoldRecord(
            key="验收日期",
            expected="present",
            value=TextValue(text=SURFACE_DATE),
            mentions=[
                GoldMention(locator=DocxLocator(body_index=2, paragraph_number=3), surface_text=SURFACE_DATE)
            ],
        ),
        GoldRecord(key="保证金", expected="absent"),
    ]
    return GoldDocument(
        doc_sha256=SHA,
        format=DocFormat.DOCX,
        source=DataSource.SYNTHETIC,
        keyset_name="test_keyset",
        keyset_version="1",
        records=records,
    )


def _chat_request(keys=KEYS, block_ids=ALL_BLOCKS, attempt=0, window="w1", doc_id=DOC_ID) -> ChatRequest:
    return ChatRequest(
        messages=[ChatMessage(role="user", content="提示词占位")],
        context=RequestContext(
            doc_id=doc_id,
            window_id=window,
            keys=list(keys),
            block_ids=list(block_ids),
            attempt_index=attempt,
        ),
    )


def _run(backend, request):
    return asyncio.run(backend.chat(request))


# ---------------------------------------------------------------- Oracle
def test_oracle_output_validates_against_wire_schema():
    backend = OracleBackend([make_gold()], [make_parsed()])
    resp = _run(backend, _chat_request())
    out = LLMExtractionOutput.model_validate_json(resp.content)
    assert [f.key for f in out.fields] == KEYS


def test_oracle_present_quotes_surface_from_its_block():
    parsed = make_parsed()
    backend = OracleBackend([make_gold()], [parsed])
    out = LLMExtractionOutput.model_validate_json(_run(backend, _chat_request()).content)
    amount = out.fields[0]
    assert amount.value == SURFACE_AMOUNT
    assert amount.evidence[0].block_id == "p1-b2"
    assert amount.evidence[0].quote == SURFACE_AMOUNT
    assert amount.evidence[0].quote in parsed.blocks[1].text  # 引文是块中的逐字子串


def test_oracle_absent_key_is_null_with_no_evidence():
    backend = OracleBackend([make_gold()], [make_parsed()])
    out = LLMExtractionOutput.model_validate_json(_run(backend, _chat_request()).content)
    absent = out.fields[2]
    assert absent.key == "保证金"
    assert absent.value is None and absent.evidence == []


def test_oracle_value_outside_window_is_reported_as_absent():
    backend = OracleBackend([make_gold()], [make_parsed()])
    out = LLMExtractionOutput.model_validate_json(
        _run(backend, _chat_request(keys=["验收日期"], block_ids=["p1-b1"])).content
    )
    assert out.fields[0].value is None and out.fields[0].evidence == []


def test_oracle_requires_context():
    backend = OracleBackend([make_gold()], [make_parsed()])
    with pytest.raises(BackendError):
        _run(backend, ChatRequest(messages=[ChatMessage(role="user", content="x")]))


def test_oracle_unknown_key_is_backend_error():
    backend = OracleBackend([make_gold()], [make_parsed()])
    with pytest.raises(BackendError):
        _run(backend, _chat_request(keys=["不存在的键"]))


def test_oracle_unknown_document_is_backend_error():
    backend = OracleBackend([make_gold()], [make_parsed()])
    with pytest.raises(BackendError):
        _run(backend, _chat_request(doc_id="0" * 16))


def test_sha_mismatch_between_gold_and_parsed_rejected():
    # 同一 doc_id（sha 前 16 位相同）但完整哈希不同 → 必须拒绝
    parsed = make_parsed(sha=SHA[:32] + "cd" * 16)
    with pytest.raises(ValueError):
        OracleBackend([make_gold()], [parsed])


# ---------------------------------------------------------- NoisyOracle
ALL_PER_KEY_TYPES = [
    InjectedErrorType.WRONG_BLOCK_ID,
    InjectedErrorType.PARAPHRASED_QUOTE,
    InjectedErrorType.VALUE_NOT_IN_QUOTE,
    InjectedErrorType.MISSING_KEY,
    InjectedErrorType.HALLUCINATED_VALUE,
    InjectedErrorType.DISTRACTOR_VALUE,
    InjectedErrorType.CROSS_BLOCK_QUOTE,
]

# 夹具中各类错误对各键的适用情况（合同金额 / 验收日期 / 保证金）：
#  WRONG_BLOCK_ID、PARAPHRASED_QUOTE、VALUE_NOT_IN_QUOTE、MISSING_KEY：两个存在的键
#  HALLUCINATED_VALUE：三个键都适用（缺失键也会被编造值）
#  DISTRACTOR_VALUE：仅合同金额有干扰项
#  CROSS_BLOCK_QUOTE：仅合同金额（其后有块）
EXPECTED_APPLICABLE = {
    InjectedErrorType.WRONG_BLOCK_ID: 2,
    InjectedErrorType.PARAPHRASED_QUOTE: 2,
    InjectedErrorType.VALUE_NOT_IN_QUOTE: 2,
    InjectedErrorType.MISSING_KEY: 2,
    InjectedErrorType.HALLUCINATED_VALUE: 3,
    InjectedErrorType.DISTRACTOR_VALUE: 1,
    InjectedErrorType.CROSS_BLOCK_QUOTE: 1,
}


@pytest.mark.parametrize("etype", ALL_PER_KEY_TYPES)
def test_each_error_type_fires_at_ratio_one(etype):
    backend = NoisyOracleBackend([make_gold()], [make_parsed()], ratios={etype: 1.0}, seed=1)
    _run(backend, _chat_request())
    injected = backend.injections
    assert len(injected) == EXPECTED_APPLICABLE[etype]
    assert all(i.error_type is etype for i in injected)
    assert all(i.doc_id == DOC_ID for i in injected)
    assert len({i.key for i in injected}) == len(injected)  # 每键至多一条


def test_missing_key_removes_fields():
    backend = NoisyOracleBackend(
        [make_gold()], [make_parsed()], ratios={InjectedErrorType.MISSING_KEY: 1.0}, seed=1
    )
    out = LLMExtractionOutput.model_validate_json(_run(backend, _chat_request()).content)
    # 两个存在的键被省略；缺失键 保证金 不适用 MISSING_KEY，仍原样输出
    assert [f.key for f in out.fields] == ["保证金"]


def test_paraphrased_quote_is_not_verbatim():
    backend = NoisyOracleBackend(
        [make_gold()], [make_parsed()], ratios={InjectedErrorType.PARAPHRASED_QUOTE: 1.0}, seed=1
    )
    out = LLMExtractionOutput.model_validate_json(_run(backend, _chat_request()).content)
    quote = out.fields[0].evidence[0].quote
    assert quote not in B2


def test_zero_ratio_equals_clean_oracle():
    gold, parsed = make_gold(), make_parsed()
    clean = _run(OracleBackend([gold], [parsed]), _chat_request()).content
    noisy_backend = NoisyOracleBackend([gold], [parsed], ratios={}, seed=99)
    noisy = _run(noisy_backend, _chat_request()).content
    assert noisy == clean and noisy_backend.injections == []


def test_malformed_json_truncates_and_records_key_none():
    backend = NoisyOracleBackend(
        [make_gold()], [make_parsed()], ratios={InjectedErrorType.MALFORMED_JSON: 1.0}, seed=3
    )
    resp = _run(backend, _chat_request())
    with pytest.raises(ValueError):
        LLMExtractionOutput.model_validate_json(resp.content)
    assert len(backend.injections) == 1
    assert backend.injections[0].key is None
    assert backend.injections[0].error_type is InjectedErrorType.MALFORMED_JSON


def test_same_seed_gives_identical_outputs():
    ratios = {t: 0.5 for t in ALL_PER_KEY_TYPES}
    runs = []
    for _ in range(2):
        b = NoisyOracleBackend([make_gold()], [make_parsed()], ratios=ratios, seed=42)
        contents = [_run(b, _chat_request(attempt=a)).content for a in range(4)]
        runs.append((contents, len(b.injections)))
    assert runs[0] == runs[1]


def test_outputs_do_not_depend_on_call_order():
    ratios = {t: 0.5 for t in ALL_PER_KEY_TYPES}
    b1 = NoisyOracleBackend([make_gold()], [make_parsed()], ratios=ratios, seed=5)
    a1 = _run(b1, _chat_request(window="wa"))
    b_first_order = _run(b1, _chat_request(window="wb"))

    b2 = NoisyOracleBackend([make_gold()], [make_parsed()], ratios=ratios, seed=5)
    b_reversed = _run(b2, _chat_request(window="wb"))
    a_reversed = _run(b2, _chat_request(window="wa"))
    assert a1.content == a_reversed.content
    assert b_first_order.content == b_reversed.content


def test_injections_property_returns_copy():
    backend = NoisyOracleBackend(
        [make_gold()], [make_parsed()], ratios={InjectedErrorType.MISSING_KEY: 1.0}, seed=1
    )
    _run(backend, _chat_request())
    snapshot = backend.injections
    snapshot.clear()
    assert len(backend.injections) == 2


@pytest.mark.parametrize("bad", [-0.1, 1.5])
def test_ratio_out_of_range_rejected(bad):
    with pytest.raises(ValueError):
        NoisyOracleBackend(
            [make_gold()], [make_parsed()], ratios={InjectedErrorType.MISSING_KEY: bad}, seed=1
        )


# ------------------------------------------------------------ FakeBackend
def test_fake_backend_script_and_errors():
    fake = FakeBackend(["one", BackendError("boom")], model="m")
    assert _run(fake, _chat_request()).content == "one"
    with pytest.raises(BackendError):
        _run(fake, _chat_request())
    with pytest.raises(BackendError):
        _run(fake, _chat_request())  # 脚本耗尽
    assert len(fake.requests) == 3
