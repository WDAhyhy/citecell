"""合成数据生成器（ARCHITECTURE §10）。

流水线：sampler（事实表）→ realize（DocIR）→ writers（DOCX/XLSX 写出并记账）→ gold（金标准）。

本包不得 import ``citecell.parsing`` / ``citecell.values`` / ``citecell.verify``，
也不得 import 任何 PyMuPDF 模块（金标准独立性与许可证约束，见 tests/synth/test_imports.py）。

已知限制（M1）：
* XLSX 版面只有一张"项目｜内容"表；table_style 与 cross_block_rate 不作用于 XLSX；
* XLSX 的值表面形式由 XLSX_TEXT_V1 决定，surface_variety 只作用于字符串型单元格；
* 不实现 SelfCheck（回读解析器）；PDF 写出由 T5b 的 pdf_writer 提供（与 DOCX 共用块形状）。
"""

from citecell.synth.generator import GENERATOR_VERSION, ProcurementGenerator, generate

__all__ = ["GENERATOR_VERSION", "ProcurementGenerator", "generate"]
