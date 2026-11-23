"""合成生成器入口：实现 ``interfaces.SyntheticGenerator``。

输出（out_dir）：
* ``doc_<NNNN>.<fmt>``：生成的文件（zip 条目时间与文档属性固定，见 determinism.py）；
* ``gold.json``：全部 GoldDocument 的规范 JSON 列表（按样本序号排列）。

确定性：每份文档的随机源由 (seed, 样本序号) 通过 SHA-256 派生，互不影响；
同一 GenerationConfig 运行两次，文件字节与 gold.json 字节均相同（见 tests/synth）。

PDF 写出由 T5b 的 pdf_writer 提供（DOCX 形状的 IR，PdfLocator 记账）。
未覆盖：ARCH §10 的 SelfCheck（用被测解析器回读）尚未实现。
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
from pathlib import Path
from typing import Iterator

from citecell.models import GenerationConfig, SyntheticSample
from citecell.synth.determinism import canonical_json
from citecell.synth.gold import build_gold
from citecell.synth.realize import build_ir
from citecell.synth.sampler import sample_facts
from citecell.synth.writers import get_writer

log = logging.getLogger("citecell.synth")

GENERATOR_VERSION = "synth-t5a-0.1.0"


def _doc_rng(seed: int, index: int) -> random.Random:
    digest = hashlib.sha256(f"citecell-synth|{seed}|{index}".encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


class ProcurementGenerator:
    """采购领域（合同、报价单、验收单）合成生成器，M1 支持 DOCX 与 XLSX。"""

    version = GENERATOR_VERSION

    def generate(self, config: GenerationConfig, out_dir: Path) -> Iterator[SyntheticSample]:
        diff = config.difficulty
        if diff.conflict_rate > 0 or diff.derived_rate > 0:
            raise ValueError("M1 生成器不支持 conflict_rate / derived_rate > 0（属于 M3）")
        out_dir.mkdir(parents=True, exist_ok=True)
        config_sha = hashlib.sha256(
            canonical_json(config.model_dump(mode="json")).encode("utf-8")
        ).hexdigest()
        golds: list[dict] = []
        formats = list(config.formats)
        for i in range(config.n_docs):
            fmt = formats[i % len(formats)].value
            rng = _doc_rng(config.seed, i)
            facts = sample_facts(rng, config.keyset, diff)
            ir = build_ir(facts, config.keyset, diff, rng, fmt)
            data, placements = get_writer(fmt)(ir)
            doc_sha = hashlib.sha256(data).hexdigest()
            rel = f"doc_{i:04d}.{fmt}"
            (out_dir / rel).write_bytes(data)
            gold = build_gold(
                ir=ir,
                placements=placements,
                facts=facts,
                keyset=config.keyset,
                doc_sha256=doc_sha,
                fmt=fmt,
                generator_version=GENERATOR_VERSION,
                config_sha256=config_sha,
                seed=config.seed,
                sample_index=i,
            )
            golds.append(json.loads(gold.model_dump_json()))
            log.info("synth sample %d written format=%s mentions=%d", i, fmt, len(placements))
            yield SyntheticSample(relative_path=rel, gold=gold)
        (out_dir / "gold.json").write_text(canonical_json(golds), encoding="utf-8")


def generate(config: GenerationConfig, out_dir: Path) -> Iterator[SyntheticSample]:
    """模块级便捷入口，等同于 ``ProcurementGenerator().generate(config, out_dir)``。"""
    return ProcurementGenerator().generate(config, out_dir)
