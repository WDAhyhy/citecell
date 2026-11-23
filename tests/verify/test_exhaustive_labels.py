"""穷举测试：BOOLEAN / ENUM 金标准提及在“疑问标签”中的同形出现不得被判 grounded。

对合成文档（citecell.synth，随机源 seed 固定）中每个 BOOLEAN / ENUM 型的 present 金标准提及文本 s，
构造块 T = "是否" + s + "：" + s。引文取 s 在 T 中第一次出现处（即标签中的 s），值为 s。
该处位于疑问标签上下文，因此必须判 needs_review（不 grounded）。
"""

from __future__ import annotations

from pathlib import Path

from citecell.models import (
    Block,
    BlockKind,
    Candidate,
    DifficultyConfig,
    DocFormat,
    DocumentInfo,
    DocxLocator,
    EvidenceClaim,
    ExtractionMethod,
    GenerationConfig,
    ParsedDocument,
    Status,
    ValueType,
)
from citecell.synth import generate
from citecell.synth.keysets import load_keyset
from citecell.verify import Verifier

DOC_ID = "0123456789abcdef"


def _surfaces(tmp_path: Path) -> list[tuple[str, str]]:
    keyset = load_keyset()
    specs = {k.key: k for k in keyset.keys}
    cfg = GenerationConfig(
        seed=5,
        n_docs=8,
        formats=[DocFormat.DOCX],
        keyset=keyset,
        difficulty=DifficultyConfig(surface_variety=0.5),
    )
    out: list[tuple[str, str]] = []
    for sample in generate(cfg, tmp_path / "synth"):
        for rec in sample.gold.records:
            spec = specs.get(rec.key)
            if spec is None or rec.expected != "present":
                continue
            if spec.value_type not in (ValueType.BOOLEAN, ValueType.ENUM):
                continue
            for m in rec.mentions:
                out.append((rec.key, m.surface_text))
    return sorted(set(out))


def test_question_label_copy_is_never_grounded(tmp_path):
    cases = _surfaces(tmp_path)
    assert len(cases) >= 5, "合成样本中 BOOLEAN/ENUM 提及太少"
    keyset = load_keyset()
    specs = {k.key: k for k in keyset.keys}
    info = DocumentInfo(
        doc_id=DOC_ID,
        sha256=DOC_ID + "0" * 48,
        format=DocFormat.DOCX,
        size_bytes=1,
        parser_name="test",
        parser_version="0",
    )
    verifier = Verifier()
    leaked: list[tuple[str, str]] = []
    for key, s in cases:
        text = f"是否{s}：{s}"
        assert text.find(s) == 2  # 第一次出现处即标签中的文字
        block = Block(
            doc_id=DOC_ID,
            block_id="para1",
            kind=BlockKind.PARAGRAPH,
            text=text,
            order=0,
            locator=DocxLocator(body_index=0, paragraph_number=1),
        )
        doc = ParsedDocument(info=info, blocks=[block])
        cand = Candidate(
            doc_id=DOC_ID,
            key=key,
            value_text=s,
            evidence=[EvidenceClaim(block_id="para1", quote=s)],
            method=ExtractionMethod.LLM,
            producer_id="llm:exhaustive",
        )
        rec = verifier.verify(cand, doc, specs[key])
        if rec.status == Status.GROUNDED:
            leaked.append((key, s))
    assert leaked == [], f"疑问标签中的同形文字被判 grounded：{leaked[:5]}"
