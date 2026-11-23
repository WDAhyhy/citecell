"""把窗口渲染为聊天消息与输出 Schema（ARCHITECTURE §6.6）。

行格式：

* 段落 / 列表 / 其它：``[p3-b12] 正文``；标题：``[p3-b12] # 标题``；
* 表格 ``cell_lines``（默认）：``[s1-B3 | 单价 | 螺丝M6] 25.00``（列表头、行表头以 `` / `` 连接，缺省则省略该段）；
* 表格 ``row_lines``：每个表格先输出一行 ``表 s1「订单」 列: A=产品 | B=单价``，再逐行输出
  ``[s1-A3] 螺丝M6 | [s1-B3] 25.00``；
* 隐藏块在编号后加 `` (隐藏)``，例如 ``[s1-C2 (隐藏) | 备注] 内容``；
* 块文本中的换行与制表符替换为空格，保证一块一行。

模板文件版本化，``template_sha256`` 为模板文件字节的 sha256。文档内容放在随机性无关、由 doc_id 派生的
数据标记之间，并在系统提示中声明其为数据而非指令。日志只记录计数，不记录正文。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import string
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from citecell.models import (
    Block,
    BlockFlag,
    BlockKind,
    ChatMessage,
    ContextWindow,
    ExtractionRecord,
    KeySpec,
    ParsedDocument,
    PromptBundle,
    RunConfig,
)
from citecell.prompting.output import build_output_schema, parse_llm_output

logger = logging.getLogger("citecell.prompting")

TEMPLATES_DIR = Path(__file__).parent / "templates"
EXTRACT_TEMPLATE_FILE = "extract_v1.zh.txt"
RETRY_TEMPLATE_FILE = "retry_v1.zh.txt"
EXTRACT_TEMPLATE_ID = "extract_v1"
RETRY_TEMPLATE_ID = "retry_v1"

_XLSX_CELL_ID = re.compile(r"^s\d+-([A-Z]{1,3})\d+$")

_TABLE_NOTE = {
    "cell_lines": "表格说明：表格单元格形如 [块编号 | 列表头 | 行表头] 单元格内容；方括号中的列表头与行表头用于理解该单元格的含义。",
    "row_lines": "表格说明：每个表格先列出列字母与列表头的对应关系（A=列表头），再逐行列出单元格，形如 [块编号] 单元格内容。",
}

_ISSUE_HINTS = {
    "quote_not_in_block": "引文不在所指块中，请从该块正文逐字复制",
    "block_not_found": "块编号不存在，请只使用文档中出现的编号",
    "value_not_in_quote": "值不在引文中，引文应包含该值",
    "empty_evidence_with_value": "给出了值但没有证据",
    "evidence_without_value": "给出了证据但没有值",
    "output_schema_invalid": "输出不是合法 JSON 或不符合结构，请只输出一个 JSON 对象",
    "key_missing_in_output": "输出中缺少该键，请输出该键的记录",
    "value_unparseable": "值无法按键的类型解析",
    "enum_out_of_range": "值不在可选值范围内",
    "unit_mismatch": "单位与键的期望单位不一致",
}


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_sections(path: Path) -> dict[str, str]:
    """模板文件以 ``---NAME---`` 分段。"""
    text = path.read_text(encoding="utf-8")
    sections: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        m = re.fullmatch(r"---([A-Z]+)---", line.strip())
        if m:
            if current is not None:
                sections[current] = "\n".join(buf).strip("\n")
            current, buf = m.group(1), []
        else:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip("\n")
    return sections


# ---------------------------------------------------------------------------
# 块行渲染
# ---------------------------------------------------------------------------


def _clean(text: str) -> str:
    return text.replace("\r", " ").replace("\n", " ").replace("\t", " ")


def _col_letter(index0: int) -> str:
    n = index0 + 1
    out = ""
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(ord("A") + rem) + out
    return out


def _cell_letter(block: Block) -> str:
    m = _XLSX_CELL_ID.match(block.block_id)
    if m:
        return m.group(1)
    assert block.table is not None
    return _col_letter(block.table.col_index)


def _hidden(block: Block) -> str:
    return " (隐藏)" if BlockFlag.HIDDEN in block.flags else ""


def _join_nonempty(parts: Sequence[str]) -> str:
    return " | ".join(p for p in parts if p)


def render_block_line(block: Block, table_style: str = "cell_lines") -> str:
    """单个块在 ``cell_lines`` 风格下的一行（``row_lines`` 下的单元格片段也使用同一 ``[id] 文本`` 形式）。"""
    text = _clean(block.text)
    if block.kind == BlockKind.TABLE_CELL:
        assert block.table is not None
        if table_style == "cell_lines":
            head = _join_nonempty(
                [
                    f"{block.block_id}{_hidden(block)}",
                    " / ".join(block.table.column_headers),
                    " / ".join(block.table.row_headers),
                ]
            )
            return f"[{head}] {text}".rstrip()
        return f"[{block.block_id}{_hidden(block)}] {text}".rstrip()
    if block.kind == BlockKind.HEADING:
        text = f"# {text}"
    return f"[{block.block_id}{_hidden(block)}] {text}".rstrip()


def _render_table_rows(run: Sequence[Block], doc: ParsedDocument) -> list[str]:
    """``row_lines``：一段连续的同一表格单元格 → 列映射行 + 逐行行。"""
    assert run[0].table is not None
    table_id = run[0].table.table_id
    title = ""
    m = re.fullmatch(r"s(\d+)", table_id)
    if m and doc.info.sheet_names:
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(doc.info.sheet_names):
            title = f"「{doc.info.sheet_names[idx]}」"

    col_headers: dict[str, str] = {}
    for cell in run:
        assert cell.table is not None
        letter = _cell_letter(cell)
        if letter not in col_headers and cell.table.column_headers:
            col_headers[letter] = " / ".join(cell.table.column_headers)
    header = f"表 {table_id}{title}"
    if col_headers:
        header += " 列: " + " | ".join(f"{k}={v}" for k, v in col_headers.items())
    lines = [header]

    rows: dict[int, list[str]] = {}
    for cell in run:
        assert cell.table is not None
        rows.setdefault(cell.table.row_index, []).append(render_block_line(cell, "row_lines"))
    for row_index in rows:
        lines.append(" | ".join(rows[row_index]))
    return lines


def render_document_lines(
    doc: ParsedDocument,
    blocks: Sequence[Block],
    table_style: str = "cell_lines",
) -> list[str]:
    """渲染给定块（已按文档顺序）为提示词文本行。"""
    if table_style not in _TABLE_NOTE:
        raise ValueError(f"未知的 table_style: {table_style}")
    lines: list[str] = []
    i = 0
    n = len(blocks)
    while i < n:
        b = blocks[i]
        if table_style == "row_lines" and b.kind == BlockKind.TABLE_CELL:
            assert b.table is not None
            j = i
            run: list[Block] = []
            while j < n and blocks[j].kind == BlockKind.TABLE_CELL and blocks[j].table is not None and blocks[j].table.table_id == b.table.table_id:
                run.append(blocks[j])
                j += 1
            lines.extend(_render_table_rows(run, doc))
            i = j
            continue
        lines.append(render_block_line(b, table_style))
        i += 1
    return lines


def _window_blocks(doc: ParsedDocument, window: ContextWindow, config: RunConfig) -> list[Block]:
    bmap = doc.block_map()
    missing = [bid for bid in window.block_ids if bid not in bmap]
    if missing:
        raise ValueError(f"窗口 {window.window_id} 引用了不存在的块（数量 {len(missing)}）")
    blocks = sorted((bmap[bid] for bid in window.block_ids), key=lambda b: b.order)
    if not config.include_hidden_cells:
        blocks = [b for b in blocks if BlockFlag.HIDDEN not in b.flags]
    return blocks


def _format_key(spec: KeySpec) -> str:
    parts = [f"- 键：{spec.key}", f"类型：{spec.value_type.value}"]
    if spec.description:
        parts.append(f"说明：{spec.description}")
    if spec.aliases:
        parts.append("别名：" + "、".join(spec.aliases))
    if spec.enum_options:
        parts.append("可选值：" + "、".join(o.value for o in spec.enum_options))
    if spec.unit:
        parts.append(f"期望单位：{spec.unit}")
    if spec.value_type.value in ("money",):
        parts.append(f"币种：{spec.currency}")
    return "；".join(parts)


class PromptRendererV1:
    """``extract_v1`` / ``retry_v1`` 模板的渲染器。"""

    template_id = EXTRACT_TEMPLATE_ID

    def __init__(self, templates_dir: Path = TEMPLATES_DIR) -> None:
        extract_path = templates_dir / EXTRACT_TEMPLATE_FILE
        retry_path = templates_dir / RETRY_TEMPLATE_FILE
        self.template_sha256 = _sha256_file(extract_path)
        self._retry_sha256 = _sha256_file(retry_path)
        sections = _load_sections(extract_path)
        if "SYSTEM" not in sections or "USER" not in sections:
            raise ValueError("extract 模板缺少 SYSTEM 或 USER 段")
        self._system = sections["SYSTEM"]
        self._user_tpl = string.Template(sections["USER"])
        retry_sections = _load_sections(retry_path)
        if "USER" not in retry_sections:
            raise ValueError("retry 模板缺少 USER 段")
        self._retry_tpl = string.Template(retry_sections["USER"])

    # -- 公共接口 ---------------------------------------------------------

    def render_extraction(
        self,
        doc: ParsedDocument,
        window: ContextWindow,
        keys: Sequence[KeySpec],
        config: RunConfig,
    ) -> PromptBundle:
        selected = self._select_keys(keys, window.keys)
        blocks = _window_blocks(doc, window, config)
        user = self._user_tpl.substitute(
            keys_block="\n".join(_format_key(k) for k in selected),
            data_open=self._data_open(doc),
            document_block="\n".join(render_document_lines(doc, blocks, config.table_style)),
            data_close=self._data_close(doc),
            table_note=_TABLE_NOTE[config.table_style],
        )
        logger.debug("渲染窗口 %s：%d 块，%d 键", window.window_id, len(blocks), len(selected))
        return self._bundle(window, selected, user, config, EXTRACT_TEMPLATE_ID, self.template_sha256)

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
        failed_names = [r.key for r in failed]
        if not failed_names:
            raise ValueError("render_retry 需要至少一个失败的键")
        unknown = [n for n in failed_names if n not in window.keys]
        if unknown:
            raise ValueError("失败的键不在窗口内")
        selected = self._select_keys(keys, failed_names)

        prior = self._prior_entries(previous_output, failed_names, window)
        failed_lines = []
        for rec in failed:
            codes = list(dict.fromkeys(i.code.value for i in rec.issues))
            hints = [f"{c}（{_ISSUE_HINTS[c]}）" if c in _ISSUE_HINTS else c for c in codes]
            line = f"- 键：{rec.key}；错误：{'；'.join(hints) if hints else '未知'}"
            if rec.key in prior:
                line += "；上次输出：" + json.dumps(prior[rec.key], ensure_ascii=False)
            failed_lines.append(line)

        blocks = _window_blocks(doc, window, config)
        user = self._retry_tpl.substitute(
            failed_block="\n".join(failed_lines),
            data_open=self._data_open(doc),
            document_block="\n".join(render_document_lines(doc, blocks, config.table_style)),
            data_close=self._data_close(doc),
            table_note=_TABLE_NOTE[config.table_style],
        )
        logger.debug("重试窗口 %s：%d 个失败键", window.window_id, len(failed_names))
        return self._bundle(window, selected, user, config, RETRY_TEMPLATE_ID, self._retry_sha256)

    def output_schema(self, keys: Sequence[KeySpec], block_ids: Sequence[str] | None) -> dict[str, Any]:
        return build_output_schema(keys, block_ids)

    # -- 内部 -------------------------------------------------------------

    @staticmethod
    def _select_keys(keys: Sequence[KeySpec], names: Sequence[str]) -> list[KeySpec]:
        wanted = set(names)
        selected = [k for k in keys if k.key in wanted]
        if {k.key for k in selected} != wanted:
            missing = sorted(wanted - {k.key for k in selected})
            raise ValueError(f"缺少键定义: {', '.join(missing)}")
        return selected

    @staticmethod
    def _data_open(doc: ParsedDocument) -> str:
        return f"<<<BEGIN_DOCUMENT_DATA {doc.info.doc_id}>>>"

    @staticmethod
    def _data_close(doc: ParsedDocument) -> str:
        return f"<<<END_DOCUMENT_DATA {doc.info.doc_id}>>>"

    def _bundle(
        self,
        window: ContextWindow,
        selected: Sequence[KeySpec],
        user: str,
        config: RunConfig,
        template_id: str,
        template_sha256: str,
    ) -> PromptBundle:
        block_ids = list(window.block_ids) if config.constrain_block_ids else None
        schema = build_output_schema(selected, block_ids)
        return PromptBundle(
            window_id=window.window_id,
            keys=[k.key for k in selected],
            messages=[
                ChatMessage(role="system", content=self._system),
                ChatMessage(role="user", content=user),
            ],
            output_schema=schema,
            template_id=template_id,
            template_sha256=template_sha256,
            table_style=config.table_style,
        )

    @staticmethod
    def _prior_entries(previous_output: str, failed_names: Sequence[str], window: ContextWindow) -> dict[str, dict[str, Any]]:
        """从上一次输出中只取失败键的条目（其它键不得出现在重试提示中）。"""
        if not previous_output:
            return {}
        parsed, _issues = parse_llm_output(previous_output, list(window.keys))
        if parsed is None:
            return {}
        wanted = set(failed_names)
        out: dict[str, dict[str, Any]] = {}
        for f in parsed.fields:
            if f.key in wanted and f.key not in out:
                out[f.key] = {
                    "value": f.value,
                    "evidence": [{"block_id": e.block_id, "quote": e.quote} for e in f.evidence],
                }
        return out


__all__ = ["PromptRendererV1", "render_block_line", "render_document_lines", "TEMPLATES_DIR"]
