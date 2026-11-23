"""Realizer：把事实表排版为 DocIR。

覆盖旋钮：layout（fixed/perturbed/free）、surface_variety、distractors_per_key、missing_rate
（在 sampler 中）、duplicate_rate、filler_blocks、cross_block_rate（仅 DOCX 自由段落）、
table_style（仅 DOCX 固定/扰动表格）、hidden_row_rate（仅 XLSX）、noise_rate。
conflict_rate / derived_rate 为 M3 旋钮，M1 中非零即报错。

XLSX 的版面只有一张"项目｜内容"表：layout 只影响标签（别名）与行序；table_style、
cross_block_rate 对 XLSX 不生效（已知限制，记录在 synth 模块文档中）。
"""

from __future__ import annotations

import datetime as _dt
import random
from typing import Any

from citecell.models import (
    BooleanValue,
    DateValue,
    DifficultyConfig,
    EnumValue,
    KeySet,
    KeySpec,
    MoneyValue,
    NumberValue,
    PercentValue,
    TextValue,
)
from citecell.synth.ir import DocIR, ParaIR, Seg, SheetRow, Span, TableIR
from citecell.synth.sampler import ABSENT, make_distractors
from citecell.synth.surface import (
    amount_to_uppercase,
    arabic_date,
    cn_date,
    cn_int,
    money_plain,
    money_wan,
    percent_cn,
    percent_sign,
    xlsx_cell_text,
)

DISTRACTOR_LABELS = ("初报价", "修订前", "草案", "历史版本", "备选报价")
FILLER_TEXTS = (
    "本合同未尽事宜，由双方另行协商解决。",
    "双方应依法履行本合同约定的全部义务。",
    "本文件仅供内部流程演示使用。",
    "如有争议，双方首先应通过友好协商解决。",
    "本页内容以正式签署版本为准。",
    "相关附件与本合同具有同等法律效力。",
)
_TITLE = "采购合同"
_SENTENCES_PER_PARAGRAPH = 3


class _Builder:
    def __init__(self, rng: random.Random, diff: DifficultyConfig, keyset: KeySet, fmt: str) -> None:
        self.rng = rng
        self.diff = diff
        self.fmt = fmt
        self.layout = diff.layout.value
        self.specs: dict[str, KeySpec] = {k.key: k for k in keyset.keys}
        self.keyset = keyset
        self.spans: list[Span] = []
        self._sid = 0

    # ------------------------------------------------------------------ 值出现
    def span(self, key: str, role: str, value: Any) -> Span:
        self._sid += 1
        if self.fmt == "xlsx":
            cell_value, nf, surface, tags = self._xlsx_cell(self.specs[key], value)
        else:
            cell_value, nf = None, None
            surface, tags = self._docx_surface(self.specs[key], value)
        sp = Span(self._sid, key, role, surface, cell_value, nf, tags)
        self.spans.append(sp)
        return sp

    def _option_forms(self, spec: KeySpec, option_value: str) -> list[str]:
        for opt in spec.enum_options or []:
            if opt.value == option_value:
                return [opt.value, *opt.aliases]
        raise ValueError(f"键 {spec.key!r} 没有选项 {option_value!r}")

    def _bool_forms(self, spec: KeySpec, value: bool) -> list[str]:
        """布尔值的表面形式只能是别名（ARCH §6.3）。"""
        opt_value = "true" if value else "false"
        for opt in spec.enum_options or []:
            if opt.value == opt_value and opt.aliases:
                return list(opt.aliases)
        raise ValueError(f"键 {spec.key!r} 缺少 {opt_value} 的别名")

    def _docx_surface(self, spec: KeySpec, value: Any) -> tuple[str, tuple[str, ...]]:
        rng = self.rng
        alt = rng.random() < self.diff.surface_variety
        if isinstance(value, TextValue):
            return value.text, ("text",)
        if isinstance(value, MoneyValue):
            amt = value.amount
            frac = amt != amt.to_integral_value()
            wan_ok = (not frac) and amt >= 10000 and amt % 10000 == 0
            if alt:
                kind = rng.choice(["uppercase_money"] + (["wan_money"] if wan_ok else []))
            else:
                kind = "wan_money" if wan_ok else "plain_money"
            if kind == "wan_money":
                text = money_wan(amt)
            elif kind == "plain_money":
                text = money_plain(amt)
            else:
                text = amount_to_uppercase(amt, suffix_zheng=not frac)
            return text, (kind,)
        if isinstance(value, PercentValue):
            if alt:
                return percent_cn(value.value), ("cn_percent",)
            return percent_sign(value.value), ("percent_sign",)
        if isinstance(value, DateValue):
            day = _dt.date.fromisoformat(value.iso)
            if alt:
                kind = rng.choice(["iso_date", "cn_numeral_date"])
                text = day.isoformat() if kind == "iso_date" else cn_date(day)
                return text, (kind,)
            return arabic_date(day), ("arabic_date",)
        if isinstance(value, NumberValue):
            n = int(value.value)
            unit = value.unit or ""
            suffix = "个" + unit if unit == "月" else unit
            if alt:
                return cn_int(n) + suffix, ("cn_number",)
            return f"{n}{suffix}", ("arabic_number",)
        if isinstance(value, EnumValue):
            forms = self._option_forms(spec, value.value)
            if alt and len(forms) > 1:
                return rng.choice(forms[1:]), ("enum_alias",)
            return forms[0], ("enum_canonical",)
        if isinstance(value, BooleanValue):
            forms = self._bool_forms(spec, value.value)
            text = rng.choice(forms) if alt else forms[0]
            return text, ("boolean_alias",)
        raise TypeError(f"未知值类型: {type(value).__name__}")

    def _xlsx_cell(self, spec: KeySpec, value: Any) -> tuple[Any, str | None, str, tuple[str, ...]]:
        """XLSX 单元格的原始值、数字格式与 XLSX_TEXT_V1 文本。表面形式变化仅作用于字符串型单元格。"""
        rng = self.rng
        alt = rng.random() < self.diff.surface_variety
        nf: str | None = None
        if isinstance(value, TextValue):
            cell: Any = value.text
        elif isinstance(value, MoneyValue):
            cell = int(value.amount) if value.amount == value.amount.to_integral_value() else float(value.amount)
        elif isinstance(value, PercentValue):
            cell = float(value.value / 100)
            nf = "0%"
        elif isinstance(value, DateValue):
            cell = _dt.datetime.combine(_dt.date.fromisoformat(value.iso), _dt.time())
            nf = "yyyy-mm-dd"
        elif isinstance(value, NumberValue):
            cell = int(value.value) if value.value == value.value.to_integral_value() else float(value.value)
        elif isinstance(value, EnumValue):
            forms = self._option_forms(spec, value.value)
            cell = rng.choice(forms[1:]) if alt and len(forms) > 1 else forms[0]
        elif isinstance(value, BooleanValue):
            forms = self._bool_forms(spec, value.value)
            cell = rng.choice(forms) if alt else forms[0]
        else:
            raise TypeError(f"未知值类型: {type(value).__name__}")
        return cell, nf, xlsx_cell_text(cell, nf), ("xlsx_cell",)

    def value_segs(self, key: str, value: Any, dists: list[Any]) -> list[Seg]:
        """DOCX：真值后紧跟括号列出的干扰值，如 "120万元（初报价：100万元）"。"""
        segs: list[Seg] = [self.span(key, "value", value)]
        if dists:
            segs.append("（")
            for i, d in enumerate(dists):
                if i:
                    segs.append("；")
                segs.append(f"{DISTRACTOR_LABELS[i]}：")
                segs.append(self.span(key, "distractor", d))
            segs.append("）")
        return segs

    # ------------------------------------------------------------------ 公共
    def label(self, key: str) -> str:
        """固定版面用键名；扰动/自由版面随机选别名。噪声：末尾加全角空格。"""
        spec = self.specs[key]
        if self.layout == "fixed" or not spec.aliases:
            text = key
        else:
            text = self.rng.choice([key, *spec.aliases])
        if self.rng.random() < self.diff.noise_rate:
            text += "　"
        return text

    def filler_text(self) -> str:
        return self.rng.choice(FILLER_TEXTS)

    def _order(self, facts: dict[str, Any]) -> list[KeySpec]:
        order = list(self.keyset.keys)
        if self.layout != "fixed":
            self.rng.shuffle(order)
        return [s for s in order if facts[s.key] is not ABSENT]

    def _distractors(self, present: list[KeySpec], facts: dict[str, Any]) -> dict[str, list[Any]]:
        n = self.diff.distractors_per_key
        if n == 0:
            return {s.key: [] for s in present}
        return {s.key: make_distractors(s, facts[s.key], self.rng, n) for s in present}

    def _duplicates(self, present: list[KeySpec]) -> list[str]:
        return [s.key for s in present if self.rng.random() < self.diff.duplicate_rate]

    # ------------------------------------------------------------------ DOCX
    def build_docx(self, facts: dict[str, Any]) -> DocIR:
        ir = DocIR(fmt="docx", layout=self.layout)
        present = self._order(facts)
        dists = self._distractors(present, facts)
        dups = self._duplicates(present)
        blocks: list[ParaIR | TableIR] = [ParaIR([_TITLE], heading_level=1)]

        if self.layout == "free":
            blocks.extend(self._free_paragraphs(present, facts, dists))
        else:
            blocks.append(self._table(present, facts, dists))

        if dups:
            summary: list[Seg] = ["摘要："]
            for key in dups:
                summary.append(f"{key}为")
                summary.append(self.span(key, "value", facts[key]))
                summary.append("；")
            summary[-1] = "。"
            blocks.append(ParaIR(summary))

        for _ in range(self.diff.filler_blocks):
            blocks.insert(self.rng.randint(1, len(blocks)), ParaIR([self.filler_text()]))

        ir.blocks = blocks
        return ir

    def _free_paragraphs(self, present: list[KeySpec], facts: dict[str, Any], dists: dict[str, list[Any]]) -> list[ParaIR]:
        paras: list[ParaIR] = []
        cur: list[Seg] = []
        count = 0
        for spec in present:
            key = spec.key
            label = self.label(key)
            cross = self.rng.random() < self.diff.cross_block_rate
            value_segs = self.value_segs(key, facts[key], dists[key])
            if cross:
                cur.append(f"{label}为")
                paras.append(ParaIR(cur))
                cur = []
            else:
                cur.append(f"{label}为")
            cur.extend(value_segs)
            cur.append("。")
            count += 1
            if count % _SENTENCES_PER_PARAGRAPH == 0:
                paras.append(ParaIR(cur))
                cur = []
        if cur:
            paras.append(ParaIR(cur))
        return paras

    def _table(self, present: list[KeySpec], facts: dict[str, Any], dists: dict[str, list[Any]]) -> TableIR:
        rows: list[list[list[Seg]]] = []
        merges: list[tuple[int, int, int, int]] = []
        style = self.diff.table_style
        if style in ("merged_header", "multi_row_header"):
            rows.append([["采购要素"], []])
            merges.append((0, 0, 0, 1))
        if style == "multi_row_header":
            rows.append([["基础信息"], []])
            merges.append((1, 0, 1, 1))
        rows.append([["项目"], ["内容"]])
        for spec in present:
            key = spec.key
            rows.append([[self.label(key)], self.value_segs(key, facts[key], dists[key])])
        return TableIR(rows=rows, merges=merges)

    # ------------------------------------------------------------------ XLSX
    def build_xlsx(self, facts: dict[str, Any]) -> DocIR:
        ir = DocIR(fmt="xlsx", layout=self.layout)
        n = self.diff.distractors_per_key
        header = ["项目", "内容", *DISTRACTOR_LABELS[:n]]
        present = self._order(facts)
        dists = self._distractors(present, facts)
        dups = set(self._duplicates(present))
        data: list[SheetRow] = []
        for spec in present:
            key = spec.key
            hidden = self.rng.random() < self.diff.hidden_row_rate
            cells: list[Any] = [self.label(key), self.span(key, "value", facts[key])]
            cells += [self.span(key, "distractor", d) for d in dists[key]]
            data.append(SheetRow(cells=cells, hidden=hidden))
            if key in dups:
                data.append(SheetRow(cells=[f"{key}（复核）", self.span(key, "value", facts[key])]))
        for _ in range(self.diff.filler_blocks):
            pos = self.rng.randint(0, len(data))
            data.insert(pos, SheetRow(cells=[self.filler_text()]))
        if self.rng.random() < self.diff.hidden_row_rate:
            pos = self.rng.randint(0, len(data))
            data.insert(pos, SheetRow(cells=["内部编号", f"IN-{self.rng.randint(1000, 9999)}"], hidden=True))
        ir.rows = [SheetRow(cells=header), *data]
        return ir


def build_ir(facts: dict[str, Any], keyset: KeySet, diff: DifficultyConfig, rng: random.Random, fmt: str) -> DocIR:
    """事实表 → DocIR。所有随机性来自 ``rng``。"""
    if diff.conflict_rate > 0 or diff.derived_rate > 0:
        raise ValueError("M1 生成器不支持 conflict_rate / derived_rate > 0（属于 M3）")
    if fmt not in ("docx", "xlsx", "pdf"):
        raise ValueError(f"build_ir 不支持格式 {fmt!r}")
    # PDF 与 DOCX 共用同一块形状（pdf_writer 接受 ir.fmt 为 "docx" 或 "pdf"），仅标记格式不同。
    builder = _Builder(rng, diff, keyset, "xlsx" if fmt == "xlsx" else "docx")
    if fmt == "xlsx":
        ir = builder.build_xlsx(facts)
    else:
        ir = builder.build_docx(facts)
        ir.fmt = fmt
    ir.spans = builder.spans
    return ir
