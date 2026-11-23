"""验收 6：隐私。

* 清单（RunManifest）与结果（RunResult）的规范 JSON 中不出现临时目录的绝对路径；
* record_filenames=False（默认）时，清单与结果中不出现输入文件名；
* 捕获 INFO 及以上级别的 citecell 日志，其中不含任何金标准 surface_text。
"""

from __future__ import annotations

import logging
import re

from citecell.backends import NoisyOracleBackend
from citecell.canonical import canonical_json
from citecell.models import InjectedErrorType, RunConfig

RATIOS = {
    InjectedErrorType.WRONG_BLOCK_ID: 0.2,
    InjectedErrorType.PARAPHRASED_QUOTE: 0.1,
    InjectedErrorType.DISTRACTOR_VALUE: 0.1,
}


def test_manifest_and_results_have_no_absolute_paths_or_filenames(corpus, keyset, run_pipeline, tmp_path) -> None:  # noqa: ANN001
    backend = NoisyOracleBackend(corpus.golds, corpus.parsed, ratios=RATIOS, seed=5)
    config = RunConfig(backend=backend.info())  # record_filenames 默认 False
    assert config.record_filenames is False
    result = run_pipeline(backend, config, corpus.paths, keyset, run_id="privacy")

    blob = canonical_json(result.manifest) + canonical_json(result)
    forbidden = [str(corpus.out_dir), str(tmp_path), str(corpus.out_dir.parent)]
    for needle in forbidden:
        assert needle not in blob, "清单或结果中出现了临时目录的绝对路径"
    for path in corpus.paths:
        assert path.name not in blob, f"清单或结果中出现了文件名 {path.name}"


def test_info_logs_contain_no_gold_surface_text(corpus, keyset, run_pipeline, caplog) -> None:  # noqa: ANN001
    caplog.set_level(logging.INFO, logger="citecell")
    backend = NoisyOracleBackend(corpus.golds, corpus.parsed, ratios=RATIOS, seed=5)
    run_pipeline(backend, RunConfig(backend=backend.info()), corpus.paths, keyset, run_id="logs")

    info_records = [r for r in caplog.records if r.name.startswith("citecell") and r.levelno >= logging.INFO]
    assert info_records, "应至少捕获到一条 INFO 日志，否则测试为空"

    surfaces = {m.surface_text for g in corpus.golds for r in g.records for m in r.mentions}
    surfaces |= {m.surface_text for g in corpus.golds for r in g.records for m in r.distractors}
    leaked: list[str] = []
    for rec in info_records:
        # 日志中的 doc_id 是 16 位十六进制哈希（非正文），先去掉，避免它偶然包含纯数字的 surface 而误报。
        message = re.sub(r"[0-9a-f]{16}", "", rec.getMessage())
        for s in surfaces:
            if s and s in message:
                leaked.append(f"{rec.name}: {message[:120]!r} 含金标准文本 {s!r}")
    assert not leaked, "INFO 及以上日志泄露了金标准文本：\n" + "\n".join(leaked[:20])
