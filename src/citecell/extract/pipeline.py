"""端到端编排（ARCH §2）：解析 → 生产者链 → 统一校验 → Resolver → DocumentResult / RunResult。

* 生产者链：前一层未 grounded 的键交给下一层。M1 只有一个 LLM 生产者，但结构已支持多层。
* 统一校验：所有层的候选都由同一个 Verifier 再校验一次（纯函数，结果与生产者内部一致）。
* 文档按输入顺序处理（同一内容的重复文件按 doc_id 去重）；记录按 KeySet 顺序排列。
* 无法解析的文档不中断运行：其全部键记为 failed（UNSUPPORTED_DOCUMENT）。
* 日志只记录 doc_id 与计数，不记录正文、引文、提示词或文件名。
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from citecell.extract.producer import LLMCandidateProducer
from citecell.interfaces import (
    CandidateProducer,
    ContextPlanner,
    ExtractionBackend,
    ParserRegistry,
    PromptRenderer,
    Resolver,
    UnsupportedDocumentError,
    Verifier,
)
from citecell.manifest import build_manifest, collect_dependencies, static_components
from citecell.models import (
    DocFormat,
    DocumentInfo,
    DocumentResult,
    ExtractionRecord,
    Issue,
    IssueCode,
    KeySet,
    ManifestDocument,
    ParsedDocument,
    RunConfig,
    RunResult,
    Severity,
    Status,
    UsageStats,
)
from citecell.parsing import build_default_registry, fingerprint
from citecell.prompting import DefaultContextPlanner, PromptRendererV1
from citecell.verify import SimpleMerger, Verifier as CoreVerifier

logger = logging.getLogger("citecell.extract")

_SUFFIX_FORMAT = {".pdf": DocFormat.PDF, ".docx": DocFormat.DOCX, ".xlsx": DocFormat.XLSX}


def _format_of(path: Path) -> DocFormat:
    fmt = _SUFFIX_FORMAT.get(path.suffix.lower())
    if fmt is None:
        raise UnsupportedDocumentError(f"不支持的文件类型: {path.suffix!r}")
    return fmt


class CiteCellPipeline:
    """满足 ``citecell.interfaces.Pipeline`` 协议的编排器。"""

    def __init__(
        self,
        *,
        registry: ParserRegistry,
        producers: Sequence[CandidateProducer],
        verifier: Verifier,
        resolver: Resolver,
        renderer: PromptRenderer,
        planner: ContextPlanner,
    ) -> None:
        self._registry = registry
        self._producers = list(producers)
        self._verifier = verifier
        self._resolver = resolver
        self._renderer = renderer
        self._planner = planner

    async def run(
        self,
        paths: Sequence[Path],
        keyset: KeySet,
        config: RunConfig,
        *,
        run_id: str | None = None,
    ) -> RunResult:
        run_id = run_id or uuid.uuid4().hex[:16]
        created = datetime.now(timezone.utc)
        t0 = time.perf_counter()

        # 先检查全部文件类型，避免跑到一半才发现不支持的输入。
        formats = [_format_of(Path(p)) for p in paths]

        doc_results: list[DocumentResult] = []
        manifest_docs: list[ManifestDocument] = []
        seen: set[str] = set()
        for path, fmt in zip(paths, formats, strict=True):
            path = Path(path)
            fp = fingerprint(path)
            if fp.doc_id in seen:
                logger.info("重复文件已跳过（doc_id=%s）", fp.doc_id)
                continue
            seen.add(fp.doc_id)
            display = path.name if config.record_filenames else None
            manifest_docs.append(
                ManifestDocument(
                    doc_id=fp.doc_id,
                    sha256=fp.sha256,
                    format=fmt,
                    size_bytes=fp.size_bytes,
                    display_name=display,
                )
            )
            try:
                parsed = self._registry.for_path(path).parse(path, display_name=display)
            except UnsupportedDocumentError as exc:
                # 只记录异常类型：异常消息可能含路径或正文片段。
                logger.warning("文档无法解析（doc_id=%s，%s）", fp.doc_id, type(exc).__name__)
                doc_results.append(self._unsupported(fp, fmt, display, keyset))
                continue
            if parsed.info.doc_id != fp.doc_id:
                raise RuntimeError("解析器返回的 doc_id 与文件哈希不一致")
            doc_results.append(await self._process(parsed, keyset, config))
            logger.info("文档完成（doc_id=%s，键=%d）", fp.doc_id, len(keyset.keys))

        wall = time.perf_counter() - t0
        finished = datetime.now(timezone.utc)
        usage = _usage(doc_results, wall)
        parser_infos = [d.info for d in doc_results]
        manifest = build_manifest(
            run_id=run_id,
            created_at=created,
            finished_at=finished,
            config=config,
            keyset=keyset,
            documents=manifest_docs,
            components=static_components(
                planner_version=getattr(self._planner, "version", "unknown"),
                parser_infos=parser_infos,
            ),
            template_id=self._renderer.template_id,
            template_sha256=self._renderer.template_sha256,
            output_schema=self._renderer.output_schema(keyset.keys, None),
            dependencies=collect_dependencies(config.pdf_backend),
            usage=usage,
        )
        return RunResult(manifest=manifest, documents=doc_results)

    # ------------------------------------------------------------------ 单文档
    async def _process(self, parsed: ParsedDocument, keyset: KeySet, config: RunConfig) -> DocumentResult:
        doc_id = parsed.info.doc_id
        spec_by = {k.key: k for k in keyset.keys}
        accumulated: dict[str, list[ExtractionRecord]] = {k.key: [] for k in keyset.keys}
        resolved: dict[str, ExtractionRecord] = {}
        remaining = list(keyset.keys)
        windows = []
        attempts = []

        for producer in self._producers:
            if not remaining:
                break
            out = await producer.produce(parsed, remaining, config)
            windows.extend(out.windows)
            attempts.extend(out.attempts)
            remaining_keys = {s.key for s in remaining}

            for cand in out.candidates:
                if cand.key not in remaining_keys:
                    logger.warning("生产者返回了未请求的键（doc_id=%s）", doc_id)
                    continue
                rec = self._verifier.verify(cand, parsed, spec_by[cand.key])
                if cand.attempt_id is not None:
                    rec = rec.model_copy(update={"attempt_ids": [cand.attempt_id]})
                accumulated[cand.key].append(rec)

            for key, issues in out.failed_keys.items():
                if key in remaining_keys:
                    accumulated[key].append(ExtractionRecord(doc_id=doc_id, key=key, status=Status.FAILED, issues=list(issues)))

            for spec in remaining:
                recs = accumulated[spec.key] or [_no_output_record(doc_id, spec.key)]
                resolved[spec.key] = self._resolver.resolve(doc_id, spec, recs)
            remaining = [s for s in remaining if resolved[s.key].status != Status.GROUNDED]

        records = [
            resolved.get(spec.key) or _no_output_record(doc_id, spec.key) for spec in keyset.keys
        ]
        return DocumentResult(info=parsed.info, records=records, windows=windows, attempts=attempts)

    @staticmethod
    def _unsupported(fp, fmt: DocFormat, display: str | None, keyset: KeySet) -> DocumentResult:
        info = DocumentInfo(
            doc_id=fp.doc_id,
            sha256=fp.sha256,
            format=fmt,
            size_bytes=fp.size_bytes,
            display_name=display,
            parser_name="none",
            parser_version="0",
            warnings=["文档无法解析"],
        )
        records = [
            ExtractionRecord(
                doc_id=fp.doc_id,
                key=k.key,
                status=Status.FAILED,
                issues=[Issue(code=IssueCode.UNSUPPORTED_DOCUMENT, severity=Severity.ERROR, message="文档无法解析")],
            )
            for k in keyset.keys
        ]
        return DocumentResult(info=info, records=records)


def _no_output_record(doc_id: str, key: str) -> ExtractionRecord:
    return ExtractionRecord(
        doc_id=doc_id,
        key=key,
        status=Status.FAILED,
        issues=[Issue(code=IssueCode.KEY_MISSING_IN_OUTPUT, severity=Severity.ERROR, message="没有任何抽取层产出该键")],
    )


def _usage(docs: Sequence[DocumentResult], wall_time_s: float) -> UsageStats:
    attempts = [a for d in docs for a in d.attempts]
    prompt = [a.prompt_tokens for a in attempts if a.prompt_tokens is not None]
    completion = [a.completion_tokens for a in attempts if a.completion_tokens is not None]
    return UsageStats(
        total_attempts=len(attempts),
        cache_hits=sum(1 for a in attempts if a.cache_hit),
        prompt_tokens=sum(prompt) if prompt else None,
        completion_tokens=sum(completion) if completion else None,
        wall_time_s=round(wall_time_s, 3),
    )


def build_pipeline(backend: ExtractionBackend, config: RunConfig) -> CiteCellPipeline:
    """用默认组件组装流水线（M1：解析器注册表 + LLM 生产者 + Verifier + SimpleMerger）。"""
    registry = build_default_registry(
        include_hidden_sheets=config.include_hidden_sheets,
        include_hidden_cells=config.include_hidden_cells,
        pdf_options={"backend": config.pdf_backend},
    )
    planner = DefaultContextPlanner()
    renderer = PromptRendererV1()
    verifier = CoreVerifier(
        review_possible_miss=config.review_possible_miss,
        include_hidden_cells=config.include_hidden_cells,
    )
    producer = LLMCandidateProducer(backend=backend, renderer=renderer, planner=planner, verifier=verifier)
    return CiteCellPipeline(
        registry=registry,
        producers=[producer],
        verifier=verifier,
        resolver=SimpleMerger(),
        renderer=renderer,
        planner=planner,
    )


__all__ = ["CiteCellPipeline", "build_pipeline"]
