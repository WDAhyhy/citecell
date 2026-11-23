# CiteCell 架构（里程碑 1 定稿，契约版本 0.1.0）

> 作者角色：Opus-Architect。与 `PROJECT_BRIEF.md` 冲突时以本文为准（Brief 的约定）。
> 标注「未核实」的第三方库行为与许可证，必须由 T0 验证任务确认后才能作为实现依据。
> 本文不包含任何实测指标数值；所有数字均为可配置默认值或待测量项。

---

## 1. 一句话设计

**模型只负责"指出"，程序负责"证明"。** 所有格式先解析成带稳定 ID 的 Block；模型对每个键输出「块 ID + 逐字引文 + 值原文」；确定性的 Verifier 证明引文确在原文、值确能由引文导出，并自己计算字符偏移和原生定位（PDF 框 / Word 段落 / Excel 单元格）。证明不了的值不会以"确定"的姿态出现。

---

## 2. 总览数据流

```
 输入文件(pdf/docx/xlsx)                KeySet(键+类型+别名+说明)
        │                                        │
        ▼                                        │
 ┌──────────────┐  PDF: PdfBackend(字符流) ─► PdfLayout(分行/分块/片段)
 │ ParserRegistry│  DOCX: python-docx 正文段落+表格
 │   Parser      │  XLSX: openpyxl 两遍读取(公式/缓存值)
 └──────┬───────┘
        │ ParsedDocument{info, blocks[Block{block_id,text,locator,fragments}]}
        ▼                                        │
 ┌──────────────┐   预算内→单窗口全键；超预算→按键检索(字符二元组BM25+别名+邻块+表头)
 │ContextPlanner │──────────────► ContextWindow[{block_ids, keys}]
 └──────┬───────┘
        ▼
 ┌──────────────┐   块渲染: [p3-b12] 正文… / [s1-B3 | 单价 | 螺丝] 25.00
 │PromptRenderer │──► PromptBundle{messages, output_schema(LLMExtractionOutput)}
 └──────┬───────┘
        ▼
 ┌──────────────┐   OpenAI 兼容 /v1/chat/completions (llama.cpp / Ollama / vLLM)
 │ExtractionBack-│   结构化输出 + 传输层重试 + 请求哈希缓存(可 replay)
 │end (+Cache)   │   测试: Fake / Oracle / NoisyOracle
 └──────┬───────┘
        ▼ LLMExtractionOutput{fields[{key, evidence[{block_id,quote}], value}]}
 ┌──────────────┐
 │CandidateProdu-│──► Candidate{key, value_text, evidence claims, method=llm}
 │cer (第2层)    │      ▲ M2: TemplateProducer(method=template)
 └──────┬───────┘      ▲ M3: DecisionProducer(method=decision, derivation)
        ▼
 ┌──────────────┐   TextNormalizer(偏移保持) + QuoteMatcher(exact→normalized→跨块→重定位→fuzzy诊断)
 │  Verifier     │   ValueNormalizer(类型化可导出性检查) + Localizer(偏移→bbox/段落/单元格)
 └──────┬───────┘
        │ ExtractionRecord{status, value, evidence[Evidence], issues}
        │   └─ 可重试问题 ─► PromptRenderer.render_retry(只问失败键) ─► 回到后端（≤2 次）
        ▼
 ┌──────────────┐   多窗口/多次尝试/多层合并：同值合并证据，异值→needs_review(冲突)
 │  Resolver     │   M3: 决策层 + ConfidenceGate(接受/升级推理LLM/人工)
 └──────┬───────┘
        ▼
 RunResult{manifest, documents[DocumentResult]} ──► results.json(权威) ──► CSV/XLSX 表格(派生)
        │                                                 ▲
        ▼                                                 │ Correction(人工覆盖层，追加写)
 Evaluator(GoldDocument, 原生位置) ──► EvalReport(按类型/格式/难度切片)

 SyntheticGenerator: 事实采样→DocIR(带标记片段)→表面实现→Writer(docx/xlsx/pdf)→Gold(原生位置)
```

---

## 3. 模块划分与职责边界

| 子包 / 文件 | 职责 | 可依赖 | 不得做 |
|---|---|---|---|
| `models.py` | 全部数据契约（pydantic v2） | 标准库、pydantic | import 任何解析库 |
| `interfaces.py` | Protocol 接口 | models | 实现逻辑 |
| `parsing/` | 文件 → ParsedDocument；`pdf_backends/` 隔离 PDF 库 | models、对应格式库 | 调用模型；写日志正文 |
| `textnorm/` | 偏移保持归一化、引文匹配 | 标准库 | 依赖格式库 |
| `values/` | 值类型系统：解析、提及识别、等价判断（中文数字/大写金额/日期） | 标准库 | 依赖格式库 |
| `verify/` | Verifier、Localizer、SimpleMerger(Resolver) | models、textnorm、values | 调用模型 |
| `prompting/` | ContextPlanner、TokenCounter、PromptRenderer、模板文件 | models、textnorm | 网络 |
| `backends/` | OpenAI 兼容客户端、缓存、Fake/Oracle/NoisyOracle | models、httpx | 解析 JSON 语义 |
| `extract/` | LLMCandidateProducer（解析输出→Candidate、语义重试）、Pipeline | 以上全部 | 直接处理文件格式 |
| `synth/` | 合成数据生成器（可选安装 extra） | models、values（仅测试中交叉验证） | 被运行时代码 import |
| `eval/` | 对齐与指标、报告 | models | 依赖具体后端 |
| `manifest.py`, `cli.py` | 清单、命令行 | 全部 | — |

边界原则：**Verifier 是唯一能把状态置为 grounded 的地方**；解析器不知道模型存在；后端不知道文档格式；生成器的金标准不依赖被测解析器。

---

## 4. 数据契约要点（详见 `src/citecell/models.py`）

- **ID**：`doc_id` = 文件字节 SHA-256 前 16 位（与文件名、运行顺序无关）。`block_id` 为文档内局部 ID：PDF `p3-b12`、DOCX `para17` / `t2-r3-c1`、XLSX `s1-B3`（合并单元格用左上角）。代码**不得解析 block_id 取位置**，一律读 `Block.locator`。全局引用 = `(doc_id, block_id)`。
- **索引命名**：`*_index` 0 起，`*_number` 1 起。所有偏移是 Python 码点下标、半开区间、相对 `Block.text`（前端 JS 是 UTF-16，需换算，生僻字扩展区会出现差异）。
- **Locator** 按 `format` 判别：`PdfLocator{page_number, page_width, page_height, regions[BBox]}`（单页，跨页证据拆分）；`DocxLocator{body_index, paragraph_number | table_number+row_index+col_index, span, style}`；`XlsxLocator{sheet_index, sheet_name, cell, cell_range, hidden, formula, number_format, raw_value}`。
- **三段式产物**：`EvidenceClaim`（模型声明，线格式，容忍多余字段）→ `Candidate`（任一层的未校验候选）→ `Evidence` / `ExtractionRecord`（校验后，字段严格）。
- **状态机**（`Status`）：`grounded` / `derived`(M3) / `not_found` / `needs_review` / `failed`。不变量写进契约校验器：grounded 的证据必须全部 exact/normalized 且至少一条带值区间——"fuzzy 永不 grounded"是**类型层面**的保证，而不是约定。
- **问题**：`Issue{code, severity, message, evidence_index}`，`IssueCode` 同时用于记录和重试反馈；message 不得含正文。
- **人工修正**是追加写的覆盖层 `Correction`，不改写机器结果；修正给出的证据同样走 Verifier。
- **金标准** `GoldDocument` 以文件 SHA-256 关联，位置用**原生定位**（页+框 / 段落+字符区间 / 单元格），与 block_id 和解析器版本无关。

---

## 5. 解析层

### 5.1 Block 粒度结论

| 格式 | 块单位 | 理由 |
|---|---|---|
| PDF | **视觉段落**（版面模块把行按行距/缩进合并），超长按句切分（默认上限 800 码点，可配） | 行太细：引文频繁跨块、ID 数量撑大提示词；文字块（库原生）随后端不同而不稳定。段落 + 逐字几何兼顾两者 |
| DOCX | 正文顶层段落；表格**一格一块**（格内多段落以 `\n` 连接） | 与 Word 的编辑单位一致，定位直观 |
| XLSX | **一个非空单元格一块**（合并区域一块） | 单元格是 Excel 溯源的天然最小单位，也是本项目差异点 |

**粒度与上下文的解耦**：块的粒度只决定"可引用单位"，不决定"送多少给模型"——后者由 ContextPlanner 按预算决定。引文跨块由 Verifier 自动修复（§6.2 步骤 5c），因此不必为避免跨块而把块做大。

### 5.2 PDF（许可证隔离设计）

**PyMuPDF 不是 CiteCell 的依赖。** 据用户核实，PyMuPDF 为 AGPL/Artifex 商业双授权，与 Apache-2.0 仓库及后续 exe 分发冲突。PDF 层拆为三层：

1. `PdfBackend`（许可证隔离点）：只输出 `PdfPageInfo` 与 `PdfChar{text, bbox, font_size, generated}` 字符流，坐标换算到 **PDF_COORDS_V1**（CropBox 左上原点、y 向下、按 /Rotate=0 方向）。
2. `PdfLayout`（与后端无关，我们自己实现）：字符→行（基线/纵向重叠聚类）→段落（行距、缩进、字号变化）→Block，按 **LINE_JOIN_V1** 拼接（两侧均为 CJK 字符/标点直接相连，否则插一个空格；插入字符无几何），同时产出逐字 `TextFragment`。
3. `Localizer`（与后端无关）：字符区间 → 逐行合并框。

这样更换后端不改变分块算法，也**不需要 `search_for` 一类的反查**：定位直接来自解析时记录的逐字几何，从根上避开连字、换行、字距导致的反查失败。

- **默认后端候选**：`pypdfium2`（PDFium 绑定，速度与鲁棒性好，有逐字框 API——未核实）与 `pdfminer.six`（纯 Python，`LTChar` 逐字框，打包友好——未核实）。**默认推荐 pypdfium2**，前提是 T0 验证其许可证与逐字框 API；若不满足，改用 pdfminer.six。两者都实现时，测试中用另一个后端交叉校验文本一致性。`pdfplumber` 构建于 pdfminer 之上，M1 不需要。
- **PyMuPDF**：仅允许作为单独安装的可选 extra（`citecell[pymupdf-agpl]`），模块不在默认导入路径中，文档明确标注 AGPL，exe 不得打包。**M1 不实现它。** T4 验收中包含"默认导入后 `sys.modules` 中没有 fitz/pymupdf"的断言。
- **失败场景与兜底**：
  - 字体缺 ToUnicode / 私有区码点比例异常 → 块标 `TEXT_LAYER_SUSPECT`，文档级 `TEXT_LAYER_UNRELIABLE`，记录 `failed`（不猜）。
  - 连字（ﬁ）→ 逐码点 NFKC 归一化可匹配；几何按该字符框整体高亮。
  - 行尾断开的数字/英文 → LINE_JOIN_V1 插入的空格在归一化层被删除，引文仍可 normalized 命中。
  - 后端不给逐字框、只给 span 框 → 片段按 span 记录，Localizer 线性插值，精度降为 `line`。
  - 完全无几何 → 高亮整块框，精度 `block`。
  - 非零 /Rotate、多栏、复杂跨页表格 → M1 不支持，文档级警告；扫描件不支持（无 OCR）。
- PDF 表格在 M1 不恢复行列语义（视为普通段落/行）；评测用框比对，不受影响。

### 5.3 Word（DOCX）

- 按文档顺序遍历正文顶层元素（段落与表格交错），`body_index` 记录该顺序。python-docx 的 `Document.paragraphs`/`.tables` 分开返回会丢失交错顺序，需遍历 body 子元素——具体 API 未核实，T0 写最小脚本确认。
- 标题识别：样式名（含本地化名称，如 "Heading 1"/"标题 1"）→ `heading`，并维护 `section_path`。
- 表格合并单元格：python-docx 对合并区域可能重复返回同一底层单元格（未核实），需按底层元素去重并记录 span。嵌套表格按行优先拼入外层单元格文本，标 `NESTED_TABLE_FLATTENED`。
- 不纳入：批注、页眉页脚、文本框、脚注；含修订痕迹（w:ins/w:del）的文档给文档级警告（`paragraph.text` 对修订的处理未核实）。
- 前端后续不渲染原始 docx 版式，而是用 Block 重建可高亮的 HTML 预览（版式不保真，是已知取舍）。

### 5.4 Excel（XLSX）约定

| 情形 | 约定 |
|---|---|
| 公式 | openpyxl 读两遍：`data_only=False` 取公式、`data_only=True` 取缓存值（未核实）。`Block.text`=缓存值，`locator.formula`=公式，标 `FORMULA`。**无缓存值**（openpyxl 生成且未经 Excel/LibreOffice 重算的文件都是如此）→ text 为空串、标 `FORMULA_NO_CACHED_VALUE`，该格不可能被 grounded。M1 不自带公式求值器 |
| 合并单元格 | 一个块，ID/`cell` 用左上角，`cell_range` 记范围；合并的表头向其覆盖的每一列传播 `column_headers` |
| 隐藏 | 隐藏工作表默认**不纳入**（常为模板/查找表）；隐藏行/列默认纳入但标 `HIDDEN`，提示词中加 `(隐藏)` 标记，作为证据时附 `HIDDEN_SOURCE` 警告（仍可 grounded）。均可配置 |
| 日期 | openpyxl 按数字格式把序列号转为日期（1900/1904 纪元处理未核实）。text 用 ISO `2024-03-05`，`raw_value` 记原值，标 `DATE_FROM_SERIAL` |
| 数字格式 | openpyxl 不渲染 Excel 显示格式。text 规则 XLSX_TEXT_V1：浮点用最短往返 repr 转 Decimal 字符串（不出现科学计数法）；含 `%` 的格式渲染为 `15%`；日期见上；其余保持原值。`number_format` 原样记录以便审计。已知偏差：text 可能与 Excel 界面显示不同（如千分位、货币符号），高亮仍落在正确单元格 |
| 表头 | M1 启发式：工作表已用区域中第一行非空行为表头（合成数据保证此约定）；多级表头由合成器 `multi_row_header` 旋钮测试 |
| 安全 | xlsx/docx 是 zip，需限制解压后总大小与条目数（防 zip 炸弹）；是否自动使用 defusedxml 未核实 |

### 5.5 ID 稳定性

同一文件字节 + 同一解析器版本 ⇒ 相同 block_id（测试断言两次解析 JSON 逐字节相同）。解析器升级可能改变 block_id，因此：清单记录解析器版本；金标准不用 block_id；人工修正同时保存 `quote`，可在新版本解析结果上按引文重新锚定。

---

## 6. 关键算法

### 6.1 偏移保持归一化 NORMALIZER_V1

对原文**逐码点**处理，产出归一化文本与 `index_map`（归一化第 i 个码点 → 原文下标）：

1. 空白（`str.isspace`）、零宽字符（U+200B–U+200D、U+FEFF）、软连字符 U+00AD → 删除（产出 0 个码点）。
2. 单码点 `unicodedata.normalize("NFKC", ch)`：全角 ASCII→半角、U+3000→空格（随即按 1 删除）、①→1、㎡→m2（1→多，每个产出码点都映射回同一原文下标）。逐码点而非整串 NFKC，是为了保持映射简单可证；对组合字符序列的差异可接受并写入测试。
3. 标点映射表（只**映射**不删除）：`“”„"`→`"`，`‘’`→`'`，`、`→`,`，`。`→`.`，`【】〔〕`→`[]`，各类横线→`-`，`～〜`→`~`。**禁止删除标点**：删掉小数点或千分位会让 `12.5` 与 `125` 相等。
4. 拉丁字母 `casefold()`。

映射回原文：归一化区间 `[ns, ne)` → `[index_map[ns], index_map[ne-1]+1)`（`NormalizedText.to_original_span`）。属性测试：对随机串，映射回的原文子串再归一化必须等于归一化命中串。

### 6.2 校验流程（Verifier，确定性纯函数）

输入 `Candidate c`、`ParsedDocument d`、`KeySpec k`：

1. `k.cardinality == "many"` → `needs_review(CARDINALITY_UNSUPPORTED)`（M1）。`c.derivation` 非空 → M1 输出 `needs_review(DERIVATION_UNSUPPORTED)`，M3 交给 DerivationEngine。
2. 值与证据都为空 → `not_found`。附加 POSSIBLE_MISS 检查：键名或别名在文档归一化文本中出现 → 加 info 级 `POSSIBLE_MISS`（`review_possible_miss=True` 时升级为 needs_review）。
3. 有证据无值 → `EVIDENCE_WITHOUT_VALUE`；有值无证据 → `EMPTY_EVIDENCE_WITH_VALUE`；均可重试。
4. 对每条证据声明：
   - a. **剥离渲染噪声**：去掉引文中混入的 `[s1-B3 | 单价]` 前缀、块编号、表头前缀，标 `RENDER_NOISE_STRIPPED`。
   - b. 块存在 → 在该块中 EXACT → NORMALIZED 查找。
   - c. 未命中 → **跨块修复**：在阅读顺序相邻的块拼接文本（前一块+本块、本块+后一块，最多 2 块，同页或相邻页）中查找；命中且跨越边界 → 拆成多条证据，标 `CROSS_BLOCK_SPLIT`。
   - d. 块不存在或仍未命中 → **全文重定位**：在全部块中查找；唯一命中 → 改正 block_id，标 `RELOCATED`（保留 `claimed_block_id`）；多处命中 → `QUOTE_AMBIGUOUS`，不定位。
   - e. 仍未命中 → 可选 FUZZY 对齐（仅诊断，不定位，供重试反馈"最接近的原文片段"）→ `QUOTE_FUZZY_ONLY` 或 `QUOTE_NOT_IN_BLOCK`/`BLOCK_NOT_FOUND`。
   - f. 命中：记录 `occurrences`，多次出现取第一次（`AMBIGUOUS_OCCURRENCE`）；Localizer 计算 locator 与精度。
5. **值可导出性**（§6.3）：`parse_value(value_text)` 失败 → `VALUE_UNPARSEABLE`；在每条已定位证据的引文区间内 `find_mentions`，存在与值等价的提及 → 设置值区间、标 `VALUE_BEARING`。
6. 判定：至少一条带值区间的已定位证据 → `grounded`（未定位的其余证据被丢弃并记 warning）；否则 `needs_review`，issues 列出原因。`UNIT_MISMATCH` 也导致 needs_review。
7. **语义重试**（在 LLMCandidateProducer 中，非 Verifier）：可重试 code 集合 = {BLOCK_NOT_FOUND, QUOTE_NOT_IN_BLOCK, QUOTE_FUZZY_ONLY, QUOTE_AMBIGUOUS, VALUE_NOT_IN_QUOTE, VALUE_UNPARSEABLE, ENUM_OUT_OF_RANGE, EMPTY_EVIDENCE_WITH_VALUE, EVIDENCE_WITHOUT_VALUE, KEY_MISSING_IN_OUTPUT, OUTPUT_SCHEMA_INVALID}。只重问失败键，附结构化错误，最多 `max_semantic_retries`（默认 2）次；每键保留最好结果（grounded > needs_review > failed）。

**grounded 的精确定义**：值能从原文中真实存在的引文确定性导出。它**不保证**值对应的是正确的键（例如把"预付款"当成"合同总额"）——这类错误只能由评测中的"证据支持率/干扰项命中率"度量、由人工复核或 M3 决策层发现。这一点必须在 README 和界面上如实表述。

### 6.3 值类型系统与可导出性检查

**规则：模型输出值的原文形式（应为引文子串），程序负责规范化。** 两条独立路径（解析模型给的值、识别引文中的提及）在规范化值上比较。

| 类型 | 规范化值 | 引文中可识别的表面形式（M1） | 等价判断 |
|---|---|---|---|
| text | `TextValue.text` | — | 归一化后值是引文子串（值区间由 index_map 回映射） |
| number | `Decimal` + 可选单位 | 阿拉伯数字（千分位逗号/空格、全角、小数、负号/"负"）、万/亿/千倍数、中文小写数字（一百二十、两千、〇） | Decimal 相等（`25 == 25.00`）；`spec.unit` 存在且提及单位不同 → UNIT_MISMATCH |
| money | 以元为单位的 `Decimal` + 币种 | `¥/￥/RMB/人民币` 前缀、`元/圆` 后缀、`万元/亿元`、**中文大写**（零壹贰叁肆伍陆柒捌玖拾佰仟万亿、圆/元、角、分、整/正） | 换算到元后 Decimal 相等（"120万元" = 1200000） |
| percent | `Decimal`，15 表示 15% | `15%`、`15 %`、`百分之十五` | Decimal 相等；`0.15` 不识别为百分比 |
| date | `iso` + precision(year/month/day) | `2024年3月5日`、`2024-03-05`、`2024/3/5`、`2024.3.5`、`二〇二四年三月五日`、`2024年3月`、XLSX 日期单元格的 ISO 渲染 | iso 与精度都相等；**拒绝** `03/05/2024` 等年份不在首位的歧义格式和两位年份 |
| enum | 规范选项值 | 选项值或其别名 | 值映射到选项，且该选项某别名出现在引文中 |
| boolean | `bool` | `enum_options` 中 true/false 的别名（"含税"/"不含税"） | 同 enum；无别名命中的判断题属于 M3 派生 |

实现要求：中文数字与大写金额解析器必须与合成器中**独立实现**的"金额→大写"生成函数做往返属性测试（两边由不同工人实现，互相发现错误）。

### 6.4 定位（Localizer）

- PDF：取与 `[s, e)` 相交的 TextFragment；按 `(page_number, line)` 分组，每组合并为一个框 → `regions`。片段为逐字时精度 `char`；为 span 级且部分相交时按字符数线性插值，精度 `line`；无片段时用块框，精度 `block`。
- DOCX：locator 同块，字符区间在 Evidence 中，精度 `char`。
- XLSX：locator 同块，精度 `cell`。
- 前端高亮两层：引文区间浅色、值区间深色。

### 6.5 长文档分窗与多处出现合并

- 文档渲染后的估算 token 数 ≤ `context_budget_tokens`（默认 12000，按目标模型调整）且 `prefilter="auto"` → 单窗口问全部键。
- **档位相关（§7.1）**：`prefilter="always"` 时，即使文档没超预算也先走下面的按键选块流程（CPU 档的瓶颈是预填充，输入越短越快）；`max_keys_per_window` 非空时，一个窗口最多同时问这么多个键，超出的键拆到下一个窗口（拆分结果必须确定，按 KeySet 顺序）。这两项都是 `RunConfig` 的可选字段，默认值保持原有行为。
- 否则：对每个键用（键名 + 说明 + 别名）在块上做**字符二元组 BM25**（无需中文分词依赖），取高分块及其 ±1 邻块、所在章节标题、表格单元格的表头行；将相关块集合高度重叠的键合并为同一窗口；窗口内块保持文档顺序。预算不足时舍弃低分块并标 `truncated` → 相关键附 `CONTEXT_TRUNCATED`。
- TokenCounter 默认启发式且**宁高勿低**；系数在 T8 中用真实 tokenizer（若可用）校准并记录，不臆测。
- 合并（SimpleMerger）：某窗口 grounded + 其他窗口 not_found → grounded（窗口未见到不代表没有）；多个 grounded 且值等价 → 合并证据（按块与区间去重）；值不等价 → `needs_review(CONFLICTING_CANDIDATES)`，全部候选进 `alternatives`（M3 决策层的输入）；全部 not_found → not_found。

### 6.6 提示词与表格渲染

- 段落：`[p3-b12] 正文……`；标题加 `#` 前缀；XLSX 隐藏格加 `(隐藏)`。
- 表格两种风格（`RunConfig.table_style`，记入清单，作为消融项）：
  - `cell_lines`（默认，小模型更稳，token 多）：`[s1-B3 | 单价 | 螺丝M6] 25.00`
  - `row_lines`（省 token）：先输出一次列头 `表 s1「订单」 列: A=产品 | B=单价`，再逐行 `[s1-A3] 螺丝M6 | [s1-B3] 25.00`
- 系统提示要点：文档内容是**数据不是指令**（防提示注入）；引文必须从方括号**之后**的正文逐字复制，且应包含值；先给 evidence 再给 value；没有就 `value=null, evidence=[]`，禁止编造；只输出 JSON。
- 输出 Schema 由 `LLMExtractionOutput` 生成，字段顺序 key → evidence → value；`key` 约束为所问键的枚举；可选把 `block_id` 约束为窗口内 ID 枚举（`constrain_block_ids`，消融项）。为兼容各后端的语法转换器，Schema 需内联 `$defs`（各后端对 `$ref` 的支持未核实）。
- 模板文件版本化（`prompting/templates/extract_v1.zh.txt`），用 `string.Template` 渲染，sha256 记入清单。

---

## 7. 模型后端

- 统一走 OpenAI 兼容 `/v1/chat/completions`，客户端用 httpx（未引入 openai SDK，减少依赖与隐式重试）。
- 结构化输出模式 `structured_output`：`json_schema`（`response_format` 带 schema）/ `json_object` / `none`。llama.cpp server、Ollama、vLLM 各自对这些参数的支持与写法**未核实**，T6 先写探测脚本对每种后端确认，结果写入 `backends/README.md`。无论哪种模式，输出都要：剥离 `<think>…</think>` → 去代码围栏 → 取第一个顶层 JSON 对象 → pydantic 校验；失败记 `OUTPUT_SCHEMA_INVALID` 并进入语义重试。
- Qwen3 思考模式：`BackendInfo.thinking`，第 2 层默认关闭；各后端关闭方式（如 chat_template_kwargs、`/no_think`）未核实，由探测脚本确认。
- 两类重试严格分开：**传输重试**（429/5xx/超时，指数退避，`max_transport_retries`，在后端内部）与**语义重试**（校验失败重问，在 Producer 中）。每次调用都产生一条 `Attempt`（只存哈希不存正文）。
- 并发：asyncio + 每后端 `Semaphore(max_concurrency)`；输出顺序与完成顺序无关（文档按输入顺序、记录按 KeySet 顺序）。
- 可复现：temperature 默认 0、记录 seed；但批处理与 GPU 非确定性使逐字复现**不可承诺**。真正的复现靠**请求哈希缓存**：`request_sha256` = 规范化 {model, messages, schema, temperature, top_p, seed, max_tokens, extra} 的哈希；`replay_only=True` 时只读缓存，未命中即失败。
- 安全：`base_url` 非 localhost/127.0.0.1/::1 时拒绝运行，除非 `allow_remote_backend=True`（文档发往远端即数据出境）。
- 测试后端：`FakeBackend`（按脚本返回）、`OracleBackend`（由金标准构造完美输出，给出流水线上限）、`NoisyOracleBackend`（按比例注入错误：错块 ID、改写引文、值不在引文、漏键、对缺失键编造值、选中干扰值）。后者让我们无需 GPU 就能测出**校验器对每类错误的拦截率**——这是一个可写进简历、且由脚本实测的指标。

### 7.1 模型档位（Profiles）

目标：同一套流水线既能在**普通 CPU 电脑**上运行，也能在 GPU 上跑得更好；用户按自己的硬件选档位，评测也按档位分组。档位只是一组 `RunConfig` + `BackendInfo` 的预设值，**不新增代码路径**；档位名记入 `RunConfig.profile` 和清单。

| 档位 | 目标硬件 | 模型方向（量化 GGUF 等） | `context_budget_tokens` | `prefilter` | `max_keys_per_window` | 备注 |
|---|---|---|---|---|---|---|
| `cpu-lite` | 约 8GB 内存、无独显 | Qwen3 约 1.7B 级，4 位量化 | 约 3000（初始值） | always | 约 3 | 只求能跑；难键依赖模板规则与人工复核 |
| `cpu-std` | 约 16GB 内存、无独显 | Qwen3 约 4B 级，4 位量化 | 约 6000（初始值） | always | 约 6 | 默认推荐的"普通电脑"档 |
| `gpu-std` | 8～16GB 显存 | 4B 全精度或 8B 级 4 位量化 | 约 12000 | auto | 不限 | 速度与准确率更好 |
| `gpu-ref` | 24GB 以上显存或租用 | 14B～32B 级 | 约 12000 | auto | 不限 | **仅作评测参考上限**，不作为默认部署档 |

**重要说明**：
- 表中的模型尺寸、预算和键数都是**初始假设**，模型是否存在、体积多大、各后端能否加载**均未核实**；真实取值由实测校准（见下）。本文档不给出任何速度或准确率数字。
- CPU 档的瓶颈是**预填充**（读入整个提示词），不是逐 token 生成。对策：①按键预筛选相关块并压小预算；②同一窗口共用一次输入问多个键；③在后端支持时启用语法约束解码，减少无效输出与重试；④第 1 层模板规则（M2）尽量接管整批同模板文档，不调用模型；⑤小模型更容易出错，由 Verifier 拦截并标 `needs_review`，而不是放过。
- 评测按「档位 × 指标」出表：值准确率、引文有效率、静默错误率、`needs_review` 比例、每份文档耗时、峰值内存。**这些由 T9 的评测脚本产生，在没有实测前不得写入 README。**
- CPU 推理在 M1 不做真实评测（M1 全部使用 Fake/Oracle/NoisyOracle 后端）；第一次真实 CPU 实测放在 M1 之后，先从 `cpu-std` 与 `cpu-lite` 开始，沙箱的可用内存有限，超过其容量的档位不在沙箱里测。

### 7.2 训练位置

模型微调（M3 之后的 LoRA/QLoRA 与决策层判别模型）**不在本地机器上训练**，统一放在 Google Colab 上进行。含义：
- 仓库里提供可在 Colab 运行的训练脚本与说明（`training/`），不依赖本地 GPU；
- 训练数据由合成器按 seed 在 Colab 里重新生成（体积小、可复现，无需上传大文件），评测用的真实文档只在本地使用，不上传；
- 训练产物（LoRA 适配器、合并后的量化模型）以及训练配置、数据 seed、代码提交哈希一起记录；权重不进 git（`.gitignore` 已忽略），发布到模型托管平台；
- Colab 的资源与会话时长有限，训练脚本必须支持断点续训，并且在文档中如实写明所用的 GPU 型号与耗时（只写实测值）。

---

## 8. 第 1 层与第 3 层的接口预留

**核心不变量：所有层产出 `Candidate`，全部经过同一个 Verifier。** Pipeline 在 M1 就实现"有序生产者链"：对每个文档依次运行 `CandidateProducer` 列表，前一层未 grounded 的键交给下一层。M1 链中只有 LLM 一层。

- **M2 模板层**：`TemplateProducer(method=template)` 加到链首；`TemplateInducer.induce/match` 与 `TemplateSpec`、`ParsedDocument.structure_fingerprint` 已预留。模板产出的候选同样要 grounded，失败即回退第 2 层——"模板失效回退"不需要新机制。
- **M3 决策层**：
  - 线格式 v2 在 `LLMFieldOutput` 上增加可选 `derivation: DerivationClaim`（新增可选字段，不破坏 v1）。
  - `DerivationEngine` 重算，写入 `Derivation{operand_values, recomputed, matches_claim, completeness_checked}`，状态 `derived`。
  - 运算集合 `DerivationOp`：sum / difference / product / ratio / count / average / min / max / date_diff / date_add / compare / convert_unit / round / select（冲突归并规则：latest / earliest / majority / source_priority）。
  - **可验证边界**：程序能验证"给定这些操作数，算得对"，以及每个操作数本身 grounded；**不能**验证操作数是否完整（求和漏项、计数漏计）和否定式判断（"不存在违约条款"）。这些情况 `completeness_checked=False`，由 ConfidenceGate 决定接受/升级/复核，默认偏向复核。
  - `Resolver` 的 M3 实现替换 SimpleMerger；`ConfidenceGate.decide → accept/escalate/review`；升级 = 链中再加一个使用推理型后端的 Producer。`ExtractionRecord.confidence`、`alternatives` 已预留。

---

## 9. 评测指标（定义先行，数值只由脚本产生）

对齐键：`(doc_sha256, key)`。预测证据先由 Localizer 结果转成原生位置再与金标准比对。

| 指标 | 定义 |
|---|---|
| 字段级 P/R/F1 | TP=预测非空且与金标准等价；FP=预测非空且（金标准缺失或不等价）；FN=金标准存在且（预测为空或不等价）。text 另报字符级 F1 |
| 引文有效率 | 已定位（exact/normalized）的证据声明 / 全部证据声明；分别报**首轮**（模型能力）与**最终**（系统能力） |
| 定位准确率 | 值正确的记录中，至少一条证据命中金标准提及的比例：PDF 同页且框 IoU ≥ 阈值（默认 0.5，可配）；DOCX 同段落/单元格且字符区间相交；XLSX 同单元格 |
| 证据支持率 / 干扰项命中率 | 值所在证据落在该键金标准提及上 vs 落在干扰项上（合成数据知道干扰项位置） |
| **静默错误率** | grounded 但值错误 / 全部 grounded。最重要的安全指标（草案遗漏） |
| 缺失字段幻觉率 | 金标准 absent 中预测非空的比例；另报误判缺失率（金标准 present 但 not_found） |
| needs_review 比例与命中率 | needs_review / 全部；命中率 = needs_review 中首选候选确实错误（或金标准缺失）的比例 |
| 校验器拦截率 | NoisyOracle 注入的各类错误中被拦下（未 grounded）的比例，按错误类型分列 |
| 效率 | 每文档/每键延迟分布、token 数、尝试次数、缓存命中；显存由外部采样（未测记 None），本地模型"成本"以 GPU 时间表示 |

切片：值类型、格式、layout、表面形式标签、是否跨块、合成/真实。**合成集与真实集分开报告，绝不合并成一个数字。**

---

## 10. 合成数据生成器

```
FactSampler(seed, domain, keyset) → 事实表 {key: NormalizedValue | ABSENT}
      → DocIR：有序 IR 块（heading/paragraph/table），值以 Span(key, surface, role=value|distractor) 标记嵌入
      → Realizer：表面形式（大写金额/万元/中文日期…）、干扰值、填充段、跨块切分、噪声
      → Writers：docx(python-docx) / xlsx(openpyxl) / pdf（自排版 + 宽松许可证 PDF 写入库）
      → GoldBuilder：由 Writer 自身的排版记账得到原生位置 → GoldDocument
      → SelfCheck：用被测解析器解析，断言每个金标准提及可被找到（失败=生成器或解析器缺陷，报错而不是静默修正）
```

- **金标准独立性**：位置来自 Writer 的记账，而不是被测解析器。PDF 由我们自己逐行排版（计算字宽、换行），每个字符的绘制坐标已知 → 金标准框；DOCX 记录段落序号与字符区间；XLSX 记录单元格。
- **PDF 写入**：候选 reportlab（许可证未核实）。中文字体二选一，由 T0 验证：内置 CID 字体（不嵌入，文本提取依赖 CMap，未核实）或脚本下载 OFL 授权字体并校验哈希（不入库）。不使用 PyMuPDF。
- **难度旋钮**（`DifficultyConfig`，相互独立，便于单因素消融）：layout（fixed/perturbed/free）、surface_variety、distractors_per_key、missing_rate、duplicate_rate、conflict_rate（M3）、filler_blocks、cross_block_rate、table_style、hidden_row_rate、derived_rate（M3）、noise_rate。M1 生成器遇到 conflict_rate/derived_rate > 0 直接报错。
- **确定性**：同一配置与 seed ⇒ 相同金标准内容。docx/xlsx 的 zip 条目时间戳、文档属性时间、PDF 元数据时间需固定；若某库无法固定，则以写出后的实际 SHA-256 作为金标准关联键（同一数据集内仍自洽），并在清单中说明。
- **领域**（默认推荐，见开放问题）：采购场景——采购合同（docx/pdf）、报价/清单（xlsx）、验收单（pdf）。示例键：合同编号、甲方、乙方、合同金额（money）、签订日期（date）、付款比例（percent）、质保期（number+月）、采购方式（enum）、是否含税（boolean）。实体名由组件词表随机组合（不使用真实个人信息）。
- **缩小分布差距的措施**：
  1. 真实指针评测集才是主报告口径；合成数据只用于训练、压力测试和消融，分开报告。
  2. 模板的字段清单、值格式频率、段落长度、表格尺寸取自对公开真实文档的**统计**（只记统计，不复制文本）。
  3. 填充文本只用模板或开源模型生成（不用闭源 API 输出）。
  4. 报告"合成-真实差距表"：同一配置在两类数据上的各项指标并列，并对比块长、表格尺寸、值格式分布等简单统计。
  5. 难度旋钮只在真实**开发集**上校准，绝不在真实测试集上调参。
- **指针式真实集**：每条 = {URL, SHA-256, 格式, 许可说明, 标注文件}；下载脚本校验哈希，失败则跳过并计入 `n_documents_missing`。标注使用原生位置，不依赖 block_id。

---

## 11. 版本化与可复现

`RunManifest` 记录：run_id、契约版本、citecell 版本、git commit 与 dirty 标志、Python 与平台、依赖版本（含 PDF 后端库版本）、各组件版本（parser/layout/normalizer/value-normalizer/verifier/planner/renderer）、提示词模板 ID 与 sha256、输出 Schema sha256、完整 `RunConfig` 及其 sha256、后端快照（provider、host:port、模型名、权重哈希/量化（可得时）、temperature/top_p/seed/max_tokens/thinking/结构化模式）、KeySet 名称/版本/sha256、文档列表（doc_id、sha256、格式、大小；文件名默认不记录）、用量统计。

运行目录沿用仓库现有 `.gitignore` 已忽略的 `outputs/`：

```
outputs/runs/<run_id>/manifest.json   results.json   parsed/<doc_id>.json   eval.json
outputs/cache/llm/<sha[:2]>/<sha>.json            # 含正文，永不提交
```

规范 JSON：`sort_keys=True, ensure_ascii=False, separators=(",", ":")`，Decimal 为字符串。所有哈希基于规范 JSON。

---

## 12. 隐私、合规与安全

- 仓库不存放他人文档：现有 `.gitignore` 已忽略 `data/raw/`、`data/downloads/`、`data/private/`、`outputs/`、模型权重；建议再加 `data/synthetic/`（可由 seed 重新生成）与 `data/pointer_cache/`（下载缓存）——由 Sonnet 修改，本任务不改动该文件。
- 日志：logger `citecell`；INFO 及以上不输出正文、提示词、引文；调试转储提示词需显式参数，且只写入运行目录。`Issue.message`、`DocumentInfo.warnings` 不含正文。
- 路径：契约中不存绝对路径；错误信息中的路径替换为 doc_id；文件名仅在 `record_filenames=True` 时记录。
- 数据出境：远端后端需显式开启；无遥测。
- 缓存与 parsed 文件含正文，只放在被忽略目录，并提供清理命令。
- 输入安全（Web 阶段必须，M1 先留接口）：文件大小上限、zip 解压上限、解析放子进程并设超时/内存上限、上传文件名清洗。
- 提示注入：文档中的"忽略以上指令"可能误导模型；校验层限制了危害面（值必须在原文中），但不能阻止选错值——计入失败模式并在合成数据中加入注入样本（M2 旋钮）。
- 指针集只发布 URL+哈希+标注；合成实体名不使用真实个人信息。

---

## 13. 依赖许可证

仓库为 Apache-2.0，且计划打包 exe。**默认安装路径中禁止 GPL/AGPL；LGPL 与 MPL 需人工批准。** 下表除注明者外均为**未核实**，T0 用脚本读取已安装包的 `importlib.metadata`（License-Expression / License / Classifier）及 dist-info 中的 LICENSE 文件核实，并生成 `docs/THIRD_PARTY_LICENSES.md`；之后在测试中加入许可证白名单检查。

| 组件 | 用途 | 安装方式 | 许可证 |
|---|---|---|---|
| pydantic / pydantic-core | 契约 | 默认 | MIT（**已核实**：本机 2.14.3 / 2.50.4 的元数据） |
| annotated-types / typing-extensions | pydantic 依赖 | 默认 | MIT / PSF-2.0（**已核实**：本机元数据） |
| pypdfium2 | PDF 字符后端（默认候选） | 默认 | 未核实（据称 Apache-2.0 或 BSD-3-Clause；其捆绑的 PDFium 二进制含多个第三方组件，exe 需附 NOTICE） |
| pdfminer.six | PDF 字符后端（备选/交叉校验） | 默认或 extra | 未核实（据称 MIT） |
| pdfplumber | M1 不用 | — | 未核实（据称 MIT） |
| PyMuPDF | 可选后端，M1 不实现 | 单独 extra `pymupdf-agpl` | AGPL-3.0 / Artifex 商业（用户已核实）；**不得进入默认安装与 exe** |
| python-docx | DOCX 解析与合成 | 默认 | 未核实（据称 MIT） |
| openpyxl | XLSX 解析与合成 | 默认 | 未核实（据称 MIT） |
| httpx | 后端客户端 | 默认 | 未核实（据称 BSD-3-Clause） |
| reportlab | 合成 PDF 写入 | extra `synth` | 未核实（据称 BSD 类） |
| 中文字体（Noto/思源等） | 合成 PDF | 下载脚本，不入库 | 未核实（据称 SIL OFL-1.1） |
| pytest | 测试 | extra `dev` | 未核实（据称 MIT） |
| FastAPI | 后续 Web 后端 | 后续 | 未核实（据称 MIT） |
| React / PDF.js | 后续前端 | 后续 | 未核实（据称 MIT / Apache-2.0） |
| Tauri / Electron | 后续打包 | 后续 | 未核实（据称 MIT/Apache-2.0 / MIT） |
| Docling | 不在 M1 使用；如后续引入，其依赖的模型权重需另行核查 | 不使用 | 未核实（据称 MIT） |
| LibreOffice（外部程序） | 可选：公式重算 | 不捆绑 | 未核实（据称 MPL-2.0） |
| Qwen3 权重 | 本地模型 | 用户自行下载，不入库 | 未核实（据称 Apache-2.0，各尺寸可能不同） |

---

## 14. 失败模式与对策

| 失败模式 | 检测 | 对策 |
|---|---|---|
| 模型改写引文/漏字 | NORMALIZED 未命中 | fuzzy 诊断 → 重试反馈最接近片段 → needs_review |
| 引文跨块 | 块内未命中、相邻拼接命中 | 自动拆分证据（CROSS_BLOCK_SPLIT） |
| block_id 写错 | 块不存在或块内未命中 | 全文唯一命中则重定位；多处命中则歧义并重试；可开 `constrain_block_ids` |
| 值不在引文中（编造/计算） | 提及识别无等价项 | VALUE_NOT_IN_QUOTE → 重试 → needs_review；M3 走派生 |
| 选错键（干扰值） | 校验器**无法**发现 | 评测干扰项命中率；复核界面并列 alternatives；M3 判别模型 |
| 缺失键被编造 | 校验通常能拦（无引文可依）；但若原文中有干扰值则拦不住 | 评测幻觉率；合成数据缺失+干扰组合测试 |
| 应有却答 not_found | POSSIBLE_MISS 启发式 | 可配置升级复核；评测误判缺失率 |
| 多处不同值 | Resolver 冲突 | needs_review + alternatives；M3 select 规则 |
| 长文档相关块被截断 | 规划器预算 | CONTEXT_TRUNCATED 标注；提高预算或拆窗 |
| 输出非 JSON / 漏键 | pydantic 校验 | 修复提取 → 语义重试 → failed / needs_review |
| 后端超时、宕机 | 传输错误 | 指数退避重试 → failed(BACKEND_*) |
| PDF 文本层损坏 | 私有区/替换字符比例 | TEXT_LAYER_UNRELIABLE → failed，不猜 |
| 公式无缓存值 | 解析时检测 | FORMULA_NO_CACHED_VALUE；提示用户用 Excel/LibreOffice 重算后再导入 |
| 合成与真实分布差距 | 合成-真实差距表 | 分开报告；用真实开发集校准难度 |
| 提示注入 | 合成注入样本（M2） | 系统提示隔离 + 校验层兜底 + 复核 |

---

## 15. 里程碑边界

| | M1（本次） | M2 | M3 | 之后 |
|---|---|---|---|---|
| 解析 | PDF（pypdfium2 或 pdfminer.six 后端 + 自研版面）、DOCX、XLSX | 指纹 | — | Docling（可选） |
| 抽取 | 第 2 层 + 校验 + 语义重试 + SimpleMerger | 第 1 层模板 + 回退 | 派生、决策层、门控、升级 | LoRA/QLoRA 微调 |
| 数据 | 合成器（fixed/perturbed/free，无冲突/派生） | 注入样本、冲突 | 派生真值 | 指针真实集扩充 |
| 评测 | §9 指标（除 derived 相关）、Fake/Oracle/NoisyOracle | 模板节省的调用与延迟 | 派生准确率、门控校准 | — |
| 交付 | CLI + results.json + CSV 导出 | — | — | FastAPI + React + PDF.js，Tauri/Electron |

M1 契约已包含 M2/M3 所需的全部预留字段与接口；M2/M3 只允许**新增可选字段**（次版本号）。

M1 开工前补充：`RunConfig` 新增三个可选字段 `profile`、`prefilter`、`max_keys_per_window`（默认值不改变原有行为，见 §6.5、§7.1），用于 CPU/GPU 档位。训练统一在 Colab 上进行（§7.2），不在 M1 范围内。

---

## 16. 对 Sonnet 草案的修改清单

1. **`verified` 改名为 `grounded`，并新增 `failed`、`derived`**。"verified"会被读成"值正确"，而程序只能证明"值有原文依据"；面试或用户追问时这是硬伤。`failed`（基础设施失败）与 `needs_review`（内容存疑）分开，否则指标会混淆。Brief 中的 `unverified` 并入 `needs_review`（带 issue code）。
2. **block_id 去掉运行内序号 `d1`**：`d1` 依赖文件输入顺序，跨运行不稳定。改为 `doc_id`=内容哈希前缀，加上文档内局部 ID；每次调用只含一个文档，提示词中只出现局部 ID。
3. **不再依赖 `page.search_for` 反查 bbox**：解析时记录逐字几何（TextFragment），定位由偏移直接计算。这也是摆脱 PyMuPDF 的前提。
4. **PDF 后端抽象与许可证隔离**（采纳用户更正）：PdfBackend 只出字符流，版面分析自研；默认宽松许可证后端；PyMuPDF 仅为 AGPL 可选 extra，M1 不实现。
5. **归一化必须偏移保持，且禁止删除标点**（防 `12.5`≡`125`）。
6. **"值能由引文导出"改为类型化的双路径比较**：模型给值原文，程序规范化；引文内做提及识别；在规范化值上判等。补充了类型系统（含中文大写金额、部分日期、歧义日期拒绝）。
7. **跨块与错 ID 的确定性修复**（相邻拼接、全文唯一重定位），比单纯重试便宜且可审计。
8. **新增多候选/冲突处理**（Resolver、alternatives），草案没有覆盖多窗口与多处不同值。
9. **人工修正与机器结果分离**（追加写覆盖层），保证评测与复现不被人工编辑污染。
10. **重试只重问失败键，并区分传输重试与语义重试**；每次调用留 Attempt 审计（只存哈希）。
11. **评测增加静默错误率、干扰项命中率、首轮/最终引文有效率、校验器拦截率**；"证据支持"在合成数据上用干扰项位置可客观计算。
12. **金标准使用原生位置，不用 block_id**，否则解析器升级会让历史标注失效。
13. **Excel 补充约定**：openpyxl 生成的公式无缓存值、数字格式不渲染、隐藏工作表默认排除。
14. **表格逐格带表头渲染 token 开销大**：保留为默认（小模型更稳），同时提供行式渲染，作为消融项而非定论。
15. **删减过度设计**：M1 不做 fuzzy 以外的近似算法、不引入 Docling、不做置信度；派生在 M1 只保留契约。
16. **补充安全项**：远端后端出境开关、提示注入、zip 炸弹、日志脱敏、`not_found` 不可证伪需单独度量。

---

## 17. 需要用户决定的开放问题（均有默认值，不阻塞 M1 开工）

1. **状态名对外用 `grounded` 还是保留 `verified`？** 默认 `grounded`（理由见 §16-1）。若同意，请 Sonnet 同步修改 `PROJECT_BRIEF.md` 第 27 行的状态列表（本任务未改动该文件）。
2. **合成数据领域**：默认"采购"（合同/报价单/验收单），因为公开的真实采购文档较多，便于后续做指针评测集。
3. **目标硬件与模型档位**：决定默认上下文预算与评测矩阵。默认按 8B 级 Qwen3 4-bit 量化、经 llama.cpp 或 Ollama 运行设计，预算 12000 token（具体模型仓库名与上下文长度未核实）。M1 全部测试用 Fake/Oracle 后端，不依赖 GPU。

（PDF 默认后端在 pypdfium2 与 pdfminer.six 之间的选择由 T0 的验证结果决定，不需要用户拍板；若两者都不满足需求，再升级为用户问题。）
