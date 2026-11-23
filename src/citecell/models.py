"""CiteCell 数据契约（contract version 见 ``CONTRACT_VERSION``）。

本文件是各模块之间唯一的数据交换格式定义。约定：

* 只依赖标准库与 pydantic v2，**不得** import PyMuPDF / openpyxl / python-docx。
* 修改任何字段的语义或删除字段都属于破坏性变更，必须提升 ``CONTRACT_VERSION``
  的主版本并经 Sonnet-Controller 复核；新增**可选**字段只提升次版本。
* 索引命名规则：``*_index`` 一律 0 起；``*_number`` 一律 1 起（与人类/PDF.js/Excel 一致）。
* 所有字符偏移（``char_start``/``char_end``/``start``/``end``）均指 Python ``str``
  的 Unicode 码点下标，半开区间 ``[start, end)``，相对于 ``Block.text``。
  前端（JS 使用 UTF-16）需自行换算。
* 金额/数值一律使用 ``Decimal``，JSON 序列化为字符串，禁止 float。
* 契约对象中不保存绝对路径（隐私），路径只在运行时以 ``pathlib.Path`` 传递。

模型分两类：

* ``_Contract``：系统内部产物，``extra="forbid"``，字段拼写错误会直接报错。
* ``_Wire``：模型（LLM）直接输出的线格式，``extra="ignore"``，容忍模型多给字段。
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal, Union

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

CONTRACT_VERSION = "0.1.0"

# ---------------------------------------------------------------------------
# 基础类型与常量
# ---------------------------------------------------------------------------

Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
"""完整 SHA-256 十六进制小写串。"""

DocId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{16}$")]
"""文档 ID = 文件字节 SHA-256 的前 16 个十六进制字符。同一文件在任何运行中 ID 相同。"""

#: 块局部 ID 正则（同一文档内唯一）。结构仅供人读与提示词使用，
#: **代码不得解析 block_id 来获取位置**，位置一律读 ``Block.locator``。
#:   PDF  : p{page_number}-b{k}          例 p3-b12（第 3 页阅读顺序第 12 块）
#:   DOCX : para{n}                      例 para17（正文第 17 个顶层段落）
#:          t{m}-r{r}-c{c}               例 t2-r3-c1（第 2 个表格第 3 行第 1 列，均 1 起）
#:   XLSX : s{sheet_number}-{A1}         例 s1-B3（合并单元格用左上角地址）
BLOCK_ID_PATTERN = r"^(p[1-9]\d*-b[1-9]\d*|para[1-9]\d*|t[1-9]\d*-r[1-9]\d*-c[1-9]\d*|s[1-9]\d*-[A-Z]{1,3}[1-9]\d*)$"
BlockId = Annotated[str, StringConstraints(pattern=BLOCK_ID_PATTERN)]

A1Address = Annotated[str, StringConstraints(pattern=r"^[A-Z]{1,3}[1-9]\d*$")]
A1Range = Annotated[str, StringConstraints(pattern=r"^[A-Z]{1,3}[1-9]\d*:[A-Z]{1,3}[1-9]\d*$")]

ExactDecimal = Annotated[Decimal, Field(allow_inf_nan=False)]
"""有限 Decimal；JSON 中序列化为字符串以保证精确。"""

ParamValue = Union[str, int, bool]
"""通用参数值。数值型参数如需精确请用字符串形式的十进制数。"""

_EXCEL_MAX_ROW = 1_048_576
_EXCEL_MAX_COL = 16_384
_A1_RE = re.compile(r"^([A-Z]{1,3})([1-9][0-9]*)$")


def a1_to_row_col(a1: str) -> tuple[int, int]:
    """把 A1 地址转换为 (row_number, column_number)，均 1 起。

    >>> a1_to_row_col("B3")
    (3, 2)
    """
    m = _A1_RE.match(a1)
    if m is None:
        raise ValueError(f"非法 A1 地址: {a1!r}")
    letters, digits = m.groups()
    col = 0
    for ch in letters:
        col = col * 26 + (ord(ch) - 64)
    row = int(digits)
    if row > _EXCEL_MAX_ROW or col > _EXCEL_MAX_COL:
        raise ValueError(f"A1 地址超出 Excel 范围: {a1!r}")
    return row, col


class _Contract(BaseModel):
    """内部契约基类：禁止未知字段，赋值时也校验。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True, use_enum_values=False)


class _Wire(BaseModel):
    """LLM 线格式基类：忽略未知字段（模型可能多输出），不做字符串裁剪（引文空白有意义）。"""

    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class DocFormat(StrEnum):
    """支持的文档格式。M1 仅文字型 PDF；DOCX 仅正文段落与表格；XLSX 全部可见工作表。"""

    PDF = "pdf"
    DOCX = "docx"
    XLSX = "xlsx"


class BlockKind(StrEnum):
    """块类型。表格单元格无论来自 DOCX 还是 XLSX 都是 ``table_cell``。"""

    PARAGRAPH = "paragraph"
    HEADING = "heading"
    LIST_ITEM = "list_item"
    TABLE_CELL = "table_cell"
    OTHER = "other"


class BlockFlag(StrEnum):
    """块级标记，提示解析时的特殊情况。"""

    HIDDEN = "hidden"
    """XLSX 隐藏行/列中的单元格（默认仍纳入，但证据会被标注）。"""
    FORMULA = "formula"
    """XLSX 公式单元格；text 为缓存值，公式见 locator.formula。"""
    FORMULA_NO_CACHED_VALUE = "formula_no_cached_value"
    """公式单元格没有缓存值（例如由 openpyxl 生成且未经 Excel/LibreOffice 重算），text 为空串。"""
    MERGED = "merged"
    """合并单元格；locator.cell_range 给出范围。"""
    DATE_FROM_SERIAL = "date_from_serial"
    """XLSX 日期单元格，text 为 ISO 渲染，原始序列号见 locator.raw_value。"""
    TEXT_LAYER_SUSPECT = "text_layer_suspect"
    """PDF 文本层可疑（大量私有区/替换字符等），抽取结果不可信。"""
    NESTED_TABLE_FLATTENED = "nested_table_flattened"
    """DOCX 嵌套表格被按行优先拼接进外层单元格文本。"""
    SPLIT_FROM_LONG_BLOCK = "split_from_long_block"
    """超长段落按句切分得到的子块。"""


class LocatorPrecision(StrEnum):
    """证据定位精度，前端据此决定高亮方式。"""

    CHAR = "char"
    """精确到字符（PDF 有逐字几何；DOCX/XLSX 有字符区间）。"""
    LINE = "line"
    """PDF 只有行/span 级几何，高亮整行片段。"""
    BLOCK = "block"
    """只能高亮整块（几何缺失时的兜底）。"""
    CELL = "cell"
    """XLSX/DOCX 表格单元格整格高亮。"""


class MatchLevel(StrEnum):
    """引文与块文本的匹配等级。只有 EXACT / NORMALIZED 可以支撑 grounded。"""

    EXACT = "exact"
    """逐字子串（仅去除引文首尾空白与渲染噪声）。"""
    NORMALIZED = "normalized"
    """经 NORMALIZER_V1（逐码点 NFKC、删空白、标点归一、拉丁小写）后为子串。"""
    FUZZY = "fuzzy"
    """仅近似匹配（编辑距离），**只告警，永不算 grounded**。"""
    NONE = "none"
    """未找到。"""


class ValueType(StrEnum):
    """键的值类型。M1 实现 text/number/money/percent/date/enum；boolean 仅当原文含别名时可 grounded。"""

    TEXT = "text"
    NUMBER = "number"
    MONEY = "money"
    PERCENT = "percent"
    DATE = "date"
    ENUM = "enum"
    BOOLEAN = "boolean"


class Status(StrEnum):
    """单元格（文档×键）的机器状态。与人工复核状态分离（见 ``Correction``）。

    注意 ``grounded`` 的含义是"值可由存在于原文中的引文确定性导出"，
    **不等于值正确**——正确性只能由评测（金标准）或人工复核判断。
    """

    GROUNDED = "grounded"
    DERIVED = "derived"
    """（M3）值由已 grounded 的操作数经程序重算得到且与模型声明一致。"""
    NOT_FOUND = "not_found"
    """模型声明文档中没有该键。这是不可证伪的声明，评测单独统计。"""
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"
    """基础设施失败（后端超时、输出始终不合法、文档不受支持），与内容判断无关。"""


class ExtractionMethod(StrEnum):
    """候选值来自哪一层。所有层产出同一种 ``Candidate``，走同一个 Verifier。"""

    TEMPLATE = "template"
    """第 1 层：模板/锚点规则（M2）。"""
    LLM = "llm"
    """第 2 层：小模型抽取（M1）。"""
    DECISION = "decision"
    """第 3 层：决策层（判别模型/推理型 LLM，M3）。"""
    HUMAN = "human"
    """人工修正。"""


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class IssueCode(StrEnum):
    """问题代码。既用于记录，也用于重试时给模型的结构化反馈。"""

    # --- 引文/定位 ---
    BLOCK_NOT_FOUND = "block_not_found"
    QUOTE_NOT_IN_BLOCK = "quote_not_in_block"
    QUOTE_FUZZY_ONLY = "quote_fuzzy_only"
    QUOTE_RELOCATED = "quote_relocated"
    QUOTE_CROSS_BLOCK = "quote_cross_block"
    QUOTE_AMBIGUOUS = "quote_ambiguous"
    RENDER_NOISE_STRIPPED = "render_noise_stripped"
    # --- 值 ---
    VALUE_NOT_IN_QUOTE = "value_not_in_quote"
    VALUE_UNPARSEABLE = "value_unparseable"
    ENUM_OUT_OF_RANGE = "enum_out_of_range"
    UNIT_MISMATCH = "unit_mismatch"
    EMPTY_EVIDENCE_WITH_VALUE = "empty_evidence_with_value"
    EVIDENCE_WITHOUT_VALUE = "evidence_without_value"
    CARDINALITY_UNSUPPORTED = "cardinality_unsupported"
    # --- 合并/状态 ---
    CONFLICTING_CANDIDATES = "conflicting_candidates"
    POSSIBLE_MISS = "possible_miss"
    HIDDEN_SOURCE = "hidden_source"
    FORMULA_NO_CACHED_VALUE = "formula_no_cached_value"
    KEY_MISSING_IN_OUTPUT = "key_missing_in_output"
    CONTEXT_TRUNCATED = "context_truncated"
    # --- 派生（M3） ---
    DERIVATION_MISMATCH = "derivation_mismatch"
    DERIVATION_UNSUPPORTED = "derivation_unsupported"
    DERIVATION_INCOMPLETE = "derivation_incomplete"
    # --- 基础设施 ---
    OUTPUT_SCHEMA_INVALID = "output_schema_invalid"
    BACKEND_ERROR = "backend_error"
    BACKEND_TIMEOUT = "backend_timeout"
    UNSUPPORTED_DOCUMENT = "unsupported_document"
    TEXT_LAYER_UNRELIABLE = "text_layer_unreliable"


class EvidenceFlag(StrEnum):
    """证据级标记。"""

    VALUE_BEARING = "value_bearing"
    """该证据包含值本身（value_char_start/end 已设置）。"""
    RELOCATED = "relocated"
    """模型给的 block_id 错误，引文在别的块中唯一命中，已改正（claimed_block_id 保留原值）。"""
    CROSS_BLOCK_SPLIT = "cross_block_split"
    """引文跨越相邻块，已被拆为多条证据。"""
    AMBIGUOUS_OCCURRENCE = "ambiguous_occurrence"
    """引文在块内出现多次，取第一次。"""
    HIDDEN_SOURCE = "hidden_source"
    """来自 XLSX 隐藏行/列。"""
    RENDER_NOISE_STRIPPED = "render_noise_stripped"
    """引文中混入了提示词渲染的块编号/表头前缀，已剥离后再匹配。"""


# ---------------------------------------------------------------------------
# 几何与定位
# ---------------------------------------------------------------------------


class BBox(_Contract):
    """PDF 坐标框，单位为 PDF 点（1/72 英寸）。

    坐标系约定 PDF_COORDS_V1：以页面 **CropBox** 左上角为原点，x 向右、y 向下，
    按未旋转（/Rotate=0）的页面方向表示。各 PDF 后端适配器负责把自身坐标
    （pdfminer.six / PDFium 通常为左下原点）换算到此约定。M1 只支持 /Rotate=0 的页面，
    其他旋转角度的页面会产生文档级警告（见 ARCHITECTURE.md §5.2）。
    """

    x0: float
    y0: float
    x1: float
    y1: float

    @model_validator(mode="after")
    def _check_order(self) -> BBox:
        if self.x1 < self.x0 or self.y1 < self.y0:
            raise ValueError("bbox 需满足 x0<=x1 且 y0<=y1")
        return self


class TextFragment(_Contract):
    """PDF 块文本的一个几何片段：``Block.text[start:end]`` 位于 ``bbox``。

    解析器可以按字符（推荐，精度 CHAR）或按 span 输出（精度 LINE）。
    行间拼接时插入的分隔符（空格）不对应任何片段。
    """

    start: int = Field(ge=0)
    end: int = Field(ge=1)
    page_number: int = Field(ge=1)
    line: int = Field(ge=0, description="块内行号（0 起），用于把片段合并为逐行高亮框")
    bbox: BBox

    @model_validator(mode="after")
    def _check_span(self) -> TextFragment:
        if self.end <= self.start:
            raise ValueError("fragment 需满足 start < end")
        return self


class PdfLocator(_Contract):
    """PDF 定位：单页上的若干矩形。跨页证据必须拆为多条 Evidence。"""

    format: Literal["pdf"] = "pdf"
    page_number: int = Field(ge=1)
    page_width: float = Field(gt=0, description="页面宽度（点），前端用于坐标换算")
    page_height: float = Field(gt=0, description="页面高度（点）")
    regions: list[BBox] = Field(min_length=1, description="块级为块框；证据级为逐行合并后的框")


class DocxLocator(_Contract):
    """DOCX 定位。正文段落给 ``paragraph_number``；表格单元格给 table/row/col。

    ``body_index`` 为正文顶层元素（段落与表格交错）的文档顺序下标，用于还原阅读顺序。
    字符区间由 Evidence.char_start/char_end 给出，不放在 locator 中。
    """

    format: Literal["docx"] = "docx"
    body_index: int = Field(ge=0)
    paragraph_number: int | None = Field(default=None, ge=1, description="正文顶层段落序号（1 起）")
    table_number: int | None = Field(default=None, ge=1)
    row_index: int | None = Field(default=None, ge=0)
    col_index: int | None = Field(default=None, ge=0)
    row_span: int = Field(default=1, ge=1)
    col_span: int = Field(default=1, ge=1)
    style_name: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _check_container(self) -> DocxLocator:
        in_table = self.table_number is not None
        cell_fields = (self.row_index, self.col_index)
        if in_table:
            if any(v is None for v in cell_fields) or self.paragraph_number is not None:
                raise ValueError("表格单元格需同时给出 table_number/row_index/col_index，且不给 paragraph_number")
        else:
            if self.paragraph_number is None or any(v is not None for v in cell_fields):
                raise ValueError("正文段落需给出 paragraph_number，且不给 row_index/col_index")
        return self


class XlsxLocator(_Contract):
    """XLSX 定位：工作表 + 单元格。

    约定（详见 ARCHITECTURE.md §5.4）：
    * 合并单元格：值在左上角，``cell`` 为左上角，``cell_range`` 为合并范围。
    * 公式单元格：``formula`` 保存公式文本（如 ``=SUM(B2:B9)``），Block.text 为缓存值。
    * 日期：Block.text 为 ISO 渲染，``raw_value`` 保存解析器读到的原始值的 repr。
    * ``number_format`` 保存 Excel 数字格式字符串，用于审计 text 渲染。
    """

    format: Literal["xlsx"] = "xlsx"
    sheet_index: int = Field(ge=0)
    sheet_name: str = Field(min_length=1, max_length=255)
    cell: A1Address
    cell_range: A1Range | None = None
    row_number: int = Field(ge=1, le=_EXCEL_MAX_ROW)
    column_number: int = Field(ge=1, le=_EXCEL_MAX_COL)
    hidden: bool = False
    formula: str | None = None
    number_format: str | None = None
    raw_value: str | None = None

    @model_validator(mode="after")
    def _check_address(self) -> XlsxLocator:
        if a1_to_row_col(self.cell) != (self.row_number, self.column_number):
            raise ValueError("cell 与 row_number/column_number 不一致")
        if self.cell_range is not None and not self.cell_range.startswith(self.cell + ":"):
            raise ValueError("cell_range 必须以左上角 cell 开头")
        return self


Locator = Annotated[Union[PdfLocator, DocxLocator, XlsxLocator], Field(discriminator="format")]
"""按 ``format`` 区分的定位联合类型。"""


# ---------------------------------------------------------------------------
# 文档与块
# ---------------------------------------------------------------------------


class TableContext(_Contract):
    """表格单元格的行列语义，供提示词渲染与检索使用（不参与定位）。"""

    table_id: str = Field(min_length=1, max_length=32, description="DOCX 为 t{m}，XLSX 为 s{n}")
    row_index: int = Field(ge=0, description="在表格（或工作表已用区域）内的行下标")
    col_index: int = Field(ge=0)
    column_headers: list[str] = Field(default_factory=list, description="自上而下的列表头文本（多级表头）")
    row_headers: list[str] = Field(default_factory=list, description="自左而右的行表头文本")
    is_header: bool = False


class Block(_Contract):
    """统一的最小可引用文本单元。所有格式都解析为 Block 序列。

    * ``text`` 是该块的规范文本，所有偏移都相对它；一旦生成不可修改（frozen）。
    * PDF 行拼接规则 LINE_JOIN_V1：两侧均为 CJK 字符或 CJK 标点时直接相连，
      否则插入一个半角空格；插入的空格不对应 TextFragment。
    * 超长块（> 解析器配置的上限，默认 800 码点）按句末标点切分，标记 SPLIT_FROM_LONG_BLOCK。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    doc_id: DocId
    block_id: BlockId
    kind: BlockKind
    text: str = Field(description="可为空串（例如无缓存值的公式单元格）")
    order: int = Field(ge=0, description="文档内阅读顺序下标，等于其在 ParsedDocument.blocks 中的位置")
    section_path: list[str] = Field(default_factory=list, description="所属标题路径，自顶向下")
    table: TableContext | None = None
    locator: Locator
    fragments: list[TextFragment] = Field(default_factory=list, description="仅 PDF：字符区间到几何的映射")
    flags: list[BlockFlag] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_block(self) -> Block:
        if (self.kind == BlockKind.TABLE_CELL) != (self.table is not None):
            raise ValueError("kind=table_cell 当且仅当 table 不为空")
        if self.fragments:
            if not isinstance(self.locator, PdfLocator):
                raise ValueError("fragments 仅允许出现在 PDF 块中")
            n = len(self.text)
            for f in self.fragments:
                if f.end > n:
                    raise ValueError("fragment 超出 text 范围")
        return self


class DocumentInfo(_Contract):
    """文档元数据。``display_name`` 默认为 None（隐私），仅在用户显式允许时记录文件名。"""

    doc_id: DocId
    sha256: Sha256Hex
    format: DocFormat
    size_bytes: int = Field(ge=0)
    display_name: str | None = Field(default=None, max_length=255)
    page_count: int | None = Field(default=None, ge=0, description="PDF 页数")
    sheet_names: list[str] | None = Field(default=None, description="XLSX 工作表名（按顺序）")
    parser_name: str
    parser_version: str
    warnings: list[str] = Field(default_factory=list, description="文档级警告（不含正文内容）")

    @model_validator(mode="after")
    def _check_id(self) -> DocumentInfo:
        if not self.sha256.startswith(self.doc_id):
            raise ValueError("doc_id 必须是 sha256 的前 16 位")
        return self


class ParsedDocument(_Contract):
    """解析器输出。包含正文，属于敏感数据，只写入被 .gitignore 忽略的运行目录。"""

    info: DocumentInfo
    blocks: list[Block]
    structure_fingerprint: str | None = Field(
        default=None,
        description="（M2 预留）版式指纹，用于模板匹配；M1 解析器可不填",
    )

    @model_validator(mode="after")
    def _check_blocks(self) -> ParsedDocument:
        seen: set[str] = set()
        for i, b in enumerate(self.blocks):
            if b.doc_id != self.info.doc_id:
                raise ValueError(f"块 {b.block_id} 的 doc_id 与文档不一致")
            if b.order != i:
                raise ValueError(f"块 {b.block_id} 的 order={b.order} 与位置 {i} 不一致")
            if b.block_id in seen:
                raise ValueError(f"重复的 block_id: {b.block_id}")
            seen.add(b.block_id)
        return self

    def block_map(self) -> dict[str, Block]:
        """block_id -> Block 的查找表。"""
        return {b.block_id: b for b in self.blocks}


# ---------------------------------------------------------------------------
# 文本归一化与匹配（运行时轻量结构，不序列化）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NormalizedText:
    """偏移保持的归一化结果。

    ``index_map[i]`` 是归一化文本第 i 个码点对应的原文码点下标；
    一个原文码点可产生 0 个（被删除）或多个（NFKC 展开，如 ㎡→m2）归一化码点。
    """

    original: str
    text: str
    index_map: tuple[int, ...]

    def to_original_span(self, start: int, end: int) -> tuple[int, int]:
        """把归一化文本上的半开区间映射回原文半开区间。要求 0 <= start < end <= len(text)。"""
        if not (0 <= start < end <= len(self.text)):
            raise ValueError("非法归一化区间")
        return self.index_map[start], self.index_map[end - 1] + 1


@dataclass(frozen=True, slots=True)
class PdfPageInfo:
    """PDF 后端报告的页面信息（PDF_COORDS_V1 下的 CropBox 尺寸）。"""

    page_number: int
    width: float
    height: float
    rotation: int


@dataclass(frozen=True, slots=True)
class PdfChar:
    """PDF 后端输出的一个字符及其几何（PDF_COORDS_V1）。

    后端只负责产出字符流；分行、分块、拼接全部由与后端无关的版面模块完成，
    因此更换后端（pypdfium2 / pdfminer.six / 可选的 PyMuPDF）不改变分块算法。
    ``generated=True`` 表示后端自行插入的虚拟字符（空格/换行），版面模块应忽略其几何。
    """

    text: str
    bbox: BBox
    font_size: float | None = None
    font_name: str | None = None
    generated: bool = False


@dataclass(frozen=True, slots=True)
class QuoteMatch:
    """引文在某段文本中的一次命中（原文坐标）。score 仅对 FUZZY 有意义（0~1）。"""

    level: MatchLevel
    start: int
    end: int
    score: float = 1.0


# ---------------------------------------------------------------------------
# 键与规范化值
# ---------------------------------------------------------------------------


class EnumOption(_Contract):
    """枚举选项：规范值 + 原文中可能出现的别名。BOOLEAN 键用 value='true'/'false'。"""

    value: str = Field(min_length=1, max_length=100)
    aliases: list[str] = Field(default_factory=list)


class KeySpec(_Contract):
    """用户定义的一个待抽取键。"""

    key: str = Field(min_length=1, max_length=64, pattern=r"^\S(.*\S)?$", description="键名，可为中文，不含首尾空白")
    description: str | None = Field(default=None, max_length=500, description="给模型看的说明")
    value_type: ValueType = ValueType.TEXT
    aliases: list[str] = Field(default_factory=list, description="同义标签，用于检索与 POSSIBLE_MISS 检查")
    enum_options: list[EnumOption] | None = None
    unit: str | None = Field(default=None, max_length=20, description="NUMBER 的期望单位，M1 只比对不换算")
    currency: str = Field(default="CNY", pattern=r"^[A-Z]{3}$")
    cardinality: Literal["one", "many"] = Field(default="one", description="M1 仅支持 one")
    allow_derivation: bool = Field(default=False, description="（M3）是否允许由运算得到")

    @model_validator(mode="after")
    def _check_options(self) -> KeySpec:
        if self.value_type == ValueType.ENUM and not self.enum_options:
            raise ValueError("ENUM 键必须提供 enum_options")
        if self.value_type not in (ValueType.ENUM, ValueType.BOOLEAN) and self.enum_options:
            raise ValueError("只有 ENUM/BOOLEAN 键可以提供 enum_options")
        if self.value_type == ValueType.BOOLEAN and self.enum_options:
            if not {o.value for o in self.enum_options} <= {"true", "false"}:
                raise ValueError("BOOLEAN 键的 enum_options.value 只能是 'true'/'false'")
        return self


class KeySet(_Contract):
    """一组键。其规范 JSON 的 SHA-256 记入 RunManifest。"""

    name: str = Field(min_length=1, max_length=100)
    version: str = Field(default="1", max_length=32)
    keys: list[KeySpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> KeySet:
        names = [k.key for k in self.keys]
        if len(names) != len(set(names)):
            raise ValueError("KeySet 中键名重复")
        return self


class TextValue(_Contract):
    type: Literal["text"] = "text"
    text: str = Field(min_length=1)


class NumberValue(_Contract):
    type: Literal["number"] = "number"
    value: ExactDecimal
    unit: str | None = None


class MoneyValue(_Contract):
    """金额，统一换算到基本单位（元），例如"120万元"→ amount=1200000。"""

    type: Literal["money"] = "money"
    amount: ExactDecimal
    currency: str = Field(default="CNY", pattern=r"^[A-Z]{3}$")


class PercentValue(_Contract):
    """百分比，``value=15`` 表示 15%（不是 0.15）。"""

    type: Literal["percent"] = "percent"
    value: ExactDecimal


class DateValue(_Contract):
    """日期（不含时间）。支持部分日期：precision=year → 'YYYY'，month → 'YYYY-MM'。"""

    type: Literal["date"] = "date"
    iso: str = Field(pattern=r"^\d{4}(-\d{2}(-\d{2})?)?$")
    precision: Literal["year", "month", "day"]

    @model_validator(mode="after")
    def _check_date(self) -> DateValue:
        parts = self.iso.split("-")
        expected = {"year": 1, "month": 2, "day": 3}[self.precision]
        if len(parts) != expected:
            raise ValueError("iso 与 precision 不一致")
        y = int(parts[0])
        m = int(parts[1]) if expected >= 2 else 1
        d = int(parts[2]) if expected == 3 else 1
        _dt.date(y, m, d)  # 非法日期抛 ValueError
        return self


class EnumValue(_Contract):
    type: Literal["enum"] = "enum"
    value: str = Field(min_length=1)


class BooleanValue(_Contract):
    type: Literal["boolean"] = "boolean"
    value: bool


NormalizedValue = Annotated[
    Union[TextValue, NumberValue, MoneyValue, PercentValue, DateValue, EnumValue, BooleanValue],
    Field(discriminator="type"),
]
"""按 ``type`` 区分的规范化值。比较一律在规范化值上进行。"""


@dataclass(frozen=True, slots=True)
class ValueMention:
    """在一段文本中识别出的一个值提及（原文坐标），由 ValueNormalizer.find_mentions 产生。"""

    value: Any  # NormalizedValue 实例；dataclass 中不展开联合类型以免运行时开销
    start: int
    end: int
    surface: str


# ---------------------------------------------------------------------------
# 证据、候选、派生
# ---------------------------------------------------------------------------


class EvidenceClaim(_Wire):
    """模型（或模板/人工）声明的一条证据：块 ID + 逐字引文。"""

    block_id: str = Field(max_length=64, description="必须是提示词中出现过的块编号")
    quote: str = Field(min_length=1, max_length=1000, description="从该块正文逐字复制的片段，应包含值")


class DerivationOp(StrEnum):
    """（M3）派生运算集合。每个运算都必须能由程序确定性重算。"""

    SUM = "sum"
    DIFFERENCE = "difference"
    PRODUCT = "product"
    RATIO = "ratio"
    COUNT = "count"
    AVERAGE = "average"
    MIN = "min"
    MAX = "max"
    DATE_DIFF = "date_diff"
    """params: unit=day|month|year"""
    DATE_ADD = "date_add"
    COMPARE = "compare"
    """params: op=eq|ne|gt|ge|lt|le → BooleanValue"""
    CONVERT_UNIT = "convert_unit"
    ROUND = "round"
    SELECT = "select"
    """冲突归并：从多个候选中按 params.rule（latest|earliest|majority|source_priority）选择一个。"""


class DerivationOperand(_Wire):
    """运算的一个操作数：引用同一候选中 evidence 列表的下标，并给出该处的值原文。"""

    evidence_index: int = Field(ge=0)
    value_text: str = Field(min_length=1, max_length=200)


class DerivationClaim(_Wire):
    """（M3）模型声明的运算。M1 的线格式不包含此字段。"""

    op: DerivationOp
    operands: list[DerivationOperand] = Field(min_length=1)
    params: dict[str, ParamValue] = Field(default_factory=dict)


class Derivation(_Contract):
    """（M3）经程序重算后的派生记录。

    可验证边界：程序只能验证"给定这些操作数，运算结果正确"；
    操作数是否**完整**（例如求和是否漏项）一般不可验证，``completeness_checked`` 记录是否做过额外检查。
    """

    op: DerivationOp
    operands: list[DerivationOperand]
    params: dict[str, ParamValue] = Field(default_factory=dict)
    operand_values: list[NormalizedValue] = Field(description="各操作数的规范化值，与 operands 一一对应")
    recomputed: NormalizedValue | None = None
    matches_claim: bool = False
    completeness_checked: bool = False

    @model_validator(mode="after")
    def _check_len(self) -> Derivation:
        if len(self.operand_values) != len(self.operands):
            raise ValueError("operand_values 与 operands 长度不一致")
        return self


class Evidence(_Contract):
    """经 Verifier 校验后的证据。偏移相对于所在块的 ``Block.text``。"""

    doc_id: DocId
    block_id: str = Field(max_length=64, description="解析后的真实块 ID（可能经 RELOCATED 改正）")
    claimed_block_id: str | None = Field(default=None, max_length=64, description="模型原始给出的块 ID（仅当被改正时）")
    quote: str
    match_level: MatchLevel
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)
    value_char_start: int | None = Field(default=None, ge=0, description="值本身在块文本中的起点")
    value_char_end: int | None = Field(default=None, ge=0)
    match_score: float | None = Field(default=None, ge=0, le=1, description="仅 FUZZY 使用")
    locator: Locator | None = None
    precision: LocatorPrecision | None = None
    occurrences: int = Field(default=0, ge=0, description="引文在该块内的命中次数")
    flags: list[EvidenceFlag] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_evidence(self) -> Evidence:
        located = self.match_level in (MatchLevel.EXACT, MatchLevel.NORMALIZED)
        if located:
            if self.char_start is None or self.char_end is None or self.locator is None or self.precision is None:
                raise ValueError("exact/normalized 证据必须给出 char_start/char_end/locator/precision")
        if self.match_level == MatchLevel.NONE and (self.char_start is not None or self.locator is not None):
            raise ValueError("未命中的证据不得给出偏移或定位")
        if self.char_start is not None and self.char_end is not None and self.char_end <= self.char_start:
            raise ValueError("需满足 char_start < char_end")
        if (self.value_char_start is None) != (self.value_char_end is None):
            raise ValueError("value_char_start/end 必须同时给出")
        if self.value_char_start is not None:
            if self.char_start is None or self.char_end is None:
                raise ValueError("给出值区间前必须先定位引文")
            assert self.value_char_end is not None
            if not (self.char_start <= self.value_char_start < self.value_char_end <= self.char_end):
                raise ValueError("值区间必须落在引文区间内")
        return self

    @property
    def is_located(self) -> bool:
        return self.match_level in (MatchLevel.EXACT, MatchLevel.NORMALIZED)


class Candidate(_Contract):
    """任一层产出的未校验候选值。Verifier 把它变成 ExtractionRecord。"""

    doc_id: DocId
    key: str
    value_text: str | None = Field(default=None, max_length=1000, description="值的原文形式，应为引文的子串")
    evidence: list[EvidenceClaim] = Field(default_factory=list)
    derivation: DerivationClaim | None = None
    method: ExtractionMethod
    producer_id: str = Field(max_length=200, description="如 'llm:<backend_id>' 或 'template:<template_id>'")
    window_id: str | None = None
    attempt_id: str | None = None


class LLMFieldOutput(_Wire):
    """LLM 对单个键的输出。字段顺序即生成顺序：先 key，再 evidence，最后 value（先引文后给值）。"""

    key: str
    evidence: list[EvidenceClaim] = Field(default_factory=list)
    value: str | None = Field(default=None, description="文档中没有该键时为 null 且 evidence 为空")


class LLMExtractionOutput(_Wire):
    """LLM 对一个上下文窗口的完整输出（M1 线格式 v1）。"""

    fields: list[LLMFieldOutput]


# ---------------------------------------------------------------------------
# 结果记录
# ---------------------------------------------------------------------------


class Issue(_Contract):
    """一条问题。``message`` 不得包含正文（引文除外），以便日志脱敏。"""

    code: IssueCode
    severity: Severity
    message: str = Field(default="", max_length=500)
    evidence_index: int | None = Field(default=None, ge=0)


class AlternativeValue(_Contract):
    """未被采纳的候选（冲突值、被拒值），供复核界面与 M3 决策层使用。"""

    value_text: str | None
    value: NormalizedValue | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    producer_id: str = Field(max_length=200)


class ExtractionRecord(_Contract):
    """一个"文档×键"单元格的机器结果（权威 JSON 的核心）。

    状态不变量（由校验器强制）：
    * grounded    ⇒ value 非空；evidence 非空且全部 exact/normalized；至少一条带值区间；无 derivation。
    * derived     ⇒ value 非空；derivation 非空且 matches_claim=True。
    * not_found   ⇒ value 与 value_text 为空，evidence 为空。
    * needs_review⇒ 至少一条 warning/error 级 issue。
    * failed      ⇒ 至少一条 error 级 issue。
    """

    doc_id: DocId
    key: str
    status: Status
    value: NormalizedValue | None = None
    value_text: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    derivation: Derivation | None = None
    method: ExtractionMethod | None = None
    producer_id: str | None = Field(default=None, max_length=200)
    alternatives: list[AlternativeValue] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1, description="（M3）校准后的置信度")
    attempt_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_status(self) -> ExtractionRecord:
        s = self.status
        sev = {i.severity for i in self.issues}
        if s == Status.GROUNDED:
            if self.value is None or not self.evidence:
                raise ValueError("grounded 需要 value 与 evidence")
            if not all(e.is_located for e in self.evidence):
                raise ValueError("grounded 的所有证据必须为 exact/normalized（fuzzy 永不 grounded）")
            if not any(e.value_char_start is not None for e in self.evidence):
                raise ValueError("grounded 至少需要一条带值区间的证据")
            if self.derivation is not None:
                raise ValueError("grounded 不应带 derivation，请使用 derived")
            if self.method is None:
                raise ValueError("grounded 必须给出 method")
        elif s == Status.DERIVED:
            if self.value is None or self.derivation is None or not self.derivation.matches_claim:
                raise ValueError("derived 需要 value 与重算一致的 derivation")
        elif s == Status.NOT_FOUND:
            if self.value is not None or self.value_text is not None or self.evidence:
                raise ValueError("not_found 不得带值或证据")
        elif s == Status.NEEDS_REVIEW:
            if not (sev & {Severity.WARNING, Severity.ERROR}):
                raise ValueError("needs_review 至少需要一条 warning/error issue")
        elif s == Status.FAILED:
            if Severity.ERROR not in sev:
                raise ValueError("failed 至少需要一条 error issue")
        return self


class AttemptOutcome(StrEnum):
    OK = "ok"
    SCHEMA_INVALID = "schema_invalid"
    TRANSPORT_ERROR = "transport_error"
    TIMEOUT = "timeout"


class Attempt(_Contract):
    """一次后端调用的审计记录（不含提示词与输出正文，只含哈希）。"""

    attempt_id: str = Field(min_length=1, max_length=64)
    doc_id: DocId
    window_id: str
    keys: list[str]
    retry_of: str | None = Field(default=None, description="语义重试时指向上一次 attempt_id")
    backend_id: str
    prompt_template_id: str
    request_sha256: Sha256Hex = Field(description="规范化请求 JSON 的哈希，也是缓存键")
    response_sha256: Sha256Hex | None = None
    started_at: AwareDatetime
    latency_ms: float = Field(ge=0)
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    outcome: AttemptOutcome
    error_message: str | None = Field(default=None, max_length=500)
    cache_hit: bool = False


class ContextWindow(_Contract):
    """上下文规划器产出的一个窗口：送给模型的块子集与要问的键。"""

    window_id: str = Field(min_length=1, max_length=64)
    doc_id: DocId
    block_ids: list[str] = Field(min_length=1)
    keys: list[str] = Field(min_length=1)
    estimated_tokens: int = Field(ge=0)
    truncated: bool = Field(default=False, description="预算不足导致相关块被舍弃")


class ProducerOutput(_Contract):
    """一个抽取层对一个文档的产出：全部尝试的候选（含被重试替代的）+ 审计。

    Pipeline 会对全部候选统一校验并交给 Resolver 合并（grounded 优先）。
    """

    candidates: list[Candidate] = Field(default_factory=list)
    attempts: list[Attempt] = Field(default_factory=list)
    windows: list[ContextWindow] = Field(default_factory=list)
    failed_keys: dict[str, list[Issue]] = Field(
        default_factory=dict,
        description="基础设施失败（后端/输出始终非法）且没有任何候选的键 → Pipeline 生成 failed 记录",
    )


class DocumentResult(_Contract):
    """单个文档的全部结果。records 按 KeySet 中键的顺序排列，每个键恰好一条。"""

    info: DocumentInfo
    records: list[ExtractionRecord]
    windows: list[ContextWindow] = Field(default_factory=list)
    attempts: list[Attempt] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_records(self) -> DocumentResult:
        keys = [r.key for r in self.records]
        if len(keys) != len(set(keys)):
            raise ValueError("同一文档中键重复")
        for r in self.records:
            if r.doc_id != self.info.doc_id:
                raise ValueError("record.doc_id 与文档不一致")
        return self


# ---------------------------------------------------------------------------
# 人工修正（覆盖层，追加写，不修改机器结果）
# ---------------------------------------------------------------------------


class ReviewAction(StrEnum):
    ACCEPT = "accept"
    CORRECT = "correct"
    REJECT = "reject"
    SET_NOT_FOUND = "set_not_found"


class Correction(_Contract):
    """人工复核动作。修正给出的证据同样经过 Verifier；无证据的修正标记为人工未溯源。"""

    correction_id: str = Field(min_length=1, max_length=64)
    run_id: str
    doc_id: DocId
    key: str
    action: ReviewAction
    value_text: str | None = None
    value: NormalizedValue | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    reviewer: str = Field(default="anonymous", max_length=64, description="化名，不存真实身份")
    created_at: AwareDatetime
    note: str | None = Field(default=None, max_length=1000)


# ---------------------------------------------------------------------------
# 后端请求/响应（运行时对象；可进入本地缓存，缓存目录必须被 git 忽略）
# ---------------------------------------------------------------------------


class ChatMessage(_Contract):
    role: Literal["system", "user", "assistant"]
    content: str


class RequestContext(_Contract):
    """请求的追踪上下文。**不发送给服务端，也不参与缓存哈希**；
    供 Oracle/NoisyOracle 测试后端定位文档与窗口、供日志关联 Attempt。"""

    doc_id: DocId
    window_id: str
    keys: list[str]
    block_ids: list[str] = Field(default_factory=list)
    attempt_index: int = Field(default=0, ge=0, description="0=首轮，1..=语义重试")


class ChatRequest(_Contract):
    """OpenAI 兼容 chat completion 请求的后端无关表示。"""

    context: RequestContext | None = Field(default=None, description="仅本地追踪用，见 RequestContext")
    messages: list[ChatMessage] = Field(min_length=1)
    json_schema: dict[str, Any] | None = Field(default=None, description="结构化输出约束；None 表示仅靠提示词")
    temperature: float = Field(default=0.0, ge=0, le=2)
    top_p: float | None = Field(default=None, gt=0, le=1)
    seed: int | None = None
    max_tokens: int = Field(default=2048, ge=1)
    timeout_s: float = Field(default=120.0, gt=0)
    extra: dict[str, Any] = Field(default_factory=dict, description="后端特有参数，如 chat_template_kwargs")


class ChatResponse(_Contract):
    content: str
    finish_reason: str | None = None
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    latency_ms: float = Field(default=0.0, ge=0)
    model_reported: str | None = Field(default=None, description="服务端回报的模型名")
    cache_hit: bool = False


class PromptBundle(_Contract):
    """渲染好的一次请求的提示词与元数据。"""

    window_id: str
    keys: list[str]
    messages: list[ChatMessage]
    output_schema: dict[str, Any]
    template_id: str
    template_sha256: Sha256Hex
    table_style: Literal["cell_lines", "row_lines"]


# ---------------------------------------------------------------------------
# 运行配置与清单（可复现性）
# ---------------------------------------------------------------------------


class BackendInfo(_Contract):
    """后端配置快照。``base_url_host`` 只记录 host:port，不记录凭据或路径参数。"""

    backend_id: str = Field(min_length=1, max_length=100)
    provider: Literal["openai_compatible", "fake", "oracle", "noisy_oracle"]
    base_url_host: str | None = None
    model: str = Field(min_length=1, max_length=200)
    model_sha256: Sha256Hex | None = Field(default=None, description="本地权重文件哈希（如 GGUF），未知则 None")
    quantization: str | None = None
    structured_output: Literal["json_schema", "json_object", "none"] = "json_schema"
    thinking: bool = False
    temperature: float = 0.0
    top_p: float | None = None
    seed: int | None = None
    max_tokens: int = 2048
    timeout_s: float = 120.0
    max_concurrency: int = Field(default=2, ge=1)
    extra_params: dict[str, ParamValue] = Field(default_factory=dict)


class RunConfig(_Contract):
    """一次运行的全部可调参数。其规范 JSON 哈希记入 RunManifest.config_sha256。"""

    backend: BackendInfo
    context_budget_tokens: int = Field(default=12_000, ge=1_000)
    profile: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9_\-]+$",
        description="档位名（如 cpu-lite、cpu-std、gpu-std、gpu-ref），仅用于记入清单与评测分组；各参数的实际取值以本配置的其他字段为准",
    )
    prefilter: Literal["auto", "always"] = Field(
        default="auto",
        description="上下文预筛选：auto=仅当文档超出 context_budget_tokens 时才按键做 BM25 选块；always=总是先按键选块（CPU 档：预填充是瓶颈，需把输入压短）",
    )
    max_keys_per_window: int | None = Field(
        default=None,
        ge=1,
        description="每个窗口最多同时询问的键数；None 表示不限。小模型或小上下文时调低，减少一次问太多键造成的遗漏",
    )
    max_semantic_retries: int = Field(default=2, ge=0, le=5)
    max_transport_retries: int = Field(default=3, ge=0, le=10)
    table_style: Literal["cell_lines", "row_lines"] = "cell_lines"
    pdf_backend: str = Field(
        default="pypdfium2",
        pattern=r"^[a-z0-9_]+$",
        description="PDF 字符后端名。默认值待 T0 许可证与 API 验证后确认；'pymupdf' 仅在单独安装 AGPL 可选扩展后可用",
    )
    constrain_block_ids: bool = Field(default=False, description="在 JSON Schema 中把 block_id 约束为窗口内枚举")
    include_hidden_sheets: bool = False
    include_hidden_cells: bool = True
    review_possible_miss: bool = Field(default=False, description="POSSIBLE_MISS 是否把 not_found 升级为 needs_review")
    allow_remote_backend: bool = Field(default=False, description="后端不在 localhost 时必须显式开启（数据出境）")
    record_filenames: bool = False
    replay_only: bool = Field(default=False, description="只用缓存响应，缓存未命中即失败（复现实验）")


class ComponentVersion(_Contract):
    name: str
    version: str


class UsageStats(_Contract):
    total_attempts: int = Field(default=0, ge=0)
    cache_hits: int = Field(default=0, ge=0)
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    wall_time_s: float = Field(default=0.0, ge=0)
    peak_vram_mb: float | None = Field(default=None, ge=0, description="外部测量（如 nvidia-smi 采样）；未测量则 None")


class ManifestDocument(_Contract):
    doc_id: DocId
    sha256: Sha256Hex
    format: DocFormat
    size_bytes: int = Field(ge=0)
    display_name: str | None = None


class RunManifest(_Contract):
    """复现一次运行所需的全部元数据。不含正文、不含绝对路径。"""

    run_id: str = Field(min_length=1, max_length=64)
    contract_version: str = CONTRACT_VERSION
    citecell_version: str
    created_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    git_commit: str | None = None
    git_dirty: bool | None = None
    python_version: str
    platform: str
    dependencies: dict[str, str] = Field(default_factory=dict, description="包名 -> 版本")
    components: list[ComponentVersion] = Field(default_factory=list, description="解析器/归一化/校验/渲染/规划器版本")
    prompt_template_id: str
    prompt_template_sha256: Sha256Hex
    output_schema_sha256: Sha256Hex
    config: RunConfig
    config_sha256: Sha256Hex
    keyset_name: str
    keyset_version: str
    keyset_sha256: Sha256Hex
    documents: list[ManifestDocument]
    usage: UsageStats = Field(default_factory=UsageStats)
    notes: str | None = Field(default=None, max_length=2000)


class RunResult(_Contract):
    """一次运行的权威输出（results.json）。表格/CSV/Excel 均由它派生。"""

    manifest: RunManifest
    documents: list[DocumentResult]


# ---------------------------------------------------------------------------
# 预留：模板层（M2）与决策层（M3）
# ---------------------------------------------------------------------------


class TemplateSpec(_Contract):
    """（M2 预留）归纳出的模板。payload 的结构由 M2 定义，M1 不得依赖其内容。"""

    template_id: str
    version: str
    fingerprint: str
    keyset_sha256: Sha256Hex
    payload: dict[str, Any] = Field(default_factory=dict)


class GateDecision(StrEnum):
    """（M3）置信度门控决策。"""

    ACCEPT = "accept"
    ESCALATE = "escalate"
    REVIEW = "review"


# ---------------------------------------------------------------------------
# 金标准与评测
# ---------------------------------------------------------------------------


class DataSource(StrEnum):
    SYNTHETIC = "synthetic"
    POINTER = "pointer"
    """指针式真实评测集：只发布标注 + URL + 哈希 + 下载脚本。"""


class GoldMention(_Contract):
    """金标准中值（或干扰项）在原文中的一处出现，以**与解析器无关**的原生位置表示。

    * PDF：locator.regions 为该处文字的框；char_* 为 None。
    * DOCX：locator 给段落/单元格，char_* 相对于 python-docx 所见的段落（单元格）文本。
    * XLSX：locator 给单元格；char_* 通常为 None（整格）。
    """

    locator: Locator
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)
    surface_text: str = Field(min_length=1)


class GoldRecord(_Contract):
    key: str
    expected: Literal["present", "absent", "derived"]
    value: NormalizedValue | None = None
    mentions: list[GoldMention] = Field(default_factory=list)
    distractors: list[GoldMention] = Field(default_factory=list, description="相似但错误的值出现处（合成数据可知）")
    derivation: DerivationClaim | None = Field(default=None, description="（M3）派生真值；operands 引用 mentions 下标")
    tags: list[str] = Field(default_factory=list, description="难度标签，如 surface=uppercase_money、cross_block")

    @model_validator(mode="after")
    def _check_gold(self) -> GoldRecord:
        if self.expected == "present" and (self.value is None or not self.mentions):
            raise ValueError("present 的金标准需要 value 与至少一个 mention")
        if self.expected == "absent" and (self.value is not None or self.mentions):
            raise ValueError("absent 的金标准不得有 value 或 mention")
        if self.expected == "derived" and (self.value is None or self.derivation is None):
            raise ValueError("derived 的金标准需要 value 与 derivation")
        return self


class GeneratorStamp(_Contract):
    generator_version: str
    config_sha256: Sha256Hex
    seed: int
    sample_index: int = Field(ge=0)


class GoldDocument(_Contract):
    """一份文档的金标准。用文件 SHA-256 关联文档，不依赖 block_id（解析器版本无关）。"""

    doc_sha256: Sha256Hex
    format: DocFormat
    source: DataSource
    url: str | None = Field(default=None, description="仅 POINTER：原始下载地址")
    license_note: str | None = None
    generator: GeneratorStamp | None = None
    keyset_name: str
    keyset_version: str
    records: list[GoldRecord]

    @property
    def doc_id(self) -> str:
        return self.doc_sha256[:16]


class LayoutMode(StrEnum):
    FIXED = "fixed"
    """同一模板：字段标签、顺序、位置不变，仅值不同。"""
    PERTURBED = "perturbed"
    """模板微变：标签同义替换、字段顺序扰动、增删无关字段、表格/段落互换。"""
    FREE = "free"
    """版面完全不同：值嵌入叙述性段落，跨章节出现。"""


class DifficultyConfig(_Contract):
    """合成数据难度旋钮。每个旋钮相互独立，便于做单因素消融。"""

    layout: LayoutMode = LayoutMode.FIXED
    surface_variety: float = Field(default=0.0, ge=0, le=1, description="值表面形式多样性（大写金额、万元、中文日期等）")
    distractors_per_key: int = Field(default=0, ge=0, le=5, description="每个键插入的相似干扰值数量")
    missing_rate: float = Field(default=0.0, ge=0, le=1, description="键在文档中缺失的概率")
    duplicate_rate: float = Field(default=0.0, ge=0, le=1, description="值在文档中一致地重复出现的概率")
    conflict_rate: float = Field(default=0.0, ge=0, le=1, description="（M3）重复出现但值冲突的概率；M1 必须为 0")
    filler_blocks: int = Field(default=0, ge=0, le=2000, description="无关填充块数量（测试长文档分窗）")
    cross_block_rate: float = Field(default=0.0, ge=0, le=1, description="值所在句子被换行/分段切开的概率")
    table_style: Literal["simple", "merged_header", "multi_row_header"] = "simple"
    hidden_row_rate: float = Field(default=0.0, ge=0, le=1, description="仅 XLSX：插入隐藏行的概率")
    derived_rate: float = Field(default=0.0, ge=0, le=1, description="（M3）需要计算的键比例；M1 必须为 0")
    noise_rate: float = Field(default=0.0, ge=0, le=1, description="全半角混用、多余空格等噪声概率")


class GenerationConfig(_Contract):
    seed: int
    n_docs: int = Field(ge=1)
    formats: list[DocFormat] = Field(min_length=1)
    domain: str = Field(default="procurement", description="领域模板集名称")
    keyset: KeySet
    difficulty: DifficultyConfig = Field(default_factory=DifficultyConfig)


class SyntheticSample(_Contract):
    """生成器产出的一份样本。``relative_path`` 相对于输出目录。"""

    relative_path: str = Field(pattern=r"^[^/\\][^\\]*$", description="相对路径，不得以 / 开头")
    gold: GoldDocument


class InjectedErrorType(StrEnum):
    """NoisyOracle 注入的错误类型，用于度量校验器对各类错误的拦截率。"""

    WRONG_BLOCK_ID = "wrong_block_id"
    PARAPHRASED_QUOTE = "paraphrased_quote"
    VALUE_NOT_IN_QUOTE = "value_not_in_quote"
    MISSING_KEY = "missing_key"
    HALLUCINATED_VALUE = "hallucinated_value"
    DISTRACTOR_VALUE = "distractor_value"
    CROSS_BLOCK_QUOTE = "cross_block_quote"
    MALFORMED_JSON = "malformed_json"


class InjectedError(_Contract):
    """一次注入记录（由 NoisyOracleBackend 产出，Evaluator 消费）。"""

    doc_id: DocId
    key: str | None = Field(default=None, description="MALFORMED_JSON 作用于整次输出时为 None")
    error_type: InjectedErrorType
    attempt_index: int = Field(ge=0)


class MetricValue(_Contract):
    """比率型指标：value = numerator / denominator；分母为 0 时 value 为 None（绝不填默认值）。"""

    name: str
    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)
    value: float | None = None

    @model_validator(mode="after")
    def _check_ratio(self) -> MetricValue:
        if self.denominator == 0:
            if self.value is not None:
                raise ValueError("分母为 0 时 value 必须为 None")
        else:
            expected = self.numerator / self.denominator
            if self.value is None or abs(self.value - expected) > 1e-9:
                raise ValueError("value 必须等于 numerator/denominator")
        return self


class DistributionStat(_Contract):
    """分布型指标（延迟、token 数、每键尝试次数等）。"""

    name: str
    count: int = Field(ge=0)
    mean: float | None = None
    p50: float | None = None
    p90: float | None = None
    max: float | None = None


class EvalSlice(_Contract):
    """一个切片上的指标，如 'all'、'type=money'、'format=pdf'、'layout=free'。"""

    slice_name: str
    n_records: int = Field(ge=0)
    metrics: list[MetricValue] = Field(default_factory=list)
    distributions: list[DistributionStat] = Field(default_factory=list)


class EvalReport(_Contract):
    run_id: str
    dataset_id: str
    dataset_sha256: Sha256Hex = Field(description="金标准文件集合的规范哈希")
    evaluator_version: str
    created_at: AwareDatetime
    n_documents: int = Field(ge=0)
    n_documents_missing: int = Field(default=0, ge=0, description="指针集中下载失败/哈希不符而跳过的文档数")
    slices: list[EvalSlice]


__all__ = [name for name in dir() if not name.startswith("_")]
