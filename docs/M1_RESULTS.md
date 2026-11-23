# M1 评测结果

> **这是仿真后端的结果，不是模型准确率。**
> 本页数字由 `scripts/run_m1_eval.py` 生成后粘贴。后端是 Oracle（金标准完美输出）与 NoisyOracle（按类型注入错误）。
> 它们只说明「流水线与校验器在这些注入错误下的行为」，**不代表任何真实模型在任何文档上的准确率**。
> 指标定义见 `docs/ARCHITECTURE.md` §9。本页只含合成数据，合成集与真实集分开报告。

## 状态与语料

- 语料：合成**短文档**（约 200～560 个 token），**不能检验长文档分窗、预筛选与 CPU 档预算策略**（ADR-019）。中等难度，docx/xlsx/pdf 各 5 份。
- 真实模型冒烟（TASKS T11 验收 8）：**未运行**（未设置 `CITECELL_E2E_BASE_URL` / `CITECELL_E2E_MODEL`，测试被跳过）。本页没有任何真实模型的数字。

## 复现

### A. NoisyOracle 注入错误（`configs/runs/m1_synth_medium.json`）

```
python scripts/run_m1_eval.py --config configs/runs/m1_synth_medium.json
run_id: m1_synth_medium
config_file_sha256: 780fb21267387a7cb6fc360182a2debe52d3b1a902aef54736099dd354d6f4b3
manifest_sha256: 531bdd08359b9e08cd0c91f36484bd3ad829263715f24dbe5705aaeb4f2f414e
config_sha256 (RunConfig, manifest): 23105dbdd00bbb1b57c9ccbdc44a85982c4e741781d8fbd870d4e44a21854f0b
keyset_sha256: 5387a7d982c8e29167ae17c488db4ebaa77a731b84c44143f748793b72ce628a
dataset_sha256: 050c3b18440c9701c25e60a27de8d1e88ec9c746d6f0cd0410d7c4ae61a1dbc4
documents: 15  injections: 110
```

### B. Oracle 流水线上限（`configs/runs/m1_synth_medium_oracle.json`）

```
python scripts/run_m1_eval.py --config configs/runs/m1_synth_medium_oracle.json
run_id: m1_synth_medium_oracle
config_file_sha256: 4c93153a08fb7b091fd1384332dfc0f774c27c885b5bd21ed3661e8c7610b39b
manifest_sha256: 5c92bf8fa41e996199233419cb6b0a3e9eebcd84f27443108957ea570cf2d635
config_sha256 (RunConfig, manifest): c07b3853341d9c8b9ddf74f5826f95bf0c3125011d85458c2188c0eafe472598
keyset_sha256: 5387a7d982c8e29167ae17c488db4ebaa77a731b84c44143f748793b72ce628a
dataset_sha256: 050c3b18440c9701c25e60a27de8d1e88ec9c746d6f0cd0410d7c4ae61a1dbc4
documents: 15  injections: 0
```

- 运行产物：`outputs/runs/<run_id>/`（manifest.json、results.json、eval.json、eval.md），`outputs/` 已被 .gitignore 忽略。
- `manifest_sha256` 是该次 `manifest.json` 的文件字节哈希；清单含 `created_at`，每次运行的文件哈希都不同。复现时以 `config_sha256`、`keyset_sha256`、`dataset_sha256` 与下表数字为准。

## 自上一版以来的变化（事实记录）

上一版数字来自旧校验器与旧评测口径。本版之间有四处变化，叠加影响数字：
1. 合成器（T5a）：XLSX 金额新增表头单位、万元表头、数字格式三种形式；语料内容变化，`dataset_sha256` 由 `3c99d4e2…` 变为 `050c3b18…`，因此 NoisyOracle 的注入计数也随之变化。
2. 校验器（T7）：整块文本绑定、符号/序数/问句/否定守卫、表头单位、值区间选择。
3. Oracle（T6）：按金标准位置选择证据。
4. 评测器（T9）：定位与证据支持改为**值口径**（需传入 `parsed`），并报告口径字段与回退条数。旧版为引文口径。

NoisyOracle 的拦截率与旧版不同，原因包括上述 1～2 点；未调整配置以改变结果。

## A. NoisyOracle 结果（`m1_synth_medium`，合成集，15 份）

- 定位口径：值区间（PDF 用解析片段还原；DOCX 用值字符区间；XLSX 用单元格）
- 定位口径计数：正确记录 111 条，其中值口径 102 条；回退引文口径 9 条
- 证据锚定口径：值区间（PDF 用解析片段还原；DOCX 用值字符区间；XLSX 用单元格）
- 证据锚定计数：有值区间证据的记录 110 条，其中值口径 110 条；回退引文口径 0 条；既非支持也非干扰 0 条

以下表格原样粘贴自 `outputs/runs/m1_synth_medium/eval.md` 的 `all` 切片。
- `intercept_rate:*` 分母很小（每类型 1～29 次注入），多数标注「样本过少」，置信区间很宽。
- `silent_error_rate` 6/110：来源未按注入类型逐类归因（DISTRACTOR_VALUE 是其中一类，未核对其余）。
- `needs_review_hit_rate` 0/18：口径未逐条核对，不据此作结论。
- `attempt_latency_ms` 为 0：仿真后端无真实延迟；token 数无数据。

#### 切片 `all`（记录数 135）

| 指标 | 分子 | 分母 | 值 | CI（Wilson，双侧） | 备注 |
|---|---:|---:|---:|---|---|
| `value_precision` | 111 | 117 | 0.9487 | [0.8926, 0.9763] |  |
| `value_recall` | 111 | 127 | 0.8740 | [0.8051, 0.9209] |  |
| `value_f1` | 222 | 244 | 0.9098 | [0.8673, 0.9397] |  |
| `value_accuracy` | 111 | 127 | 0.8740 | [0.8051, 0.9209] |  |
| `text_char_f1_micro` | 802 | 881 | 0.9103 | [0.8896, 0.9275] |  |
| `citation_validity_final` | 127 | 127 | 1.0000 | [0.9706, 1.0000] |  |
| `localization_accuracy` | 104 | 111 | 0.9369 | [0.8755, 0.9691] |  |
| `localization_value_basis` | 102 | 111 | 0.9189 | [0.8531, 0.9568] |  |
| `localization_quote_fallback` | 9 | 111 | 0.0811 | [0.0432, 0.1469] |  |
| `evidence_support_rate` | 104 | 110 | 0.9455 | [0.8861, 0.9748] |  |
| `distractor_hit_rate` | 6 | 110 | 0.0545 | [0.0252, 0.1139] |  |
| `evidence_unanchored_rate` | 0 | 110 | 0.0000 | [0.0000, 0.0337] |  |
| `anchor_value_basis` | 110 | 110 | 1.0000 | [0.9663, 1.0000] |  |
| `anchor_quote_fallback` | 0 | 110 | 0.0000 | [0.0000, 0.0337] |  |
| `silent_error_rate` | 6 | 110 | 0.0545 | [0.0252, 0.1139] |  |
| `hallucination_rate_absent` | 0 | 8 | 0.0000 | [0.0000, 0.3244] | 样本过少 |
| `missed_present_rate` | 0 | 127 | 0.0000 | [0.0000, 0.0294] |  |
| `needs_review_ratio` | 18 | 135 | 0.1333 | [0.0860, 0.2009] |  |
| `needs_review_hit_rate` | 0 | 18 | 0.0000 | [0.0000, 0.1759] | 样本过少 |
| `intercept_rate:wrong_block_id` | 8 | 29 | 0.2759 | [0.1470, 0.4572] | 样本过少 |
| `intercept_rate:paraphrased_quote` | 9 | 15 | 0.6000 | [0.3575, 0.8018] | 样本过少 |
| `intercept_rate:value_not_in_quote` | 11 | 15 | 0.7333 | [0.4805, 0.8910] | 样本过少 |
| `intercept_rate:missing_key` | 4 | 16 | 0.2500 | [0.1018, 0.4950] | 样本过少 |
| `intercept_rate:hallucinated_value` | 9 | 16 | 0.5625 | [0.3318, 0.7690] | 样本过少 |
| `intercept_rate:distractor_value` | 2 | 8 | 0.2500 | [0.0715, 0.5907] | 样本过少 |
| `intercept_rate:cross_block_quote` | 1 | 10 | 0.1000 | [0.0179, 0.4042] | 样本过少 |
| `intercept_rate:malformed_json` | 1 | 1 | 1.0000 | [0.2065, 1.0000] | 样本过少 |
| `cache_hit_rate` | 0 | 45 | 0.0000 | [0.0000, 0.0787] |  |

| 分布 | 样本数 | 均值 | P50 | P90 | 最大 |
|---|---:|---:|---:|---:|---:|
| `attempt_latency_ms` | 45 | 0.000 | 0.000 | 0.000 | 0.000 |
| `attempt_prompt_tokens` | 0 | 无数据 | 无数据 | 无数据 | 无数据 |
| `attempt_completion_tokens` | 0 | 无数据 | 无数据 | 无数据 | 无数据 |
| `attempts_per_key` | 135 | 1.385 | 1.000 | 3.000 | 3.000 |

## B. Oracle 流水线上限（`m1_synth_medium_oracle`，合成集，15 份）

- 定位口径：值区间（PDF 用解析片段还原；DOCX 用值字符区间；XLSX 用单元格）
- 定位口径计数：正确记录 122 条，其中值口径 122 条；回退引文口径 0 条
- 证据锚定口径：值区间（PDF 用解析片段还原；DOCX 用值字符区间；XLSX 用单元格）
- 证据锚定计数：有值区间证据的记录 122 条，其中值口径 122 条；回退引文口径 0 条；既非支持也非干扰 0 条

以下表格原样粘贴自 `outputs/runs/m1_synth_medium_oracle/eval.md` 的 `all` 切片。
这是流水线正确性的参照：定位准确率、证据支持率均为满分且全部为值口径，干扰项命中与静默错误为 0。
`value_recall` 的缺口 5 条即为全部 `needs_review`（无任何单位线索的纯数字金额，设计如此，不猜单位）。

#### 切片 `all`（记录数 135）

| 指标 | 分子 | 分母 | 值 | CI（Wilson，双侧） | 备注 |
|---|---:|---:|---:|---|---|
| `value_precision` | 122 | 122 | 1.0000 | [0.9695, 1.0000] |  |
| `value_recall` | 122 | 127 | 0.9606 | [0.9111, 0.9831] |  |
| `value_f1` | 244 | 249 | 0.9799 | [0.9539, 0.9914] |  |
| `value_accuracy` | 122 | 127 | 0.9606 | [0.9111, 0.9831] |  |
| `text_char_f1_micro` | 920 | 920 | 1.0000 | [0.9958, 1.0000] |  |
| `citation_validity_final` | 127 | 127 | 1.0000 | [0.9706, 1.0000] |  |
| `localization_accuracy` | 122 | 122 | 1.0000 | [0.9695, 1.0000] |  |
| `localization_value_basis` | 122 | 122 | 1.0000 | [0.9695, 1.0000] |  |
| `localization_quote_fallback` | 0 | 122 | 0.0000 | [0.0000, 0.0305] |  |
| `evidence_support_rate` | 122 | 122 | 1.0000 | [0.9695, 1.0000] |  |
| `distractor_hit_rate` | 0 | 122 | 0.0000 | [0.0000, 0.0305] |  |
| `evidence_unanchored_rate` | 0 | 122 | 0.0000 | [0.0000, 0.0305] |  |
| `anchor_value_basis` | 122 | 122 | 1.0000 | [0.9695, 1.0000] |  |
| `anchor_quote_fallback` | 0 | 122 | 0.0000 | [0.0000, 0.0305] |  |
| `silent_error_rate` | 0 | 122 | 0.0000 | [0.0000, 0.0305] |  |
| `hallucination_rate_absent` | 0 | 8 | 0.0000 | [0.0000, 0.3244] | 样本过少 |
| `missed_present_rate` | 0 | 127 | 0.0000 | [0.0000, 0.0294] |  |
| `needs_review_ratio` | 5 | 135 | 0.0370 | [0.0159, 0.0838] |  |
| `needs_review_hit_rate` | 0 | 5 | 0.0000 | [0.0000, 0.4345] | 样本过少 |
| `cache_hit_rate` | 0 | 25 | 0.0000 | [0.0000, 0.1332] | 样本过少 |

| 分布 | 样本数 | 均值 | P50 | P90 | 最大 |
|---|---:|---:|---:|---:|---:|
| `attempt_latency_ms` | 25 | 0.000 | 0.000 | 0.000 | 0.000 |
| `attempt_prompt_tokens` | 0 | 无数据 | 无数据 | 无数据 | 无数据 |
| `attempt_completion_tokens` | 0 | 无数据 | 无数据 | 无数据 | 无数据 |
| `attempts_per_key` | 135 | 1.074 | 1.000 | 1.000 | 3.000 |

## 已知局限与设计边界

| 项 | 性质 | 说明 |
|---|---|---|
| XLSX 金额单元格和表头都没有单位线索的纯数字（金标准 tag `surface=bare_money_plain`） | 设计边界 | 判为 needs_review + VALUE_UNPARSEABLE，不猜单位。其余四种带单位线索的形式为 grounded（见 `tests/e2e/test_xlsx_money_forms.py`） |
| 合成文档为短文档 | 语料限制 | 不能支持长文档结论 |
| 真实模型冒烟 | 未运行 | 见上 |

## 未覆盖

- 真实模型准确率、长文档分窗与预算策略（ADR-019）、CPU/GPU 档位对比（ADR-016）、派生与冲突（M3）、复杂版面 PDF。
