"""M1 评测运行脚本（T11）：生成合成语料 → 仿真后端抽取 → 校验 → 评测 → Markdown 报告。

用法（仓库根目录）::

    .venv/bin/python scripts/run_m1_eval.py --config configs/runs/m1_synth_medium.json

输出（均在 ``outputs/`` 下，已被 .gitignore 忽略）::

    outputs/runs/<run_id>/manifest.json   清单（RunManifest 规范 JSON）
    outputs/runs/<run_id>/results.json    结果（RunResult 规范 JSON）
    outputs/runs/<run_id>/eval.json       评测报告（EvalReport 列表）
    outputs/runs/<run_id>/eval.md         Markdown 报告（由 eval.report 渲染）
    outputs/synthetic/<run_id>/           合成文档与金标准（可由 seed 重建，不入库）

重要：后端为 Oracle / NoisyOracle **仿真后端**。结果只说明流水线与校验器在注入错误下的行为，
**不代表任何真实模型的准确率**。报告开头会写明这一点。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from citecell.backends import NoisyOracleBackend, OracleBackend
from citecell.canonical import canonical_json
from citecell.eval import CiteCellEvaluator, dataset_sha256, render_json, render_markdown
from citecell.export import write_results_json
from citecell.extract.pipeline import build_pipeline
from citecell.models import (
    DifficultyConfig,
    DocFormat,
    GenerationConfig,
    GoldDocument,
    InjectedErrorType,
    KeySet,
    RunConfig,
)
from citecell.parsing import build_default_registry
from citecell.synth import generate

ROOT = Path(__file__).resolve().parents[1]
log = logging.getLogger("citecell.scripts.run_m1_eval")


class RunSpec(BaseModel):
    """运行配置（configs/runs/*.json）。路径相对于仓库根目录。"""

    model_config = ConfigDict(extra="forbid")

    run_name: str = Field(pattern=r"^[a-z0-9_\-]+$")
    description: str = ""
    seed: int
    n_docs: int = Field(ge=1)
    formats: list[DocFormat] = Field(min_length=1)
    keyset: str
    difficulty: str
    backend: Literal["oracle", "noisy_oracle"]
    noisy_ratios: dict[InjectedErrorType, float] = Field(default_factory=dict)
    noisy_seed: int = 7
    max_semantic_retries: int = Field(default=2, ge=0, le=5)
    iou_threshold: float = Field(default=0.5, gt=0, le=1)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M1 仿真后端评测（不代表真实模型准确率）")
    parser.add_argument("--config", required=True, help="运行配置 JSON，如 configs/runs/m1_synth_medium.json")
    parser.add_argument("--run-id", default=None, help="默认取配置中的 run_name")
    parser.add_argument("--out-root", default=str(ROOT / "outputs"), help="输出根目录")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)

    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        cfg_path = ROOT / cfg_path
    spec = RunSpec.model_validate_json(cfg_path.read_text(encoding="utf-8"))
    run_id = args.run_id or spec.run_name
    out_root = Path(args.out_root)

    keyset = KeySet.model_validate_json((ROOT / spec.keyset).read_text(encoding="utf-8"))
    difficulty = DifficultyConfig.model_validate_json((ROOT / spec.difficulty).read_text(encoding="utf-8"))

    # 1) 合成语料与金标准
    synth_dir = out_root / "synthetic" / run_id
    gen_cfg = GenerationConfig(
        seed=spec.seed,
        n_docs=spec.n_docs,
        formats=list(spec.formats),
        keyset=keyset,
        difficulty=difficulty,
    )
    golds: list[GoldDocument] = [s.gold for s in generate(gen_cfg, synth_dir)]
    paths = sorted(synth_dir.glob("doc_*"))
    registry = build_default_registry()
    parsed = [registry.for_path(p).parse(p) for p in paths]

    # 2) 仿真后端
    if spec.backend == "oracle":
        backend: OracleBackend | NoisyOracleBackend = OracleBackend(golds, parsed)
    else:
        backend = NoisyOracleBackend(golds, parsed, ratios=spec.noisy_ratios, seed=spec.noisy_seed)

    # 3) 流水线
    config = RunConfig(backend=backend.info(), max_semantic_retries=spec.max_semantic_retries)
    result = asyncio.run(build_pipeline(backend, config).run(paths, keyset, config, run_id=run_id))

    # 4) 输出
    run_dir = out_root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "manifest.json").write_text(canonical_json(result.manifest), encoding="utf-8")
    write_results_json(result, run_dir / "results.json")

    injections = backend.injections if isinstance(backend, NoisyOracleBackend) else []
    evaluator = CiteCellEvaluator(
        keyset,
        dataset_id=f"synthetic:{dataset_sha256(golds)[:16]}",
        iou_threshold=spec.iou_threshold,
    )
    parsed_by_id = {p.info.doc_id: p for p in parsed}
    report = evaluator.evaluate(result, golds, parsed=parsed_by_id, injections=injections)
    (run_dir / "eval.json").write_text(render_json([report]), encoding="utf-8")
    (run_dir / "eval.md").write_text(render_markdown([report]), encoding="utf-8")

    # 5) 打印复现所需的元数据（不打印正文）
    print(f"run_id: {run_id}")
    print(f"config: {cfg_path.relative_to(ROOT) if cfg_path.is_relative_to(ROOT) else cfg_path.name}")
    print(f"config_file_sha256: {_sha256_file(cfg_path)}")
    print(f"manifest_sha256: {_sha256_file(run_dir / 'manifest.json')}")
    print(f"config_sha256 (RunConfig, manifest): {result.manifest.config_sha256}")
    print(f"keyset_sha256: {result.manifest.keyset_sha256}")
    print(f"dataset_sha256: {report.dataset_sha256}")
    print(f"documents: {report.n_documents}  injections: {len(injections)}")
    print(f"output: {run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
