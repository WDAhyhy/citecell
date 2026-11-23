"""上下文规划测试（ARCHITECTURE §6.5 与 T8 追加验收）。"""

import logging

import pytest

from citecell.models import BlockKind
from citecell.prompting import DefaultContextPlanner, HeuristicTokenCounter, render_document_lines

COUNTER = HeuristicTokenCounter()


def _small_doc(t8):
    blocks = [
        t8.para(1, "第一章 采购", kind=BlockKind.HEADING),
        t8.para(2, "合同金额为人民币 12,000 元。", section_path=["第一章 采购"]),
        t8.para(3, "本合同自签署之日起生效。"),
        t8.para(4, "付款日期为二〇二五年一月十日。"),
    ]
    return t8.build_doc(blocks)


def _big_doc(t8, n_fill=2000, targets=None):
    """n_fill 个无关填充段落，另在最终下标 pos 处插入目标段落（块 ID 为 p{pos+1}-b1）。目标在按升序插入时下标保持不变。"""
    if targets is None:
        targets = {600: "合同金额为人民币一万元。", 1200: "付款日期为二〇二五年一月。", 1900: "联系电话为一三八〇〇〇〇〇〇〇〇。"}
    texts = [f"填充第{i}段，与采购事项无关的说明文字。" for i in range(n_fill)]
    for pos in sorted(targets):
        texts.insert(pos, targets[pos])
    blocks = [t8.para(i + 1, text) for i, text in enumerate(texts)]
    return t8.build_doc(blocks), targets


# ------------------------------------------------ 默认行为（auto）


def test_small_doc_single_window_with_all_blocks(t8):
    doc = _small_doc(t8)
    keys = [t8.make_key("合同金额"), t8.make_key("付款日期")]
    windows = DefaultContextPlanner().plan(doc, keys, budget_tokens=12000)
    assert len(windows) == 1
    w = windows[0]
    assert w.keys == ["合同金额", "付款日期"]
    assert w.block_ids == [b.block_id for b in doc.blocks]
    assert w.truncated is False
    full = COUNTER.count("\n".join(render_document_lines(doc, doc.blocks, "cell_lines")))
    assert w.estimated_tokens == full


def test_default_config_equals_no_config(t8):
    doc = _small_doc(t8)
    keys = [t8.make_key("合同金额"), t8.make_key("付款日期")]
    planner = DefaultContextPlanner()
    assert planner.plan(doc, keys, budget_tokens=12000) == planner.plan(
        doc, keys, budget_tokens=12000, config=t8.make_run_config()
    )


def test_auto_small_doc_stays_single_window_even_for_many_keys_when_unlimited(t8):
    doc = _small_doc(t8)
    keys = [t8.make_key(f"键{i}") for i in range(7)]
    windows = DefaultContextPlanner().plan(doc, keys, budget_tokens=12000, config=t8.make_run_config())
    assert len(windows) == 1
    assert windows[0].keys == [k.key for k in keys]


# ------------------------------------------------ prefilter = always


def test_always_small_doc_uses_key_selection(t8):
    blocks = [t8.para(i, f"无关说明第{i}条。") for i in range(1, 11)]
    blocks[4] = t8.para(5, "合同金额为人民币一万元。")  # 第 5 块为目标，前后有邻块
    doc = t8.build_doc(blocks)
    cfg = t8.make_run_config(prefilter="always")
    windows = DefaultContextPlanner().plan(doc, [t8.make_key("合同金额")], budget_tokens=3000, config=cfg)
    assert len(windows) == 1
    ids = set(windows[0].block_ids)
    assert "p5-b1" in ids
    # 只含目标块及其 ±1 邻块，其余块不得出现
    assert ids <= {"p4-b1", "p5-b1", "p6-b1"}
    assert len(ids) < len(doc.blocks)


def test_auto_small_doc_is_single_window_with_all_blocks(t8):
    blocks = [t8.para(i, f"无关说明第{i}条。") for i in range(1, 11)]
    blocks[4] = t8.para(5, "合同金额为人民币一万元。")
    doc = t8.build_doc(blocks)
    cfg = t8.make_run_config(prefilter="auto")
    windows = DefaultContextPlanner().plan(doc, [t8.make_key("合同金额")], budget_tokens=3000, config=cfg)
    assert len(windows) == 1
    assert windows[0].block_ids == [b.block_id for b in doc.blocks]


# ------------------------------------------------ 大文档按键选块


def test_big_doc_targets_in_their_windows_and_within_budget(t8):
    doc, _ = _big_doc(t8)
    keys = [t8.make_key("合同金额"), t8.make_key("付款日期"), t8.make_key("联系电话")]
    budget = 2000
    windows = DefaultContextPlanner().plan(doc, keys, budget_tokens=budget)
    targets = {"合同金额": "p601-b1", "付款日期": "p1201-b1", "联系电话": "p1901-b1"}
    for w in windows:
        assert w.estimated_tokens <= budget
        assert w.block_ids == sorted(w.block_ids, key=lambda bid: int(bid.split("-")[0][1:]))
    for key, bid in targets.items():
        owning = [w for w in windows if key in w.keys]
        assert len(owning) == 1, key
        assert bid in owning[0].block_ids, key


def test_big_doc_is_deterministic(t8):
    doc, _ = _big_doc(t8)
    keys = [t8.make_key("合同金额"), t8.make_key("付款日期"), t8.make_key("联系电话")]
    planner = DefaultContextPlanner()
    first = planner.plan(doc, keys, budget_tokens=2000, config=t8.make_run_config(prefilter="always"))
    second = planner.plan(doc, keys, budget_tokens=2000, config=t8.make_run_config(prefilter="always"))
    assert first == second


def test_unmatched_key_is_marked_truncated_when_doc_does_not_fit(t8):
    doc, _ = _big_doc(t8, n_fill=300, targets={})
    windows = DefaultContextPlanner().plan(doc, [t8.make_key("完全不存在的字段")], budget_tokens=1500)
    assert len(windows) == 1
    assert windows[0].truncated is True
    assert windows[0].estimated_tokens <= 1500


# ------------------------------------------------ max_keys_per_window


def test_max_keys_splits_in_keyset_order_deterministically(t8):
    doc = _small_doc(t8)
    names = ["键甲", "键乙", "键丙", "键丁", "键戊"]
    keys = [t8.make_key(n) for n in names]
    cfg = t8.make_run_config(max_keys_per_window=2)
    planner = DefaultContextPlanner()
    windows = planner.plan(doc, keys, budget_tokens=12000, config=cfg)
    assert len(windows) >= 3
    assert all(len(w.keys) <= 2 for w in windows)
    flat = [k for w in windows for k in w.keys]
    assert flat == names  # 每个键出现且仅出现一次，顺序与 KeySet 一致
    assert windows == planner.plan(doc, keys, budget_tokens=12000, config=cfg)


def test_max_keys_applies_in_key_selection_path(t8):
    doc, _ = _big_doc(t8)
    names = ["合同金额", "付款日期", "联系电话", "未知甲", "未知乙"]
    keys = [t8.make_key(n) for n in names]
    cfg = t8.make_run_config(prefilter="always", max_keys_per_window=2)
    windows = DefaultContextPlanner().plan(doc, keys, budget_tokens=2000, config=cfg)
    assert all(len(w.keys) <= 2 for w in windows)
    flat = [k for w in windows for k in w.keys]
    assert flat == names


def test_budget_too_small_raises(t8):
    doc, _ = _big_doc(t8, n_fill=50)
    with pytest.raises(ValueError):
        DefaultContextPlanner().plan(doc, [t8.make_key("合同金额")], budget_tokens=3)


# ------------------------------------------------ 表格：表头与同列块


def test_table_header_cells_are_selected_with_target_cell(t8):
    blocks = [t8.para(i, f"无关说明第{i}条。") for i in range(1, 40)]
    blocks += [
        t8.cell("A", 1, "产品", row_index=0, col_index=0, is_header=True),
        t8.cell("B", 1, "单价", row_index=0, col_index=1, is_header=True),
        t8.cell("A", 2, "螺丝M6", row_index=1, col_index=0, column_headers=("产品",)),
        t8.cell("B", 2, "25.00", row_index=1, col_index=1, column_headers=("单价",), row_headers=("螺丝M6",)),
    ]
    doc = t8.build_doc(blocks, sheet_names=[t8.SHEET])
    cfg = t8.make_run_config(prefilter="always")
    windows = DefaultContextPlanner().plan(doc, [t8.make_key("单价")], budget_tokens=3000, config=cfg)
    ids = set(windows[0].block_ids)
    assert {"s1-B1", "s1-B2"} <= ids  # 表头与目标单元格一并入选
