# 里程碑 1 任务拆分

> 面向最多 5 个并行的 Haiku 工人。依据：`docs/ARCHITECTURE.md`（下称 ARCH）、`src/citecell/models.py`、`src/citecell/interfaces.py`。
> 每个任务都写明了独占文件、依赖、验收命令和难度。验收命令默认在仓库根目录、T0 建好的 `.venv` 中运行。

## 0. 全体工人的共同规则

1. **契约冻结**：不得修改 `src/citecell/models.py`、`src/citecell/interfaces.py`、`src/citecell/__init__.py`。需要改契约时，在最终回复里写明"契约变更请求：字段/理由/影响"，由 Sonnet 汇总，必要时请 Opus 复核。
2. **只写自己独占的文件**（见各任务"独占文件"）；读取任何文件都可以。`pyproject.toml` 只归 T0；其他任务需要新依赖时，在回复中申请。
3. **不运行 git 命令、不提交**；不改 `LICENSE`、`README.md`、`.gitignore`、`docs/PROJECT_BRIEF.md`、`docs/ARCHITECTURE.md`。
4. **不使用真实文档**：测试数据一律在测试运行时用代码生成，不提交二进制样例文件；不访问网络（T0 安装依赖、经批准的字体下载除外）。
5. **不得引入 PyMuPDF**（AGPL）。任何默认导入路径都不得 import `fitz`/`pymupdf`。
6. **日志**：使用 `logging.getLogger("citecell.<子包>")`；INFO 及以上不得输出正文、引文、提示词；异常信息里的路径替换为 doc_id。
7. **确定性**：随机性只用 `random.Random(seed)` 实例，不用全局 random；输出排序明确。
8. **"未核实"事项**：用到 ARCH 中标为"未核实"的第三方行为时，先查 `docs/VERIFICATION_T0.md`；那里没有的，先写最小验证脚本（放进自己的 tests 目录，作为测试用例保留）再实现。
9. **完成标准**：运行本任务全部验收命令并把原始输出贴进最终回复；列出未完成项与已知缺陷；不得声称未运行的测试已通过。
10. 测试目录用 `tests/<子包>/`，pytest 以 `--import-mode=importlib` 运行（T0 在 pyproject 中配置），因此不同目录中的测试文件可以同名。

## 1. 依赖图与并行波次

```
T0 (验证+许可证+pyproject)  ──►  第 1 波：T1 T2 T3 T4 T5a   （5 个并行）
                                     │
                                     ▼
                                第 2 波：T5b T6 T7 T8 T9   （5 个并行；T7 需 T1+T2；T9 需 T2；T5b 需 T5a）
                                     │
                                     ▼
                                第 3 波：T10（编排）──► T11（整合与端到端）
```

| 任务 | 名称 | 波次 | 依赖 | 难度 | Sonnet 复核 |
|---|---|---|---|---|---|
| T0 | 依赖验证、许可证检查、pyproject | 0 | 用户批准安装 | 低-中 | **是**（决定 PDF 默认后端） |
| T1 | 文本归一化与引文匹配 | 1 | T0 | 中 | 抽查 |
| T2 | 值类型系统（中文数字/金额/日期） | 1 | T0 | 中-高 | **是** |
| T3 | DOCX/XLSX 解析 + 解析器注册表 | 1 | T0 | 中 | **是** |
| T4 | PDF 解析（后端 + 版面） | 1 | T0 | 高 | **是**（版面启发式难题可升级 Opus） |
| T5a | 合成器核心 + DOCX/XLSX 写出 + 金标准 | 1 | T0 | 高 | **是** |
| T5b | 合成器 PDF 写出 | 2 | T5a | 中-高 | **是** |
| T6 | 模型后端、缓存、Fake/Oracle/NoisyOracle | 2 | T0 | 中 | 抽查 |
| T7 | Verifier + Localizer + SimpleMerger | 2 | T1, T2 | 高 | **是**，并建议 Opus 独立复核 |
| T8 | 上下文规划、提示词渲染、输出解析 | 2 | T0 | 中 | 抽查 |
| T9 | 评测指标与报告 | 2 | T2 | 中 | **是** |
| T10 | 抽取编排、清单、导出、CLI | 3 | T3 T4 T6 T7 T8 | 中-高 | **是** |
| T11 | 整合与端到端测试 | 3 | 全部 | 中-高 | **是**（M1 验收由 Sonnet 执行） |

---

## T0 依赖验证、许可证检查与项目配置

**前置**：Sonnet 先取得用户同意，允许在仓库内 `.venv/`（已被 .gitignore 忽略）安装：`httpx`、`python-docx`、`openpyxl`、`pypdfium2`、`pdfminer.six`、`reportlab`、`pytest`。**不安装 PyMuPDF。**

**目标**：把 ARCH 中所有"未核实"的第三方事实变成有脚本证据的结论，并确定 PDF 默认后端。

**独占文件**：`pyproject.toml`、`scripts/check_licenses.py`、`scripts/verify_deps.py`、`docs/THIRD_PARTY_LICENSES.md`、`docs/VERIFICATION_T0.md`、`tests/test_licenses.py`、`tests/test_contracts.py`

**步骤**：
1. `pyproject.toml`：包名 `citecell`，`requires-python = ">=3.11"`，src 布局；默认依赖 = pydantic、httpx、python-docx、openpyxl、选定的 PDF 后端；extras：`pdfminer`（备选后端，若未被选为默认）、`synth = [reportlab]`、`dev = [pytest]`、`pymupdf-agpl = [PyMuPDF]`（行内注释写明 AGPL-3.0，不得进入默认安装与 exe；M1 不实现该后端）。pytest 配置：`testpaths = ["tests"]`，`pythonpath = ["src"]`，`addopts = "--import-mode=importlib -q"`。
2. `scripts/check_licenses.py`：用 `importlib.metadata` 计算默认依赖（含传递依赖）的闭包，读取 License-Expression / License / Classifier，并列出 dist-info 中的 LICENSE 文件路径；白名单 = MIT、BSD-2/3-Clause、Apache-2.0、ISC、PSF-2.0、Python-2.0；MPL-2.0 与 LGPL 输出 WARN；GPL/AGPL 或无法识别的输出 FAIL（无法识别时要求人工核对 LICENSE 文件）。参数 `--default-only` / `--all`，`--write docs/THIRD_PARTY_LICENSES.md`。
3. `scripts/verify_deps.py`：逐项输出 PASS/FAIL/NOTE，把以下"未核实"事项变成结论：
   - **reportlab 中文**：内置 CID 字体（如 STSong-Light）写出的中文，能否被 pypdfium2 与 pdfminer.six 提取为正确 Unicode；若不能，记录"需要下载 OFL 字体"（由 Sonnet 向用户申请）。
   - **pypdfium2**：逐字字符与字符框 API 的名称和用法；坐标原点；如何识别后端插入的虚拟字符；CropBox 与页面尺寸的获取；用 reportlab 在已知坐标画字，比对误差。
   - **pdfminer.six**：`LTChar` 字符框、`LTAnno` 虚拟字符、坐标原点，用同一份 PDF 比对。
   - **python-docx**：按文档顺序交错遍历段落与表格的方法；合并单元格是否重复返回同一底层单元格；标题样式名（含中文本地化名）；能否固定 core properties 时间；同内容写两次的字节是否相同。
   - **openpyxl**：`data_only=True/False` 两遍读取；openpyxl 自己写出的公式单元格缓存值是否为 None；`merged_cells.ranges`；隐藏行、隐藏列、隐藏工作表的判定属性；日期单元格的转换与 1900/1904 纪元；百分比格式的 `number_format`；同内容写两次的字节是否相同；是否自动使用 defusedxml。
4. `docs/VERIFICATION_T0.md`：每项写结论、证据（脚本输出摘录）和对实现的影响；最后给出**PDF 默认后端建议**（pypdfium2 或 pdfminer.six）与理由（许可证、逐字框、中文提取、速度）。
5. `tests/test_contracts.py`：契约冒烟测试（导入 models/interfaces；全部 BaseModel 能生成 `model_json_schema()`；状态不变量的正反例，如 fuzzy 证据不能 grounded）。`tests/test_licenses.py`：调用 check_licenses 的函数，断言默认闭包中没有 GPL/AGPL，且没有安装 PyMuPDF。

**验收**：
```bash
.venv/bin/python scripts/check_licenses.py --default-only          # 退出码 0，无 FAIL
.venv/bin/python scripts/verify_deps.py                            # 每项都有 PASS/FAIL/NOTE，无未处理异常
.venv/bin/python -m pytest tests/test_contracts.py tests/test_licenses.py
.venv/bin/python -c "import importlib.util as u; assert u.find_spec('fitz') is None and u.find_spec('pymupdf') is None"
test -s docs/VERIFICATION_T0.md && test -s docs/THIRD_PARTY_LICENSES.md
```
**复核**：Sonnet 根据 VERIFICATION_T0.md 确认 PDF 默认后端，必要时更新 `RunConfig.pdf_backend` 的默认值（这属于契约变更，走第 0 节第 1 条的流程）。

---

## T1 文本归一化与引文匹配

**目标**：实现 ARCH §6.1 的 NORMALIZER_V1 与 `QuoteMatcher`。纯标准库实现。
**独占文件**：`src/citecell/textnorm/**`（建议 `__init__.py`、`normalize.py`、`match.py`），`tests/textnorm/**`
**接口**：`TextNormalizer`（`Normalizer`，`version="normalizer-v1"`）、`QuoteMatcher`（`Matcher`）。
**要点**：逐码点处理并产出 `index_map`；**禁止删除标点**；FUZZY 用标准库实现的子串编辑距离对齐（Sellers 算法或 difflib），设长度上限（例如文本 5000、引文 500 码点，可配置），超限直接返回空；`find` 只返回最高命中等级的全部命中，按 start 升序。
**验收**（`.venv/bin/python -m pytest tests/textnorm`），至少覆盖：
- `normalize("ＡＢＣ　１２３").text == "abc123"`，且 `to_original_span` 能映射回原文 `[0, 7)`。
- `"１２.５"` 与 `"125"` 不匹配；`"1,200"` 与 `"1200"` 不匹配（逗号保留）。
- 原文 `"合同金额：\n1,200 元"` 中查找 `"合同金额:1,200元"` → 等级 NORMALIZED，原文区间覆盖到 `元`。
- `"面积50㎡"` 中查找 `"50m2"` → NORMALIZED，区间覆盖 `㎡`。
- 存在逐字命中时不返回 NORMALIZED；多处命中按 start 升序。
- `allow_fuzzy=False` 时不返回 FUZZY；FUZZY 的 score 在 [0,1]。
- 属性测试：用 `random.Random(0)` 从（CJK、全角、空白、中英文标点、数字、㎡①）字符池生成 1000 个随机串，对随机归一化区间映射回原文后再归一化，必须等于原归一化子串。
- `isinstance(Normalizer(), TextNormalizer)` 与 `isinstance(Matcher(), QuoteMatcher)` 为 True。

---

## T2 值类型系统

**目标**：实现 ARCH §6.3 的 `ValueNormalizer`：`parse_value`、`find_mentions`、`equivalent`。纯标准库实现，全程使用 Decimal。
**独占文件**：`src/citecell/values/**`（建议 `numerals.py`（中文小写/大写数字）、`money.py`、`dates.py`、`percent.py`、`normalizer.py`），`tests/values/**`
**禁止**：import `citecell.synth`（T11 会让 T2 与 T5a 独立实现的结果互相校验）。
**验收**（`.venv/bin/python -m pytest tests/values`），至少覆盖以下表格（由测试参数化）：

| 输入 | 类型 | 期望 |
|---|---|---|
| `人民币壹佰贰拾万元整` | money | 1200000 |
| `壹仟零伍元伍角` | money | 1005.5 |
| `叁万元整` | money | 30000 |
| `壹拾元零伍分` | money | 10.05 |
| `¥1,234.50` / `￥1 234.50` / `１２３４.５元` | money | 1234.5 |
| `120万元` / `1.2亿元` | money | 1200000 / 120000000 |
| `一百零五` / `两千` / `十五` | number | 105 / 2000 / 15 |
| `-3.5` / `负3.5` | number | -3.5 |
| `15%` / `15 ％` / `百分之十五` | percent | 15 |
| `0.15` | percent | 抛 ValueParseError |
| `2024年3月5日` / `2024-03-05` / `2024/3/5` / `2024.3.5` / `二〇二四年三月五日` | date | 2024-03-05, day |
| `2024年3月` / `2024年` | date | 2024-03 month / 2024 year |
| `03/05/2024` / `24年3月5日` / `2024年2月30日` | date | 抛 ValueParseError |

- `find_mentions("合同总价为人民币壹佰贰拾万元整（¥1,200,000.00）", money_spec)` 返回 2 个提及，规范化值均为 1200000，且 `text[m.start:m.end] == m.surface`。
- enum：别名映射到规范值；不在选项中的值 → ValueParseError（Verifier 会转为 ENUM_OUT_OF_RANGE）。boolean：`含税`/`不含税` 别名。
- `equivalent(MoneyValue(25), MoneyValue("25.00"))` 为 True；`DateValue("2024-03")` 与 `DateValue("2024-03-01", day)` 为 False。
- number 带单位：`spec.unit="月"` 时 `12个月` 识别为 12、单位为 月（单位匹配规则写进 docstring）。
- 所有错误信息不包含超过 50 个码点的输入原文。

---

## T3 DOCX / XLSX 解析与解析器注册表

**目标**：实现 ARCH §5.3、§5.4、§5.5。
**独占文件**：`src/citecell/parsing/__init__.py`、`src/citecell/parsing/common.py`（文件哈希、大小与 zip 解压上限、`ParserRegistry`、魔数识别）、`src/citecell/parsing/docx_parser.py`、`src/citecell/parsing/xlsx_parser.py`、`tests/parsing/office/**`
**约定**：注册表通过**字符串路径懒加载** PDF 解析器（`citecell.parsing.pdf.parser:PdfParser`，由 T4 实现），T4 未完成时 PDF 输入抛 `UnsupportedDocumentError`。XLSX_TEXT_V1：整数值浮点渲染为整数（`25.0`→`25`），其他浮点用最短往返 repr 转 Decimal 字符串、不出现科学计数法；`%` 格式乘 100 加 `%`；日期为 ISO 格式。解析器构造参数：`include_hidden_sheets`、`include_hidden_cells`、`max_block_chars`。
**验收**（`.venv/bin/python -m pytest tests/parsing/office`），测试在运行时用 python-docx / openpyxl 生成文件：
- DOCX：标题 + 3 个段落 + 3×3 表格（首行合并 2 格）+ 表格后 1 段 → 块顺序与 body 顺序一致；ID 为 `para1..`、`t1-r1-c1..`；标题块 kind=heading，后续块的 section_path 含该标题；合并格只出一个块且 `col_span=2`；全部 `DocxLocator` 通过校验。
- XLSX：表头行、数据行、合并 `B1:C1`、隐藏第 5 行、隐藏 D 列、一个隐藏工作表、公式单元格（无缓存值）、日期单元格、`0.15` 配 `0%` 格式 → 文本 `15%`；默认不含隐藏工作表的块；隐藏行和隐藏列的格带 HIDDEN；公式格 text=="" 且带 FORMULA_NO_CACHED_VALUE、`locator.formula` 以 `=` 开头；日期格带 DATE_FROM_SERIAL，文本为 ISO；空单元格不出块；`column_headers` 正确（合并表头会传播到它覆盖的各列）。
- 同一文件解析两次，`model_dump_json()` 完全相同；`doc_id == sha256[:16]`；`display_name` 默认为 None。
- 注册表：按扩展名加魔数识别；扩展名为 .docx 但内容是 xlsx → 按内容识别或报错（二选一，写进 docstring）；未知格式 → `UnsupportedDocumentError`；解压后超过上限 → `UnsupportedDocumentError`。
- `python -c "import citecell.parsing, sys; assert 'fitz' not in sys.modules"`

---

## T4 PDF 解析（后端 + 版面）

**目标**：实现 ARCH §5.2：`PdfBackend`（按 T0 结论实现 pypdfium2 和/或 pdfminer.six）、`PdfLayout`（LINE_JOIN_V1、分行分段、长块切分、逐字 TextFragment）、`PdfParser`。
**独占文件**：`src/citecell/parsing/pdf/**`（`__init__.py`、`parser.py`、`layout.py`、`backends/__init__.py`（按名字懒加载）、`backends/pypdfium2_backend.py`、`backends/pdfminer_backend.py`），`tests/parsing/pdf/**`
**约定**：坐标遵循 PDF_COORDS_V1；`/Rotate≠0` 的页给文档级警告；没有任何字符 → `UnsupportedDocumentError("no text layer")`；私有区或替换字符比例超过阈值 → `TEXT_LAYER_SUSPECT` 加文档警告。后端名为 `pymupdf` 时抛出明确错误："AGPL 可选扩展，M1 未实现"。测试中的 PDF 由测试自带的 reportlab 小工具生成（**不得**依赖 T5 的代码，以保持独立）。
**验收**（`.venv/bin/python -m pytest tests/parsing/pdf`）：
- 生成含标题、2 个会折行的中文段落、1 行含数字与英文的段落的 PDF → 块数与段落数一致；块文本等于源段落文本（中文跨行处无空格，中英交界插入一个空格）。
- 每个非空白字符都被某个 fragment 覆盖；fragment 的框在页面内；与 reportlab 绘制坐标的误差不超过容差（容差由 T0 的实测确定并写进测试）；第一行的 y0 小于最后一行（左上原点）。
- 超过 `max_block_chars` 的段落按句末标点切分，并带 SPLIT_FROM_LONG_BLOCK。
- 同一文件解析两次 JSON 完全相同；只画图形、没有文字的 PDF → UnsupportedDocumentError。
- 如果两个后端都实现：同一份 PDF 两个后端得到的块文本相同（作为交叉校验测试）。
- `python -c "import citecell.parsing.pdf, sys; assert not {'fitz','pymupdf'} & set(sys.modules)"`
**升级**：多行合并成段的启发式在测试中不稳定时，把失败用例交给 Sonnet，必要时请 Opus 设计。

---

## T5a 合成器核心、DOCX/XLSX 写出与金标准

**目标**：实现 ARCH §10 中除 PDF 写出以外的部分：FactSampler → DocIR → Realizer → DOCX/XLSX Writer → GoldBuilder，以及采购领域 KeySet 与难度预设。
**独占文件**：`src/citecell/synth/**`，**但不包括** `src/citecell/synth/writers/pdf_writer.py`（归 T5b，T5a 只在 writer 注册表中按字符串懒加载它）；`configs/keysets/procurement_v1.json`、`configs/synth/{easy,medium,hard}.json`；`tests/synth/**`，**但不包括** `tests/synth/test_pdf_writer.py`
**约定**：
- 不得 import `citecell.parsing`、`citecell.values`、`citecell.verify`（金标准独立性；测试中用 AST 扫描断言）。
- 自带独立实现的 `amount_to_uppercase(Decimal) -> str` 以及中文日期、万元等表面形式生成函数。
- 金标准位置来自 Writer 自己的记账：DOCX 为 body_index、段落序号和字符区间；XLSX 为单元格。
- M1 中 `conflict_rate>0` 或 `derived_rate>0` → `ValueError`。
- 实体名由组件词表随机组合，不使用真实个人信息。
- 固定 docx/xlsx 的文档属性时间与 zip 条目时间（方法以 T0 结论为准）；做不到就以写出后的实际 SHA-256 作为金标准关联键，并在 docstring 中说明。
**验收**（`.venv/bin/python -m pytest tests/synth -k "not pdf"`）：
- `generate(seed=1, n_docs=6, formats=[docx,xlsx], 中等难度)` → 每个样本的文件存在；`GoldDocument` 通过校验；`doc_sha256` 与文件实际哈希一致。
- 相同配置运行两次，金标准 JSON 完全相同（字节是否相同写进测试输出）。
- 用 python-docx 直接读回（不用 CiteCell 解析器）：每个 DOCX 金标准提及满足 `段落文本[char_start:char_end] == surface_text`；用 openpyxl 直接读回：XLSX 提及的单元格值的字符串形式等于 surface_text（按 T3 的 XLSX_TEXT_V1 规则）。
- 旋钮：`missing_rate=1` → 全部 absent；`distractors_per_key=2` → 每个 present 记录恰有 2 个干扰项，且值与真值不等价；fixed 与 free 两种 layout 生成的块结构签名不同；`conflict_rate=0.1` → ValueError。
- 大写金额：1200000→`壹佰贰拾万元整`，30000→`叁万元整`，105→`壹佰零伍元整`，10.05→`壹拾元零伍分`，1005.5→`壹仟零伍元伍角`（末尾是否加"整"作为风格参数，两种写法都要能生成）。
- AST 扫描断言 synth 包中没有被禁止的 import。

---

## T5b 合成器 PDF 写出

**目标**：用宽松许可证的 PDF 库（按 T0 结论，候选 reportlab）**自行排版**（计算字宽、折行），记录每个字符的绘制框，产出 PDF 金标准（页码 + 框）。
**独占文件**：`src/citecell/synth/writers/pdf_writer.py`、`src/citecell/synth/pdf_typeset.py`、`tests/synth/test_pdf_writer.py`；如 T0 结论需要外部字体，还包括 `scripts/fetch_fonts.py`（下载 OFL 字体并校验 SHA-256，字体文件放进被忽略的目录，不入库；下载前须经 Sonnet 批准）
**依赖**：T5a 的 DocIR 与 writer 注册表。
**验收**（`.venv/bin/python -m pytest tests/synth/test_pdf_writer.py`）：
- 生成 PDF 样本；用 T0 选定的后端库**直接**提取字符（不用 CiteCell 的 PDF 解析器），取中心点落在金标准框内的字符，拼接并去空白后等于 surface_text 去空白。
- `cross_block_rate=1` 时至少有一个值被折行切开，其金标准有两个框（同页的两行）。
- 相同 seed 两次运行金标准一致；PDF 元数据中的时间已固定，或在测试输出中如实报告无法固定。
- `python -c "import citecell.synth, sys; assert not {'fitz','pymupdf'} & set(sys.modules)"`

---

## T6 模型后端、缓存与测试后端

**目标**：实现 ARCH §7：OpenAI 兼容客户端（httpx）、传输重试、localhost 限制、请求哈希缓存、`FakeBackend`、`OracleBackend`、`NoisyOracleBackend`、后端探测脚本。
**独占文件**：`src/citecell/backends/**`（含 `backends/README.md`）、`src/citecell/canonical.py`（规范 JSON 与 sha256 工具，T10 会使用）、`scripts/probe_backend.py`、`tests/backends/**`
**约定**：
- 请求哈希 = 规范 JSON {model, messages, json_schema, temperature, top_p, seed, max_tokens, extra} 的 sha256，**不含** `context` 与 `timeout_s`。
- 缓存文件放在 `outputs/cache/llm/<sha[:2]>/<sha>.json`。
- `context` 字段绝不发送给服务端。
- Oracle 由（GoldDocument 列表 + ParsedDocument 列表）构造，根据 `request.context` 定位文档、窗口和键，输出合法的 LLMExtractionOutput JSON（引文取金标准 surface 所在块中的原文片段）。
- NoisyOracle 按每种 `InjectedErrorType` 的比例注入错误（使用带 seed 的 RNG），并通过 `injections` 属性返回 `list[InjectedError]`。
**验收**（`.venv/bin/python -m pytest tests/backends`），用 httpx 的 mock 传输（API 先写最小测试确认）模拟服务端：
- 200 正常；先 500 后 200 → 重试成功，Attempt 计数正确；超时 → BackendError；400 → 不重试，直接 BackendError。
- `base_url="http://10.0.0.5:8000"` 且 `allow_remote_backend=False` → 构造时报错；localhost、127.0.0.1、::1 允许。
- 发往服务端的请求体中没有 `context`；json_schema 模式下的 `response_format` 结构写成可配置项（各后端的差异由 probe 脚本确认）。
- 缓存：调换 dict 顺序后哈希相同；修改 `context` 后哈希不变；修改 temperature 后哈希改变；`replay_only` 下缓存未命中 → BackendError。
- Oracle：对一个手工构造的 ParsedDocument 加 GoldDocument，输出能被 `LLMExtractionOutput.model_validate_json` 解析，absent 键输出 `value=null, evidence=[]`。
- NoisyOracle：每种错误类型在比例为 1 时确实生效，且注入记录数等于受影响的键数；相同 seed 两次输出相同。
- `scripts/probe_backend.py --help` 可运行（真实探测不进 CI，结果由用户在本地运行后写入 backends/README.md）。

**追加（档位，ARCH §7.1）**：
- 独占文件新增 `src/citecell/backends/profiles.py` 与 `tests/backends/test_profiles.py`。
- `profiles.py` 提供函数 `get_profile(name, backend) -> RunConfig`，内置 `cpu-lite`、`cpu-std`、`gpu-std`、`gpu-ref` 四个预设，取值按 ARCH §7.1 表中的**初始假设**写成常量并加注释"待实测校准"；未知档位名 → 报错并列出可用档位。预设不得包含任何模型下载地址或真实模型仓库名（这些未核实），模型名由调用方传入。
- 验收：四个预设都能通过 `RunConfig.model_validate`；`cpu-*` 档的 `prefilter == "always"` 且 `max_keys_per_window` 非空；`gpu-*` 档的 `prefilter == "auto"`；`gpu-ref` 的文档字符串中必须出现"仅作评测参考"字样；`RunConfig.profile` 等于所选档位名；同一档位两次调用得到的规范 JSON 哈希相同。

---

## T7 Verifier、Localizer、SimpleMerger（核心）

**目标**：严格按 ARCH §6.2、§6.4、§6.5（合并部分）实现。确定性纯函数，不调用模型。
**独占文件**：`src/citecell/verify/**`（建议 `verifier.py`、`localizer.py`、`merger.py`、`render_noise.py`、`retryable.py`（导出可重试 IssueCode 集合，供 T10 使用）），`tests/verify/**`
**依赖**：T1（Normalizer、Matcher）、T2（ValueNormalizer）。测试只用手工构造的契约对象（三种格式的 ParsedDocument），不依赖解析器。
**构造参数**：`review_possible_miss`、`include_hidden_cells`、`max_cross_block=2`、`enable_fuzzy_diagnostics=True`。
**验收**（`.venv/bin/python -m pytest tests/verify`），每条一个测试：
1. 逐字引文、money 值 `1,200元` → grounded，值区间正确；PDF 块中跨两行的引文 → `regions` 恰为 2 个框，精度为 char。
2. 引文与原文只有空白和全半角差异 → NORMALIZED → grounded。
3. 改写过的引文 → needs_review，带 QUOTE_NOT_IN_BLOCK 或 QUOTE_FUZZY_ONLY（属于可重试集合）。
4. block_id 写错但引文在别处唯一 → grounded，带 RELOCATED，`claimed_block_id` 为原值。
5. block_id 写错且引文在多处出现 → needs_review，带 QUOTE_AMBIGUOUS。
6. 引文跨越相邻块 p1-b3、p1-b4 → 拆成 2 条证据，带 CROSS_BLOCK_SPLIT，grounded。
7. 引文 `合同金额`、值 `1200` → needs_review，带 VALUE_NOT_IN_QUOTE。
8. 值 `120万元`、引文 `¥1,200,000.00` → grounded。
9. not_found；键的别名出现在文档中时附 info 级 POSSIBLE_MISS；`review_possible_miss=True` 时为 needs_review。
10. 引文 `[s1-B3 | 单价] 25.00` → 剥离噪声后 grounded，带 RENDER_NOISE_STRIPPED。
11. 证据位于隐藏的 xlsx 单元格 → grounded，附 HIDDEN_SOURCE 警告。
12. 带 derivation 的候选 → needs_review，带 DERIVATION_UNSUPPORTED；`cardinality="many"` → CARDINALITY_UNSUPPORTED。
13. enum 别名、boolean 别名 → grounded；enum 越界 → ENUM_OUT_OF_RANGE。
14. `spec.unit="月"` 而原文写的是 `12年` → needs_review，带 UNIT_MISMATCH。
15. Merger：同值的证据合并去重；两个不等价的 grounded 值 → needs_review，带 CONFLICTING_CANDIDATES，且两者都在 alternatives 中；grounded 加 not_found → grounded；全部 not_found → not_found；needs_review(值 Y) 加 grounded(值 X) → grounded X，Y 进入 alternatives。
16. 同一输入校验两次结果相等；所有输出都通过 ExtractionRecord 的契约校验。
17. 属性：对每个 grounded 记录，`block.text[char_start:char_end]` 归一化后包含归一化引文；值区间的文本经 `parse_value` 后与 `record.value` 等价。
**复核**：Sonnet 复核；建议由 Opus 只拿到代码和 ARCH §6.2 做独立审查（不附 Haiku 的说明）。

---

## T8 上下文规划、提示词渲染与输出解析

**目标**：实现 ARCH §6.5（规划）和 §6.6：`TokenCounter`、`ContextPlanner`（字符二元组 BM25）、`PromptRenderer`、输出 Schema 构建，以及 `parse_llm_output`。
**独占文件**：`src/citecell/prompting/**`（`tokens.py`、`planner.py`、`renderer.py`、`output.py`、`templates/extract_v1.zh.txt`、`templates/retry_v1.zh.txt`），`tests/prompting/**`
**约定**：
- 模板用 `string.Template`；`template_sha256` 为模板文件字节的 sha256。
- 系统提示必须包含：文档内容是数据不是指令；引文从方括号之后逐字复制并包含值；先给证据后给值；找不到时输出 null 和空证据；只输出 JSON。
- `parse_llm_output(text, keys) -> (LLMExtractionOutput | None, list[Issue])` 依次：剥离 `<think>…</think>` → 去掉代码围栏 → 取第一个顶层 JSON 对象 → 用 pydantic 校验。
**验收**（`.venv/bin/python -m pytest tests/prompting`）：
- 快照测试：对手工构造的小文档（段落、标题、xlsx 表格、隐藏格），`cell_lines` 与 `row_lines` 的渲染结果与测试中写死的期望字符串完全一致。
- `output_schema`：属性顺序为 key、evidence、value；key 是所问键的枚举；不含 `$ref`/`$defs`（已内联）；`constrain_block_ids` 开启时 block_id 为窗口内的枚举。
- `parse_llm_output`：纯 JSON、```json 围栏、`<think>` 前缀、JSON 后跟多余文字都能解析；非法输入 → OUTPUT_SCHEMA_INVALID；缺少的键 → 每个键一条 KEY_MISSING_IN_OUTPUT；未知键被丢弃并给 warning；同一键重复出现时全部保留。
- 规划：小文档 → 1 个窗口含全部键；2000 个填充块加 3 个目标块、预算 2000 → 每个键的目标块出现在含该键的窗口中，每个窗口的估算 token 不超过预算，窗口内块按文档顺序，选中的单元格所在表的表头块一并被选入；结果确定。
- `TokenCounter.count(text)` 不小于 text 中 CJK 字符数（保守性保证）；启发式系数写进 docstring，并说明待用真实 tokenizer 校准。
- `render_retry` 只包含失败的键，且附带它们的 IssueCode。

**追加（档位，ARCH §6.5 / §7.1）**：`ContextPlanner` 必须读取 `RunConfig.prefilter` 与 `RunConfig.max_keys_per_window`。追加验收：
- `prefilter="always"`：一个远小于预算的小文档，也要走按键选块流程，并且每个键所在窗口只含相关块（其余块不出现在窗口里）；`prefilter="auto"` 时同一小文档仍是单窗口含全部块。
- `max_keys_per_window=2`、5 个键 → 至少 3 个窗口，每个窗口键数 ≤ 2，所有键都出现且仅出现一次，拆分顺序按 KeySet 顺序，重复运行结果完全相同。
- 两项都不影响默认行为：用默认 `RunConfig` 时，原有的规划测试结果必须保持不变。

---

## T9 评测指标与报告

**目标**：实现 ARCH §9 全部 M1 指标、切片和报告输出。
**独占文件**：`src/citecell/eval/**`（`align.py`、`metrics.py`、`geometry.py`（IoU）、`report.py`），`tests/eval/**`
**依赖**：T2（`equivalent` 用于判断值是否相等）。
**验收**（`.venv/bin/python -m pytest tests/eval`）：
- 构造一个包含 10 条记录、每条结果已知的 RunResult 与 GoldDocument（4 条 grounded 且正确、1 条 grounded 但错误、2 条正确的 not_found（金标准为 absent）、1 条幻觉（金标准 absent 但预测有值）、1 条 needs_review 且确实错误、1 条误判缺失），断言每个指标的分子和分母，包括 P/R/F1、静默错误率 1/5、幻觉率、误判缺失率、needs_review 比例与命中率。
- IoU：完全重合为 1，不相交为 0，一半重叠的值正确；PDF 定位要求同页；DOCX 要求同段落且字符区间相交；XLSX 要求同单元格。
- 证据落在干扰项上 → 计入干扰项命中率。
- 传入 `injections` 时按错误类型输出拦截率；分母为 0 的指标 value 为 None。
- 切片：all、按 type、按 format、按 tag。
- `report.py` 输出 Markdown：其中出现的每个数字都必须来自 EvalReport（测试中解析 Markdown 里的数字并与报告对比）；合成数据与真实数据的切片分开展示。

---

## T10 抽取编排、清单、导出与 CLI

**目标**：实现 `LLMCandidateProducer`（规划 → 渲染 → 调用后端 → 解析 → 生成候选 → 校验 → 只对可重试的失败键做语义重试）、`Pipeline`（解析 → 生产者链 → 统一校验 → Resolver → DocumentResult/RunResult）、`RunManifest` 构建、CSV/XLSX 导出、CLI。
**独占文件**：`src/citecell/extract/**`、`src/citecell/manifest.py`、`src/citecell/export.py`、`src/citecell/cli.py`、`src/citecell/__main__.py`、`tests/extract/**`
**约定**：
- 生产者链在 M1 只有一个 LLM 生产者，但代码结构须支持"上一层未 grounded 的键交给下一层"。
- 并发用 asyncio，并受 backend.max_concurrency 限制；输出顺序固定。
- 清单不得出现绝对路径；`record_filenames=False` 时不记录文件名。
- results.json 使用规范 JSON 写出（`citecell.canonical`）。
- CLI 子命令：`extract`、`synth`、`eval`。
**验收**（`.venv/bin/python -m pytest tests/extract`）：
- 用 FakeBackend 返回脚本化输出：首轮引文错误、第二轮正确 → grounded，attempts=2，且第二条的 `retry_of` 指向第一条；两轮都错 → needs_review；`max_semantic_retries=0` 时不重试。
- 后端始终 BackendError → 该窗口的键都是 failed，带 BACKEND_ERROR；输出始终不合法 → failed 或 needs_review（按 ARCH §6.2 第 7 步的规则）。
- FakeBackend 随机延迟、并发为 4 时，两次运行的 results 去掉 run_id 和时间后完全相同。
- 清单：所有 sha256 字段为 64 位十六进制；`json.dumps(manifest)` 中不出现临时目录的绝对路径；`dependencies` 中包含所选 PDF 后端的库版本。
- 导出：CSV 的行是文档、列是键，单元格是显示值；第二个文件或工作表给出证据的定位。
- `.venv/bin/python -m citecell --help` 与 `extract --help` 可以运行。

---

## T11 整合与端到端测试（独立任务）

**目标**：把各模块串起来，验证 M1 的整体行为；不新增功能。发现模块缺陷时**不要修改别人的文件**，写成失败测试，加上 `xfail(strict=True)` 和原因，然后报告 Sonnet，由 Sonnet 派回原任务修复。
**独占文件**：`tests/e2e/**`、`scripts/run_m1_eval.py`、`configs/runs/**`、`docs/M1_RESULTS.md`（只写模板；数值只能由 `run_m1_eval.py` 生成后粘贴，并注明运行命令与清单哈希）
**验收**（`.venv/bin/python -m pytest tests/e2e`）：
1. **合成 → 解析自检**：固定 seed，docx/xlsx/pdf 中等难度各 5 份 → 用 CiteCell 解析器解析 → 每个金标准提及都能对应到某个块和原生定位（不能对应即缺陷）。
2. **Oracle 端到端**：所有 present 键 grounded 且值等价，所有 absent 键 not_found，定位全部命中。这是流水线正确性的断言，不是对外报告的指标；不满足就是缺陷。
3. **NoisyOracle 逐类型**：每种 InjectedErrorType 单独设比例为 1，断言 Verifier 的行为符合预期表（例如 WRONG_BLOCK_ID 且引文唯一 → RELOCATED 后 grounded；PARAPHRASED_QUOTE → 不 grounded；HALLUCINATED_VALUE 在无干扰项时 → 不 grounded；DISTRACTOR_VALUE → grounded 但值错误，属于静默错误）。预期表写进测试文件的 docstring。
4. **T2 与 T5a 交叉往返**：用 `random.Random(0)` 生成 1000 个金额（0.01 到 10^10，两位小数），`values` 解析 `synth.amount_to_uppercase(x)` 的结果等于 x；合成器产出的各种日期表面形式都能被解析回原值。
5. **复现**：同一配置运行两次，第二次全部命中缓存，results.json 去掉 run_id 和时间后完全相同；`replay_only` 运行的结果也相同。
6. **隐私**：清单与 results 中不出现临时目录的绝对路径；`record_filenames=False` 时不出现文件名；捕获 INFO 级日志，断言其中不含任何金标准 surface_text。
7. **许可证与导入隔离**：`check_licenses.py --default-only` 退出码为 0；运行完整流水线后 `sys.modules` 中没有 fitz/pymupdf。
8. **可选的真实模型冒烟**：设置了环境变量 `CITECELL_E2E_BASE_URL` 和 `CITECELL_E2E_MODEL` 时，用 2 份文档跑一遍真实后端（否则跳过），只断言输出合法、清单完整，不断言准确率。
- `scripts/run_m1_eval.py --config configs/runs/m1_synth_medium.json` 生成 `outputs/runs/<run_id>/{manifest,results,eval}.json` 和 Markdown 报告。

**复核**：M1 验收由 Sonnet 执行：在干净的 `.venv` 中运行全部测试，抽查 results.json 与清单，确认 `docs/M1_RESULTS.md` 中的每个数字都能由脚本复现。

---

## 附：文件归属总表（防冲突）

| 路径 | 归属 |
|---|---|
| `pyproject.toml`、`scripts/check_licenses.py`、`scripts/verify_deps.py`、`docs/THIRD_PARTY_LICENSES.md`、`docs/VERIFICATION_T0.md`、`tests/test_licenses.py`、`tests/test_contracts.py` | T0 |
| `src/citecell/textnorm/**`、`tests/textnorm/**` | T1 |
| `src/citecell/values/**`、`tests/values/**` | T2 |
| `src/citecell/parsing/{__init__,common,docx_parser,xlsx_parser}.py`、`tests/parsing/office/**` | T3 |
| `src/citecell/parsing/pdf/**`、`tests/parsing/pdf/**` | T4 |
| `src/citecell/synth/**`（不含 pdf_writer.py、pdf_typeset.py）、`configs/keysets/**`、`configs/synth/**`、`tests/synth/**`（不含 test_pdf_writer.py） | T5a |
| `src/citecell/synth/writers/pdf_writer.py`、`src/citecell/synth/pdf_typeset.py`、`tests/synth/test_pdf_writer.py`、`scripts/fetch_fonts.py` | T5b |
| `src/citecell/backends/**`、`src/citecell/canonical.py`、`scripts/probe_backend.py`、`tests/backends/**` | T6 |
| `src/citecell/verify/**`、`tests/verify/**` | T7 |
| `src/citecell/prompting/**`、`tests/prompting/**` | T8 |
| `src/citecell/eval/**`、`tests/eval/**` | T9 |
| `src/citecell/extract/**`、`src/citecell/{manifest,export,cli,__main__}.py`、`tests/extract/**` | T10 |
| `tests/e2e/**`、`scripts/run_m1_eval.py`、`configs/runs/**`、`docs/M1_RESULTS.md` | T11 |
| `src/citecell/{models,interfaces,__init__}.py`、`docs/ARCHITECTURE.md`、`docs/DECISIONS.md`、`docs/TASKS.md` | Opus/Sonnet（冻结） |
| `LICENSE`、`README.md`、`.gitignore`、`docs/PROJECT_BRIEF.md` | Sonnet/用户 |

## 附：建议 Sonnet 在派工前顺手完成的事项（不属于工人任务）

1. 征得用户同意后批准 T0 安装依赖。
2. 在 `.gitignore` 中补充 `data/synthetic/`、`data/pointer_cache/`（ARCH §12）。
3. 若用户同意状态名改为 `grounded`，同步修改 `docs/PROJECT_BRIEF.md` 中的状态列表（ARCH §17 第 1 条）。
