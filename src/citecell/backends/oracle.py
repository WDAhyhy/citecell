"""测试后端：OracleBackend（金标准完美输出）与 NoisyOracleBackend（按比例注入错误）。

用途：无需 GPU 即可给出流水线上限，并度量校验器对每类错误的拦截率（ARCH §7，ADR-013）。
* 由 GoldDocument 列表与 ParsedDocument 列表构造；文档按 ``doc_sha256`` ↔ ``info.sha256`` 关联。
* 根据 ``request.context`` 定位文档、窗口（``block_ids``）与键；缺 context 或未知文档/键 → BackendError。
* 证据按金标准提及的**原生位置**选块（DOCX 段落/表格行列、XLSX sheet+cell、PDF 页码+字符几何），
  永不按文本搜索，也永不取干扰项的位置。
* 引文是块内的逐字子串：从提及的真实字符区间向左扩展上下文，直到在块内恰好出现一次，且包含完整提及；
  超过 ``QUOTE_MAX_CHARS`` 则取以提及为中心的窗口。金标准中的值不在窗口块内 → 输出 ``value=null``。
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
from citecell.textnorm import Normalizer
from citecell.models import (
    BBox,
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


QUOTE_MAX_CHARS = 80  # 引文长度上限（字符）


@dataclass(frozen=True, slots=True)
class _KeyTruth:
    key: str
    record: GoldRecord
    block: Block | None  # 窗口内承载该值的块（无则 None）
    mention: GoldMention | None
    quote: str | None = None  # 块内的唯一引文（包含完整提及）


def _docx_position(loc: DocxLocator) -> tuple:
    return (loc.body_index, loc.paragraph_number, loc.table_number, loc.row_index, loc.col_index)


def _regions_overlap(a: list[BBox], b: list[BBox]) -> bool:
    return any(
        min(p.x1, q.x1) > max(p.x0, q.x0) and min(p.y1, q.y1) > max(p.y0, q.y0)
        for p in a
        for q in b
    )


def _center_in_regions(bbox: BBox, regions: list[BBox]) -> bool:
    cx = (bbox.x0 + bbox.x1) / 2
    cy = (bbox.y0 + bbox.y1) / 2
    return any(r.x0 <= cx <= r.x1 and r.y0 <= cy <= r.y1 for r in regions)


def same_native_place(a: object, b: object) -> bool:
    """两个原生定位是否指向同一位置。

    只比较位置字段：DOCX 为 body_index / 段落号 / 表格号 / 行 / 列；XLSX 为 sheet_name + cell；
    PDF 为页码且区域有正面积重叠。不比较 number_format、style、formula 等属性。
    格式不同一律为 False。
    """
    if type(a) is not type(b):
        return False
    if isinstance(a, DocxLocator) and isinstance(b, DocxLocator):
        return _docx_position(a) == _docx_position(b)
    if isinstance(a, XlsxLocator) and isinstance(b, XlsxLocator):
        return a.sheet_name == b.sheet_name and a.cell == b.cell
    if isinstance(a, PdfLocator) and isinstance(b, PdfLocator):
        return a.page_number == b.page_number and _regions_overlap(a.regions, b.regions)
    return False


def _mention_span(block: Block, mention: GoldMention) -> tuple[int, int] | None:
    """提及在块文本中的字符区间；该块不承载此提及时返回 None。

    * DOCX：先用金标准的 char_start/char_end（相对段落/单元格文本），不一致则取 surface 的首次出现。
    * XLSX：整格（块文本即单元格文本）。
    * PDF：块内字符的几何片段（TextFragment）中，中心落在提及区域内的那些字符的范围。
    """
    ml = mention.locator
    text = block.text
    surface = mention.surface_text
    if isinstance(ml, PdfLocator):
        hits = [
            f
            for f in block.fragments
            if f.page_number == ml.page_number and _center_in_regions(f.bbox, ml.regions)
        ]
        if not hits:
            return None
        s = min(f.start for f in hits)
        e = max(f.end for f in hits)
        if text[s:e] == surface:
            return s, e
        occ = _occurrences(text, surface)
        if occ:
            o = min(occ, key=lambda x: abs(x - s))
            return o, o + len(surface)
        return (s, e) if e > s else None
    if not same_native_place(block.locator, ml):
        return None
    if isinstance(ml, XlsxLocator):
        return (0, len(text)) if text else None
    cs, ce = mention.char_start, mention.char_end
    if cs is not None and ce is not None and text[cs:ce] == surface:
        return cs, ce
    idx = text.find(surface)
    return (idx, idx + len(surface)) if idx >= 0 else None


def _occurrences(hay: str, needle: str) -> list[int]:
    out: list[int] = []
    if not needle:
        return out
    i = hay.find(needle)
    while i >= 0:
        out.append(i)
        i = hay.find(needle, i + 1)
    return out


_NORMALIZER = Normalizer()


def _is_unique_quote(text: str, norm_text: str, quote: str) -> bool:
    """引文在块内是否唯一：原文中恰好一次，且归一化后（校验器的匹配方式）也恰好一次。

    引文首尾不得是空白（校验器会忽略空白，首尾空白会让唯一性失效）。
    """
    if not quote or quote[0].isspace() or quote[-1].isspace():
        return False
    if text.count(quote) != 1:
        return False
    nq = _NORMALIZER.normalize(quote).text
    return bool(nq) and norm_text.count(nq) == 1


def expand_quote(text: str, start: int, end: int, cap: int = QUOTE_MAX_CHARS) -> tuple[str, int, int]:
    """从提及区间 [start, end) 出发，向左（不足则向右）扩展上下文，直到引文在块内恰好出现一次。

    * 引文始终包含完整提及。
    * 超过 ``cap`` 仍不唯一：取以提及为中心、长度为 ``cap`` 的窗口（可能仍不唯一，由校验器裁决）。
    * 提及本身不短于 ``cap``：原样返回提及。
    返回 (引文, 引文在块内的起点, 引文在块内的终点)。
    """
    if not 0 <= start < end <= len(text):
        raise ValueError("提及区间越界")
    if end - start >= cap:
        return text[start:end], start, end
    norm_text = _NORMALIZER.normalize(text).text
    left, right = start, end
    while True:
        quote = text[left:right]
        if _is_unique_quote(text, norm_text, quote):
            return quote, left, right
        if right - left >= cap:
            break
        if left > 0:
            left -= 1
        elif right < len(text):
            right += 1
        else:
            break
    width = min(cap, len(text))
    ws = max(0, min(start - (width - (end - start)) // 2, len(text) - width))
    if not (ws <= start and ws + width >= end):  # pragma: no cover - 数学上不会发生
        raise RuntimeError("中心窗口未包含提及")
    return text[ws : ws + width], ws, ws + width


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
            block, mention, quote = self._locate(parsed, rec, allowed)
            truths.append(_KeyTruth(key=key, record=rec, block=block, mention=mention, quote=quote))
        return gold, parsed, truths

    @staticmethod
    def _locate(
        parsed: ParsedDocument, rec: GoldRecord, allowed: set[str]
    ) -> tuple[Block | None, GoldMention | None, str | None]:
        """按金标准提及的**原生位置**选块（不做文本搜索，干扰项永不作为证据）。"""
        if rec.expected != "present":
            return None, None, None
        for mention in rec.mentions:
            for block in parsed.blocks:
                if allowed and block.block_id not in allowed:
                    continue
                span = _mention_span(block, mention)
                if span is None:
                    continue
                quote, _, _ = expand_quote(block.text, span[0], span[1])
                return block, mention, quote
        return None, None, None

    def _output_for(self, truths: list[_KeyTruth]) -> LLMExtractionOutput:
        fields: list[LLMFieldOutput] = []
        for t in truths:
            if t.block is None or t.mention is None or t.quote is None:
                fields.append(LLMFieldOutput(key=t.key, evidence=[], value=None))
            else:
                fields.append(
                    LLMFieldOutput(
                        key=t.key,
                        evidence=[EvidenceClaim(block_id=t.block.block_id, quote=t.quote)],
                        value=t.mention.surface_text,
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
