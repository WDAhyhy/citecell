"""Oracle 经完整流水线与校验器（hard 档合成文档）的 grounded 统计。

断言：所有 present 键都应 grounded，除已知局限「XLSX 纯数字金额单元格（无『元』）」外。
打印 grounded 计数（用 pytest -s 查看）。
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from pathlib import Path

from citecell.backends import OracleBackend
from citecell.extract.pipeline import build_pipeline
from citecell.models import (
    DifficultyConfig,
    DocFormat,
    GenerationConfig,
    KeySet,
    RunConfig,
    Status,
)
from citecell.parsing import build_default_registry
from citecell.synth import generate

ROOT = Path(__file__).resolve().parents[2]
KEYSET = KeySet.model_validate_json((ROOT / "configs/keysets/procurement_v1.json").read_text(encoding="utf-8"))
HARD = DifficultyConfig.model_validate_json((ROOT / "configs/synth/hard.json").read_text(encoding="utf-8"))
BARE = re.compile(r"^\d+(\.\d+)?$")


def test_oracle_hard_documents_grounded(tmp_path_factory):
    out = tmp_path_factory.mktemp("hard_e2e")
    cfg = GenerationConfig(
        seed=21,
        n_docs=9,
        formats=[DocFormat.DOCX, DocFormat.XLSX, DocFormat.PDF],
        keyset=KEYSET,
        difficulty=HARD,
    )
    golds = [s.gold for s in generate(cfg, out)]
    paths = sorted(out.glob("doc_*"))
    registry = build_default_registry()
    parsed = [registry.for_path(p).parse(p) for p in paths]
    oracle = OracleBackend(golds, parsed)
    config = RunConfig(backend=oracle.info())
    result = asyncio.run(build_pipeline(oracle, config).run(paths, KEYSET, config, run_id="hard-oracle"))

    gold_by_sha = {g.doc_sha256: g for g in golds}
    counts: Counter = Counter()
    problems: list[str] = []
    for doc in result.documents:
        gold = gold_by_sha[doc.info.sha256]
        rec_by_key = {r.key: r for r in gold.records}
        for rec in doc.records:
            grec = rec_by_key[rec.key]
            if grec.expected != "present":
                continue
            fmt = gold.format.value
            bare = (
                fmt == "xlsx"
                and grec.value is not None
                and grec.value.type == "money"
                and all(BARE.match(m.surface_text) for m in grec.mentions)
            )
            if bare:
                counts[(fmt, "bare_amount_known_limitation")] += 1
                continue
            counts[(fmt, rec.status.value)] += 1
            if rec.status != Status.GROUNDED:
                problems.append(f"{fmt} {rec.key}: {rec.status.value} {[i.code.value for i in rec.issues]}")

    total = sum(counts.values())
    grounded = sum(v for (f, s), v in counts.items() if s == Status.GROUNDED.value)
    print(f"\nHARD_ORACLE present_keys={total} grounded={grounded} breakdown={dict(counts)}")
    assert total > 0
    assert not problems, "非 grounded 的 present 键：\n" + "\n".join(problems[:20])
