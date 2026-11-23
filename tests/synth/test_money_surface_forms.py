"""XLSX 金额单元格的表面形式：纯数字（表头带单位 / 万元 / 货币格式）与带"元"的文本。

金标准：value 为以元计的精确金额；mention 为金额单元格本身（sheet+cell），surface_text 为 XLSX_TEXT_V1 文本。
回读只用 openpyxl 与测试内的独立实现。
"""

import collections
import datetime as dt
import hashlib
from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest

from citecell.models import GenerationConfig, GoldDocument, ValueType, XlsxLocator
from citecell.synth.generator import generate
from citecell.synth.keysets import load_keyset, load_preset

MONEY_FORMS = {
    "surface=bare_money_header_yuan",
    "surface=bare_money_header_wanyuan",
    "surface=bare_money_currency_format",
    "surface=text_money_yuan",
}


def _money_key() -> str:
    return next(k.key for k in load_keyset().keys if k.value_type == ValueType.MONEY)


def _config(preset: str, n_docs: int, seed: int = 1, formats=("xlsx",)) -> GenerationConfig:
    return GenerationConfig(
        seed=seed,
        n_docs=n_docs,
        formats=list(formats),
        keyset=load_keyset(),
        difficulty=load_preset(preset),
    )


def _run(tmp_path: Path, preset: str, n_docs: int, seed: int = 1, formats=("xlsx",)):
    out = tmp_path / f"{preset}-{seed}"
    return out, list(generate(_config(preset, n_docs, seed, formats), out))


def _xlsx_text_v1_independent(value, number_format) -> str:
    if isinstance(value, dt.datetime):
        return f"{value.year:04d}-{value.month:02d}-{value.day:02d}"
    if isinstance(value, str):
        return value
    d = Decimal(str(value)) if isinstance(value, int) else Decimal(repr(value))
    pct = number_format is not None and "%" in number_format
    if pct:
        d = d * 100
    text = format(d.normalize(), "f")
    return text + "%" if pct else text


def _money_mentions(samples):
    key = _money_key()
    for s in samples:
        for rec in s.gold.records:
            if rec.key == key and rec.expected == "present":
                yield s, rec


def _cell_amount_yuan(ws, loc: XlsxLocator) -> Decimal:
    """按单元格本身的表达（表头单位、数字格式、文本后缀）换算为以元计的金额。"""
    raw = ws[loc.cell].value
    if isinstance(raw, str):
        assert raw.endswith("元"), raw
        return Decimal(raw[:-1])
    amount = Decimal(str(raw)) if isinstance(raw, int) else Decimal(repr(raw))
    header = ws.cell(row=1, column=loc.column_number).value or ""
    if "（万元）" in header:
        return amount * 10000
    return amount  # 表头无单位或带（元）→ 元


# ----------------------------------------------------------------- 红灯阶段：形式是否出现

@pytest.mark.parametrize("preset", ["medium", "hard"])
def test_new_money_forms_appear_in_medium_and_hard(tmp_path, preset):
    _, samples = _run(tmp_path, preset, n_docs=40)
    seen = collections.Counter()
    for _, rec in _money_mentions(samples):
        for m in rec.mentions:
            seen.update(t for t in rec.tags if t in MONEY_FORMS)
    for form in MONEY_FORMS:
        assert seen[form] > 0, f"{preset} 档未出现 {form}"


def test_easy_preset_keeps_bare_money_only(tmp_path):
    _, samples = _run(tmp_path, "easy", n_docs=12)
    for _, rec in _money_mentions(samples):
        assert not (set(rec.tags) & MONEY_FORMS), rec.tags


# ----------------------------------------------------------------- 回读与金标准

@pytest.mark.parametrize("preset", ["medium", "hard"])
def test_money_cells_read_back_with_unit_and_gold_amount(tmp_path, preset):
    out, samples = _run(tmp_path, preset, n_docs=40)
    checked = 0
    for s, rec in _money_mentions(samples):
        wb = openpyxl.load_workbook(out / s.relative_path)
        for m in list(rec.mentions) + list(rec.distractors):
            loc = m.locator
            assert isinstance(loc, XlsxLocator)
            ws = wb[loc.sheet_name]
            cell = ws[loc.cell]
            # 文本与金标准一致
            nf = cell.number_format if cell.number_format != "General" else None
            assert _xlsx_text_v1_independent(cell.value, nf) == m.surface_text, (s.relative_path, loc.cell)
            # 单位换算后的金额与金标准相等（主值；干扰值同理按同一规则换算）
            if m.surface_text and rec.value is not None and m in rec.mentions:
                assert _cell_amount_yuan(ws, loc) == rec.value.amount, (s.relative_path, loc.cell)
            checked += 1
    assert checked > 0


def test_header_of_money_cell_is_not_a_mention(tmp_path):
    out, samples = _run(tmp_path, "hard", n_docs=40)
    for s, rec in _money_mentions(samples):
        for m in rec.mentions:
            loc = m.locator
            assert loc.row_number != 1, "表头行不得作为提及"


@pytest.mark.parametrize("preset", ["medium", "hard"])
def test_gold_validates_and_hash_matches(tmp_path, preset):
    out, samples = _run(tmp_path, preset, n_docs=20)
    for s in samples:
        gold = GoldDocument.model_validate_json(s.gold.model_dump_json())
        assert gold.doc_sha256 == hashlib.sha256((out / s.relative_path).read_bytes()).hexdigest()


# ----------------------------------------------------------------- 确定性

def test_same_seed_identical_xlsx_bytes(tmp_path):
    out_a, _ = _run(tmp_path / "a", "hard", n_docs=12, seed=3)
    out_b, _ = _run(tmp_path / "b", "hard", n_docs=12, seed=3)
    names = sorted(p.name for p in out_a.glob("*.xlsx"))
    assert names
    for name in names:
        ha = hashlib.sha256((out_a / name).read_bytes()).hexdigest()
        hb = hashlib.sha256((out_b / name).read_bytes()).hexdigest()
        assert ha == hb, name
    assert (out_a / "gold.json").read_bytes() == (out_b / "gold.json").read_bytes()
