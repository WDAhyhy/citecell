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
import re
import unicodedata
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
from citecell.values.units import UNIT_CANON, match_unit
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
#: 逐处守卫检查的出现次数上限；超过则无法逐处确认，判 needs_review。
MAX_OCCURRENCES_CHECKED = 20
#: 单个块文本的字符上限；超过则该块不做校验（判 needs_review）。
MAX_BLOCK_CHARS = 20000
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
    occ_spans: tuple[tuple[int, int], ...] = ()
    """同一块内该引文的全部出现处（升序）；未设置时只有 [start, end) 本身。"""


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
    crossing_note: str = ""
    negated: bool = False
    negated_note: str = ""
    unit_source: str | None = None
    label_only: bool = False
    too_many: bool = False


#: 纯数字金额文本：只含数字、千分位逗号与小数点（不含货币符号或单位字）。
_PURE_NUMBER = re.compile(r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
#: 表头中可接受的金额单位（长的在前）。
_UNIT_ALT = "亿元|百万元|万元|千元|元"
_UNIT_RE = re.compile(_UNIT_ALT)
#: 接受的单位位置：整段即单位；括号包住的单位；“单位：”之后的单位。
_ACCEPTED_UNIT_POS = re.compile(rf"^(?:{_UNIT_ALT})$|[（(]\s*(?:{_UNIT_ALT})\s*[）)]|单位[:：]\s*(?:{_UNIT_ALT})")
#: 出现即表示“单位相关”的标记（元、百分号、其他币种）。出现在非接受位置时无法确定单位。
_UNIT_ISH_RE = re.compile(r"元|%|％|美元|美金|港元|欧元|日元|英镑|澳元|加元|法郎|韩元|人民币|RMB|CNY|USD|HKD|EUR|JPY|GBP|¥|￥|\$|€|£")
#: 表头最长字符数；超过的整句备注不是表头。
HEADER_MAX_CHARS = 20
_KNOWN_CURRENCY_UNITS = frozenset({"元", "千元", "万元", "百万元", "亿元"})
_FORMAT_LITERAL_RE = re.compile(r'"([^"]*)"')
#: 数字占位符后紧跟的逗号（缩放写法，如 0.0,, 或 0,）。千分位写法 #,##0 不匹配。
_SCALING_COMMA_RE = re.compile(r"[0#?],+(?![0#?])")


def _header_unit(h: str) -> tuple[str, str | None, bool]:
    """表头文本的单位判定：返回 (类别, 单位, 是否由隐藏表头贡献)，类别为 unit / neutral / undetermined。

    * 不含任何单位相关标记 → neutral（不影响判定）。
    * 含单位标记：必须恰有一个处于接受位置（括号包住、单位：之后、或整段即单位），且其余部分无单位标记；
      否则 undetermined（如“千美元”“元/件”“万元以上须审批”）。
    """
    if _UNIT_ISH_RE.search(h) is None:
        return "neutral", None, False
    acc = _ACCEPTED_UNIT_POS.search(h)
    if acc is None:
        return "undetermined", None, False
    rest = h[: acc.start()] + h[acc.end() :]
    if _UNIT_ISH_RE.search(rest) is not None:
        return "undetermined", None, False
    m = _UNIT_RE.search(acc.group(0))
    return "unit", (m.group(0) if m else None), False


def _format_unit(fmt: str) -> str | None:
    """XLSX 数字格式中的金额单位。只认：字面量恰为已知单位（如 0"万元"）；或无字面量且含 ¥/￥/CNY/元。

    含其他字面量、或含缩放逗号（0.0,, 之类）的格式不使用。
    """
    literals = _FORMAT_LITERAL_RE.findall(fmt)
    rest = _FORMAT_LITERAL_RE.sub("", fmt)
    if _SCALING_COMMA_RE.search(rest):
        return None
    if literals:
        if len(literals) == 1 and literals[0] in _KNOWN_CURRENCY_UNITS:
            return literals[0]
        return None
    if any(t in rest for t in ("¥", "￥", "CNY", "元")):
        return "元"
    return None


def _table_unit(block: Block, headers: list[tuple[str, bool]]) -> tuple[str, str, bool] | None:
    """表格单元格的金额单位：(单位字, 来源说明, 是否来自隐藏表头)；无线索、线索冲突或无法确定时返回 None。

    ``headers`` 为该单元格上方同一表格区域内的表头文本（文本, 是否隐藏）。
    1. 任一表头 undetermined → None（不猜测）。
    2. 表头中的接受单位恰好一种 → (单位, “列表头”)；多种 → None。
    3. 无表头单位：XLSX 数字格式按 _format_unit 判定 → (单位, “单元格数字格式”)。
    """
    if block.table is None:
        return None
    units: set[str] = set()
    hidden_used = False
    for text, hidden in headers:
        kind, unit, _ = _header_unit(text)
        if kind == "undetermined":
            return None
        if kind == "unit" and unit is not None:
            units.add(unit)
            hidden_used = hidden_used or hidden
    if len(units) > 1:
        return None
    if len(units) == 1:
        return units.pop(), "列表头", hidden_used
    loc = block.locator
    if isinstance(loc, XlsxLocator) and loc.number_format:
        fu = _format_unit(loc.number_format)
        if fu is not None:
            return fu, "单元格数字格式", False
    return None


def _header_money(
    values: ValueNormalizerV1,
    value_text: str,
    spec: KeySpec,
    drafts: list["_Draft"],
    ctx: "_Ctx",
) -> tuple[NormalizedValue, tuple[str, str], bool] | None:
    """纯数字金额在表格中用表头单位解析：把单位字拼到数字后交给 ``parse_value``（不做金额运算）。

    返回 (值, (单位字, 来源), 是否来自隐藏表头)。仅当所有证据块给出同一个单位来源时生效。
    """
    if spec.value_type != ValueType.MONEY or _PURE_NUMBER.fullmatch(value_text) is None or not drafts:
        return None
    units = {ctx.table_unit(d.span.index) for d in drafts}
    if len(units) != 1:
        return None
    unit = next(iter(units))
    if unit is None:
        return None
    try:
        value = values.parse_value(value_text + unit[0], spec)
    except (ValueError, ArithmeticError):
        return None
    return value, (unit[0], unit[1]), unit[2]


@dataclass
class _Check:
    """单处出现的检查结果。"""

    vstart: int | None = None
    vend: int | None = None
    mention_unit: str | None = None
    crossing: bool = False
    crossing_note: str = ""
    negated: bool = False
    negated_note: str = ""
    label_only: bool = False


#: 符号字符：紧邻引文起点时，表示数值的符号被引文截断（如 “负5”→“5”）。
_SIGN_CHARS = frozenset("-－−负+＋")
#: 序数标记：紧邻引文起点时，数量词是序数（如 “第六个月”→“六个月”），不是时长。
_ORDINAL_CHAR = "第"
_SIGN_TYPES = frozenset({ValueType.NUMBER, ValueType.MONEY, ValueType.PERCENT})
_QUESTION_WORD = "是否"
_QUESTION_WINDOW = 3


#: 应忽略的字符：零宽、软连字符，以及组合标记（由调用方另行判断）。
_IGNORABLE_CHARS = frozenset("​‌‍⁠﻿­")
#: 子句分隔符（否定范围不跨越）。
_CLAUSE_DELIMS = frozenset("。；;，,！!？?\n")
#: 否定与疑问的子句段边界：句分隔符、冒号、以及括号（括号内是另一层子句）。
_SEGMENT_DELIMS = _CLAUSE_DELIMS | frozenset("（）()")
_COLON_CHARS = frozenset("：:")
_LABEL_TYPES = frozenset({ValueType.BOOLEAN, ValueType.ENUM, ValueType.TEXT})
_NUMERIC_BOUNDARY_TYPES = frozenset({ValueType.NUMBER, ValueType.MONEY, ValueType.PERCENT, ValueType.DATE})
_ACCOUNTING_TYPES = frozenset({ValueType.NUMBER, ValueType.MONEY, ValueType.PERCENT})
_BOUNDARY_SEPARATORS = frozenset(",，.．-/:")
_HYPHENS_AND_BRACKETS = frozenset("-－‐‑–—()（）")
_UNCHECKED_BOX = frozenset("□☐")
#: 上下文向前/向后扫描的字符上限（子句分隔符之外的长文本不做逐字扫描，保证线性耗时）。
_CLAUSE_SCAN_LIMIT = 200
_PAREN_LIST_RE = re.compile(r"[（(][^（）()]*[/／][^（）()]*[）)]")


def _is_skip(c: str) -> bool:
    """上下文判断时跳过的字符：空白、零宽/软连字符、组合标记。"""
    return c.isspace() or c in _IGNORABLE_CHARS or unicodedata.category(c).startswith("M")


def _fold(c: str) -> str:
    """上下文字符的归一化：NFKC（全角→半角等），并把 U+2212 减号折叠为 ASCII 连字符。"""
    return unicodedata.normalize("NFKC", c).replace("−", "-")


def _folded(s: str) -> str:
    return "".join(_fold(c) for c in s if not _is_skip(c))


def _before(text: str, p: int, n: int) -> str:
    """位置 p 之前最后 n 个“有效”字符（归一化后，跳过空白、零宽与组合标记）。"""
    out: list[str] = []
    for c in reversed(text[:p]):
        if _is_skip(c):
            continue
        out.append(_fold(c))
        if len(out) >= n:
            break
    return "".join(reversed(out))


def _after(text: str, q: int, n: int) -> str:
    """位置 q 之后前 n 个有效字符（归一化后）。"""
    out: list[str] = []
    for c in text[q:]:
        if _is_skip(c):
            continue
        out.append(_fold(c))
        if len(out) >= n:
            break
    return "".join(out)


def _segment_before(text: str, a: int) -> str:
    """提及 [a, …) 之前的子句段：向前至子句分隔符或冒号为止（有界扫描）。"""
    i = a - 1
    floor = max(-1, a - _CLAUSE_SCAN_LIMIT - 1)
    while i > floor and text[i] not in _SEGMENT_DELIMS and text[i] not in _COLON_CHARS:
        i -= 1
    return text[i + 1 : a]


def _segment_after(text: str, b: int) -> str:
    """提及 […, b) 之后的子句段：向后至子句分隔符或冒号为止（有界扫描）。"""
    j = b
    ceil = min(len(text), b + _CLAUSE_SCAN_LIMIT)
    while j < ceil and text[j] not in _SEGMENT_DELIMS and text[j] not in _COLON_CHARS:
        j += 1
    return text[b:j]


#: 答案区分隔符：问句“是否…”之后，第一个出现的冒号、“为”或“是”开始答案区。
_ANSWER_SEPARATORS = frozenset("：:为是")


def _in_question_clause(left: str) -> bool:
    """提及之前的子句段（已归一化）是否处于“是否…”问句中、且尚未进入答案区。

    取最后一个“是否”，其后至提及之间若出现答案区分隔符（冒号、为、是），则已进入答案区，不是问句上下文。
    """
    k = left.rfind(_QUESTION_WORD)
    if k < 0:
        return False
    tail = left[k + len(_QUESTION_WORD) :]
    return not any(ch in _ANSWER_SEPARATORS for ch in tail)


def _space_after_label(text: str, ctx: "_Ctx", index: int, a: int) -> bool:
    """提及 a 之前是否紧跟一次键名/别名出现，且二者之间只有空白（含 PDF 行连接、全角空格）。

    这是第五轮的空白分隔规则：只有键名或别名之后的空白才开启答案区；零宽字符等非空白不算。
    """
    for _ka, kb in ctx.key_spans(index):
        if kb < a and text[kb:a] and text[kb:a].strip() == "":
            return True
    return False


def _guards(
    text: str, a: int, b: int, spec: KeySpec, overlaps: bool, value_bearing: bool, space_sep: bool = False
) -> tuple[str | None, str | None]:
    """提及 [a, b) 上的守卫，返回 (截断说明或 None, 否定说明或 None)。

    * 符号 / 序数 / 会计括号（value_bearing；NUMBER/MONEY/PERCENT）：紧邻前一个有效字符是符号，或“第”，
      或提及被圆括号包住（会计负数）→ 截断。
    * 否定与疑问（overlaps；BOOLEAN/ENUM/TEXT）：提及所在子句段（冒号与句分隔符之间）里，
      提及之外若含“是否”→疑问标签上下文；含否定字 → 否定上下文。
    """
    crossing_note: str | None = None
    negated_note: str | None = None
    if value_bearing and spec.value_type in _SIGN_TYPES:
        prev = _before(text, a, 1)
        if prev in _SIGN_CHARS:
            crossing_note = "数值符号被引文边界截断"
        elif prev == _ORDINAL_CHAR:
            crossing_note = "序数上下文（第…），不是数量或时长"
        elif (
            spec.value_type in _ACCOUNTING_TYPES
            and prev == "("
            and _after(text, b, 1) == ")"
        ):
            crossing_note = "括号可能表示负数（会计写法），符号有歧义"
    if overlaps and spec.value_type in _NEGATION_TYPES:
        left = _folded(_segment_before(text, a))
        right = _folded(_segment_after(text, b))
        if not space_sep and _in_question_clause(left):
            negated_note = "疑问标签上下文（提及位于“是否”之后、答案区之前），不能据此判定"
        elif any(ch in NEGATION_CHARS for ch in left.replace(_QUESTION_WORD, "") + right):
            negated_note = "否定字位于值所在子句中（子句级否定），不能据此判定"
    return crossing_note, negated_note


def _is_label_candidate(text: str, ctx: "_Ctx", index: int, a: int, b: int, colon_abs: int | None) -> bool:
    """提及是否位于标签区域（不是值候选）：键名或别名内部、括号内选项列表、未勾选项、冒号之前的标签区。"""
    for ka, kb in ctx.key_spans(index):
        if ka <= a and b <= kb:
            return True
    for pa, pb in ctx.paren_spans(index):
        if pa <= a and b <= pb:
            return True
    if _before(text, a, 1) in _UNCHECKED_BOX:
        return True
    return colon_abs is not None and a < colon_abs


def _char_before_skip(text: str, p: int) -> str | None:
    i = p - 1
    while i >= 0 and _is_skip(text[i]):
        i -= 1
    return text[i] if i >= 0 else None


def _char_after_skip(text: str, q: int) -> str | None:
    j = q
    while j < len(text) and _is_skip(text[j]):
        j += 1
    return text[j] if j < len(text) else None


def _quote_cut(text: str, qs: int, qe: int, ctx: "_Ctx", index: int) -> bool:
    """数字边界截断：引文边缘字符是数字，且紧邻的（跳过空白后的）外侧字符是数字或连接符。

    引文起点在块首时，同页（或带 CONTINUED 标记）的上一块末尾字符也计入。
    """
    if qs >= qe:
        return False
    inner_first, inner_last = text[qs], text[qe - 1]
    if inner_first.isdigit():
        before = _char_before_skip(text, qs)
        if before is None and qs == 0:
            before = ctx.prev_tail(index)
        if before is not None and (before.isdigit() or before in _BOUNDARY_SEPARATORS):
            return True
    if inner_last.isdigit():
        after = _char_after_skip(text, qe)
        if after is not None and (after.isdigit() or after in _BOUNDARY_SEPARATORS):
            return True
    return False


def _tail_unit(text: str, q: int) -> str | None:
    """引文之后紧随的单位（跳过空白、括号与连字符），仅接受 UNIT_CANON 中的规范单位。"""
    out: list[str] = []
    j = q
    while j < len(text) and len(out) < 12:
        c = text[j]
        j += 1
        if _is_skip(c) or c in _HYPHENS_AND_BRACKETS:
            continue
        out.append(_fold(c))
    found = match_unit("".join(out), 0)
    if found is None:
        return None
    unit = found[1]
    return unit if unit in set(UNIT_CANON.values()) else None


def _check_at(
    text: str,
    ctx: _Ctx,
    index: int,
    qs: int,
    qe: int,
    value: NormalizedValue,
    spec: KeySpec,
    numeric: tuple[str, str] | None = None,
) -> _Check:
    """在块文本的 [qs, qe) 这一处出现上检查值（整块上的提及，不随引文截断）。

    * 标签区域（键名/别名内部、括号选项列表、未勾选项、冒号之前；仅 BOOLEAN/ENUM/TEXT）不是值候选。
    * 与引文相交但不完全位于其内、且引文边界拆开了记号 → 截断；边界不拆记号时，交集本身若是完整等价表述则可用。
    * 完全位于引文内且与值等价的提及：逐个检查守卫，取第一个通过全部守卫的提及。
    * 值区间确定后：守卫在值区间上复核；数字边界截断；引文之后的单位（UNIT_MISMATCH 经 mention_unit 判定）。
    """
    chk = _Check()
    if isinstance(value, TextValue):
        occ, _ = ctx.find(value.text, index)
        candidates: list[tuple[int, int, ValueMention | None]] = [(a, b, None) for a, b in occ]
    else:
        candidates = list(ctx.mentions(index))
    colon_abs = next((k for k in range(qs, qe) if text[k] in _COLON_CHARS), None)

    inside: list[tuple[int, int, ValueMention | None]] = []
    clean_outside: list[tuple[int, int, ValueMention | None]] = []
    overlaps = False
    label_hit = False
    for a, b, vm in candidates:
        if not (a < qe and b > qs):
            continue
        overlaps = True
        if qs <= a and b <= qe:
            eq = isinstance(value, TextValue) or ctx.values.equivalent(vm.value, value, spec)
            if not eq:
                continue
            if spec.value_type in _LABEL_TYPES and _is_label_candidate(text, ctx, index, a, b, colon_abs):
                label_hit = True
                continue
            inside.append((a, b, vm))
            continue
        cut_left = a < qs and _token_cut(text, qs)
        cut_right = b > qe and _token_cut(text, qe)
        if cut_left or cut_right:
            chk.crossing = True
            chk.crossing_note = chk.crossing_note or "同类表述与引文边界相交，引文可能截断了值"
        else:
            ia, ib = max(a, qs), min(b, qe)
            standalone = _standalone_mention(ctx, index, ia, ib, value, spec)
            if standalone is not None:
                clean_outside.append((ia, ib, standalone))
    chk.label_only = label_hit

    # 表格纯数字金额：单元格整格即引文（完整覆盖），且“单元格数字 + 表头单位”等价于值。
    if numeric is not None and not inside and qs == 0 and qe == len(text) and _PURE_NUMBER.fullmatch(text) is not None:
        try:
            parsed = ctx.values.parse_value(text + numeric[0], spec)
        except (ValueError, ArithmeticError):
            parsed = None
        if parsed is not None and ctx.values.equivalent(parsed, value, spec):
            inside = [(qs, qe, None)]

    # 兜底：引文整体解析为等价值，且引文文本恰好等于值文本、两端不拆记号、没有标签区命中。
    if (
        not inside
        and not clean_outside
        and not chk.crossing
        and not label_hit
        and not isinstance(value, TextValue)
        and ctx.value_text is not None
        and text[qs:qe].strip() == ctx.value_text
        and not (_token_cut(text, qs) or _token_cut(text, qe))
    ):
        try:
            parsed = ctx.values.parse_value(text[qs:qe], spec)
        except (ValueError, ArithmeticError):
            parsed = None
        if parsed is not None and ctx.values.equivalent(parsed, value, spec):
            inside = [(qs, qe, None)]
            if isinstance(parsed, NumberValue):
                chk.mention_unit = parsed.unit

    # 选择值区间
    if inside:
        chosen: tuple[int, int, ValueMention | None] | None = None
        for a, b, vm in inside:
            cn, ng = _guards(text, a, b, spec, overlaps, True, _space_after_label(text, ctx, index, a))
            if cn is None and ng is None:
                chosen = (a, b, vm)
                break
        if chosen is None:
            chosen = inside[0]  # 全部未通过：取第一个，守卫说明在下方统一记录（needs_review）
        a, b, vm = chosen
        chk.vstart, chk.vend = a, b
        if vm is not None and isinstance(vm.value, NumberValue):
            chk.mention_unit = vm.value.unit
    elif clean_outside and not chk.crossing:
        a, b, vm = clean_outside[0]
        chk.vstart, chk.vend = a, b
        if vm is not None and isinstance(vm.value, NumberValue):
            chk.mention_unit = vm.value.unit

    if chk.vstart is None:
        # 无值区间：守卫在引文范围上做标记（否定上下文提示；数字边界截断同样记录）。
        cn, ng = _guards(text, qs, qe, spec, overlaps, False, _space_after_label(text, ctx, index, qs))
        if ng is not None:
            chk.negated, chk.negated_note = True, ng
        if spec.value_type in _NUMERIC_BOUNDARY_TYPES and _quote_cut(text, qs, qe, ctx, index):
            chk.crossing = True
            chk.crossing_note = "数字在引文边界处被截断（相邻数字或连接符）"
        return chk

    # 值区间已确定：守卫在值区间上复核（全部未通过时在此记录说明）。
    cn, ng = _guards(
        text,
        chk.vstart,
        chk.vend or chk.vstart,
        spec,
        overlaps,
        True,
        _space_after_label(text, ctx, index, chk.vstart),
    )
    if cn is not None:
        chk.crossing = True
        chk.crossing_note = chk.crossing_note or cn
    if ng is not None:
        chk.negated = True
        chk.negated_note = ng
    if spec.value_type in _NUMERIC_BOUNDARY_TYPES and _quote_cut(text, qs, qe, ctx, index):
        chk.crossing = True
        chk.crossing_note = chk.crossing_note or "数字在引文边界处被截断（相邻数字或连接符）"
    if spec.unit is not None and chk.mention_unit is None:
        chk.mention_unit = _tail_unit(text, qe)
    return chk


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
        # 表格单元格索引：table_id -> {row_index: {col_index: 块下标}}，用于向上扫描表头。
        self._cells: dict[str, dict[int, dict[int, int]]] = {}
        for i, b in enumerate(self.blocks):
            if b.table is not None:
                self._cells.setdefault(b.table.table_id, {}).setdefault(b.table.row_index, {})[b.table.col_index] = i
        self._key_spans: dict[int, list[tuple[int, int]]] = {}
        self._paren_spans: dict[int, list[tuple[int, int]]] = {}
        self._long = [len(b.text) > MAX_BLOCK_CHARS for b in self.blocks]
        self.value_text: str | None = None

    def header_cells(self, i: int) -> list[tuple[str, bool]]:
        """单元格上方同一表格区域内的表头文本（文本, 是否隐藏）：从单元格向上扫描，遇到空行即停止；
        同列的数值单元格属于同一表格、跳过；只收集不含数字、不超过 HEADER_MAX_CHARS 的文本；隐藏表头仅在
        include_hidden_cells 为真时参与（available 已处理）。"""
        t = self.blocks[i].table
        if t is None:
            return []
        rows = self._cells.get(t.table_id, {})
        out: list[tuple[str, bool]] = []
        r = t.row_index - 1
        while r >= 0:
            if r not in rows:
                break  # 空行：表头扫描不跨越空行
            j = rows[r].get(t.col_index)
            if j is not None and self.available(j):
                b = self.blocks[j]
                txt = b.text.strip()
                if txt and not any(ch.isdigit() for ch in txt) and len(txt) <= HEADER_MAX_CHARS:
                    out.append((txt, _is_hidden(b)))
            r -= 1
        return out

    def table_unit(self, i: int) -> tuple[str, str, bool] | None:
        return _table_unit(self.blocks[i], self.header_cells(i))

    def key_spans(self, i: int) -> list[tuple[int, int]]:
        """键名与别名在块文本中每次出现的区间（标签区域）。"""
        cached = self._key_spans.get(i)
        if cached is None:
            text = self.blocks[i].text
            cached = []
            for name in [self.spec.key, *self.spec.aliases]:
                if name:
                    cached.extend((k, k + len(name)) for k in _all_occurrences(text, name))
            self._key_spans[i] = cached
        return cached

    def paren_spans(self, i: int) -> list[tuple[int, int]]:
        """括号内的选项列表（形如“（A/B）”）区间。"""
        cached = self._paren_spans.get(i)
        if cached is None:
            cached = [(m.start(), m.end()) for m in _PAREN_LIST_RE.finditer(self.blocks[i].text)]
            self._paren_spans[i] = cached
        return cached

    def is_long(self, i: int) -> bool:
        return self._long[i]

    def raw_index(self, block_id: str) -> int | None:
        return self._index.get(block_id)

    def prev_tail(self, i: int) -> str | None:
        """块 i 之前一块的末尾有效字符，仅当两块可以相连（同页、或前一块带 CONTINUED 标记，或非 PDF）。"""
        j = i - 1
        if j < 0 or not self.available(j):
            return None
        bj, bi = self.blocks[j], self.blocks[i]
        if bj.table is not None or bi.table is not None:
            return None  # 表格单元格之间不构成文本相连
        a, b = bj.locator, bi.locator
        if not (isinstance(a, PdfLocator) and isinstance(b, PdfLocator)):
            return None  # 只有 PDF 文本块才可能跨行/跨页相连
        if a.page_number != b.page_number and BlockFlag.CONTINUED_ON_NEXT_PAGE not in bj.flags:
            return None
        text = self.blocks[j].text
        return _char_before_skip(text, len(text))

    def available(self, i: int) -> bool:
        return (self._include_hidden or not self._hidden[i]) and not self._long[i]


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
        ctx.value_text = value_text
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
        numeric: tuple[str, str] | None = None
        hdr_hidden = False
        try:
            value = self._values.parse_value(value_text, spec)
        except (ValueError, ArithmeticError):
            hv = _header_money(self._values, value_text, spec, drafts, ctx)
            if hv is not None:
                value, numeric, hdr_hidden = hv
                if hdr_hidden:
                    value_issues.append(_issue(IssueCode.HIDDEN_SOURCE, Severity.WARNING, "金额单位来自隐藏的表头"))
            elif spec.value_type == ValueType.ENUM:
                value_issues.append(_issue(IssueCode.ENUM_OUT_OF_RANGE, Severity.ERROR, "值不在枚举选项中"))
            else:
                value_issues.append(_issue(IssueCode.VALUE_UNPARSEABLE, Severity.ERROR, "值无法按键的类型解析"))

        bearing: list[_Draft] = []
        crossing_any = negated_any = unit_conflict = False
        if value is not None:
            for d in drafts:
                self._check_draft(d, ctx, value, spec, numeric)
            bearing = [d for d in drafts if d.vstart is not None]
            crossing_any = any(d.crossing for d in drafts)
            negated_any = any(d.negated for d in drafts)
            unit_conflict = spec.unit is not None and _unit_conflict(value, bearing, spec.unit)
            if drafts and not bearing and not any(d.too_many for d in drafts):
                if any(d.label_only for d in drafts):
                    msg = "值只出现在标签区（键名、别名、选项列表或未勾选项中）"
                else:
                    msg = "引文中找不到与值等价且完整位于引文内的表述"
                value_issues.append(_issue(IssueCode.VALUE_NOT_IN_QUOTE, Severity.ERROR, msg))
            if unit_conflict:
                value_issues.append(_issue(IssueCode.UNIT_MISMATCH, Severity.ERROR, "引文中的单位与键定义的单位不一致"))

        too_many = any(d.too_many for d in drafts)
        # 步骤 6：判定
        good = [d for d in bearing if not d.negated]
        if too_many:
            value_issues.append(_issue(IssueCode.QUOTE_AMBIGUOUS, Severity.ERROR, "引文出现次数过多，无法逐处确认"))
        grounded = (
            value is not None
            and bool(good)
            and not crossing_any
            and not negated_any
            and not unit_conflict
            and not too_many
        )
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
        raw = ctx.raw_index(claim.block_id)
        if raw is not None and ctx.is_long(raw):
            return _Outcome(fail=(IssueCode.CONTEXT_TRUNCATED, "块过长（超过 20000 字符），未做校验"))
        variants = _variants(claim.quote)
        claimed = ctx.index_of(claim.block_id)

        # 4b：声明块内 EXACT → NORMALIZED
        if claimed is not None:
            for q, noise in variants:
                spans, level = ctx.find(q, claimed)
                if spans:
                    s0, e0 = spans[0]
                    return _Outcome(
                        spans=self._finish(
                            ctx, [(claimed, s0, e0)], level, len(spans), False, None, noise, occ=tuple(spans)
                        )
                    )

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
        occ: tuple[tuple[int, int], ...] = (),
    ) -> tuple[_Span, ...]:
        """按 PDF 页切分原始片段，生成 _Span；多于一段则全部标记 cross。

        ``occ`` 为同一块内的全部出现处；只有未被切分（单段）时才随 _Span 保留，供逐处守卫检查。
        """
        split: list[tuple[int, int, int]] = []
        for i, s, e in pieces:
            for ps, pe in _split_pages(ctx.blocks[i], s, e):
                split.append((i, ps, pe))
        cross = len(split) > 1
        out: list[_Span] = []
        for i, s, e in split:
            occ_spans = occ if (len(split) == 1 and occ) else ((s, e),)
            out.append(
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
                    occ_spans=occ_spans,
                )
            )
        return tuple(out)

    # ------------------------------------------------------------ 步骤 5 值检查（唯一的绑定函数）
    @staticmethod
    def _check_draft(
        d: _Draft,
        ctx: _Ctx,
        value: NormalizedValue,
        spec: KeySpec,
        numeric: tuple[str, str] | None = None,
    ) -> None:
        """所有 grounded 路径共用的值检查。对引文在块内的每一处出现逐一检查（_check_at）。

        * 出现次数超过 MAX_OCCURRENCES_CHECKED 时无法逐处确认 → too_many（needs_review）；
        * 值区间只取第一处出现，且只能来自第一处（证据的引文区间即第一处）；
        * 任一处出现触发截断、标签区、否定等守卫，整条证据即带相应标记。
        """
        text = ctx.text(d.span.index)
        first = (d.span.start, d.span.end)
        occs = d.span.occ_spans or (first,)
        if len(occs) > MAX_OCCURRENCES_CHECKED:
            d.too_many = True
            return
        # 出现处若严格位于另一个**不等价**的更长提及之内（如“含税”位于“不含税”之内），它是另一个值的表述的一部分，
        # 不作为该值的独立出现处；只有当没有其他出现处时，才对它做守卫检查（以便给出否定/截断标记）。
        other_values = [
            (ma, mb)
            for ma, mb, vm in ctx.mentions(d.span.index)
            if not ctx.values.equivalent(vm.value, value, spec)
        ]
        excluded = tuple(
            (a, b)
            for a, b in occs
            if any(ma <= a and b <= mb and (ma, mb) != (a, b) for ma, mb in other_values)
        )
        kept = tuple(o for o in occs if o not in excluded)
        occs = kept if kept else excluded
        for a, b in occs:
            chk = _check_at(text, ctx, d.span.index, a, b, value, spec, numeric)
            if (a, b) == first:
                d.label_only = chk.label_only
                if chk.vstart is not None and d.vstart is None:
                    d.vstart, d.vend, d.mention_unit = chk.vstart, chk.vend, chk.mention_unit
                    if numeric is not None:
                        d.unit_source = numeric[1]
            if chk.crossing:
                d.crossing = True
                d.crossing_note = d.crossing_note or chk.crossing_note
            if chk.negated:
                d.negated = True
                d.negated_note = d.negated_note or chk.negated_note

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
                _issue(
                    IssueCode.VALUE_CROSSES_QUOTE_BOUNDARY,
                    Severity.ERROR,
                    d.crossing_note or "同类表述与引文边界相交，引文可能截断了值",
                    idx,
                )
            )
        if d.unit_source is not None:
            out.append(
                _issue(
                    IssueCode.UNIT_FROM_HEADER,
                    Severity.INFO,
                    f"金额单位来自{d.unit_source}（纯数字单元格，按该单位解析）",
                    idx,
                )
            )
        if d.negated:
            out.append(
                _issue(IssueCode.NEGATION_CONTEXT, Severity.ERROR, d.negated_note or "值前紧邻否定字，不能据此判定", idx)
            )
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
