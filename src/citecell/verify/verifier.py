"""校验器 VerifierV1：把任一层的 Candidate 变成 ExtractionRecord（ARCH §6.2）。

本模块是溯源保证的核心。性质：

* **确定性纯函数**：不调用模型、不读取时间/随机数、不依赖字典或集合的遍历顺序。
  同一 (candidate, doc, spec) 两次调用得到完全相等的 ExtractionRecord。
* **grounded 的含义**：值能由原文中真实存在的引文（EXACT 或 NORMALIZED 命中）确定性导出。
  它**不保证**值对应的键是正确的（ARCH §6.2 末尾）。
* **fuzzy 永不 grounded**：FUZZY 只用于诊断（QUOTE_FUZZY_ONLY 的重试反馈），不产生证据。
* 异常：候选的 doc_id / key 与参数不一致属于调用方的编程错误，抛 ValueError。
  值解析失败（ValueParseError 等）**不抛出**，转为 needs_review（VALUE_UNPARSEABLE / ENUM_OUT_OF_RANGE）。

流程与 ARCH §6.2 的步骤编号对应（注释中标出）：

1. cardinality=many 或带 derivation → needs_review（CARDINALITY_UNSUPPORTED / DERIVATION_UNSUPPORTED）。
2. 值与证据都为空 → not_found，并做 POSSIBLE_MISS 检查。
3. 只有值或只有证据 → needs_review（EMPTY_EVIDENCE_WITH_VALUE / EVIDENCE_WITHOUT_VALUE）。
4. 逐条证据定位：渲染噪声剥离 → 声明块内 EXACT/NORMALIZED → 相邻块拼接 → 全文唯一重定位
   → （可选）FUZZY 诊断。
5. 值解析与可导出性检查：在已定位证据的引文区间内查找与值等价的提及，并比较单位。
6. 判定：有值、有带值区间的已定位证据、单位无冲突 → grounded；否则 needs_review。

分页与拼接的说明：一条引文若跨越 PDF 页面边界，会按页切成多段（每段是同一块内的子区间），
这些段与跨块拆分一样记为 CROSS_BLOCK_SPLIT。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from citecell.models import (
    Block,
    BlockFlag,
    Candidate,
    Evidence,
    EvidenceClaim,
    EvidenceFlag,
    ExtractionRecord,
    Issue,
    IssueCode,
    KeySpec,
    MatchLevel,
    NormalizedValue,
    NumberValue,
    ParsedDocument,
    PdfLocator,
    Severity,
    Status,
    TextValue,
    ValueType,
    XlsxLocator,
)
from citecell.textnorm import Matcher, Normalizer
from citecell.values import ValueNormalizerV1
from citecell.verify.localizer import LocalizerV1
from citecell.verify.render_noise import strip_render_noise

_log = logging.getLogger("citecell.verify")

VERIFIER_VERSION = "verifier-v1"

#: 每个候选最多执行的 FUZZY 诊断次数（限制最坏耗时，见 ADR-008 与 T1 的耗时说明）。
MAX_FUZZY_DIAGNOSTICS = 4


# ---------------------------------------------------------------------------
# 内部数据结构（不对外暴露）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Span:
    """一条已定位的引文片段：块下标 + 块内半开区间 + 元信息。跨块/跨页时一条声明会产生多个 _Span。"""

    index: int
    start: int
    end: int
    level: MatchLevel
    occurrences: int
    relocated: bool
    claimed_id: str | None
    noise: bool
    cross: bool


@dataclass(frozen=True)
class _Outcome:
    """单条证据声明的定位结果：成功（spans 非空）或失败（fail 非空）。"""

    spans: tuple[_Span, ...] = ()
    fail: tuple[IssueCode, str] | None = None
    fuzzy_used: bool = False


@dataclass
class _Draft:
    """一条已定位证据在绑定值之前的可变草稿。"""

    span: _Span
    vstart: int | None = None
    vend: int | None = None
    mention_unit: str | None = None


class _DocContext:
    """对一个 ParsedDocument 预计算的只读视图：块列表、块 ID 索引、隐藏标记、页信息。"""

    def __init__(self, doc: ParsedDocument, include_hidden: bool) -> None:
        self.blocks: list[Block] = list(doc.blocks)
        self._include_hidden = include_hidden
        self._index = {b.block_id: i for i, b in enumerate(self.blocks)}
        self._hidden = [_is_hidden(b) for b in self.blocks]

    def available(self, i: int) -> bool:
        return self._include_hidden or not self._hidden[i]

    def index_of(self, block_id: str) -> int | None:
        """声明的块 ID 对应的下标；不存在或被排除（隐藏）时返回 None。"""
        i = self._index.get(block_id)
        if i is None or not self.available(i):
            return None
        return i

    def available_indices(self) -> list[int]:
        return [i for i in range(len(self.blocks)) if self.available(i)]

    def page_adjacent(self, i: int, j: int) -> bool:
        """两块若都是 PDF，页号差不超过 1 才允许拼接（ARCH §6.2 步骤 4c）。非 PDF 视为可拼接。"""
        a, b = self.blocks[i].locator, self.blocks[j].locator
        if isinstance(a, PdfLocator) and isinstance(b, PdfLocator):
            return abs(a.page_number - b.page_number) <= 1
        return True

    def continued_boundary(self, i: int) -> bool:
        """块 i 是否为页内最后一块且标记了 CONTINUED_ON_NEXT_PAGE，且下一块位于另一页。"""
        if i + 1 >= len(self.blocks):
            return False
        if BlockFlag.CONTINUED_ON_NEXT_PAGE not in self.blocks[i].flags:
            return False
        a, b = self.blocks[i].locator, self.blocks[i + 1].locator
        return isinstance(a, PdfLocator) and isinstance(b, PdfLocator) and a.page_number != b.page_number


def _is_hidden(block: Block) -> bool:
    if BlockFlag.HIDDEN in block.flags:
        return True
    return isinstance(block.locator, XlsxLocator) and block.locator.hidden


def _issue(code: IssueCode, severity: Severity, message: str, evidence_index: int | None = None) -> Issue:
    # message 只放固定说明，绝不包含正文或引文（日志脱敏，见 models.Issue）。
    return Issue(code=code, severity=severity, message=message, evidence_index=evidence_index)


def _clean_value_text(value_text: str | None) -> str | None:
    if value_text is None:
        return None
    s = value_text.strip()
    return s or None


def _variants(quote: str) -> list[tuple[str, bool]]:
    """引文的查找变体：先原始引文，若含渲染噪声再加入剥离后的版本（ARCH §6.2 步骤 4a）。"""
    raw = quote.strip()
    out: list[tuple[str, bool]] = [(raw, False)]
    stripped = strip_render_noise(raw)
    if stripped and stripped != raw:
        out.append((stripped, True))
    return out


def _split_pages(block: Block, start: int, end: int) -> list[tuple[int, int]]:
    """若 PDF 块的片段跨越多个页面，在页面边界处把 [start, end) 切开。无片段或单页时原样返回。"""
    frs = sorted(
        (f for f in block.fragments if f.end > start and f.start < end),
        key=lambda f: (f.start, f.end),
    )
    if not frs or len({f.page_number for f in frs}) == 1:
        return [(start, end)]
    cuts: list[int] = []
    prev_page = frs[0].page_number
    for f in frs[1:]:
        if f.page_number != prev_page:
            cuts.append(max(f.start, start))
            prev_page = f.page_number
    pieces: list[tuple[int, int]] = []
    cur = start
    for c in cuts:
        pieces.append((cur, c))
        cur = c
    pieces.append((cur, end))
    return [(a, b) for a, b in pieces if b > a]


# ---------------------------------------------------------------------------
# 校验器
# ---------------------------------------------------------------------------


class Verifier:
    """满足 ``citecell.interfaces.Verifier`` 协议的确定性校验器。

    构造参数（均可选）：
    * ``review_possible_miss``：POSSIBLE_MISS 是否升级为 needs_review（默认只记 info）。
    * ``include_hidden_cells``：是否纳入 XLSX 隐藏单元格（默认纳入，证据标注 HIDDEN_SOURCE）。
    * ``max_cross_block``：跨块拼接最多合并的相邻块数（默认 2）。
    * ``enable_fuzzy_diagnostics``：是否对未命中的引文做 FUZZY 诊断（仅影响 issue 代码，永不定位）。
    * ``fuzzy_max_block_chars`` / ``fuzzy_max_quote_chars``：FUZZY 诊断的长度上限，超过则跳过，
      以限制最坏耗时（T1 实测 5000 字 + 500 字约 0.84 s）。
    """

    version: str = VERIFIER_VERSION

    def __init__(
        self,
        *,
        review_possible_miss: bool = False,
        include_hidden_cells: bool = True,
        max_cross_block: int = 2,
        enable_fuzzy_diagnostics: bool = True,
        fuzzy_max_block_chars: int = 2000,
        fuzzy_max_quote_chars: int = 200,
    ) -> None:
        if max_cross_block < 1:
            raise ValueError("max_cross_block 必须 >= 1")
        self._review_possible_miss = review_possible_miss
        self._include_hidden = include_hidden_cells
        self._max_cross_block = max_cross_block
        self._enable_fuzzy = enable_fuzzy_diagnostics
        self._norm = Normalizer()
        self._matcher = Matcher()
        self._fuzzy_matcher = Matcher(fuzzy_max_text=fuzzy_max_block_chars, fuzzy_max_quote=fuzzy_max_quote_chars)
        self._values = ValueNormalizerV1()
        self._localizer = LocalizerV1()

    # ------------------------------------------------------------------ 入口
    def verify(self, candidate: Candidate, doc: ParsedDocument, spec: KeySpec) -> ExtractionRecord:
        if candidate.doc_id != doc.info.doc_id:
            raise ValueError("候选与文档的 doc_id 不一致")
        if candidate.key != spec.key:
            raise ValueError("候选与键定义不一致")

        ctx = _DocContext(doc, self._include_hidden)
        value_text = _clean_value_text(candidate.value_text)
        claims = list(candidate.evidence)

        # 步骤 1：M1 不支持多值与派生
        pre: list[Issue] = []
        if spec.cardinality == "many":
            pre.append(_issue(IssueCode.CARDINALITY_UNSUPPORTED, Severity.ERROR, "M1 仅支持单值键"))
        if candidate.derivation is not None:
            pre.append(_issue(IssueCode.DERIVATION_UNSUPPORTED, Severity.ERROR, "M1 不处理派生声明"))
        if pre:
            return self._record(candidate, Status.NEEDS_REVIEW, None, value_text, [], pre)

        # 步骤 2：值与证据都为空 → not_found（可能漏抽时附 POSSIBLE_MISS）
        if value_text is None and not claims:
            if self._possible_miss(ctx, spec):
                if self._review_possible_miss:
                    return self._record(
                        candidate,
                        Status.NEEDS_REVIEW,
                        None,
                        None,
                        [],
                        [_issue(IssueCode.POSSIBLE_MISS, Severity.WARNING, "键名或别名出现在文档中，但模型声明未找到")],
                    )
                info = [_issue(IssueCode.POSSIBLE_MISS, Severity.INFO, "键名或别名出现在文档中，但模型声明未找到")]
                return self._record(candidate, Status.NOT_FOUND, None, None, [], info)
            return self._record(candidate, Status.NOT_FOUND, None, None, [], [])

        # 步骤 3：只有值或只有证据
        if value_text is None:
            return self._record(
                candidate,
                Status.NEEDS_REVIEW,
                None,
                None,
                [],
                [_issue(IssueCode.EVIDENCE_WITHOUT_VALUE, Severity.ERROR, "给出了证据但没有值")],
            )
        if not claims:
            return self._record(
                candidate,
                Status.NEEDS_REVIEW,
                None,
                value_text,
                [],
                [_issue(IssueCode.EMPTY_EVIDENCE_WITH_VALUE, Severity.ERROR, "给出了值但没有证据")],
            )

        # 步骤 4：逐条证据定位
        drafts: list[_Draft] = []
        fail_issues: list[Issue] = []
        fuzzy_budget = MAX_FUZZY_DIAGNOSTICS
        for claim in claims:
            allow_fuzzy = self._enable_fuzzy and fuzzy_budget > 0
            outcome = self._locate_claim(claim, ctx, allow_fuzzy)
            if outcome.fuzzy_used:
                fuzzy_budget -= 1
            if outcome.fail is not None:
                code, msg = outcome.fail
                fail_issues.append(_issue(code, Severity.ERROR, msg))  # 严重度在判定后再统一设定
                continue
            drafts.extend(_Draft(span=s) for s in outcome.spans)

        # 步骤 5：值解析（失败转 needs_review，绝不抛出或猜测）
        value: NormalizedValue | None = None
        value_issues: list[Issue] = []
        try:
            value = self._values.parse_value(value_text, spec)
        except (ValueError, ArithmeticError):
            code = IssueCode.ENUM_OUT_OF_RANGE if spec.value_type == ValueType.ENUM else IssueCode.VALUE_UNPARSEABLE
            msg = "值不在枚举选项中" if code == IssueCode.ENUM_OUT_OF_RANGE else "值无法按键的类型解析"
            value_issues.append(_issue(code, Severity.ERROR, msg))

        value_bearing: list[_Draft] = []
        unit_conflict = False
        if value is not None:
            for d in drafts:
                self._bind_value(d, ctx, value, spec)
            value_bearing = [d for d in drafts if d.vstart is not None]
            if drafts and not value_bearing:
                value_issues.append(_issue(IssueCode.VALUE_NOT_IN_QUOTE, Severity.ERROR, "引文中找不到与值等价的表述"))
            if spec.unit is not None:
                unit_conflict = _unit_conflict(value, value_bearing, spec.unit)
            if unit_conflict:
                value_issues.append(_issue(IssueCode.UNIT_MISMATCH, Severity.ERROR, "引文中的单位与键定义的单位不一致"))

        # 步骤 6：判定。grounded 需要值、带值区间的已定位证据、且单位无冲突。
        grounded = value is not None and bool(value_bearing) and not unit_conflict
        status = Status.GROUNDED if grounded else Status.NEEDS_REVIEW
        # 被丢弃的证据：grounded 时只记 warning，否则为 error（它们是 needs_review 的原因）。
        fail_sev = Severity.WARNING if grounded else Severity.ERROR
        fail_issues = [i.model_copy(update={"severity": fail_sev}) for i in fail_issues]

        evidence: list[Evidence] = []
        evidence_issues: list[Issue] = []
        for d in drafts:
            idx = len(evidence)
            evidence.append(self._make_evidence(d, candidate, doc, ctx))
            evidence_issues.extend(self._evidence_issues(d, ctx, idx))

        issues = fail_issues + value_issues + evidence_issues
        if status == Status.NEEDS_REVIEW and not any(i.severity != Severity.INFO for i in issues):
            # 不变量保护：needs_review 必须至少有一条 warning/error（正常流程不会走到这里）。
            issues.append(_issue(IssueCode.VALUE_NOT_IN_QUOTE, Severity.ERROR, "未能确定值的来源"))
        _log.debug("verify key=%s status=%s", spec.key, status.value)
        return self._record(candidate, status, value, value_text, evidence, issues)

    # ------------------------------------------------------------ 步骤 2 辅助
    def _possible_miss(self, ctx: _DocContext, spec: KeySpec) -> bool:
        """键名或别名（归一化后）是否出现在文档的任一可用块中。"""
        needles = [n for n in (self._norm.normalize(k).text for k in [spec.key, *spec.aliases]) if n]
        if not needles:
            return False
        for i, block in enumerate(ctx.blocks):
            if not ctx.available(i):
                continue
            hay = self._norm.normalize(block.text).text
            if any(n in hay for n in needles):
                return True
        return False

    # ------------------------------------------------------------ 步骤 4 定位
    def _locate_claim(self, claim: EvidenceClaim, ctx: _DocContext, allow_fuzzy: bool) -> _Outcome:
        variants = _variants(claim.quote)
        claimed = ctx.index_of(claim.block_id)

        # 4b：声明块内 EXACT → NORMALIZED
        if claimed is not None:
            for q, noise in variants:
                ms = self._matcher.find(q, ctx.blocks[claimed].text)
                if ms:
                    spans = self._finish(ctx, [(claimed, ms[0].start, ms[0].end)], ms[0].level, len(ms), False, None, noise)
                    return _Outcome(spans=spans)

        # 4c：相邻块拼接（最多 max_cross_block 块，且必须真正跨越边界）
        if claimed is not None:
            for q, noise in variants:
                hit = self._cross_block(q, claimed, ctx)
                if hit is not None:
                    pieces, level, occ = hit
                    spans = self._finish(ctx, pieces, level, occ, False, None, noise)
                    return _Outcome(spans=spans)

        # 4d：全文唯一重定位
        ambiguous = False
        for q, noise in variants:
            hits: list[tuple[int, list]] = []
            for i in ctx.available_indices():
                ms = self._matcher.find(q, ctx.blocks[i].text)
                if ms:
                    hits.append((i, ms))
            total = sum(len(ms) for _, ms in hits)
            if total == 1:
                i, ms = hits[0]
                spans = self._finish(ctx, [(i, ms[0].start, ms[0].end)], ms[0].level, 1, True, claim.block_id, noise)
                return _Outcome(spans=spans)
            if total > 1:
                ambiguous = True
        if ambiguous:
            return _Outcome(fail=(IssueCode.QUOTE_AMBIGUOUS, "引文在多处出现，无法唯一定位"))

        # 4e：未命中。块不存在 → BLOCK_NOT_FOUND；否则可选 FUZZY 诊断（只诊断，不定位）。
        if claimed is None:
            return _Outcome(fail=(IssueCode.BLOCK_NOT_FOUND, "声明的块编号不存在或不可用"))
        fuzzy_used = False
        if allow_fuzzy:
            fuzzy_used = True
            q = variants[-1][0]
            fz = [m for m in self._fuzzy_matcher.find(q, ctx.blocks[claimed].text, allow_fuzzy=True) if m.level == MatchLevel.FUZZY]
            if fz:
                best = max(m.score for m in fz)
                return _Outcome(
                    fail=(IssueCode.QUOTE_FUZZY_ONLY, f"仅有近似命中（相似度 {best:.2f}），未用于定位"),
                    fuzzy_used=True,
                )
        return _Outcome(fail=(IssueCode.QUOTE_NOT_IN_BLOCK, "引文在声明的块中未找到"), fuzzy_used=fuzzy_used)

    def _cross_block(
        self, q: str, claimed: int, ctx: _DocContext
    ) -> tuple[list[tuple[int, int, int]], MatchLevel, int] | None:
        """在包含声明块的相邻窗口中查找**跨越边界**的引文。返回 (块下标, 起, 止) 列表、等级、次数。

        窗口按 (块数, 优先级, 起点) 排序：先尝试较小窗口；若声明块的边界是 CONTINUED_ON_NEXT_PAGE，
        该窗口优先。确定性：排序键只含整数。
        """
        n = len(ctx.blocks)
        windows: list[tuple[int, int, int]] = []
        for k in range(2, self._max_cross_block + 1):
            for start in range(claimed - k + 1, claimed + 1):
                if start < 0 or start + k > n:
                    continue
                members = range(start, start + k)
                if not all(ctx.available(i) for i in members):
                    continue
                if not all(ctx.page_adjacent(i, i + 1) for i in range(start, start + k - 1)):
                    continue
                prio = 0 if any(ctx.continued_boundary(i) for i in range(start, start + k - 1)) else 1
                windows.append((k, prio, start))
        windows.sort()

        for k, _prio, start in windows:
            members = list(range(start, start + k))
            offsets: list[int] = []
            off = 0
            for i in members:
                offsets.append(off)
                off += len(ctx.blocks[i].text)
            joined = "".join(ctx.blocks[i].text for i in members)
            ms = self._matcher.find(q, joined)
            crossing = [m for m in ms if any(m.start < o < m.end for o in offsets[1:])]
            if not crossing:
                continue
            m = crossing[0]
            pieces: list[tuple[int, int, int]] = []
            for j, i in enumerate(members):
                a = max(m.start, offsets[j])
                b = min(m.end, offsets[j] + len(ctx.blocks[i].text))
                if b > a:
                    pieces.append((i, a - offsets[j], b - offsets[j]))
            return pieces, m.level, len(crossing)
        return None

    @staticmethod
    def _finish(
        ctx: _DocContext,
        pieces: list[tuple[int, int, int]],
        level: MatchLevel,
        occurrences: int,
        relocated: bool,
        claimed_id: str | None,
        noise: bool,
    ) -> tuple[_Span, ...]:
        """把原始命中片段（块下标, 起, 止）按 PDF 页切分，生成 _Span。

        一条声明若产生多于一段（跨块或跨页），每段都标记 ``cross=True``。
        """
        split: list[tuple[int, int, int]] = []
        for i, s, e in pieces:
            for ps, pe in _split_pages(ctx.blocks[i], s, e):
                split.append((i, ps, pe))
        cross = len(split) > 1
        return tuple(
            _Span(
                index=i,
                start=s,
                end=e,
                level=level,
                occurrences=occurrences,
                relocated=relocated,
                claimed_id=claimed_id if relocated else None,
                noise=noise,
                cross=cross,
            )
            for i, s, e in split
        )

    # ------------------------------------------------------------ 步骤 5 辅助
    def _bind_value(self, d: _Draft, ctx: _DocContext, value: NormalizedValue, spec: KeySpec) -> None:
        """在证据的引文区间内查找与值等价的提及，设置值区间。失败安全：识别异常视为无提及。"""
        s = d.span
        region = ctx.blocks[s.index].text[s.start : s.end]
        if isinstance(value, TextValue):
            ms = self._matcher.find(value.text, region)
            if ms:
                d.vstart, d.vend = s.start + ms[0].start, s.start + ms[0].end
            return
        try:
            mentions = self._values.find_mentions(region, spec)
        except ValueError:
            return
        for m in mentions:
            if self._values.equivalent(m.value, value, spec):
                d.vstart, d.vend = s.start + m.start, s.start + m.end
                if isinstance(m.value, NumberValue):
                    d.mention_unit = m.value.unit
                return

    # ------------------------------------------------------------ 步骤 6 构造
    def _make_evidence(self, d: _Draft, candidate: Candidate, doc: ParsedDocument, ctx: _DocContext) -> Evidence:
        s = d.span
        block = ctx.blocks[s.index]
        locator, precision = self._localizer.locate(block, s.start, s.end)
        flags: list[EvidenceFlag] = []
        if s.relocated:
            flags.append(EvidenceFlag.RELOCATED)
        if s.cross:
            flags.append(EvidenceFlag.CROSS_BLOCK_SPLIT)
        if s.occurrences > 1:
            flags.append(EvidenceFlag.AMBIGUOUS_OCCURRENCE)
        if _is_hidden(block):
            flags.append(EvidenceFlag.HIDDEN_SOURCE)
        if s.noise:
            flags.append(EvidenceFlag.RENDER_NOISE_STRIPPED)
        if d.vstart is not None:
            flags.append(EvidenceFlag.VALUE_BEARING)
        return Evidence(
            doc_id=doc.info.doc_id,
            block_id=block.block_id,
            claimed_block_id=s.claimed_id,
            quote=block.text[s.start : s.end],
            match_level=s.level,
            char_start=s.start,
            char_end=s.end,
            value_char_start=d.vstart,
            value_char_end=d.vend,
            locator=locator,
            precision=precision,
            occurrences=s.occurrences,
            flags=flags,
        )

    @staticmethod
    def _evidence_issues(d: _Draft, ctx: _DocContext, idx: int) -> list[Issue]:
        s = d.span
        out: list[Issue] = []
        if s.relocated:
            out.append(_issue(IssueCode.QUOTE_RELOCATED, Severity.INFO, "引文已由程序在其他块中唯一定位", idx))
        if s.cross:
            out.append(_issue(IssueCode.QUOTE_CROSS_BLOCK, Severity.INFO, "引文跨越块或页，已拆分为多条证据", idx))
        if s.noise:
            out.append(_issue(IssueCode.RENDER_NOISE_STRIPPED, Severity.INFO, "已剥离引文中的渲染前缀", idx))
        if _is_hidden(ctx.blocks[s.index]):
            out.append(_issue(IssueCode.HIDDEN_SOURCE, Severity.WARNING, "证据来自隐藏的 XLSX 单元格", idx))
        return out

    @staticmethod
    def _record(
        candidate: Candidate,
        status: Status,
        value: NormalizedValue | None,
        value_text: str | None,
        evidence: list[Evidence],
        issues: list[Issue],
    ) -> ExtractionRecord:
        return ExtractionRecord(
            doc_id=candidate.doc_id,
            key=candidate.key,
            status=status,
            value=value,
            value_text=value_text,
            evidence=evidence,
            method=candidate.method,
            producer_id=candidate.producer_id,
            issues=issues,
            attempt_ids=[candidate.attempt_id] if candidate.attempt_id else [],
        )


def _unit_conflict(value: NormalizedValue, value_bearing: list[_Draft], spec_unit: str) -> bool:
    """单位冲突：值本身带单位且不等于键的单位，或引文中的提及带单位且不等于键的单位。

    只有写出了单位的情形才算冲突；没有写单位不算冲突（ARCH §6.3：比较的是“提及单位”）。
    """
    if isinstance(value, NumberValue) and value.unit is not None and value.unit != spec_unit:
        return True
    return any(d.mention_unit is not None and d.mention_unit != spec_unit for d in value_bearing)


__all__ = ["MAX_FUZZY_DIAGNOSTICS", "VERIFIER_VERSION", "Verifier"]
