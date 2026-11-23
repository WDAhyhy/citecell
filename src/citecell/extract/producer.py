"""第 2 层：LLM 候选生产者（ARCH §2、§6.2 步骤 7、§6.5、§7）。

流程：规划窗口 → 渲染提示词 → 调用后端 → 解析输出 → 生成候选 → 用 Verifier 判定
→ 对可重试的失败键做语义重试（只重问这些键，附结构化错误，次数由 ``max_semantic_retries`` 限制）。

传输层重试由后端自己完成（见 ``ExtractionBackend``）；本模块遇到 ``BackendError`` 即视为该窗口失败，
不再额外重试。本模块不持有跨文档的可变状态；同一文档内的窗口并发受 ``backend.max_concurrency`` 限制，
输出顺序与窗口顺序一致，与完成顺序无关。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pydantic import ValidationError

from citecell.backends.cache import request_sha256
from citecell.interfaces import (
    BackendError,
    ContextPlanner,
    ExtractionBackend,
    PromptRenderer,
    Verifier,
)
from citecell.models import (
    Attempt,
    AttemptOutcome,
    Candidate,
    ChatRequest,
    ContextWindow,
    ExtractionMethod,
    ExtractionRecord,
    Issue,
    IssueCode,
    KeySpec,
    ParsedDocument,
    ProducerOutput,
    RequestContext,
    RunConfig,
    Severity,
    Status,
)
from citecell.prompting import parse_llm_output
from citecell.verify.retryable import RETRYABLE_ISSUE_CODES

logger = logging.getLogger("citecell.extract")

_ERROR_MESSAGE_LIMIT = 200


def _attempt_id(doc_id: str, window_id: str, round_index: int) -> str:
    """确定性的尝试 ID（不含时间与随机数），保证两次运行的结果可比较。"""
    return hashlib.sha256(f"{doc_id}|{window_id}|{round_index}".encode("utf-8")).hexdigest()[:32]


def _error_only(issues: Sequence[Issue]) -> list[Issue]:
    """把问题提升为 ERROR 级（用于"该键最终没有任何候选"的情形）。"""
    out: list[Issue] = []
    for i in issues:
        out.append(Issue(code=i.code, severity=Severity.ERROR, message=i.message or "该键没有可用候选"))
    if not out:
        out.append(Issue(code=IssueCode.KEY_MISSING_IN_OUTPUT, severity=Severity.ERROR, message="该键没有可用候选"))
    return out


@dataclass
class _WindowResult:
    candidates: list[Candidate] = field(default_factory=list)
    attempts: list[Attempt] = field(default_factory=list)
    unresolved: dict[str, list[Issue]] = field(default_factory=dict)


class LLMCandidateProducer:
    """满足 ``citecell.interfaces.CandidateProducer`` 协议的 M1 生产者。"""

    method = ExtractionMethod.LLM

    def __init__(
        self,
        *,
        backend: ExtractionBackend,
        renderer: PromptRenderer,
        planner: ContextPlanner,
        verifier: Verifier,
        producer_id: str | None = None,
    ) -> None:
        self._backend = backend
        self._renderer = renderer
        self._planner = planner
        self._verifier = verifier
        self.producer_id = producer_id or f"llm:{backend.backend_id}"

    async def produce(self, doc: ParsedDocument, keys: Sequence[KeySpec], config: RunConfig) -> ProducerOutput:
        specs = list(keys)
        if not specs:
            return ProducerOutput()
        spec_by = {k.key: k for k in specs}
        windows = self._planner.plan(doc, specs, budget_tokens=config.context_budget_tokens, config=config)
        model = self._backend.info().model
        sem = asyncio.Semaphore(config.backend.max_concurrency)
        # gather 按参数顺序返回结果，因此输出顺序与窗口顺序一致。
        results = await asyncio.gather(
            *(self._run_window(doc, w, spec_by, config, sem, model) for w in windows)
        )

        out = ProducerOutput(windows=list(windows))
        has_candidate: set[str] = set()
        for r in results:
            out.candidates.extend(r.candidates)
            out.attempts.extend(r.attempts)
            has_candidate.update(c.key for c in r.candidates)
        failed: dict[str, list[Issue]] = {}
        for r in results:
            for key, issues in r.unresolved.items():
                if key not in has_candidate and key not in failed:
                    failed[key] = issues
        out.failed_keys = failed
        return out

    async def _run_window(
        self,
        doc: ParsedDocument,
        window: ContextWindow,
        spec_by: dict[str, KeySpec],
        config: RunConfig,
        sem: asyncio.Semaphore,
        model: str,
    ) -> _WindowResult:
        doc_id = doc.info.doc_id
        result = _WindowResult()
        asked = list(window.keys)
        bundle = self._renderer.render_extraction(doc, window, [spec_by[k] for k in asked], config)
        ask_window = window
        round_index = 0
        prev_attempt_id: str | None = None
        have_candidate: set[str] = set()
        last_issues: dict[str, list[Issue]] = {}

        while True:
            ask_keys = list(ask_window.keys)
            attempt_id = _attempt_id(doc_id, window.window_id, round_index)
            request = ChatRequest(
                context=RequestContext(
                    doc_id=doc_id,
                    window_id=window.window_id,
                    keys=ask_keys,
                    block_ids=list(window.block_ids),
                    attempt_index=round_index,
                ),
                messages=bundle.messages,
                json_schema=bundle.output_schema,
                temperature=config.backend.temperature,
                top_p=config.backend.top_p,
                seed=config.backend.seed,
                max_tokens=config.backend.max_tokens,
                timeout_s=config.backend.timeout_s,
            )
            req_hash = request_sha256(request, model)
            started = datetime.now(timezone.utc)
            try:
                async with sem:
                    response = await self._backend.chat(request)
            except BackendError as exc:
                # 只记录异常类型与截断后的消息；消息不含正文（后端约定）。
                message = f"{type(exc).__name__}: {str(exc)[:_ERROR_MESSAGE_LIMIT]}"
                result.attempts.append(
                    Attempt(
                        attempt_id=attempt_id,
                        doc_id=doc_id,
                        window_id=window.window_id,
                        keys=ask_keys,
                        retry_of=prev_attempt_id,
                        backend_id=self._backend.info().backend_id,
                        prompt_template_id=bundle.template_id,
                        request_sha256=req_hash,
                        started_at=started,
                        latency_ms=0.0,
                        outcome=AttemptOutcome.TRANSPORT_ERROR,
                        error_message=message,
                    )
                )
                logger.warning("后端调用失败（doc_id=%s，窗口=%s，尝试=%d）", doc_id, window.window_id, round_index)
                for k in ask_keys:
                    if k not in have_candidate:
                        last_issues[k] = [
                            Issue(code=IssueCode.BACKEND_ERROR, severity=Severity.ERROR, message="后端调用失败（传输层重试已用尽）")
                        ]
                break

            text = response.content
            parsed, parse_issues = parse_llm_output(text, ask_keys)
            result.attempts.append(
                Attempt(
                    attempt_id=attempt_id,
                    doc_id=doc_id,
                    window_id=window.window_id,
                    keys=ask_keys,
                    retry_of=prev_attempt_id,
                    backend_id=self._backend.info().backend_id,
                    prompt_template_id=bundle.template_id,
                    request_sha256=req_hash,
                    response_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    started_at=started,
                    latency_ms=response.latency_ms,
                    prompt_tokens=response.prompt_tokens,
                    completion_tokens=response.completion_tokens,
                    outcome=AttemptOutcome.OK if parsed is not None else AttemptOutcome.SCHEMA_INVALID,
                    error_message=None if parsed is not None else _first_message(parse_issues),
                    cache_hit=response.cache_hit,
                )
            )

            round_records: dict[str, list[ExtractionRecord]] = {k: [] for k in ask_keys}
            round_issues: dict[str, list[Issue]] = {k: [] for k in ask_keys}
            if parsed is None:
                for k in ask_keys:
                    round_issues[k] = list(parse_issues)
            else:
                produced: set[str] = set()
                for f in parsed.fields:
                    try:
                        cand = Candidate(
                            doc_id=doc_id,
                            key=f.key,
                            value_text=f.value,
                            evidence=list(f.evidence),
                            method=ExtractionMethod.LLM,
                            producer_id=self.producer_id,
                            window_id=window.window_id,
                            attempt_id=attempt_id,
                        )
                    except ValidationError:
                        round_issues[f.key].append(
                            Issue(code=IssueCode.OUTPUT_SCHEMA_INVALID, severity=Severity.ERROR, message="字段超出长度限制")
                        )
                        continue
                    produced.add(f.key)
                    result.candidates.append(cand)
                    round_records[f.key].append(self._verifier.verify(cand, doc, spec_by[f.key]))
                for k in ask_keys:
                    if k not in produced and not round_issues[k]:
                        round_issues[k] = [
                            Issue(code=IssueCode.KEY_MISSING_IN_OUTPUT, severity=Severity.WARNING, message="输出中缺少该键")
                        ]

            retry_keys: list[str] = []
            for k in ask_keys:
                recs = round_records[k]
                if any(r.status == Status.GROUNDED for r in recs):
                    have_candidate.add(k)
                    continue
                if recs:
                    have_candidate.add(k)
                    if any(
                        r.status in (Status.NEEDS_REVIEW, Status.FAILED) and any(i.code in RETRYABLE_ISSUE_CODES for i in r.issues)
                        for r in recs
                    ):
                        retry_keys.append(k)
                else:
                    last_issues[k] = round_issues[k]
                    retry_keys.append(k)

            if not retry_keys or round_index >= config.max_semantic_retries:
                for k in ask_keys:
                    if k not in have_candidate and k not in last_issues:
                        last_issues[k] = round_issues[k]
                break

            retry_records: list[ExtractionRecord] = []
            for k in retry_keys:
                recs = round_records[k]
                if recs:
                    retry_records.extend(recs)
                else:
                    issues = round_issues[k]
                    status = Status.FAILED if any(i.severity == Severity.ERROR for i in issues) else Status.NEEDS_REVIEW
                    retry_records.append(ExtractionRecord(doc_id=doc_id, key=k, status=status, issues=issues))

            ask_window = window.model_copy(update={"keys": retry_keys})
            bundle = self._renderer.render_retry(
                doc,
                ask_window,
                [spec_by[k] for k in retry_keys],
                retry_records,
                bundle,
                text,
                config,
            )
            prev_attempt_id = attempt_id
            round_index += 1

        for k in asked:
            if k not in have_candidate:
                result.unresolved[k] = _error_only(last_issues.get(k, []))
        return result


def _first_message(issues: Sequence[Issue]) -> str | None:
    for i in issues:
        if i.severity == Severity.ERROR:
            return i.message[:_ERROR_MESSAGE_LIMIT]
    return issues[0].message[:_ERROR_MESSAGE_LIMIT] if issues else None


__all__ = ["LLMCandidateProducer"]
