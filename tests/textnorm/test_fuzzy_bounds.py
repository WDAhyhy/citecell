"""FUZZY 上限按归一化长度计算的回归测试（防止 NFKC 展开导致的病态膨胀）。

实测（本机 Python 3.13，同一输入）：
- 修复前：文本 2000 原文码点（含 1000 个 U+FDFA，归一化后约 16000 码点）+ 引文 200 码点（含 40 个 U+FDFA）
  → 单次 FUZZY 调用 4.65 s（审查者在另一台机器上测得 19.93 s）。
- 修复后：同一输入 → 超限直接返回空，0.01 s。
- 归一化 5000×500 规模的纯汉字 FUZZY（确实走 DP、返回有效结果）→ 约 0.8–0.9 s。
阈值取实测值的 3 倍以上余量，避免偶发失败。
"""

import random
import time

from citecell.textnorm import Matcher
from citecell.textnorm.normalize import Normalizer

REJECT_BUDGET_S = 0.5  # 超限应立即返回（实测 0.01 s）
DP_BUDGET_S = 3.0  # 归一化规模 5000×500 的 DP（实测约 0.9 s）


def test_nfkc_expansion_reviewer_case_rejected_fast_not_slow():
    # 修复前 4.65 s 的输入：原文 2000 码点，归一化远超 5000
    text = "合同金额" * 250 + "ﷺ" * 1000
    quote = "甲乙丙丁" * 10 + "甲乙" * 10 + "ﷺ" * 40  # 原文 200 码点，无逐字/归一化命中，归一化 >500
    assert len(Normalizer().normalize(text).text) > 5000
    assert len(Normalizer().normalize(quote).text) > 500

    t0 = time.perf_counter()
    hits = Matcher().find(quote, text, allow_fuzzy=True)
    elapsed = time.perf_counter() - t0
    assert hits == []
    assert elapsed < REJECT_BUDGET_S, f"超限应直接返回，实际 {elapsed:.3f}s"


def test_each_expanding_character_is_bounded_by_normalized_length():
    # 单字符展开：U+FDFA(15)、﷽(>1)、㎡(2)、①(1)
    # 每个字符重复 6000 次：归一化后必然超过 5000 上限（①为 1 对 1，靠数量超限）
    for ch in ("ﷺ", "﷽", "㎡", "①"):
        text = ch * 6000
        quote = ch * 200
        t0 = time.perf_counter()
        hits = Matcher().find(quote + "X", text, allow_fuzzy=True)
        elapsed = time.perf_counter() - t0
        assert hits == []
        assert elapsed < REJECT_BUDGET_S, f"{ch!r}: {elapsed:.3f}s"


def test_normalized_limits_are_configurable():
    text = "合同总金额为壹拾万元"
    quote = "合同总金额为拾万元"
    assert Matcher().find(quote, text, allow_fuzzy=True)  # 默认可命中
    assert Matcher(fuzzy_max_cells=10).find(quote, text, allow_fuzzy=True) == []


def test_cells_cap_rejects_pathological_product_but_allows_5000x500():
    rng = random.Random(0)
    pool = "合同金额总价数量面积元，。 abcdef0123"
    text = "".join(rng.choice(pool) for _ in range(5000))
    quote = "".join(rng.choice("甲乙丙丁戊己庚辛壬癸") for _ in range(500))  # 无命中，必走 DP
    # 5000×500 = 2.5M 单元格：默认值允许运行
    t0 = time.perf_counter()
    Matcher().find(quote, text, allow_fuzzy=True)
    assert time.perf_counter() - t0 < DP_BUDGET_S
    # 收紧 cells 上限后直接返回空
    assert Matcher(fuzzy_max_cells=2_000_000).find(quote, text, allow_fuzzy=True) == []


def test_dp_at_normalized_limit_with_expansions_runs_within_budget():
    rng = random.Random(0)
    pool = "合同金额总价数量面积元，。 abcdef0123"
    # 4000 个普通字符 + 60 个 U+FDFA（归一化后约 4900 码点，仍在 5000 上限内）
    text = "".join(rng.choice(pool) for _ in range(4000)) + "ﷺ" * 60
    assert len(Normalizer().normalize(text).text) <= 5000
    quote = "".join(rng.choice("甲乙丙丁戊己庚辛壬癸") for _ in range(480))  # 归一化 480，无命中
    t0 = time.perf_counter()
    Matcher().find(quote, text, allow_fuzzy=True)
    elapsed = time.perf_counter() - t0
    assert elapsed < DP_BUDGET_S, f"{elapsed:.3f}s"
