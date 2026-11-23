"""校验器 VerifierV2：把任一层的 Candidate 变成 ExtractionRecord（ARCH §6.2）。

本模块是溯源保证的核心。性质：

* **确定性纯函数**：不调用模型、不读取时间/随机数、不依赖字典或集合的遍历顺序。
  同一 (candidate, doc, spec) 两次调用得到完全相等的 ExtractionRecord。
* **grounded 的含义**：值能由原文中真实存在的引文（EXACT 或 NORMALIZED 命中）确定性导出。
  它**不保证**值对应的键是正确的（ARCH §6.2 末尾）。
* **fuzzy 永不 grounded**：FUZZY 只用于诊断（QUOTE_FUZZY_ONLY 的重试反馈），不产生证据。
* **值只在整块上解释**（v2 修复）：值提及在**整块文本**上识别（跨块时为拼接后的块文本），
  不在被截出的引文片段上识别。引文截断数字、日期、金额时，同一提及与引文边界相交，
  判 ``VALUE_CROSSES_QUOTE_BOUNDARY``，不会被当作完整值。
* **所有 grounded 路径共用同一个绑定函数** ``_check_draft``：直接命中、块 ID 重定位、
  相邻块拼接、剥离渲染噪声，都经过它。
* 异常：候选的 doc_id / key 与参数不一致属于调用方的编程错误，抛 ValueError。
  其余任何输入都不抛出：值解析失败转 needs_review（VALUE_UNPARSEABLE / ENUM_OUT_OF_RANGE），
  定位矛盾转 needs_review（LOCATOR_ERROR），最后兜底转 needs_review（OUTPUT_SCHEMA_INVALID）。

流程与 ARCH §6.2 的步骤编号对应：

1. cardinality=many 或带 derivation → needs_review（CARDINALITY_UNSUPPORTED / DERIVATION_UNSUPPORTED）。
2. 值与证据都为空 → not_found，并做 POSSIBLE_MISS 检查。
3. 只有值或只有证据 → needs_review（EMPTY_EVIDENCE_WITH_VALUE / EVIDENCE_WITHOUT_VALUE）。
4. 逐条证据定位：渲染噪声剥离 → 声明块内 EXACT/NORMALIZED → 相邻块拼接 → 全文唯一重定位
   → （可选）FUZZY 诊断。
5. 值检查（``_check_draft``，整块上的提及）：边界相交 → VALUE_CROSSES_QUOTE_BOUNDARY；
   值前紧邻否定字 → NEGATION_CONTEXT；完全位于引文内且与值等价的提及 → 值区间；
   提及单位与键单位不同 → UNIT_MISMATCH。
6. 判定：存在至少一条“有值区间、无否定”的证据，且没有任何边界相交、否定或单位冲突 → grounded；
   否则 needs_review。

分页与拼接：引文跨越 PDF 页面边界时按页切成多段，与跨块拆分一样记为 CROSS_BLOCK_SPLIT。

已知限制：文本型（TEXT）值的截断（如 ``HT-2024-001`` 只引 ``2024-001``）不做边界检查，
因为文本没有可识别的提及边界；M1 的评测只针对数字、金额、百分比、日期的截断。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
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
    Locator,
    LocatorPrecision,
    MatchLevel,
    NormalizedText,
    NormalizedValue,
    NumberValue,
    ParsedDocument,
    PdfLocator,
    Severity,
    Status,
    TextValue,
    ValueMention,
    ValueType,
    XlsxLocator,
)
from citecell.textnorm import Matcher, Normalizer
from citecell.values import ValueNormalizerV1
from citecell.verify.localizer import LocalizerV1
from citecell.verify.render_noise import strip_render_noise

_log = logging.getLogger("citecell.verify")

VERIFIER_VERSION = "verifier-v2"

#: 每个候选最多执行的 FUZZY 诊断次数（限制最坏耗时）。
MAX_FUZZY_DIAGNOSTICS = 4
#: FUZZY 诊断的长度上限，按**归一化后**的长度计算（块文本、引文）。
FUZZY_MAX_BLOCK_NORM = 2000
FUZZY_MAX_QUOTE_NORM = 200
#: 每个候选最多处理的证据条数；超出的证据忽略并附 warning。
MAX_EVIDENCE_PER_CANDIDATE = 8
#: 否定字：值前紧邻 NEGATION_WINDOW 个字符内出现即视为否定上下文。
NEGATION_CHARS = frozenset("不未非无没否")
NEGATION_WINDOW = 2
#: 只对这些类型检查否定上下文（ARCH §6.3：布尔、枚举、文本）。
_NEGATION_TYPES = frozenset({ValueType.BOOLEAN, ValueType.ENUM, ValueType.TEXT})
#: 构成“数值记号”的字符：数字、ASCII 字母、数字间分隔符、中文数字与数量单位（个月天年日号）。
#: 引文边界若把两个相邻的记号字符拆开（如 “1|1,200”、“12|个月”、“0|万”），即截断了记号。
#: 非记号字符（如 “币|1”、“元|整”、“为|1”）处的边界不算截断。
_TOKEN_EXTRA = frozenset(".,-－/%¥．，" + "零〇一二三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟两" + "个月天年日号")


def _is_token_char(ch: str) -> bool:
    return ch.isdigit() or (ch.isascii() and ch.isalpha()) or ch in _TOKEN_EXTRA


def _token_cut(text: str, p: int) -> bool:
    """位置 p 是否把相邻的两个记号字符拆开（p 处为边界）。"""
    return 0 < p < len(text) and _is_token_char(text[p - 1]) and _is_token_char(text[p])


# ---------------------------------------------------------------------------
# 文本查找（与 T1 Matcher 的 EXACT/NORMALIZED 语义一致，但复用块的归一化结果）
# ---------------------------------------------------------------------------


def _all_occurrences(hay: str, needle: str) -> list[int]:
    """needle 在 hay 中全部（含重叠）出现的起点，升序。"""
    out: list[int] = []
    pos = hay.find(needle)
    while pos != -1:
        out.append(pos)
        pos = hay.find(needle, pos + 1)
    return out


def _aligned(nt: NormalizedText, s: int, e: int) -> bool:
    """归一化区间 [s, e) 两端是否落在原文码点边界上（与 T1 Matcher 一致）。"""
    im = nt.index_map
    if s > 0 and im[s - 1] == im[s]:
        return False
    if e < len(im) and im[e - 1] == im[e]:
        return False
    return True


def _find_spans(
    q: str,
    text: str,
    nt_of: Callable[[], NormalizedText],
    normalizer: Normalizer,
) -> tuple[list[tuple[int, int]], MatchLevel]:
    """在 ``text`` 中查找引文 ``q``：EXACT 优先，否则 NORMALIZED。返回 (原文区间列表, 等级)。

    ``nt_of`` 惰性提供 ``text`` 的归一化结果（调用方缓存），仅在 EXACT 未命中时调用。
    FUZZY 不在此处理。未命中返回 ([], NONE)。
    """
    q = q.strip()
    if not q or not text:
        return [], MatchLevel.NONE
    exact = _all_occurrences(text, q)
    if exact:
        return [(s, s + len(q)) for s in exact], MatchLevel.EXACT
    nq = normalizer.normalize(q).text
    if not nq:
        return [], MatchLevel.NONE
    nt = nt_of()
    spans: list[tuple[int, int]] = []
    for s in _all_occurrences(nt.text, nq):
        e = s + len(nq)
        if _aligned(nt, s, e):
            spans.append(nt.to_original_span(s, e))
    if spans:
        return spans, MatchLevel.NORMALIZED
    return [], MatchLevel.NONE


def _concat_normalized(parts: list[tuple[str, NormalizedText]]) -> NormalizedText:
    """拼接若干块的归一化结果。NORMALIZER_V1 逐码点处理，因此拼接结果等于整体归一化；
    回映射下标加上各段在拼接原文中的偏移。"""
    text_parts: list[str] = []
    index_map: list[int] = []
    originals: list[str] = []
    offset = 0
    for original, nt in parts:
        text_parts.append(nt.text)
        index_map.extend(offset + x for x in nt.index_map)
        originals.append(original)
        offset += len(original)
    return NormalizedText(original="".join(originals), text="".join(text_parts), index_map=tuple(index_map))


# ---------------------------------------------------------------------------
# 内部数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Span:
    """一条已定位的引文片段：块下标 + 块内半开区间 + 元信息。"""

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
    spans: tuple[_Span, ...] = ()
    fail: tuple[IssueCode, str] | None = None
    fuzzy_used: bool = False


@dataclass
class _Draft:
    """一条已定位证据及其值检查结果（检查前为默认值）。"""

    span: _Span
    locator: Locator
    precision: LocatorPrecision
    vstart: int | None = None
    vend: int | None = None
    mention_unit: str | None = None
    crossing: bool = False
    negated: bool = False


def _standalone_mention(
    ctx: "_Ctx", index: int, a: int, b: int, value: NormalizedValue, spec: KeySpec
) -> ValueMention | None:
    """区间 [a, b) 的文本本身是否是与值等价的完整表述；是则返回其 ValueMention（用于取单位）。

    文本型：区间文本与值等价即可（返回 None 表示无提及对象）。
    其他类型：区间文本经 find_mentions 识别后，须有一个恰好覆盖整个区间的等价提及。
    """
    sub = ctx.text(index)[a:b]
    if not sub.strip():
        return None
    if isinstance(value, TextValue):
        return ValueMention(value=TextValue(text=sub), start=0, end=len(sub), surface=sub) if ctx.values.equivalent(
            TextValue(text=sub), value, spec
        ) else None
    try:
        mentions = ctx.values.find_mentions(sub, spec)
    except ValueError:
        return None
    for m in mentions:
        if m.start == 0 and m.end == len(sub) and ctx.values.equivalent(m.value, value, spec):
            return m
    return None


class _Ctx:
    """单次 verify 调用内的只读视图，并缓存归一化结果与整块提及（每块只计算一次）。"""

    def __init__(
        self,
        doc: ParsedDocument,
        spec: KeySpec,
        include_hidden: bool,
        normalizer: Normalizer,
        values: ValueNormalizerV1,
    ) -> None:
        self.doc_id = doc.info.doc_id
        self.blocks: list[Block] = list(doc.blocks)
        self.spec = spec
        self.normalizer = normalizer
        self.values = values
        self._include_hidden = include_hidden
        self._index = {b.block_id: i for i, b in enumerate(self.blocks)}
        self._hidden = [_is_hidden(b) for b in self.blocks]
        self._nt: dict[int, NormalizedText] = {}
        self._mentions: dict[int, list[tuple[int, int, ValueMention]]] = {}

    def available(self, i: int) -> bool:
        return self._include_hidden or not self._hidden[i]

    def index_of(self, block_id: str) -> int | None:
        i = self._index.get(block_id)
        if i is None or not self.available(i):
            return None
        return i

    def available_indices(self) -> list[int]:
        return [i for i in range(len(self.blocks)) if self.available(i)]

    def page_adjacent(self, i: int, j: int) -> bool:
        a, b = self.blocks[i].locator, self.blocks[j].locator
        if isinstance(a, PdfLocator) and isinstance(b, PdfLocator):
            return abs(a.page_number - b.page_number) <= 1
        return True

    def continued_boundary(self, i: int) -> bool:
        if i + 1 >= len(self.blocks) or BlockFlag.CONTINUED_ON_NEXT_PAGE not in self.blocks[i].flags:
            return False
        a, b = self.blocks[i].locator, self.blocks[i + 1].locator
        return isinstance(a, PdfLocator) and isinstance(b, PdfLocator) and a.page_number != b.page_number

    def text(self, i: int) -> str:
        return self.blocks[i].text

    def nt(self, i: int) -> NormalizedText:
        """块文本的归一化结果（缓存：每块只归一化一次）。"""
        cached = self._nt.get(i)
        if cached is None:
            cached = self.normalizer.normalize(self.blocks[i].text)
            self._nt[i] = cached
        return cached

    def find(self, q: str, i: int) -> tuple[list[tuple[int, int]], MatchLevel]:
        return _find_spans(q, self.text(i), lambda: self.nt(i), self.normalizer)

    def mentions(self, i: int) -> list[tuple[int, int, ValueMention]]:
        """整块文本上的全部值提及（不随引文截断）。TEXT 类型不做提及识别。"""
        cached = self._mentions.get(i)
        if cached is None:
            cached = []
            if self.spec.value_type != ValueType.TEXT:
                try:
                    cached = [(m.start, m.end, m) for m in self.values.find_mentions(self.text(i), self.spec)]
                except ValueError:  # 提及识别失败视为无提及（失败安全，不会造成误判 grounded）
                    cached = []
            self._mentions[i] = cached
        return cached

    def possible_miss(self, keys: list[str]) -> bool:
        needles = [n for n in (self.normalizer.normalize(k).text for k in keys) if n]
        if not needles:
            return False
        for i in range(len(self.blocks)):
            if not self.available(i):
                continue
            hay = self.nt(i).text
            if any(n in hay for n in needles):
                return True
        return False


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
    """引文查找变体：原始引文；若剥离渲染前缀后不同，再加入剥离版本（标记 noise）。"""
    raw = quote.strip()
    out: list[tuple[str, bool]] = [(raw, False)]
    stripped = strip_render_noise(raw)
    if stripped and stripped != raw:
        out.append((stripped, True))
    return out


def _split_pages(block: Block, start: int, end: int) -> list[tuple[int, int]]:
    """PDF 块的片段跨多页时，在页面边界切开 [start, end)。"""
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


def _unit_conflict(value: NormalizedValue, bearing: list[_Draft], spec_unit: str) -> bool:
    """单位冲突：值本身带单位与键单位不同，或某个值区间内提及的单位与键单位不同。
    只有写出了单位才算冲突。"""
    if isinstance(value, NumberValue) and value.unit is not None and value.unit != spec_unit:
        return True
    return any(d.mention_unit is not None and d.mention_unit != spec_unit for d in bearing)


# ---------------------------------------------------------------------------
# 校验器
# ---------------------------------------------------------------------------


class Verifier:
    """满足 ``citecell.interfaces.Verifier`` 协议的确定性校验器。

    构造参数（均可选）：
    * ``review_possible_miss``：POSSIBLE_MISS 是否升级为 needs_review（默认只记 info）。
    * ``include_hidden_cells``：是否纳入 XLSX 隐藏单元格（默认纳入，证据标注 HIDDEN_SOURCE）。
    * ``max_cross_block``：跨块拼接最多合并的相邻块数（默认 2）。
    * ``enable_fuzzy_diagnostics``：是否做 FUZZY 诊断（只影响 issue 代码，永不定位）。
    """

    version: str = VERIFIER_VERSION

    def __init__(
        self,
        *,
        review_possible_miss: bool = False,
        include_hidden_cells: bool = True,
        max_cross_block: int = 2,
        enable_fuzzy_diagnostics: bool = True,
    ) -> None:
        if max_cross_block < 1:
            raise ValueError("max_cross_block 必须 >= 1")
        self._review_possible_miss = review_possible_miss
        self._include_hidden = include_hidden_cells
        self._max_cross_block = max_cross_block
        self._enable_fuzzy = enable_fuzzy_diagnostics
        self._norm = Normalizer()
        # 长度上限由本类在调用前按归一化长度检查，因此这里放宽 Matcher 自身的原始长度上限。
        self._fuzzy_matcher = Matcher(fuzzy_max_text=10**9, fuzzy_max_quote=10**9)
        self._values = ValueNormalizerV1()
        self._localizer = LocalizerV1()

    # ------------------------------------------------------------------ 入口
    def verify(self, candidate: Candidate, doc: ParsedDocument, spec: KeySpec) -> ExtractionRecord:
        if candidate.doc_id != doc.info.doc_id:
            raise ValueError("候选与文档的 doc_id 不一致")
        if candidate.key != spec.key:
            raise ValueError("候选与键定义不一致")
        try:
            return self._verify(candidate, doc, spec)
        except Exception as exc:  # 不变量：对任何内容输入都不抛出；只记录异常类型，不记录内容
            _log.error("verify internal error type=%s", type(exc).__name__)
            return self._record(
                candidate,
                Status.NEEDS_REVIEW,
                None,
                _clean_value_text(candidate.value_text),
                [],
                [_issue(IssueCode.OUTPUT_SCHEMA_INVALID, Severity.ERROR, "校验过程出现内部错误，已转为人工复核")],
            )

    def _verify(self, candidate: Candidate, doc: ParsedDocument, spec: KeySpec) -> ExtractionRecord:
        ctx = _Ctx(doc, spec, self._include_hidden, self._norm, self._values)
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

        # 步骤 2：值与证据都为空 → not_found
        if value_text is None and not claims:
            miss = ctx.possible_miss([spec.key, *spec.aliases])
            if miss and self._review_possible_miss:
                return self._record(
                    candidate,
                    Status.NEEDS_REVIEW,
                    None,
                    None,
                    [],
                    [_issue(IssueCode.POSSIBLE_MISS, Severity.WARNING, "键名或别名出现在文档中，但模型声明未找到")],
                )
            info = (
                [_issue(IssueCode.POSSIBLE_MISS, Severity.INFO, "键名或别名出现在文档中，但模型声明未找到")]
                if miss
                else []
            )
            return self._record(candidate, Status.NOT_FOUND, None, None, [], info)

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

        # 步骤 4：逐条证据定位（证据条数上限）
        fail_issues: list[Issue] = []
        if len(claims) > MAX_EVIDENCE_PER_CANDIDATE:
            fail_issues.append(_issue(IssueCode.OUTPUT_SCHEMA_INVALID, Severity.WARNING, "证据条数超过上限，超出部分已忽略"))
            claims = claims[:MAX_EVIDENCE_PER_CANDIDATE]
        drafts: list[_Draft] = []
        seen: set[tuple[int, int, int]] = set()
        fuzzy_budget = MAX_FUZZY_DIAGNOSTICS
        for claim in claims:
            outcome = self._locate_claim(claim, ctx, self._enable_fuzzy and fuzzy_budget > 0)
            if outcome.fuzzy_used:
                fuzzy_budget -= 1
            if outcome.fail is not None:
                code, msg = outcome.fail
                fail_issues.append(_issue(code, Severity.ERROR, msg))  # 严重度在判定后统一设定
                continue
            for s in outcome.spans:
                key = (s.index, s.start, s.end)
                if key in seen:
                    continue  # 重复证据去重
                loc = self._localizer.try_locate(ctx.blocks[s.index], s.start, s.end)
                if loc is None:
                    fail_issues.append(_issue(IssueCode.LOCATOR_ERROR, Severity.ERROR, "定位几何矛盾，证据已丢弃"))
                    continue
                seen.add(key)
                drafts.append(_Draft(span=s, locator=loc[0], precision=loc[1]))
        if len(drafts) > MAX_EVIDENCE_PER_CANDIDATE:
            fail_issues.append(_issue(IssueCode.OUTPUT_SCHEMA_INVALID, Severity.WARNING, "证据条数超过上限，超出部分已忽略"))
            drafts = drafts[:MAX_EVIDENCE_PER_CANDIDATE]

        # 步骤 5：值解析（失败转 needs_review，绝不抛出或猜测）
        value: NormalizedValue | None = None
        value_issues: list[Issue] = []
        try:
            value = self._values.parse_value(value_text, spec)
        except (ValueError, ArithmeticError):
            if spec.value_type == ValueType.ENUM:
                value_issues.append(_issue(IssueCode.ENUM_OUT_OF_RANGE, Severity.ERROR, "值不在枚举选项中"))
            else:
                value_issues.append(_issue(IssueCode.VALUE_UNPARSEABLE, Severity.ERROR, "值无法按键的类型解析"))

        bearing: list[_Draft] = []
        crossing_any = negated_any = unit_conflict = False
        if value is not None:
            for d in drafts:
                self._check_draft(d, ctx, value, spec)
            bearing = [d for d in drafts if d.vstart is not None]
            crossing_any = any(d.crossing for d in drafts)
            negated_any = any(d.negated for d in drafts)
            unit_conflict = spec.unit is not None and _unit_conflict(value, bearing, spec.unit)
            if drafts and not bearing:
                value_issues.append(
                    _issue(IssueCode.VALUE_NOT_IN_QUOTE, Severity.ERROR, "引文中找不到与值等价且完整位于引文内的表述")
                )
            if unit_conflict:
                value_issues.append(_issue(IssueCode.UNIT_MISMATCH, Severity.ERROR, "引文中的单位与键定义的单位不一致"))

        # 步骤 6：判定
        good = [d for d in bearing if not d.negated]
        grounded = value is not None and bool(good) and not crossing_any and not negated_any and not unit_conflict
        status = Status.GROUNDED if grounded else Status.NEEDS_REVIEW
        fail_sev = Severity.WARNING if grounded else Severity.ERROR
        fail_issues = [i.model_copy(update={"severity": fail_sev}) for i in fail_issues]

        evidence: list[Evidence] = []
        evidence_issues: list[Issue] = []
        for d in drafts:
            idx = len(evidence)
            evidence.append(self._make_evidence(d, ctx))
            evidence_issues.extend(self._evidence_issues(d, ctx, idx, value is not None))

        issues = fail_issues + value_issues + evidence_issues
        if status == Status.NEEDS_REVIEW and not any(i.severity != Severity.INFO for i in issues):
            issues.append(_issue(IssueCode.VALUE_NOT_IN_QUOTE, Severity.ERROR, "未能确定值的来源"))
        _log.debug("verify key=%s status=%s", spec.key, status.value)
        return self._record(candidate, status, value, value_text, evidence, issues)

    # ------------------------------------------------------------ 步骤 4 定位
    def _locate_claim(self, claim: EvidenceClaim, ctx: _Ctx, allow_fuzzy: bool) -> _Outcome:
        variants = _variants(claim.quote)
        claimed = ctx.index_of(claim.block_id)

        # 4b：声明块内 EXACT → NORMALIZED
        if claimed is not None:
            for q, noise in variants:
                spans, level = ctx.find(q, claimed)
                if spans:
                    s0, e0 = spans[0]
                    return _Outcome(spans=self._finish(ctx, [(claimed, s0, e0)], level, len(spans), False, None, noise))

        # 4c：相邻块拼接（跨越边界的命中）
        if claimed is not None:
            for q, noise in variants:
                hit = self._cross_block(q, claimed, ctx)
                if hit is not None:
                    pieces, level, occ = hit
                    return _Outcome(spans=self._finish(ctx, pieces, level, occ, False, None, noise))

        # 4d：全文唯一重定位
        ambiguous = False
        for q, noise in variants:
            hits: list[tuple[int, list[tuple[int, int]], MatchLevel]] = []
            for i in ctx.available_indices():
                spans, level = ctx.find(q, i)
                if spans:
                    hits.append((i, spans, level))
            total = sum(len(spans) for _, spans, _ in hits)
            if total == 1:
                i, spans, level = hits[0]
                s0, e0 = spans[0]
                return _Outcome(spans=self._finish(ctx, [(i, s0, e0)], level, 1, True, claim.block_id, noise))
            if total > 1:
                ambiguous = True
        if ambiguous:
            return _Outcome(fail=(IssueCode.QUOTE_AMBIGUOUS, "引文在多处出现，无法唯一定位"))

        # 4e：未命中。块不存在 → BLOCK_NOT_FOUND；否则可选 FUZZY 诊断（只诊断，不定位）。
        if claimed is None:
            return _Outcome(fail=(IssueCode.BLOCK_NOT_FOUND, "声明的块编号不存在或不可用"))
        fuzzy_used = False
        if allow_fuzzy:
            q = variants[-1][0]
            nq_len = len(self._norm.normalize(q).text)
            nb_len = len(ctx.nt(claimed).text)
            if nq_len <= FUZZY_MAX_QUOTE_NORM and nb_len <= FUZZY_MAX_BLOCK_NORM:
                fuzzy_used = True
                fz = [
                    m
                    for m in self._fuzzy_matcher.find(q, ctx.text(claimed), allow_fuzzy=True)
                    if m.level == MatchLevel.FUZZY
                ]
                if fz:
                    best = max(m.score for m in fz)
                    return _Outcome(
                        fail=(IssueCode.QUOTE_FUZZY_ONLY, f"仅有近似命中（相似度 {best:.2f}），未用于定位"),
                        fuzzy_used=True,
                    )
        return _Outcome(fail=(IssueCode.QUOTE_NOT_IN_BLOCK, "引文在声明的块中未找到"), fuzzy_used=fuzzy_used)

    def _cross_block(
        self, q: str, claimed: int, ctx: _Ctx
    ) -> tuple[list[tuple[int, int, int]], MatchLevel, int] | None:
        """在包含声明块的相邻窗口中查找**跨越边界**的引文。

        窗口按 (块数, 优先级, 起点) 排序；声明块边界为 CONTINUED_ON_NEXT_PAGE 的窗口优先。
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
            texts = [ctx.text(i) for i in members]
            joined_nt = _concat_normalized([(t, ctx.nt(i)) for t, i in zip(texts, members)])
            spans, level = _find_spans(q, "".join(texts), lambda nt=joined_nt: nt, self._norm)
            offsets: list[int] = []
            off = 0
            for t in texts:
                offsets.append(off)
                off += len(t)
            crossing = [(s, e) for s, e in spans if any(s < o < e for o in offsets[1:])]
            if not crossing:
                continue
            s0, e0 = crossing[0]
            pieces: list[tuple[int, int, int]] = []
            for j, i in enumerate(members):
                a = max(s0, offsets[j])
                b = min(e0, offsets[j] + len(texts[j]))
                if b > a:
                    pieces.append((i, a - offsets[j], b - offsets[j]))
            return pieces, level, len(crossing)
        return None

    @staticmethod
    def _finish(
        ctx: _Ctx,
        pieces: list[tuple[int, int, int]],
        level: MatchLevel,
        occurrences: int,
        relocated: bool,
        claimed_id: str | None,
        noise: bool,
    ) -> tuple[_Span, ...]:
        """按 PDF 页切分原始片段，生成 _Span；多于一段则全部标记 cross。"""
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

    # ------------------------------------------------------------ 步骤 5 值检查（唯一的绑定函数）
    @staticmethod
    def _check_draft(d: _Draft, ctx: _Ctx, value: NormalizedValue, spec: KeySpec) -> None:
        """所有 grounded 路径共用的值检查，只在**整块文本**上进行。

        对每个与引文相交的候选（提及或文本出现）：
        * 完全位于引文内：若与值等价，取第一个作为值区间（先于其他情形）；
        * 向外延伸，且引文边界把记号拆开（``_token_cut``）→ ``crossing``，视为截断；
        * 向外延伸，但边界不拆记号（如“人民币120万元”引为“120万元”）→ 不算截断；
          若与值等价，值区间取两者交集（仍完全位于引文内）。
        否定：引文与任一候选相交，且引文起点前紧邻否定字（仅布尔、枚举、文本）→ ``negated``。
        """
        s = d.span
        qs, qe = s.start, s.end
        text = ctx.text(s.index)
        if isinstance(value, TextValue):
            occ, _ = ctx.find(value.text, s.index)
            candidates: list[tuple[int, int, ValueMention | None]] = [(a, b, None) for a, b in occ]
        else:
            candidates = list(ctx.mentions(s.index))

        inside: list[tuple[int, int, ValueMention | None]] = []
        clean_outside: list[tuple[int, int, ValueMention | None]] = []
        crossing = False
        overlaps = False
        for a, b, vm in candidates:
            if not (a < qe and b > qs):
                continue
            overlaps = True
            if qs <= a and b <= qe:
                if isinstance(value, TextValue) or ctx.values.equivalent(vm.value, value, spec):
                    inside.append((a, b, vm))
                continue
            # 向外延伸：判断引文边界是否拆开了记号
            cut_left = a < qs and _token_cut(text, qs)
            cut_right = b > qe and _token_cut(text, qe)
            if cut_left or cut_right:
                crossing = True
            else:
                # 非截断的外延提及：交集必须自身就是一个与值等价的完整表述（如 “人民币120万元”→“120万元”），
                # 否则只是残片（如 “182万元”→“元”、“百分之五”→“分之五”），不能作为值区间。
                ia, ib = max(a, qs), min(b, qe)
                standalone = _standalone_mention(ctx, s.index, ia, ib, value, spec)
                if standalone is not None:
                    clean_outside.append((ia, ib, standalone))
        d.crossing = crossing

        # 兜底：提及识别器可能漏掉无标度的中文数字（如“六个月”），此时若引文本身整体解析为与值等价的
        # 规范值，且引文两端都不拆记号、没有提及跨越引文边界，则引文整体即为值区间。
        if (
            not inside
            and not clean_outside
            and not crossing
            and not isinstance(value, TextValue)
            and not (_token_cut(text, qs) or _token_cut(text, qe))
        ):
            try:
                parsed = ctx.values.parse_value(text[qs:qe], spec)
            except (ValueError, ArithmeticError):
                parsed = None
            if parsed is not None and ctx.values.equivalent(parsed, value, spec):
                inside = [(qs, qe, None)]
                if isinstance(parsed, NumberValue):
                    d.mention_unit = parsed.unit

        if inside:
            a, b, vm = inside[0]
            d.vstart, d.vend = a, b
            if vm is not None and isinstance(vm.value, NumberValue):
                d.mention_unit = vm.value.unit
        elif clean_outside and not crossing:
            a, b, vm = clean_outside[0]
            d.vstart, d.vend = a, b
            if vm is not None and isinstance(vm.value, NumberValue):
                d.mention_unit = vm.value.unit

        if overlaps and spec.value_type in _NEGATION_TYPES and qs > 0:
            window = text[max(0, qs - NEGATION_WINDOW) : qs]
            # “是否”中的“否”是疑问词而非否定（如标签“是否含税”），不计入否定。
            if window.endswith("是否"):
                window = window[:-2]
            d.negated = any(ch in NEGATION_CHARS for ch in window)

    # ------------------------------------------------------------ 步骤 6 构造
    @staticmethod
    def _make_evidence(d: _Draft, ctx: _Ctx) -> Evidence:
        s = d.span
        block = ctx.blocks[s.index]
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
            doc_id=ctx.doc_id,
            block_id=block.block_id,
            claimed_block_id=s.claimed_id,
            quote=block.text[s.start : s.end],
            match_level=s.level,
            char_start=s.start,
            char_end=s.end,
            value_char_start=d.vstart,
            value_char_end=d.vend,
            locator=d.locator,
            precision=d.precision,
            occurrences=s.occurrences,
            flags=flags,
        )

    @staticmethod
    def _evidence_issues(d: _Draft, ctx: _Ctx, idx: int, has_value: bool) -> list[Issue]:
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
        if d.crossing:
            out.append(
                _issue(IssueCode.VALUE_CROSSES_QUOTE_BOUNDARY, Severity.ERROR, "同类表述与引文边界相交，引文可能截断了值", idx)
            )
        if d.negated:
            out.append(_issue(IssueCode.NEGATION_CONTEXT, Severity.ERROR, "值前紧邻否定字，不能据此判定", idx))
        if has_value and d.vstart is None:
            out.append(
                _issue(IssueCode.EVIDENCE_WITHOUT_VALUE, Severity.WARNING, "该证据未包含与值等价的完整表述，不计入依据", idx)
            )
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


__all__ = ["MAX_FUZZY_DIAGNOSTICS", "VERIFIER_VERSION", "Verifier"]
