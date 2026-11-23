"""GoldBuilder：由 Writer 的记账（Placement）与事实表生成 GoldDocument。

位置完全来自 Writer 自身的排版记账，不调用任何解析器（ADR-013）。
若某个 present 键没有任何出现记录，视为生成器缺陷并报错，而不是静默修正。
"""

from __future__ import annotations

from typing import Any

from citecell.models import (
    DataSource,
    DocFormat,
    GeneratorStamp,
    GoldDocument,
    GoldMention,
    GoldRecord,
    KeySet,
)
from citecell.synth.ir import DocIR, Placement
from citecell.synth.sampler import ABSENT


def build_gold(
    *,
    ir: DocIR,
    placements: list[Placement],
    facts: dict[str, Any],
    keyset: KeySet,
    doc_sha256: str,
    fmt: str,
    generator_version: str,
    config_sha256: str,
    seed: int,
    sample_index: int,
) -> GoldDocument:
    span_by_sid = {sp.sid: sp for sp in ir.spans}
    values: dict[str, list[GoldMention]] = {k.key: [] for k in keyset.keys}
    distractors: dict[str, list[GoldMention]] = {k.key: [] for k in keyset.keys}
    tags: dict[str, set[str]] = {k.key: set() for k in keyset.keys}

    for pl in placements:
        sp = span_by_sid[pl.sid]
        mention = GoldMention(
            locator=pl.locator,
            char_start=pl.char_start,
            char_end=pl.char_end,
            surface_text=pl.surface_text,
        )
        target = values if sp.role == "value" else distractors
        target[sp.key].append(mention)
        tags[sp.key].update(sp.tags)

    records: list[GoldRecord] = []
    for spec in keyset.keys:
        key = spec.key
        truth = facts[key]
        rec_tags = sorted(tags[key] | {f"layout={ir.layout}"})
        if truth is ABSENT:
            records.append(GoldRecord(key=key, expected="absent", tags=rec_tags))
            continue
        if not values[key]:
            raise RuntimeError(f"生成器缺陷：present 键 {key!r} 没有任何出现记录")
        records.append(
            GoldRecord(
                key=key,
                expected="present",
                value=truth,
                mentions=values[key],
                distractors=distractors[key],
                tags=rec_tags,
            )
        )

    return GoldDocument(
        doc_sha256=doc_sha256,
        format=DocFormat(fmt),
        source=DataSource.SYNTHETIC,
        generator=GeneratorStamp(
            generator_version=generator_version,
            config_sha256=config_sha256,
            seed=seed,
            sample_index=sample_index,
        ),
        keyset_name=keyset.name,
        keyset_version=keyset.version,
        records=records,
    )
