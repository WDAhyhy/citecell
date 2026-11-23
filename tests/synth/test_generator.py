"""合成生成器：金标准校验、python-docx / openpyxl 直接回读、确定性、难度旋钮。

回读只使用第三方库与本测试内的独立实现，不调用 CiteCell 解析器。
"""

import datetime as dt
import hashlib
import json
from decimal import Decimal
from pathlib import Path

import docx
import openpyxl
import pytest

from citecell.models import (
    DifficultyConfig,
    DocxLocator,
    GenerationConfig,
    GoldDocument,
    XlsxLocator,
)
from citecell.synth.generator import generate
from citecell.synth.keysets import load_keyset, load_preset
from citecell.synth.realize import build_ir
from citecell.synth.ir import block_signature
from citecell.synth.sampler import sample_facts
import random


def _config(diff: DifficultyConfig, n_docs: int = 6, formats=("docx", "xlsx"), seed: int = 1) -> GenerationConfig:
    return GenerationConfig(seed=seed, n_docs=n_docs, formats=list(formats), keyset=load_keyset(), difficulty=diff)


def _run(tmp_path: Path, diff: DifficultyConfig, **kw):
    out = tmp_path / "out"
    samples = list(generate(_config(diff, **kw), out))
    return out, samples


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _xlsx_text_v1_independent(value, number_format) -> str:
    """独立复核的 XLSX_TEXT_V1（与 surface.xlsx_cell_text 分别实现）。"""
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


# ----------------------------------------------------------------- 金标准结构与哈希

def test_generate_docx_xlsx_gold_valid_and_hash_matches(tmp_path):
    out, samples = _run(tmp_path, load_preset("medium"), n_docs=6)
    assert len(samples) == 6
    for s in samples:
        path = out / s.relative_path
        assert path.is_file(), s.relative_path
        gold = GoldDocument.model_validate_json(s.gold.model_dump_json())  # 校验器重跑
        assert gold.doc_sha256 == _sha(path)
        assert len(gold.records) == len(load_keyset().keys)
    assert (out / "gold.json").is_file()


# ----------------------------------------------------------------- 独立回读

def test_docx_mentions_read_back_with_python_docx(tmp_path):
    out, samples = _run(tmp_path, load_preset("hard"), n_docs=6, formats=("docx",))
    checked = 0
    for s in samples:
        document = docx.Document(str(out / s.relative_path))
        body = [c for c in document.element.body.iterchildren() if not c.tag.endswith("sectPr")]
        for rec in s.gold.records:
            for m in list(rec.mentions) + list(rec.distractors):
                loc = m.locator
                assert isinstance(loc, DocxLocator)
                child_tag = body[loc.body_index].tag.split("}")[1]
                if loc.paragraph_number is not None:
                    assert child_tag == "p"
                    text = document.paragraphs[loc.paragraph_number - 1].text
                else:
                    assert child_tag == "tbl"
                    cell = document.tables[loc.table_number - 1].cell(loc.row_index, loc.col_index)
                    text = cell.text
                    assert m.char_start is not None
                assert m.char_start is not None and m.char_end is not None
                assert text[m.char_start:m.char_end] == m.surface_text, (s.relative_path, rec.key)
                checked += 1
    assert checked > 0


def test_xlsx_cells_read_back_with_openpyxl(tmp_path):
    out, samples = _run(tmp_path, load_preset("hard"), n_docs=6, formats=("xlsx",))
    checked = 0
    for s in samples:
        wb = openpyxl.load_workbook(out / s.relative_path)
        for rec in s.gold.records:
            for m in list(rec.mentions) + list(rec.distractors):
                loc = m.locator
                assert isinstance(loc, XlsxLocator)
                ws = wb.worksheets[loc.sheet_index]
                assert ws.title == loc.sheet_name
                cell = ws[loc.cell]
                assert cell.row == loc.row_number and cell.column == loc.column_number
                text = _xlsx_text_v1_independent(cell.value, cell.number_format if cell.number_format != "General" else None)
                assert text == m.surface_text, (s.relative_path, rec.key, cell.value)
                checked += 1
    assert checked > 0


# ----------------------------------------------------------------- 确定性

def test_same_config_twice_gives_identical_bytes(tmp_path):
    diff = load_preset("medium")
    out_a, _ = _run(tmp_path / "a", diff, n_docs=6)
    out_b, _ = _run(tmp_path / "b", diff, n_docs=6)
    names = sorted(p.name for p in out_a.iterdir())
    assert names == sorted(p.name for p in out_b.iterdir())
    for name in names:
        assert _sha(out_a / name) == _sha(out_b / name), name
    gold_a = (out_a / "gold.json").read_bytes()
    gold_b = (out_b / "gold.json").read_bytes()
    assert gold_a == gold_b
    print(f"\n[determinism] {len(names)} files byte-identical across two runs; gold.json bytes equal={gold_a == gold_b}")


def test_different_seed_changes_content(tmp_path):
    diff = load_preset("medium")
    out_a, _ = _run(tmp_path / "a", diff, n_docs=2, seed=1)
    out_b, _ = _run(tmp_path / "b", diff, n_docs=2, seed=2)
    assert (out_a / "gold.json").read_bytes() != (out_b / "gold.json").read_bytes()


# ----------------------------------------------------------------- 难度旋钮

def test_missing_rate_one_makes_every_key_absent(tmp_path):
    diff = load_preset("easy").model_copy(update={"missing_rate": 1.0})
    _, samples = _run(tmp_path, diff, n_docs=4)
    for s in samples:
        assert all(r.expected == "absent" and not r.mentions and not r.distractors for r in s.gold.records)


def test_distractors_per_key_two_gives_two_nonequivalent_values(tmp_path):
    diff = load_preset("easy").model_copy(update={"distractors_per_key": 2})
    _, samples = _run(tmp_path, diff, n_docs=4)
    seen = 0
    for s in samples:
        for rec in s.gold.records:
            if rec.expected != "present":
                continue
            assert len(rec.distractors) == 2, rec.key
            assert all(d.surface_text for d in rec.distractors)
            seen += 1
    assert seen > 0


def test_distractor_values_differ_from_truth(tmp_path):
    from citecell.synth.sampler import make_distractors
    keyset = load_keyset()
    rng = random.Random(7)
    facts = sample_facts(rng, keyset, DifficultyConfig(missing_rate=0.0))
    for spec in keyset.keys:
        for d in make_distractors(spec, facts[spec.key], rng, 2):
            assert d != facts[spec.key], spec.key


def test_fixed_and_free_layouts_have_different_block_signatures():
    keyset = load_keyset()
    base = dict(distractors_per_key=0, missing_rate=0.0)
    facts = sample_facts(random.Random(3), keyset, DifficultyConfig(**base))
    fixed = build_ir(facts, keyset, DifficultyConfig(layout="fixed", **base), random.Random(4), "docx")
    free = build_ir(facts, keyset, DifficultyConfig(layout="free", **base), random.Random(4), "docx")
    assert block_signature(fixed) != block_signature(free)


def test_conflict_rate_raises_value_error(tmp_path):
    diff = DifficultyConfig(conflict_rate=0.1)
    with pytest.raises(ValueError):
        _run(tmp_path, diff, n_docs=1)


def test_derived_rate_raises_value_error(tmp_path):
    with pytest.raises(ValueError):
        _run(tmp_path, DifficultyConfig(derived_rate=0.1), n_docs=1)
