# T0 依赖验证结论

> 证据来源：`scripts/verify_deps.py`（PASS 20 / NOTE 5 / FAIL 0）、`scripts/check_licenses.py`（默认闭包 FAIL 0 / WARN 1）、`tests/test_contracts.py` + `tests/test_licenses.py`（83 passed）。
> 所有测试文件均在运行时于临时目录生成，未使用真实文档，仓库内无二进制样例。
> 本文只记录脚本输出中可见的事实；脚本未覆盖的内容明确标为"未验证"。

## 1. PDF 默认后端建议

**建议：`pypdfium2` 作为默认后端（与 ARCH §5.2 一致），`pdfminer.six` 作为交叉校验后端。前提：PDFium 随附第三方许可的 NOTICE 在 exe 打包前完成人工核对。**

依据：

| 维度 | pypdfium2 | pdfminer.six |
|---|---|---|
| 许可证（元数据） | PASS：`BSD-3-Clause, Apache-2.0, dependency licenses`，判定为 BSD-3-Clause；但 wheel 内随附 74 份许可/声明文件（含 `LICENSES/CC-BY-4.0.txt` 与 PDFium 的 `BUILD_LICENSES/`），**本脚本未逐一核验** | PASS：MIT（License-Expression） |
| 逐字字符框 | PASS：`get_charbox` 可用，坐标为 PDF 空间（原点左下） | PASS：`LTChar.bbox`，同样原点左下 |
| 虚拟字符识别 | PASS：`raw.FPDFText_IsGenerated` 可调用，样本中换行符 `\r\n` 被标为 generated（4 个） | NOTE：`LTAnno` 类存在，样本中出现 3 个；与 LAParams 的关系需 T4 补测 |
| 中文 CID 字体提取 | PASS：STSong-Light（未嵌入）整句逐字一致 | PASS：同一样本逐字一致 |
| 打包 | 含原生 PDFium 二进制，体积大；需附 NOTICE | 纯 Python，依赖 `cryptography`（Apache-2.0 OR BSD-3-Clause，PASS） |
| 速度、鲁棒性 | **未验证**（T0 未做基准） | **未验证** |

选 pypdfium2 而非 pdfminer.six 的理由是逐字 API 更直接、虚拟字符有官方标记位；代价是 PDFium 随附许可需要人工核对。若 NOTICE 核对不通过，改用 pdfminer.six 作为默认（两者 API 均已验证，切换成本低）。**这一结论需要 Sonnet/用户确认。**

## 2. 逐项结论

### 2.1 reportlab 中文（ARCH §13 未核实项）
- 结论：**PASS**。未嵌入的 CID 字体 `STSong-Light` 生成的中文，pypdfium2 与 pdfminer.six 均能提取为正确 Unicode（整句逐字一致）。
- 证据：`[PASS] pypdfium2 中文 CID 字体提取（STSong-Light，未嵌入）: 中文整句逐字一致`；`[PASS] pdfminer.six 中文 CID 字体提取...: 中文整句逐字一致`。
- 影响：**文本层测试不需要下载 OFL 中文字体**。未验证的是视觉渲染：未嵌入字体由阅读器替换，若合成 PDF 需要稳定的视觉外观（如截图比对），再申请 OFL 字体。

### 2.2 pypdfium2 API、坐标、CropBox、虚拟字符
- 结论：**PASS**。`PdfTextPage.count_chars / get_text_range / get_charbox(index, loose=False)`，`PdfPage.get_size / get_cropbox / get_mediabox / get_rotation` 均存在。
- 坐标原点：`get_charbox` 返回 PDF 空间 `(left, bottom, right, top)`，原点左下。换算到 PDF_COORDS_V1（CropBox 左上原点）：`top_y = cropbox_top - top`。
- 误差：reportlab 在 `(72, 700)` 画 `H`，pypdfium2 框为 `(72.92, 700.00, 79.75, 708.62)`，左缘偏 0.92pt，基线 700 在框内。**推断**：该偏移来自字形左侧留白（Helvetica 'H' 的 LSB），即 pypdfium2 默认返回字形紧框；未直接核对字体度量。T4 若需要与 advance 框一致，可评估 `loose=True`。
- CropBox：样本中 CropBox = MediaBox = (0,0,595.28,841.89)，rotation=0。
- 虚拟字符：`FPDFText_IsGenerated` 标记了两个行尾的 `\r` 与 `\n`（共 4 个）。**空格型**虚拟字符未在样本中出现，T4 需补测。
- 证据：`[PASS] pypdfium2 字符框坐标原点: 'H' charbox(PDF空间)=(72.92,700.00,79.75,708.62)...`；`[PASS] pypdfium2 虚拟字符（generated）识别: ... 字符=['\r', '\n', '\r', '\n']`。

### 2.3 pdfminer.six 字符框与虚拟字符
- 结论：**PASS**（字符框）/ **NOTE**（虚拟字符）。`LTChar.bbox` 原点左下；样本中 `H` 的 bbox 为 `(72.00, 697.52, 80.66, 709.52)`，左缘与理论原点完全一致（advance 框）。`LTAnno` 样本计数 3。
- 与 pypdfium2 的差异：pdfminer 框基于字体 advance 与 descent/ascent，pypdfium2 框为字形紧框。两者 y 范围不同（pdfminer 697.52–709.52，pypdfium2 700.00–708.62）。**跨后端交叉校验时应比较中心点或容差（建议 ≤1.5pt），不要求框完全相等。**

### 2.4 python-docx
- 按 body 顺序交错遍历：**PASS**。`body.iterchildren()` 得到 `['p','tbl','p']`；`doc.paragraphs` 只返回 2 段（不含表格）。→ 必须遍历 body。
- 合并单元格：**PASS（已证实重复返回）**。横向合并的行 0 `row.cells` 共 3 项，底层 `_tc` 去重后 2 个；纵向合并列 2 的行 1、行 2 去重后 1 个。→ 实现时按 `_tc` 去重，并从 `gridSpan` / `vMerge` 记录 span。
- 嵌套表格：**PASS**。外层单元格 `.tables` 可访问，数量 1。
- 标题样式：`'Heading 1'` 段落 `style.name == 'Heading 1'`；自定义 `'标题 1'` 样式读回 `'标题 1'`。**未验证**：中文版 Word 模板的内部样式名（Word 通常以英文内部名 `heading 1` 存储，但未用真实文档核实，也不允许用真实文档）。→ 建议以 `styleId`（`Heading1` / `1`）与内部名为主，中文显示名为辅。
- core properties：**PASS**。固定的 `created/modified` 保存后读回不变。
- 确定性：**NOTE**。解压后各条目内容完全相同，但整文件字节不同，原因是 zip 条目时间戳（秒级）。→ T3/T5a 需固定 `ZipInfo.date_time` 或比较解压内容。
- XML 实体：**PASS**。`docx.oxml.parser` 源码为 `XMLParser(resolve_entities=False)`。zip 炸弹限制（解压总大小/条目数）仍需 T3 实现。

### 2.5 openpyxl
- 公式与缓存值：**PASS**。`data_only=False` 得到 `=A2*2`；`data_only=True` 得到 `None`。→ openpyxl 写出的公式无缓存，必须走 `FORMULA_NO_CACHED_VALUE`。
- 合并区域：**PASS**。`merged_cells.ranges = ['A4:B4']`，非左上角 `MergedCell` 值为 `None`。
- 隐藏判定：**PASS（仅单列样本）**。`row_dimensions[5].hidden`、`column_dimensions['E'].hidden`、`sheet_state == 'hidden'` 均可读取。**注意**：openpyxl 把连续隐藏列合并存为区间（`min..max`），以单字母键查询可能漏判。T3 必须遍历 `column_dimensions` 的 min/max 区间，并补测。
- 日期：**PASS**。`C2` 读回 `datetime(2024,3,5)`，`is_date=True`。
- 1904 纪元：**PASS**。1904 工作簿读回 `epoch` 为 1904；序列号 1 → `1904-01-02`。1900 默认下 `from_excel(1) = 1900-01-01`。**未验证**：1900 闰年错误日期（序列号 60）的处理。
- 百分比：**PASS**。`number_format == '0%'`，值 `0.15`；`15%` 的渲染由 XLSX_TEXT_V1 完成。
- 确定性：**NOTE**。同内容写两次，唯一内容不同的条目为 `docProps/core.xml`（openpyxl 保存时覆盖 `modified` 为当前时间）。→ T5a 写出后需规范化该条目，或比较时排除它；zip 时间戳同样需固定。
- defusedxml：**NOTE**。`openpyxl.xml.DEFUSEDXML = False`，defusedxml 未安装，openpyxl 不自动使用它。**需要用户批准新增依赖 `defusedxml`**（不在已批准清单内，本任务未安装）；若不批准，T3 必须自行限制 XML 大小与实体。

### 2.6 pydantic
- 结论：**PASS**。pydantic 2.14.4 可生成全部 BaseModel 的 JSON Schema；契约中的 `model_validator(mode="after")` 正反例通过（见 §3）。

## 3. 契约冒烟测试

`tests/test_contracts.py` 与 `tests/test_licenses.py` 共 83 项全部通过，未修改 `models.py` / `interfaces.py`。覆盖：
- `citecell.models` 与 `citecell.interfaces` 可导入；
- 模块内全部 BaseModel 子类（62 个）的 `model_json_schema()` 可生成；
- `RunConfig` 存在 `profile`、`prefilter`、`max_keys_per_window`、`pdf_backend`，且 `pdf_backend` 默认值为 `pypdfium2`；
- fuzzy 证据不能 grounded；not_found 不得带值；needs_review 需 warning/error；failed 需 error；exact 证据必须有偏移；MetricValue 比率不变量。

## 4. 许可证结论

完整清单见 `docs/THIRD_PARTY_LICENSES.md`（由 `scripts/check_licenses.py --all --write` 生成，全部结论来自已安装包的元数据与 LICENSE 文件）。

- 默认闭包（16 个包）：**FAIL 0，WARN 1**。
  - **WARN：`certifi 2026.11.20`，MPL-2.0**。经 httpx 传递引入，属于默认依赖。按 ARCH §13，MPL 需人工批准。**需要用户决定是否批准。**
  - pypdfium2：PASS（BSD-3-Clause），但随附 74 份许可文件，需 NOTICE 核对（见 §1）。
- 全部闭包（含 extras，29 个包）：FAIL 0，WARN 3。另有 `cffi`（MIT-0）与 `pillow`（MIT-CMU）为白名单外的 MIT 变体，需人工确认；`cryptography` 为 Apache-2.0 OR BSD-3-Clause（PASS）。
- `PyMuPDF`：未安装（按设计，NOTE）。`fitz` 与 `pymupdf` 均不可导入。
- ARCH §13 中原标"未核实"的许可证：本次通过元数据确认 pydantic/httpx/python-docx/openpyxl/pdfminer.six/pytest/reportlab（BSD 类，经 LICENSE 正文判定为 BSD-3-Clause）/ pypdfium2（wrapper）。`httpx` 为 BSD-3-Clause，`certifi` 为 MPL-2.0（与 ARCH 原表述不同，原表未列出）。

## 5. 未完成项与已知缺陷

1. 虚拟字符的**空格型**样本未覆盖（pypdfium2 与 pdfminer.six 均需 T4 补测）。
2. 两后端的**速度与鲁棒性未测**，§1 的推荐不包含性能依据。
3. 中文版 Word 模板的样式内部名未核实（无真实文档，且不允许使用真实文档）。
4. 隐藏列的区间判定只测了单列。
5. 1900 闰年日期错误未测。
6. pypdfium2 随附 74 份许可文件未逐一核验。
7. 视觉渲染（未嵌入中文字体）未验证。
