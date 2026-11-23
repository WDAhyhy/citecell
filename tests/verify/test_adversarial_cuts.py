"""对抗测试：在金标准提及的内部切割引文边界，任何截断都不得判为 grounded。

输入来自 citecell.synth 生成的 DOCX 样本（金标准提及带原生定位与 surface_text），
由 DocxParser 解析后取块。切割只用于数字、金额、百分比、日期键（这些类型的提及由
find_mentions 识别，边界截断可以被程序检测）。文本型值的截断属于已知限制，不在本测试范围内。
随机源为 random.Random(0)，结果可复现。
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from citecell.models import (
    Block,
    Candidate,
    DifficultyConfig,
    DocFormat,
    DocxLocator,
    EvidenceClaim,
    ExtractionMethod,
    GenerationConfig,
    KeySpec,
    Status,
    ValueType,
)
from citecell.parsing import DocxParser
from citecell.synth import generate
from citecell.synth.keysets import load_keyset
from citecell.verify import Verifier

_CHECKED_TYPES = {ValueType.NUMBER, ValueType.MONEY, ValueType.PERCENT, ValueType.DATE}
_TRIALS = 2000
#: 切割点两侧字符都必须属于“数字/日期内部”字符集（数字、分隔符、中文数字、数量与日期单位）。
#: 只在这些位置切割，才符合“边界落在数字/日期内部”的对抗要求；在终结字（如“整”“元”“（”）处切割
#: 不切断数值，不在本测试范围内。此集合在测试中独立写出，不引用 verifier 的实现。
_INSIDE_NUMBER = frozenset("0123456789０１２３４５６７８９.,-－/%零〇一二三四五六七八九十百千万亿两壹贰叁肆伍陆柒捌玖拾佰仟年月日号")


def _block_key(loc: DocxLocator) -> tuple:
    if loc.table_number is not None:
        return ("t", loc.table_number, loc.row_index, loc.col_index)
    return ("p", loc.paragraph_number)


def _pool(tmp_path: Path):
    """返回 (解析后的文档列表, 候选切割案例池)。案例：(doc, block_id, spec, 引文, 值)。"""
    keyset = load_keyset()
    specs = {k.key: k for k in keyset.keys}
    config = GenerationConfig(
        seed=11,
        n_docs=6,
        formats=[DocFormat.DOCX],
        keyset=keyset,
        difficulty=DifficultyConfig(surface_variety=0.5, distractors_per_key=2),
    )
    out = tmp_path / "synth"
    samples = list(generate(config, out))
    parser = DocxParser()
    pool: list[tuple] = []
    for sample in samples:
        doc = parser.parse(out / sample.relative_path)
        by_loc: dict[tuple, Block] = {}
        for b in doc.blocks:
            if isinstance(b.locator, DocxLocator):
                by_loc[_block_key(b.locator)] = b
        for rec in sample.gold.records:
            spec: KeySpec | None = specs.get(rec.key)
            if spec is None or spec.value_type not in _CHECKED_TYPES or rec.expected != "present":
                continue
            for m in rec.mentions:
                if not isinstance(m.locator, DocxLocator) or m.char_start is None or m.char_end is None:
                    continue
                block = by_loc.get(_block_key(m.locator))
                if block is None:
                    continue
                s, e = m.char_start, m.char_end
                if block.text[s:e] != m.surface_text:
                    continue  # 金标准偏移与解析文本不一致（如嵌套表格展平），跳过
                text = block.text
                for p in range(s + 1, e):  # 切割点严格位于提及内部，且两侧都在数字/日期内部
                    if not (text[p - 1] in _INSIDE_NUMBER and text[p] in _INSIDE_NUMBER):
                        continue
                    # 左边界切割：引文从 p 开始，向右可能延伸
                    right = min(len(text), e + 2)
                    pool.append((doc, block.block_id, spec, text[p:right], text[p:e], "left", s, e))
                    pool.append((doc, block.block_id, spec, text[p:right], m.surface_text, "left", s, e))
                    # 右边界切割：引文到 p 结束，向左可能延伸
                    left = max(0, s - 2)
                    pool.append((doc, block.block_id, spec, text[left:p], text[s:p], "right", s, e))
                    pool.append((doc, block.block_id, spec, text[left:p], m.surface_text, "right", s, e))
    return pool


def test_random_inner_cuts_are_never_grounded(tmp_path):
    pool = _pool(tmp_path)
    assert len(pool) >= 200, "合成样本中可用的提及太少，无法进行对抗测试"

    rng = random.Random(0)
    verifier = Verifier()
    failures: list[tuple] = []
    for _ in range(_TRIALS):
        doc, block_id, spec, quote, value, side, s, e = pool[rng.randrange(len(pool))]
        if not quote.strip() or not value.strip():
            continue
        cand = Candidate(
            doc_id=doc.info.doc_id,
            key=spec.key,
            value_text=value,
            evidence=[EvidenceClaim(block_id=block_id, quote=quote)],
            method=ExtractionMethod.LLM,
            producer_id="llm:adversarial",
        )
        rec = verifier.verify(cand, doc, spec)
        if rec.status == Status.GROUNDED:
            failures.append((spec.key, side, quote, value))
    assert failures == [], f"{len(failures)} 个截断案例被判 grounded，例如：{failures[:3]}"


@pytest.mark.parametrize("seed", [0])
def test_pool_is_deterministic(tmp_path, seed):
    a = [(c[1], c[3], c[4]) for c in _pool(tmp_path / "a")]
    b = [(c[1], c[3], c[4]) for c in _pool(tmp_path / "b")]
    assert a == b
