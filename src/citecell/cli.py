"""命令行入口（T10）：``citecell extract`` / ``citecell synth`` / ``citecell eval``。

输出只包含计数、状态与输出文件位置，不打印文档正文、引文或提示词。
运行产物写入 ``<out>/<run_id>/``：manifest.json、results.json、results_values.csv、
results_evidence.csv、results.xlsx（以及可选的 results_corrections 表，见 xlsx 的"人工修正"工作表）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from citecell.backends import (
    DEFAULT_CACHE_ROOT,
    PROFILE_NAMES,
    CachedBackend,
    FileResponseCache,
    OpenAICompatibleBackend,
    get_profile,
)
from citecell.canonical import canonical_json
from citecell.export import (
    evidence_table,
    load_corrections,
    values_table,
    write_csv,
    write_results_json,
    write_xlsx,
)
from citecell.extract.pipeline import build_pipeline
from citecell.models import (
    DifficultyConfig,
    DocFormat,
    GenerationConfig,
    KeySet,
    RunConfig,
    RunResult,
    Status,
)

DEFAULT_OUT_ROOT = Path("outputs/runs")
_DOC_SUFFIXES = {".pdf", ".docx", ".xlsx"}
logger = logging.getLogger("citecell.cli")


# ---------------------------------------------------------------- extract
def _cmd_extract(args: argparse.Namespace) -> int:
    keyset = KeySet.model_validate_json(Path(args.keyset).read_text(encoding="utf-8"))
    cache = None if args.no_cache else FileResponseCache(Path(args.cache_dir))
    inner = OpenAICompatibleBackend(
        base_url=args.base_url,
        model=args.model,
        allow_remote_backend=args.allow_remote_backend,
        max_concurrency=args.max_concurrency,
        max_transport_retries=args.max_transport_retries,
    )
    backend = CachedBackend(inner, cache, replay_only=args.replay_only) if cache is not None else inner
    info = backend.info()
    base = get_profile(args.profile, info) if args.profile else RunConfig(backend=info)

    overrides: dict[str, object] = {
        "record_filenames": args.record_filenames,
        "replay_only": args.replay_only,
        "allow_remote_backend": args.allow_remote_backend,
    }
    if args.max_semantic_retries is not None:
        overrides["max_semantic_retries"] = args.max_semantic_retries
    config = base.model_copy(update=overrides)

    run_id = args.run_id or uuid.uuid4().hex[:16]
    pipeline = build_pipeline(backend, config)
    result: RunResult = asyncio.run(
        pipeline.run([Path(p) for p in args.paths], keyset, config, run_id=run_id)
    )

    out_dir = Path(args.out) / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "manifest.json").write_text(canonical_json(result.manifest), encoding="utf-8")
    write_results_json(result, out_dir / "results.json")
    write_csv(values_table(result, keyset), out_dir / "results_values.csv")
    write_csv(evidence_table(result), out_dir / "results_evidence.csv")
    corrections = load_corrections(Path(args.corrections)) if args.corrections else None
    write_xlsx(result, keyset, out_dir / "results.xlsx", corrections=corrections)

    counts = Counter(r.status.value for d in result.documents for r in d.records)
    print(f"run_id: {run_id}")
    print(f"文档: {len(result.documents)}  键: {len(keyset.keys)}  尝试: {result.manifest.usage.total_attempts}")
    print("状态: " + ", ".join(f"{s.value}={counts.get(s.value, 0)}" for s in Status))
    print(f"输出: {out_dir}")
    return 0


# ---------------------------------------------------------------- synth
def _cmd_synth(args: argparse.Namespace) -> int:
    from citecell.synth import generate

    keyset = KeySet.model_validate_json(Path(args.keyset).read_text(encoding="utf-8"))
    difficulty = DifficultyConfig.model_validate_json(Path(args.difficulty).read_text(encoding="utf-8"))
    formats = [DocFormat(f.strip()) for f in args.formats.split(",") if f.strip()]
    config = GenerationConfig(
        seed=args.seed,
        n_docs=args.n_docs,
        formats=formats,
        keyset=keyset,
        difficulty=difficulty,
    )
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    golds = [sample.gold for sample in generate(config, out)]
    gold_path = out / "gold.json"
    gold_path.write_text(canonical_json(golds), encoding="utf-8")
    print(f"生成文档: {len(golds)}  金标准: {gold_path}")
    return 0


# ---------------------------------------------------------------- eval
def _cmd_eval(args: argparse.Namespace) -> int:
    from citecell.eval import CiteCellEvaluator, dataset_sha256, render_json, render_markdown
    from citecell.models import GoldDocument
    from citecell.parsing import build_default_registry

    keyset = KeySet.model_validate_json(Path(args.keyset).read_text(encoding="utf-8"))
    result = RunResult.model_validate_json(Path(args.results).read_text(encoding="utf-8"))
    gold = [GoldDocument.model_validate(g) for g in json.loads(Path(args.gold).read_text(encoding="utf-8"))]

    registry = build_default_registry()
    parsed = {}
    docs_dir = Path(args.docs_dir)
    for path in sorted(p for p in docs_dir.iterdir() if p.suffix.lower() in _DOC_SUFFIXES):
        try:
            doc = registry.for_path(path).parse(path)
        except Exception as exc:  # 解析失败只记录类型，文档视为不可用
            logger.warning("评测跳过无法解析的文档（%s）", type(exc).__name__)
            continue
        parsed[doc.info.doc_id] = doc

    # dataset_id 必须以来源前缀开头（如 synthetic:），前缀取自金标准的 source。
    source = gold[0].source.value if gold else "synthetic"
    evaluator = CiteCellEvaluator(keyset, dataset_id=f"{source}:{dataset_sha256(gold)[:16]}")
    report = evaluator.evaluate(result, gold, parsed=parsed)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "eval.json").write_text(render_json([report]), encoding="utf-8")
    (out / "eval.md").write_text(render_markdown([report]), encoding="utf-8")
    print(f"评测输出: {out}")
    return 0


# ---------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="citecell", description="可溯源的多文档键值抽取（PDF / Word / Excel）")
    sub = parser.add_subparsers(dest="command", metavar="命令")

    ex = sub.add_parser("extract", help="对文档运行抽取流水线（需要 OpenAI 兼容服务地址）")
    ex.add_argument("paths", nargs="+", help="输入文件（.pdf/.docx/.xlsx）")
    ex.add_argument("--keyset", required=True, help="KeySet JSON 文件")
    ex.add_argument("--base-url", required=True, help="OpenAI 兼容服务地址，如 http://localhost:8080/v1")
    ex.add_argument("--model", required=True, help="模型名")
    ex.add_argument("--profile", choices=PROFILE_NAMES, help="档位预设（cpu-lite/cpu-std/gpu-std/gpu-ref）")
    ex.add_argument("--allow-remote-backend", action="store_true", help="允许非 localhost 后端（数据出境，默认禁止）")
    ex.add_argument("--max-concurrency", type=int, default=2)
    ex.add_argument("--max-transport-retries", type=int, default=3)
    ex.add_argument("--max-semantic-retries", type=int, default=None)
    ex.add_argument("--cache-dir", default=str(DEFAULT_CACHE_ROOT), help="请求缓存目录（应被 git 忽略）")
    ex.add_argument("--no-cache", action="store_true")
    ex.add_argument("--replay-only", action="store_true", help="只用缓存，未命中即失败")
    ex.add_argument("--record-filenames", action="store_true", help="在清单中记录文件名（默认不记录）")
    ex.add_argument("--corrections", help="人工修正 JSONL（只读，用于导出的「人工修正」工作表）")
    ex.add_argument("--out", default=str(DEFAULT_OUT_ROOT), help="输出根目录")
    ex.add_argument("--run-id", default=None)
    ex.set_defaults(func=_cmd_extract)

    sy = sub.add_parser("synth", help="生成合成文档与金标准")
    sy.add_argument("--keyset", required=True)
    sy.add_argument("--difficulty", required=True, help="DifficultyConfig JSON（如 configs/synth/medium.json）")
    sy.add_argument("--seed", type=int, default=0)
    sy.add_argument("--n-docs", type=int, required=True)
    sy.add_argument("--formats", default="docx,xlsx", help="逗号分隔：docx,xlsx,pdf")
    sy.add_argument("--out", required=True)
    sy.set_defaults(func=_cmd_synth)

    ev = sub.add_parser("eval", help="对 results.json 按金标准计算评测指标")
    ev.add_argument("--results", required=True)
    ev.add_argument("--gold", required=True, help="金标准 JSON（synth 输出的 gold.json）")
    ev.add_argument("--keyset", required=True)
    ev.add_argument("--docs-dir", required=True, help="与金标准对应的文档目录（用于换算原生位置）")
    ev.add_argument("--out", required=True)
    ev.set_defaults(func=_cmd_eval)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    try:
        return int(args.func(args))
    except (OSError, ValueError) as exc:
        print(f"错误（{type(exc).__name__}）：{str(exc)[:300]}", file=sys.stderr)
        return 2


__all__ = ["build_parser", "main"]
