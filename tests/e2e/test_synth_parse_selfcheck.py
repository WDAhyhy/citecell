"""验收 1：合成 → 解析自检。

固定种子生成 docx/xlsx/pdf 各 5 份中等难度文档，用 CiteCell 解析器解析后，
每个金标准提及（GoldMention，不含干扰项）都必须能对应到某个块与该格式的原生定位：

* DOCX：同一 body_index / paragraph_number（表格：table/row/col），且块文本中 char 区间即为 surface；
* XLSX：同一工作表（sheet_index）与单元格（cell），块文本含 surface；
* PDF ：同一页，且 surface 在块文本中的某次出现对应的字符几何框与金标准框的 IoU ≥ 0.5。

无法对应即为缺陷（解析器或生成器），本测试直接失败并列出样例，不做静默修正。
"""

from __future__ import annotations

from collections.abc import Iterable

from citecell.models import Block, GoldDocument, GoldMention, ParsedDocument

IOU_THRESHOLD = 0.5


def _bbox_union(boxes: Iterable[tuple[float, float, float, float]]) -> tuple[float, float, float, float] | None:
    boxes = list(boxes)
    if not boxes:
        return None
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _docx_place(loc) -> tuple:  # noqa: ANN001
    return (loc.body_index, loc.paragraph_number, loc.table_number, loc.row_index, loc.col_index)


def _block_matches(block: Block, m: GoldMention) -> bool:
    """块是否对应到金标准提及（同一格式的原生定位 + 文本证据）。"""
    gl = m.locator
    bl = block.locator
    if gl.format == "docx":
        if bl.format != "docx" or _docx_place(bl) != _docx_place(gl):
            return False
        if m.char_start is not None and m.char_end is not None:
            return block.text[m.char_start : m.char_end] == m.surface_text
        return m.surface_text in block.text
    if gl.format == "xlsx":
        return bl.format == "xlsx" and bl.sheet_index == gl.sheet_index and bl.cell == gl.cell and m.surface_text in block.text
    if gl.format == "pdf":
        if bl.format != "pdf" or bl.page_number != gl.page_number:
            return False
        gold_union = _bbox_union((r.x0, r.y0, r.x1, r.y1) for r in gl.regions)
        if gold_union is None:
            return False
        start = 0
        while True:
            idx = block.text.find(m.surface_text, start)
            if idx < 0:
                return False
            end = idx + len(m.surface_text)
            frags = [f for f in block.fragments if f.start < end and f.end > idx]
            got = _bbox_union((f.bbox.x0, f.bbox.y0, f.bbox.x1, f.bbox.y1) for f in frags)
            if got is not None and _iou(got, gold_union) >= IOU_THRESHOLD:
                return True
            start = idx + 1
    raise AssertionError(f"未知的金标准格式: {gl.format}")


def _unmatched_mentions(doc: ParsedDocument, gold: GoldDocument) -> list[str]:
    problems: list[str] = []
    for rec in gold.records:
        if rec.expected != "present":
            continue
        for m in rec.mentions:
            if not any(_block_matches(b, m) for b in doc.blocks):
                problems.append(f"{gold.format.value} key={rec.key!r} surface={m.surface_text!r} locator={m.locator.model_dump(mode='json', exclude_none=True)}")
    return problems


def test_every_gold_mention_maps_to_block_and_native_locator(corpus) -> None:  # noqa: ANN001
    assert len(corpus.parsed) == len(corpus.golds) == 15
    formats = sorted({g.format.value for g in corpus.golds})
    assert formats == ["docx", "pdf", "xlsx"]
    for fmt in formats:
        assert sum(1 for g in corpus.golds if g.format.value == fmt) == 5

    failures: list[str] = []
    n_mentions = 0
    for doc, gold in zip(corpus.parsed, corpus.golds, strict=True):
        assert doc.info.sha256 == gold.doc_sha256, "解析结果与金标准的文档顺序不一致"
        n_mentions += sum(len(r.mentions) for r in gold.records if r.expected == "present")
        failures.extend(_unmatched_mentions(doc, gold))

    assert n_mentions > 0
    assert not failures, f"{len(failures)}/{n_mentions} 个金标准提及无法对应到块与原生定位（缺陷）：\n" + "\n".join(failures[:20])
