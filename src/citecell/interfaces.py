"""CiteCell 模块接口（typing.Protocol）。

只有签名与语义说明，没有实现。实现放在各子包中（见 docs/TASKS.md 的文件归属表），
并通过 ``isinstance(obj, Proto)``（runtime_checkable）或类型检查器确认符合接口。

层次关系（数据流）::

    Parser ──ParsedDocument──► ContextPlanner ──ContextWindow──► PromptRenderer ──PromptBundle──►
    ExtractionBackend ──ChatResponse──► (LLMCandidateProducer 解析为 Candidate) ──►
    Verifier(TextNormalizer, QuoteMatcher, ValueNormalizer, Localizer) ──ExtractionRecord──►
    Resolver（多窗口/多层合并）──► DocumentResult ──► RunResult ──► Evaluator(GoldDocument) ──► EvalReport

不变量：所有抽取层（模板 M2 / LLM M1 / 决策 M3 / 人工）都只产出 ``Candidate``，
并且都经过同一个 ``Verifier``。这是 M2、M3 不需要修改 M1 契约的前提。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from citecell.models import (
    BackendInfo,
    Block,
    Candidate,
    ChatRequest,
    ChatResponse,
    ContextWindow,
    Derivation,
    DerivationClaim,
    DocFormat,
    DocumentResult,
    EvalReport,
    ExtractionMethod,
    ExtractionRecord,
    GateDecision,
    GenerationConfig,
    GoldDocument,
    InjectedError,
    KeySet,
    KeySpec,
    Locator,
    LocatorPrecision,
    NormalizedText,
    NormalizedValue,
    ParsedDocument,
    PdfChar,
    PdfPageInfo,
    ProducerOutput,
    PromptBundle,
    QuoteMatch,
    RunConfig,
    RunResult,
    SyntheticSample,
    TemplateSpec,
    ValueMention,
)


class ValueParseError(ValueError):
    """ValueNormalizer 无法把原文解析为规范化值时抛出。message 不得包含整段正文。"""


class BackendError(RuntimeError):
    """后端传输层错误（连接失败、HTTP 5xx、超时等）。重试由调用方按 RunConfig 处理。"""


class UnsupportedDocumentError(RuntimeError):
    """文档不在支持范围（扫描件、加密、损坏、超出大小限制等）。"""


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


@runtime_checkable
class Parser(Protocol):
    """把一个文件解析为 ParsedDocument。

    要求：
    * 纯函数式：同一文件字节 + 同一 ``version`` ⇒ 逐字节相同的 ParsedDocument JSON。
    * doc_id 由文件字节 SHA-256 计算，与文件名/路径无关。
    * 不得把正文写入日志；文档级问题写入 ``DocumentInfo.warnings``（不含正文）。
    * 不支持的输入抛 ``UnsupportedDocumentError``。
    """

    name: str
    version: str
    formats: frozenset[DocFormat]

    def parse(self, path: Path, *, display_name: str | None = None) -> ParsedDocument:
        """解析文件。``display_name`` 仅在用户允许记录文件名时传入。"""
        ...


@runtime_checkable
class PdfBackend(Protocol):
    """PDF 字符级后端（许可证隔离点）。

    默认实现必须是宽松许可证库（候选 pypdfium2 / pdfminer.six，许可证待 T0 核实）。
    PyMuPDF（AGPL）只能作为单独安装的可选扩展实现本接口，且不得在默认导入路径中被 import。
    后端只产出字符与页面信息，坐标须换算到 PDF_COORDS_V1（见 models.BBox）。
    """

    name: str
    library_version: str

    def open(self, path: Path) -> None:
        """打开文档。加密/损坏时抛 UnsupportedDocumentError。"""
        ...

    def pages(self) -> list[PdfPageInfo]:
        """按顺序返回所有页面信息。"""
        ...

    def iter_chars(self, page_number: int) -> Iterator[PdfChar]:
        """按内容流顺序产出该页字符（page_number 1 起）。"""
        ...

    def close(self) -> None:
        ...


@runtime_checkable
class PdfLayout(Protocol):
    """与后端无关的版面分析：字符 → 行 → 块，并生成 TextFragment 与块级 PdfLocator。

    实现 LINE_JOIN_V1 拼接规则与长块切分；阅读顺序 M1 为单栏自上而下、自左而右。
    """

    version: str

    def build_blocks(self, doc_id: str, pages: Sequence[PdfPageInfo], chars_by_page: Mapping[int, Sequence[PdfChar]]) -> list[Block]:
        """返回按阅读顺序排列、order 连续的块列表。"""
        ...


@runtime_checkable
class ParserRegistry(Protocol):
    """按文件类型选择解析器。PDF 解析器内部再按 RunConfig.pdf_backend 选择后端。"""

    def for_path(self, path: Path) -> Parser:
        """按扩展名与文件魔数选择解析器；无法识别时抛 UnsupportedDocumentError。"""
        ...


# ---------------------------------------------------------------------------
# 文本归一化、引文匹配、值规范化、定位
# ---------------------------------------------------------------------------


@runtime_checkable
class TextNormalizer(Protocol):
    """偏移保持的文本归一化（NORMALIZER_V1，规则见 ARCHITECTURE.md §6.1）。"""

    version: str

    def normalize(self, text: str) -> NormalizedText:
        """返回归一化文本与逐码点的回映射表。必须满足：对任何输入不抛异常、确定性。"""
        ...


@runtime_checkable
class QuoteMatcher(Protocol):
    """在文本中查找引文。"""

    version: str

    def find(self, quote: str, text: str, *, allow_fuzzy: bool = False) -> list[QuoteMatch]:
        """按等级依次尝试 EXACT → NORMALIZED →（可选）FUZZY，返回**最高命中等级**的全部命中，
        按 start 升序。未命中返回空列表。FUZZY 结果只用于诊断与重试反馈。"""
        ...


@runtime_checkable
class ValueNormalizer(Protocol):
    """值类型系统：原文表面形式 ⇄ 规范化值（规则见 ARCHITECTURE.md §6.3）。"""

    version: str

    def parse_value(self, value_text: str, spec: KeySpec) -> NormalizedValue:
        """把模型给出的值原文解析为规范化值；失败抛 ValueParseError。"""
        ...

    def find_mentions(self, text: str, spec: KeySpec) -> list[ValueMention]:
        """在一段文本（通常是引文所在区间）中识别该类型的全部值提及，返回原文坐标。
        例如 MONEY 会识别 '¥1,200.00'、'人民币壹仟贰佰元整'、'0.12万元'。"""
        ...

    def equivalent(self, a: NormalizedValue, b: NormalizedValue, spec: KeySpec) -> bool:
        """规范化值相等判定（Decimal 数值相等、日期 iso+precision 相等、文本归一化后相等）。"""
        ...


@runtime_checkable
class Localizer(Protocol):
    """把块内字符区间映射为原生定位。PDF 依赖 Block.fragments，无片段时退化为块框（BLOCK 精度）。"""

    version: str

    def locate(self, block: Block, start: int, end: int) -> tuple[Locator, LocatorPrecision]:
        ...


# ---------------------------------------------------------------------------
# 校验与合并
# ---------------------------------------------------------------------------


@runtime_checkable
class Verifier(Protocol):
    """把任一层的 Candidate 校验为 ExtractionRecord（算法见 ARCHITECTURE.md §6.2）。

    必须是确定性的纯函数；不调用模型。可配置项（review_possible_miss、include_hidden_cells、
    跨块拼接上限、是否启用 fuzzy 诊断）通过实现类的构造参数传入，不进入方法签名。对 derivation 的处理：
    M1 遇到 derivation 时产出 needs_review + DERIVATION_UNSUPPORTED；M3 交给 DerivationEngine。
    """

    version: str

    def verify(self, candidate: Candidate, doc: ParsedDocument, spec: KeySpec) -> ExtractionRecord:
        ...


@runtime_checkable
class Resolver(Protocol):
    """把同一 (文档, 键) 的多条记录（来自多个窗口、多次尝试或多层）合并为一条。

    M1 实现 SimpleMerger：同值合并证据；不同 grounded 值 ⇒ needs_review + CONFLICTING_CANDIDATES，
    其余值放入 alternatives；全部 not_found ⇒ not_found。M3 用决策层实现替换。
    """

    version: str

    def resolve(self, doc_id: str, spec: KeySpec, records: Sequence[ExtractionRecord]) -> ExtractionRecord:
        ...


# ---------------------------------------------------------------------------
# 上下文规划与提示词
# ---------------------------------------------------------------------------


@runtime_checkable
class TokenCounter(Protocol):
    """估算 token 数。默认启发式（保守高估），可替换为真实 tokenizer。"""

    name: str

    def count(self, text: str) -> int:
        ...


@runtime_checkable
class ContextPlanner(Protocol):
    """决定每次调用送哪些块、问哪些键。

    文档渲染后不超过预算 ⇒ 单窗口问全部键；否则按键检索相关块（字符二元组 BM25 + 别名 +
    邻块 + 表头），把共享块的键分组成多个窗口。窗口内块保持文档顺序。
    """

    version: str

    def plan(self, doc: ParsedDocument, keys: Sequence[KeySpec], *, budget_tokens: int) -> list[ContextWindow]:
        ...


@runtime_checkable
class PromptRenderer(Protocol):
    """把窗口渲染为聊天消息与输出 JSON Schema。模板文件版本化，sha256 记入清单。"""

    template_id: str
    template_sha256: str

    def render_extraction(self, doc: ParsedDocument, window: ContextWindow, keys: Sequence[KeySpec], config: RunConfig) -> PromptBundle:
        ...

    def render_retry(
        self,
        doc: ParsedDocument,
        window: ContextWindow,
        keys: Sequence[KeySpec],
        failed: Sequence[ExtractionRecord],
        previous: PromptBundle,
        previous_output: str,
        config: RunConfig,
    ) -> PromptBundle:
        """只针对失败的键重问，并附上结构化错误（IssueCode + 简短说明）。"""
        ...

    def output_schema(self, keys: Sequence[KeySpec], block_ids: Sequence[str] | None) -> dict[str, Any]:
        """返回 LLMExtractionOutput 的 JSON Schema；key 约束为所问键的枚举；
        block_ids 非空时把 evidence.block_id 约束为该枚举（RunConfig.constrain_block_ids）。"""
        ...


# ---------------------------------------------------------------------------
# 模型后端
# ---------------------------------------------------------------------------


@runtime_checkable
class ExtractionBackend(Protocol):
    """OpenAI 兼容 chat completion 后端（llama.cpp server / Ollama / vLLM），以及测试用 fake/oracle。

    职责：一次 HTTP 调用 + 传输层重试（429/5xx/超时，指数退避，次数见 RunConfig）。
    不负责解析 JSON、不负责语义重试。必须拒绝非 localhost 的 base_url，除非 allow_remote_backend。
    """

    backend_id: str

    def info(self) -> BackendInfo:
        ...

    async def chat(self, request: ChatRequest) -> ChatResponse:
        """失败抛 BackendError。"""
        ...


@runtime_checkable
class ResponseCache(Protocol):
    """以 request_sha256 为键的响应缓存（内容含正文，目录必须被 git 忽略）。replay_only 模式只读。"""

    def get(self, request_sha256: str) -> ChatResponse | None:
        ...

    def put(self, request_sha256: str, response: ChatResponse) -> None:
        ...


@runtime_checkable
class CandidateProducer(Protocol):
    """一个抽取层。M1：LLMCandidateProducer；M2：TemplateProducer；M3：DecisionProducer。"""

    producer_id: str
    method: ExtractionMethod

    async def produce(self, doc: ParsedDocument, keys: Sequence[KeySpec], config: RunConfig) -> ProducerOutput:
        """返回该层对该文档的全部候选（同一键可多条，含各次尝试）与审计记录。

        LLM 层内部会调用 Verifier 来决定是否语义重试；Pipeline 之后对全部候选
        再统一校验一次（纯函数，结果一致），再交给 Resolver。实现不得持有跨文档的可变状态。
        """
        ...


# ---------------------------------------------------------------------------
# 预留：M2 模板层、M3 决策层
# ---------------------------------------------------------------------------


@runtime_checkable
class TemplateInducer(Protocol):
    """（M2）从同模板文档及其已 grounded 的结果归纳锚点规则。"""

    version: str

    def induce(self, docs: Sequence[ParsedDocument], results: Sequence[DocumentResult], keyset: KeySet) -> TemplateSpec:
        ...

    def match(self, doc: ParsedDocument, templates: Sequence[TemplateSpec]) -> TemplateSpec | None:
        """按 structure_fingerprint 与锚点命中率选择模板；不匹配返回 None（回退第 2 层）。"""
        ...


@runtime_checkable
class DerivationEngine(Protocol):
    """（M3）确定性运算引擎：重算 DerivationClaim。"""

    version: str

    def supports(self, claim: DerivationClaim, spec: KeySpec) -> bool:
        ...

    def compute(self, claim: DerivationClaim, operand_values: Sequence[NormalizedValue], spec: KeySpec) -> Derivation:
        ...


@runtime_checkable
class ConfidenceGate(Protocol):
    """（M3）置信度门控：接受 / 升级到推理型 LLM / 人工复核。"""

    version: str

    def decide(self, record: ExtractionRecord, spec: KeySpec) -> GateDecision:
        ...


# ---------------------------------------------------------------------------
# 编排、合成数据、评测
# ---------------------------------------------------------------------------


@runtime_checkable
class Pipeline(Protocol):
    """端到端编排：解析 → 各层产出候选 → 校验 → 语义重试 → 合并 → 清单。

    输出顺序确定：文档按输入顺序，记录按 KeySet 顺序，与并发完成顺序无关。
    """

    async def run(self, paths: Sequence[Path], keyset: KeySet, config: RunConfig) -> RunResult:
        ...


@runtime_checkable
class SyntheticGenerator(Protocol):
    """合成文档生成器：同时产出文件与金标准（含与解析器无关的原生位置）。"""

    version: str

    def generate(self, config: GenerationConfig, out_dir: Path) -> Iterator[SyntheticSample]:
        """同一 config（含 seed）⇒ 相同的文件内容与金标准（PDF 允许元数据时间戳除外，须固定或剥离）。"""
        ...


@runtime_checkable
class Evaluator(Protocol):
    """按 (doc_sha256, key) 对齐预测与金标准，计算 ARCHITECTURE.md §9 定义的指标。"""

    version: str

    def evaluate(
        self,
        result: RunResult,
        gold: Sequence[GoldDocument],
        *,
        parsed: Mapping[str, ParsedDocument] | None = None,
        injections: Sequence[InjectedError] = (),
    ) -> EvalReport:
        """``parsed``（doc_id → ParsedDocument）用于把预测证据换算为原生位置后与金标准比对；
        ``injections`` 非空时额外计算校验器按错误类型的拦截率。"""
        ...


__all__ = [
    "BackendError",
    "CandidateProducer",
    "ConfidenceGate",
    "ContextPlanner",
    "DerivationEngine",
    "Evaluator",
    "ExtractionBackend",
    "Localizer",
    "Parser",
    "ParserRegistry",
    "PdfBackend",
    "PdfLayout",
    "Pipeline",
    "PromptRenderer",
    "QuoteMatcher",
    "Resolver",
    "ResponseCache",
    "SyntheticGenerator",
    "TemplateInducer",
    "TextNormalizer",
    "TokenCounter",
    "UnsupportedDocumentError",
    "ValueNormalizer",
    "ValueParseError",
    "Verifier",
]
