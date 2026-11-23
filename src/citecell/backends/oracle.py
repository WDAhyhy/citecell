"""测试后端：OracleBackend（金标准完美输出）与 NoisyOracleBackend（按比例注入错误）。

用途：无需 GPU 即可给出流水线上限，并度量校验器对每类错误的拦截率（ARCH §7，ADR-013）。
* 由 GoldDocument 列表与 ParsedDocument 列表构造；文档按 ``doc_sha256`` ↔ ``info.sha256`` 关联。
* 根据 ``request.context`` 定位文档、窗口（``block_ids``）与键；缺 context 或未知文档/键 → BackendError。
* 引文取金标准 surface 在所在块中的原文（逐字子串）。金标准中的值不在窗口块内 → 输出 ``value=null``。
* NoisyOracle 的随机性只来自带 seed 的 ``random.Random``；每次调用的 RNG 由
  ``(seed, doc_id, window_id, attempt_index)`` 派生，因此结果与调用顺序（并发）无关。

不访问网络。日志不输出正文、引文或提示词。
"""

from __future__ import annotations

import logging
import random
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from citecell.interfaces import BackendError
from citecell.models import (
    Block,
    BackendInfo,
    ChatRequest,
    ChatResponse,
    DocxLocator,
    EvidenceClaim,
    GoldDocument,
    GoldMention,
    GoldRecord,
    InjectedError,
    InjectedErrorType,
    LLMExtractionOutput,
    LLMFieldOutput,
    ParsedDocument,
    PdfLocator,
    RequestContext,
    XlsxLocator,
)

log = logging.getLogger("citecell.backends")

_HALLUCINATION_RANGE = (100_000, 999_999)


@dataclass(frozen=True, slots=True)
class _KeyTruth:
    key: str
    record: GoldRecord
    block: Block | None  # 窗口内含该值的块（无则 None）
    mention: GoldMention | None


def _same_place(a: object, b: object) -> bool:
    if type(a) is not type(b):
        return False
    if isinstance(a, PdfLocator) and isinstance(b, PdfLocator):
        return a.page_number == b.page_number
    if isinstance(a, DocxLocator) and isinstance(b, DocxLocator):
        return (a.body_index, a.paragraph_number, a.table_number, a.row_index, a.col_index) == (
            b.body_index,
            b.paragraph_number,
            b.table_number,
            b.row_index,
            b.col_index,
        )
    if isinstance(a, XlsxLocator) and isinstance(b, XlsxLocator):
        return a.model_dump() == b.model_dump()
    return False


class OracleBackend:
    """金标准完美输出的测试后端。"""

    provider = "oracle"

    def __init__(
        self,
        golds: Iterable[GoldDocument],
        parsed: Iterable[ParsedDocument],
        *,
        backend_id: str = "oracle",
        model: str = "oracle",
    ) -> None:
        self.backend_id = backend_id
        self.model = model
        self._golds: dict[str, GoldDocument] = {}
        for g in golds:
            if g.doc_id in self._golds:
                raise ValueError(f"重复的金标准文档: {g.doc_id}")
            self._golds[g.doc_id] = g
        self._parsed: dict[str, ParsedDocument] = {}
        for p in parsed:
            did = p.info.doc_id
            if did in self._parsed:
                raise ValueError(f"重复的解析结果: {did}")
            self._parsed[did] = p
        for did, g in self._golds.items():
            p = self._parsed.get(did)
            if p is not None and p.info.sha256 != g.doc_sha256:
                raise ValueError(f"金标准与解析结果的 SHA-256 不一致: {did}")

    def info(self) -> BackendInfo:
        return BackendInfo(backend_id=self.backend_id, provider=self.provider, model=self.model)

    # ------------------------------------------------------------------
    def _resolve(self, ctx: RequestContext) -> tuple[GoldDocument, ParsedDocument, list[_KeyTruth]]:
        gold = self._golds.get(ctx.doc_id)
        parsed = self._parsed.get(ctx.doc_id)
        if gold is None or parsed is None:
            raise BackendError(f"Oracle 未持有文档 {ctx.doc_id} 的金标准或解析结果")
        records = {r.key: r for r in gold.records}
        allowed = set(ctx.block_ids)
        truths: list[_KeyTruth] = []
        for key in ctx.keys:
            rec = records.get(key)
            if rec is None:
                raise BackendError(f"金标准中没有键 {key!r}")
            if rec.expected == "derived":
                raise BackendError("M1 Oracle 不支持 derived 金标准（M3 再实现）")
            block, mention = self._locate(parsed, rec, allowed)
            truths.append(_KeyTruth(key=key, record=rec, block=block, mention=mention))
        return gold, parsed, truths

    @staticmethod
    def _locate(
        parsed: ParsedDocument, rec: GoldRecord, allowed: set[str]
    ) -> tuple[Block | None, GoldMention | None]:
        if rec.expected != "present":
            return None, None
        for mention in rec.mentions:
            cands = [
                b
                for b in parsed.blocks
                if (not allowed or b.block_id in allowed) and mention.surface_text in b.text
            ]
            if not cands:
                continue
            exact = [b for b in cands if _same_place(b.locator, mention.locator)]
            return (exact or cands)[0], mention
        return None, None

    def _output_for(self, truths: list[_KeyTruth]) -> LLMExtractionOutput:
        fields: list[LLMFieldOutput] = []
        for t in truths:
            if t.block is None or t.mention is None:
                fields.append(LLMFieldOutput(key=t.key, evidence=[], value=None))
            else:
                surface = t.mention.surface_text
                fields.append(
                    LLMFieldOutput(
                        key=t.key,
                        evidence=[EvidenceClaim(block_id=t.block.block_id, quote=surface)],
                        value=surface,
                    )
                )
        return LLMExtractionOutput(fields=fields)

    async def chat(self, request: ChatRequest) -> ChatResponse:
        if request.context is None:
            raise BackendError("Oracle 需要 request.context 来定位文档与键")
        _, _, truths = self._resolve(request.context)
        output = self._output_for(truths)
        return ChatResponse(content=output.model_dump_json(), finish_reason="stop")


class NoisyOracleBackend(OracleBackend):
    """在 Oracle 输出上按比例注入错误，并记录每次注入（供 Evaluator 统计拦截率）。

    ``ratios``：每种 InjectedErrorType 的注入概率（0..1）。
    * 逐键错误：每个键按枚举顺序独立抽签，**每个键至多注入一种错误**（先命中者生效）。
      某类错误对该键不适用时（例如缺少干扰项）视为未注入，不计入记录。
    * MALFORMED_JSON：按**每次调用**抽签；命中则整次输出被截断为非法 JSON，并记录 key=None，
      此时不再注入逐键错误。
    """

    def __init__(
        self,
        golds: Iterable[GoldDocument],
        parsed: Iterable[ParsedDocument],
        *,
        ratios: Mapping[InjectedErrorType, float],
        seed: int,
        backend_id: str = "noisy_oracle",
        model: str = "noisy-oracle",
    ) -> None:
        super().__init__(golds, parsed, backend_id=backend_id, model=model)
        for etype, p in ratios.items():
            if not isinstance(etype, InjectedErrorType):
                raise ValueError(f"未知的错误类型: {etype!r}")
            if not 0.0 <= float(p) <= 1.0:
                raise ValueError(f"比例必须在 [0, 1] 之间: {etype.value}={p}")
        self.ratios: dict[InjectedErrorType, float] = {k: float(v) for k, v in ratios.items()}
        self.seed = seed
        self._injections: list[InjectedError] = []

    @property
    def injections(self) -> list[InjectedError]:
        return list(self._injections)

    def info(self) -> BackendInfo:
        return BackendInfo(
            backend_id=self.backend_id,
            provider="noisy_oracle",
            model=self.model,
            seed=self.seed,
            extra_params={f"p_{k.value}": repr(v) for k, v in sorted(self.ratios.items(), key=lambda kv: kv[0].value)},
        )

    # ------------------------------------------------------------------
    def _rng(self, ctx: RequestContext) -> random.Random:
        # 字符串种子在 CPython 中是确定性的（不受 PYTHONHASHSEED 影响）
        return random.Random(f"{self.seed}|{ctx.doc_id}|{ctx.window_id}|{ctx.attempt_index}")

    async def chat(self, request: ChatRequest) -> ChatResponse:
        ctx = request.context
        if ctx is None:
            raise BackendError("NoisyOracle 需要 request.context 来定位文档与键")
        _, parsed, truths = self._resolve(ctx)
        rng = self._rng(ctx)

        if self._draw(rng, InjectedErrorType.MALFORMED_JSON):
            clean = self._output_for(truths).model_dump_json()
            self._injections.append(
                InjectedError(
                    doc_id=ctx.doc_id,
                    key=None,
                    error_type=InjectedErrorType.MALFORMED_JSON,
                    attempt_index=ctx.attempt_index,
                )
            )
            return ChatResponse(content=clean[: max(1, len(clean) // 2)], finish_reason="stop")

        fields: list[LLMFieldOutput] = []
        for t in truths:
            field, applied = self._corrupt_key(rng, parsed, t)
            if applied is not None:
                self._injections.append(
                    InjectedError(
                        doc_id=ctx.doc_id,
                        key=t.key,
                        error_type=applied,
                        attempt_index=ctx.attempt_index,
                    )
                )
            if field is not None:
                fields.append(field)
        content = LLMExtractionOutput(fields=fields).model_dump_json()
        return ChatResponse(content=content, finish_reason="stop")

    def _draw(self, rng: random.Random, etype: InjectedErrorType) -> bool:
        p = self.ratios.get(etype, 0.0)
        if p <= 0.0:
            return False
        return rng.random() < p

    def _corrupt_key(
        self, rng: random.Random, parsed: ParsedDocument, t: _KeyTruth
    ) -> tuple[LLMFieldOutput | None, InjectedErrorType | None]:
        """返回 (该键最终输出或 None 表示省略, 注入的错误类型或 None)。"""
        clean = self._output_for([t]).fields[0]
        for etype in InjectedErrorType:
            if etype is InjectedErrorType.MALFORMED_JSON:
                continue  # 调用级，已在 chat 中处理
            if not self._draw(rng, etype):
                continue
            mutated = self._mutate(rng, parsed, t, clean, etype)
            if mutated is not _NOT_APPLICABLE:
                return mutated, etype  # type: ignore[return-value]
        return clean, None

    def _mutate(
        self,
        rng: random.Random,
        parsed: ParsedDocument,
        t: _KeyTruth,
        clean: LLMFieldOutput,
        etype: InjectedErrorType,
    ):  # noqa: ANN202 - 返回 LLMFieldOutput | None（省略）| _NOT_APPLICABLE
        present = t.block is not None and t.mention is not None
        surface = t.mention.surface_text if present else None
        block = t.block

        if etype is InjectedErrorType.HALLUCINATED_VALUE:
            value = _fabricate(rng, surface)
            return LLMFieldOutput(key=t.key, evidence=[] if not present else clean.evidence, value=value)

        if not present:
            return _NOT_APPLICABLE  # 其余错误类型都以"存在的值"为前提

        if etype is InjectedErrorType.MISSING_KEY:
            return None

        if etype is InjectedErrorType.WRONG_BLOCK_ID:
            others = sorted(
                (b for b in parsed.blocks if b.block_id != block.block_id and surface not in b.text),
                key=lambda b: b.block_id,
            )
            if not others:
                return _NOT_APPLICABLE
            wrong = rng.choice(others)
            return LLMFieldOutput(
                key=t.key,
                evidence=[EvidenceClaim(block_id=wrong.block_id, quote=surface)],
                value=surface,
            )

        if etype is InjectedErrorType.PARAPHRASED_QUOTE:
            if len(surface) < 2:
                return _NOT_APPLICABLE
            quote = surface[0] + "·" + surface[1:]
            if quote in block.text:
                return _NOT_APPLICABLE
            return LLMFieldOutput(
                key=t.key,
                evidence=[EvidenceClaim(block_id=block.block_id, quote=quote)],
                value=surface,
            )

        if etype is InjectedErrorType.VALUE_NOT_IN_QUOTE:
            idx = block.text.find(surface)
            before = block.text[:idx].strip()
            after = block.text[idx + len(surface) :].strip()
            if before:
                quote = before
            elif after:
                quote = after
            else:
                return _NOT_APPLICABLE
            return LLMFieldOutput(
                key=t.key,
                evidence=[EvidenceClaim(block_id=block.block_id, quote=quote)],
                value=surface,
            )

        if etype is InjectedErrorType.DISTRACTOR_VALUE:
            if not t.record.distractors:
                return _NOT_APPLICABLE
            dis = rng.choice(t.record.distractors).surface_text
            holders = [b for b in parsed.blocks if dis in b.text]
            if not holders:
                return _NOT_APPLICABLE
            return LLMFieldOutput(
                key=t.key,
                evidence=[EvidenceClaim(block_id=holders[0].block_id, quote=dis)],
                value=dis,
            )

        if etype is InjectedErrorType.CROSS_BLOCK_QUOTE:
            nxt_idx = block.order + 1
            if nxt_idx >= len(parsed.blocks):
                return _NOT_APPLICABLE
            nxt = parsed.blocks[nxt_idx]
            if not nxt.text:
                return _NOT_APPLICABLE
            start = block.text.find(surface)
            quote = block.text[start:] + nxt.text[:10]
            if quote in block.text:
                return _NOT_APPLICABLE
            return LLMFieldOutput(
                key=t.key,
                evidence=[EvidenceClaim(block_id=block.block_id, quote=quote)],
                value=surface,
            )

        raise AssertionError(f"未处理的错误类型: {etype}")  # pragma: no cover


_NOT_APPLICABLE = object()


def _fabricate(rng: random.Random, surface: str | None) -> str:
    """编造一个与真值不同的数字串（不会恰好等于 surface）。"""
    for _ in range(20):
        value = str(rng.randint(*_HALLUCINATION_RANGE))
        if value != surface:
            return value
    return "0"  # pragma: no cover - 概率可忽略


__all__ = ["OracleBackend", "NoisyOracleBackend"]
