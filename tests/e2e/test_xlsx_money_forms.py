"""XLSX 金额的单位线索形式（hard 档、足够多的 xlsx 文档，Oracle 完美输出）。

合成器对 XLSX 金额的单元格形式（synth/realize.py _xlsx_money）：
* text_money_yuan          字符串「1345200元」——带元文本；
* bare_money_header_yuan   纯数字，表头为「合同金额（元）」；
* bare_money_header_wanyuan 纯数字，表头为「合同金额（万元）」，单元格为万元数；
* bare_money_currency_format 纯数字，单元格数字格式 ¥#,##0.00，表头不带单位；
* bare_money_plain         纯数字，单元格和表头都没有任何单位线索。

断言：前四种必须 grounded 且金额等于金标准；bare_money_plain 设计上判为 needs_review + value_unparseable
（不猜单位，不是缺陷）。四种带线索形式与 plain 形式都必须在语料中出现，否则测试为空。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from citecell.backends import OracleBackend
from citecell.extract.pipeline import build_pipeline
from citecell.models import (
    DifficultyConfig,
    DocFormat,
    GenerationConfig,
    IssueCode,
    KeySet,
    RunConfig,
    Status,
)
from citecell.synth import generate
from citecell.parsing import build_default_registry
from citecell.values import ValueNormalizerV1

HARD_PATH = Path(__file__).resolve().parents[2] / "configs" / "synth" / "hard.json"
SEED = 11
N_DOCS = 120
CLUE_TAGS = (
    "surface=text_money_yuan",
    "surface=bare_money_header_yuan",
    "surface=bare_money_header_wanyuan",
    "surface=bare_money_currency_format",
)
PLAIN_TAG = "surface=bare_money_plain"


@pytest.fixture(scope="module")
def xlsx_money_run(tmp_path_factory, keyset: KeySet):  # noqa: ANN001
    out = tmp_path_factory.mktemp("xlsx_money_hard")
    difficulty = DifficultyConfig.model_validate_json(HARD_PATH.read_text(encoding="utf-8"))
    config = GenerationConfig(seed=SEED, n_docs=N_DOCS, formats=[DocFormat.XLSX], keyset=keyset, difficulty=difficulty)
    golds = [s.gold for s in generate(config, out)]
    paths = sorted(out.glob("doc_*"))
    parsed = [build_default_registry().for_path(p).parse(p) for p in paths]
    backend = OracleBackend(golds, parsed)
    run_config = RunConfig(backend=backend.info())
    result = asyncio.run(build_pipeline(backend, run_config).run(paths, keyset, run_config, run_id="xlsx-money"))
    return golds, result


def test_money_by_unit_clue_form(xlsx_money_run, keyset: KeySet) -> None:  # noqa: ANN001
    golds, result = xlsx_money_run
    gold_by_sha = {g.doc_sha256: g for g in golds}
    specs = {k.key: k for k in keyset.keys}
    vn = ValueNormalizerV1()
    seen: dict[str, int] = {t: 0 for t in CLUE_TAGS + (PLAIN_TAG,)}
    problems: list[str] = []
    for doc in result.documents:
        gold = gold_by_sha[doc.info.sha256]
        gold_rec = {r.key: r for r in gold.records}
        for rec in doc.records:
            g = gold_rec[rec.key]
            if g.expected != "present" or specs[rec.key].value_type.value != "money":
                continue
            forms = [t for t in g.tags if t in seen]
            assert len(forms) == 1, f"金额键缺少单一形式标签: {g.tags}"
            form = forms[0]
            seen[form] += 1
            if form == PLAIN_TAG:
                codes = {i.code for i in rec.issues}
                if rec.status != Status.NEEDS_REVIEW or IssueCode.VALUE_UNPARSEABLE not in codes:
                    problems.append(f"plain 应为 needs_review+VALUE_UNPARSEABLE，实际 {rec.status.value}")
                continue
            if rec.status != Status.GROUNDED or rec.value is None or g.value is None or not vn.equivalent(rec.value, g.value, specs[rec.key]):
                problems.append(f"{form}: 实际 {rec.status.value} {sorted(i.code.value for i in rec.issues)}")
    missing = [t for t, n in seen.items() if n == 0]
    assert not missing, f"语料中缺少形式（测试为空）: {missing}"
    assert not problems, f"{len(problems)} 个带单位线索金额未满足断言：\n" + "\n".join(problems[:20])
