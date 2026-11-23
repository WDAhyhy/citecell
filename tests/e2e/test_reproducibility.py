"""验收 5：复现。

* 同一配置运行两次：第二次全部请求命中缓存（cache_hits == total_attempts > 0），
  results.json 去掉 run_id 与时间相关字段后完全相同；
* replay_only 运行（只读缓存，未命中即失败）的文档结果与前两次相同。

缓存使用 FileResponseCache（临时目录），后端为带注入的 NoisyOracle，因此第一次运行确实
包含重试与错误路径；第二次与 replay 运行不调用后端，全部由缓存提供。
"""

from __future__ import annotations

import json
from typing import Any

from citecell.backends import CachedBackend, FileResponseCache, NoisyOracleBackend
from citecell.models import InjectedErrorType, RunConfig, RunResult

# 运行时字段：run_id、时间戳、耗时与缓存计数会随运行变化，复现比较时去掉。
VOLATILE_KEYS = {
    "run_id",
    "created_at",
    "finished_at",
    "started_at",
    "wall_time_s",
    "cache_hits",
    "cache_hit",
    "peak_vram_mb",
}

RATIOS = {
    InjectedErrorType.WRONG_BLOCK_ID: 0.2,
    InjectedErrorType.PARAPHRASED_QUOTE: 0.1,
    InjectedErrorType.DISTRACTOR_VALUE: 0.1,
    InjectedErrorType.MISSING_KEY: 0.05,
}


def _strip(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items() if k not in VOLATILE_KEYS}
    if isinstance(obj, list):
        return [_strip(v) for v in obj]
    return obj


def _canonical(obj: Any) -> str:
    return json.dumps(_strip(obj), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def test_second_run_is_fully_cached_and_identical(corpus, keyset, run_pipeline, tmp_path) -> None:  # noqa: ANN001
    cache = FileResponseCache(tmp_path / "cache")
    inner = NoisyOracleBackend(corpus.golds, corpus.parsed, ratios=RATIOS, seed=3)
    config = RunConfig(backend=inner.info())

    first = run_pipeline(CachedBackend(inner, cache), config, corpus.paths, keyset, run_id="repro-1")
    assert first.manifest.usage.total_attempts > 0
    assert len(inner.injections) > 0, "第一次运行应包含注入的错误，否则复现测试不覆盖重试路径"

    # 第二次：同一缓存、同一配置，新的后端实例（若调用了后端则注入列表非空）。
    inner2 = NoisyOracleBackend(corpus.golds, corpus.parsed, ratios=RATIOS, seed=3)
    second = run_pipeline(CachedBackend(inner2, cache), config, corpus.paths, keyset, run_id="repro-2")
    assert inner2.injections == [], "第二次运行不应再调用后端"
    assert second.manifest.usage.cache_hits == second.manifest.usage.total_attempts > 0
    assert _canonical(second.model_dump(mode="json")) == _canonical(first.model_dump(mode="json"))


def test_replay_only_matches_recorded_results(corpus, keyset, run_pipeline, tmp_path) -> None:  # noqa: ANN001
    cache = FileResponseCache(tmp_path / "cache")
    inner = NoisyOracleBackend(corpus.golds, corpus.parsed, ratios=RATIOS, seed=3)
    recorded = run_pipeline(
        CachedBackend(inner, cache), RunConfig(backend=inner.info()), corpus.paths, keyset, run_id="rec"
    )

    replay_config = RunConfig(backend=inner.info(), replay_only=True)
    replay_inner = NoisyOracleBackend(corpus.golds, corpus.parsed, ratios=RATIOS, seed=3)
    replayed = run_pipeline(
        CachedBackend(replay_inner, cache, replay_only=True), replay_config, corpus.paths, keyset, run_id="replay"
    )
    assert replay_inner.injections == [], "replay_only 不应调用后端"
    assert replayed.manifest.usage.cache_hits == replayed.manifest.usage.total_attempts > 0
    rec_docs: RunResult = recorded
    assert _canonical([d.model_dump(mode="json") for d in replayed.documents]) == _canonical(
        [d.model_dump(mode="json") for d in rec_docs.documents]
    )
