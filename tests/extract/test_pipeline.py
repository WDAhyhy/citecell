"""Pipeline：并发下结果确定性、无法解析的文档、重复文件去重、文件名默认不记录。"""

from __future__ import annotations

import asyncio
import json
import random

from citecell.backends import FakeBackend
from citecell.canonical import canonical_json
from citecell.extract import build_pipeline
from citecell.models import ChatResponse, IssueCode, RunConfig, Status


class KeyedBackend:
    """按请求中的键返回预设答案，并加入随机延迟。用于验证并发下输出与完成顺序无关。"""

    backend_id = "keyed"

    def __init__(self, answers: dict[str, str], delay_seed: int) -> None:
        self._answers = answers
        self._rng = random.Random(delay_seed)
        self.model = "keyed-model"

    def info(self):
        from citecell.models import BackendInfo

        return BackendInfo(backend_id=self.backend_id, provider="fake", model=self.model, max_concurrency=4)

    async def chat(self, request):
        await asyncio.sleep(self._rng.uniform(0.0, 0.02))
        key = request.context.keys[0]
        return ChatResponse(content=self._answers[key])


def _answer(key: str, block_id: str, quote: str, value: str) -> str:
    return json.dumps(
        {"fields": [{"key": key, "evidence": [{"block_id": block_id, "quote": quote}], "value": value}]},
        ensure_ascii=False,
    )


def _stable_view(result) -> str:
    """去掉 run_id、时间戳与延迟后的规范 JSON。"""
    docs = json.loads(canonical_json(result.documents))
    for d in docs:
        for a in d["attempts"]:
            for k in ("started_at", "latency_ms"):
                a.pop(k, None)
    return canonical_json(docs)


def test_concurrent_runs_are_deterministic(make_docx, parse, block_id_of, keyset_three):
    path = make_docx()
    doc = parse(path)
    answers = {
        "合同编号": _answer("合同编号", block_id_of(doc, "HT-2024-001"), "HT-2024-001", "HT-2024-001"),
        "甲方": _answer("甲方", block_id_of(doc, "示例科技"), "示例科技有限公司", "示例科技有限公司"),
        "合同金额": _answer("合同金额", block_id_of(doc, "120万元"), "120万元", "120万元"),
    }
    views = []
    for seed in (1, 2):
        backend = KeyedBackend(answers, delay_seed=seed)
        # 每个键单独一个窗口，窗口数 = 3，并发上限 4，窗口完成顺序随随机延迟变化。
        config = RunConfig(backend=backend.info(), prefilter="always", max_keys_per_window=1)
        pipeline = build_pipeline(backend, config)
        result = asyncio.run(pipeline.run([path], keyset_three, config, run_id=f"run-{seed}"))
        views.append(_stable_view(result))
        assert len(result.documents[0].windows) == 3
        assert all(r.status == Status.GROUNDED for r in result.documents[0].records)
        assert [r.key for r in result.documents[0].records] == ["合同编号", "甲方", "合同金额"]
    assert views[0] == views[1]


def test_unsupported_document_becomes_failed_records(tmp_path, keyset_one):
    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"this is not a zip container")
    backend = FakeBackend(["unused"], repeat_last=True)
    config = RunConfig(backend=backend.info())
    result = asyncio.run(build_pipeline(backend, config).run([bad], keyset_one, config, run_id="u"))

    doc = result.documents[0]
    assert doc.info.parser_name == "none"
    assert doc.records[0].status == Status.FAILED
    assert IssueCode.UNSUPPORTED_DOCUMENT in {i.code for i in doc.records[0].issues}
    assert backend.requests == []  # 不应调用模型


def test_duplicate_content_is_deduplicated(make_docx, keyset_one):
    a = make_docx("a.docx")
    b = make_docx("b.docx")  # 同内容 → 同 doc_id
    backend = FakeBackend(["unused"], repeat_last=True)
    config = RunConfig(backend=backend.info())
    result = asyncio.run(build_pipeline(backend, config).run([a, b], keyset_one, config, run_id="d"))
    assert len(result.documents) == 1
    assert len(result.manifest.documents) == 1


def test_filenames_not_recorded_by_default(make_docx, keyset_one):
    path = make_docx("secret-client-name.docx")
    backend = FakeBackend(["unused"], repeat_last=True)
    config = RunConfig(backend=backend.info())
    result = asyncio.run(build_pipeline(backend, config).run([path], keyset_one, config, run_id="p"))
    dumped = json.dumps(result.model_dump(mode="json"), ensure_ascii=False)
    assert "secret-client-name" not in dumped
    assert str(path.parent) not in dumped
    assert result.manifest.documents[0].display_name is None


def test_filenames_recorded_when_enabled(make_docx, keyset_one):
    path = make_docx("visible.docx")
    backend = FakeBackend(["unused"], repeat_last=True)
    config = RunConfig(backend=backend.info(), record_filenames=True)
    result = asyncio.run(build_pipeline(backend, config).run([path], keyset_one, config, run_id="p2"))
    assert result.manifest.documents[0].display_name == "visible.docx"
    assert str(path.parent) not in json.dumps(result.model_dump(mode="json"), ensure_ascii=False)
