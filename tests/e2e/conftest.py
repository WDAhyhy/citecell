"""M1 端到端测试的共享夹具（T11）。

约定：
* 所有文档都在测试运行时由合成器生成到 pytest 临时目录，不提交任何二进制样例（TASKS §0.4）。
* 只通过公共接口（citecell.synth / citecell.parsing / citecell.backends / citecell.extract.pipeline /
  citecell.eval / citecell.models）断言行为，不依赖各模块内部实现细节。
* 金标准（GoldDocument）由合成器的写出记账产生，与被测解析器独立（ADR-013）。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from citecell.backends import NoisyOracleBackend
from citecell.extract.pipeline import build_pipeline
from citecell.models import (
    DifficultyConfig,
    DocFormat,
    GenerationConfig,
    GoldDocument,
    InjectedErrorType,
    KeySet,
    ParsedDocument,
    RunConfig,
    RunResult,
)
from citecell.parsing import build_default_registry
from citecell.synth import generate

ROOT = Path(__file__).resolve().parents[2]
KEYSET_PATH = ROOT / "configs" / "keysets" / "procurement_v1.json"
MEDIUM_PATH = ROOT / "configs" / "synth" / "medium.json"

# 固定的语料种子：同一种子 + 同一配置 ⇒ 同一批文件与金标准（TASKS T11 验收 1）。
CORPUS_SEED = 1
CORPUS_FORMATS = (DocFormat.DOCX, DocFormat.XLSX, DocFormat.PDF)
CORPUS_DOCS_PER_FORMAT = 5  # 验收 1：docx/xlsx/pdf 中等难度各 5 份
NOISY_SEED = 7


@pytest.fixture(scope="session")
def keyset() -> KeySet:
    return KeySet.model_validate_json(KEYSET_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def medium_difficulty() -> DifficultyConfig:
    return DifficultyConfig.model_validate_json(MEDIUM_PATH.read_text(encoding="utf-8"))


@dataclass
class Corpus:
    """一批合成文档：磁盘路径、金标准（与 paths 同序）与解析结果（与 paths 同序）。"""

    out_dir: Path
    paths: list[Path]
    golds: list[GoldDocument]
    parsed: list[ParsedDocument]
    gold_by_sha: dict[str, GoldDocument] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.gold_by_sha = {g.doc_sha256: g for g in self.golds}


def generate_corpus(
    out_dir: Path,
    keyset: KeySet,
    difficulty: DifficultyConfig,
    *,
    seed: int = CORPUS_SEED,
    n_docs: int = CORPUS_DOCS_PER_FORMAT * len(CORPUS_FORMATS),
    formats: Sequence[DocFormat] = CORPUS_FORMATS,
) -> Corpus:
    """生成合成语料并解析（解析器与流水线使用同一个默认注册表的行为）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    config = GenerationConfig(
        seed=seed,
        n_docs=n_docs,
        formats=list(formats),
        keyset=keyset,
        difficulty=difficulty,
    )
    golds = [sample.gold for sample in generate(config, out_dir)]
    paths = sorted(out_dir.glob("doc_*"))
    registry = build_default_registry()
    parsed = [registry.for_path(p).parse(p) for p in paths]
    return Corpus(out_dir=out_dir, paths=paths, golds=golds, parsed=parsed)


@pytest.fixture(scope="session")
def corpus(tmp_path_factory: pytest.TempPathFactory, keyset: KeySet, medium_difficulty: DifficultyConfig) -> Corpus:
    """中等难度、docx/xlsx/pdf 各 5 份的共享语料（session 级，只生成一次）。"""
    return generate_corpus(tmp_path_factory.mktemp("corpus_medium"), keyset, medium_difficulty)


@pytest.fixture(scope="session")
def run_pipeline() -> Callable[..., RunResult]:
    """同步运行 Pipeline 的辅助函数：run_pipeline(backend, config, paths, keyset, run_id=...)。"""

    def _run(backend, config: RunConfig, paths: Sequence[Path], keyset: KeySet, *, run_id: str = "e2e") -> RunResult:  # noqa: ANN001
        pipeline = build_pipeline(backend, config)
        return asyncio.run(pipeline.run(list(paths), keyset, config, run_id=run_id))

    return _run


@pytest.fixture(scope="session")
def noisy_runs(corpus: Corpus, keyset: KeySet) -> Callable[[InjectedErrorType], tuple[NoisyOracleBackend, RunResult]]:
    """每种注入类型各跑一次（该类型比例 1、max_semantic_retries=0，只看首轮行为），会话内缓存。"""
    cache: dict[InjectedErrorType, tuple[NoisyOracleBackend, RunResult]] = {}

    def _get(etype: InjectedErrorType) -> tuple[NoisyOracleBackend, RunResult]:
        if etype not in cache:
            backend = NoisyOracleBackend(corpus.golds, corpus.parsed, ratios={etype: 1.0}, seed=NOISY_SEED)
            config = RunConfig(backend=backend.info(), max_semantic_retries=0)
            result = asyncio.run(build_pipeline(backend, config).run(corpus.paths, keyset, config, run_id=f"noisy-{etype.value}"))
            cache[etype] = (backend, result)
        return cache[etype]

    return _get


def load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))
