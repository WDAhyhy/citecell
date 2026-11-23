"""提示词与上下文规划（ARCHITECTURE §6.5–§6.6，T8）。

公共入口：

* :class:`HeuristicTokenCounter` — 启发式 token 计数（宁高勿低，未校准）；
* :class:`DefaultContextPlanner` — 整文档直送或按键 BM25 选块，读取 ``RunConfig.prefilter`` 与 ``max_keys_per_window``；
* :class:`PromptRendererV1` — ``extract_v1`` / ``retry_v1`` 模板渲染与输出 Schema；
* :func:`parse_llm_output` — 模型输出解析与键对账。
"""

from citecell.prompting.output import build_output_schema, parse_llm_output
from citecell.prompting.planner import DefaultContextPlanner
from citecell.prompting.renderer import PromptRendererV1, render_block_line, render_document_lines
from citecell.prompting.tokens import HeuristicTokenCounter

__all__ = [
    "DefaultContextPlanner",
    "HeuristicTokenCounter",
    "PromptRendererV1",
    "build_output_schema",
    "parse_llm_output",
    "render_block_line",
    "render_document_lines",
]
