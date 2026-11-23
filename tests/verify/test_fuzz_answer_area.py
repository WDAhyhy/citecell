"""模糊测试（random.Random(0)）：凡判 grounded 的，值区间所在的提及都必须位于答案区，
且不处于否定、问句或标签上下文。

预言机在测试内独立实现（不复用 verifier 的函数），规则与规格一致：
* 段边界：句分隔符、括号、冒号；
* 问句：段内最后一个“是否”之后、到提及之前没有答案区分隔符（冒号、为、是）；
* 否定：段内（去掉“是否”中的“否”之后）以及提及之后的段中不得有否定字；
* 标签：提及不得落在键名或别名的出现之内。
"""

from __future__ import annotations

import random

from citecell.models import (
    Block,
    BlockKind,
    Candidate,
    DocFormat,
    DocumentInfo,
    DocxLocator,
    EnumOption,
    EvidenceClaim,
    ExtractionMethod,
    KeySpec,
    ParsedDocument,
    Status,
    ValueType,
)
from citecell.verify import Verifier

DOC_ID = "0123456789abcdef"
KEY = "是否含税"
ALIASES = ["含税情况", "税费口径"]
SPEC = KeySpec(
    key=KEY,
    value_type=ValueType.BOOLEAN,
    aliases=ALIASES,
    enum_options=[EnumOption(value="true", aliases=["含税"]), EnumOption(value="false", aliases=["不含税"])],
)
NEG = set("不未非无没否")
SEG_DELIMS = set("。；;，,！!？?\n（）()：:")
ANSWER_SEPS = set("：:为是")
ZW = "​"

PREFIXES = ["", "。", "该报价", "本合同约定", "；", "，", "备注：", "（"]
CORES = ["是否含税", "含税情况", "税费口径", "是否属于含税价", "含税价格", "是否含税为", "是否含税："]
SEPS = ["", "：", ":", "为", "是", " ", "，", "（初报价：", "；修订前："]
VALUES = ["含税", "不含税"]
SUFFIXES = ["", "。", "（初报价：不含税）", "；修订前：含税", "（含税/不含税）", "（备注：不含税）", "的说法", "，付款周期30天"]


def _oracle_ok(text: str, vs: int, ve: int) -> bool:
    """独立预言机：value 区间 [vs, ve) 是否位于答案区且无否定/问句/标签上下文。"""
    s = vs
    while s > 0 and text[s - 1] not in SEG_DELIMS:
        s -= 1
    left = "".join(c for c in text[s:vs] if not c.isspace() and c not in "​‌‍﻿­")
    e = ve
    while e < len(text) and text[e] not in SEG_DELIMS:
        e += 1
    right = "".join(c for c in text[ve:e] if not c.isspace() and c not in "​‌‍﻿­")
    # 空白分隔（第五轮）：键名或别名的一次出现之后，到提及之前只有空白，则已进入答案区
    space_after_label = False
    for name in [KEY, *ALIASES]:
        k = text.find(name)
        while k != -1 and k + len(name) <= vs:
            gap = text[k + len(name) : vs]
            if gap and gap.strip() == "":
                space_after_label = True
            k = text.find(name, k + 1)
    k = left.rfind("是否")
    if not space_after_label and k >= 0 and not any(c in ANSWER_SEPS for c in left[k + 2 :]):
        return False  # 问句上下文
    if any(c in NEG for c in left.replace("是否", "") + right):
        return False  # 否定上下文
    for name in [KEY, *ALIASES]:
        start = text.find(name)
        while start != -1:
            if start <= vs and ve <= start + len(name):
                return False  # 标签上下文
            start = text.find(name, start + 1)
    return True


def _sentence(rng: random.Random) -> str:
    parts = [rng.choice(PREFIXES), rng.choice(CORES), rng.choice(SEPS), rng.choice(VALUES), rng.choice(SUFFIXES)]
    text = "".join(parts)
    if rng.random() < 0.15 and len(text) > 2:
        i = rng.randrange(1, len(text))
        text = text[:i] + ZW + ZW + text[i:]
    return text


def test_grounded_values_are_always_in_answer_area():
    rng = random.Random(0)
    verifier = Verifier()
    checked = 0
    grounded_count = 0
    violations: list[tuple[str, str, str]] = []
    for _ in range(1600):
        text = _sentence(rng)
        occ = [i for i in range(len(text)) for w in ("含税",) if text.startswith(w, i)]
        if not occ:
            continue
        i = rng.choice(occ)
        j = i + 2
        a = max(0, i - rng.randrange(0, 3))
        b = min(len(text), j + rng.randrange(0, 4))
        quote = text[a:b].strip()
        if not quote:
            continue
        value = "不含税" if "不含税" in quote else "含税"
        info = DocumentInfo(doc_id=DOC_ID, sha256=DOC_ID + "0" * 48, format=DocFormat.DOCX, size_bytes=1, parser_name="t", parser_version="0")
        block = Block(
            doc_id=DOC_ID,
            block_id="para1",
            kind=BlockKind.PARAGRAPH,
            text=text,
            order=0,
            locator=DocxLocator(body_index=0, paragraph_number=1),
        )
        doc = ParsedDocument(info=info, blocks=[block])
        cand = Candidate(
            doc_id=DOC_ID,
            key=KEY,
            value_text=value,
            evidence=[EvidenceClaim(block_id="para1", quote=quote)],
            method=ExtractionMethod.LLM,
            producer_id="llm:fuzz",
        )
        rec = verifier.verify(cand, doc, SPEC)
        checked += 1
        if rec.status != Status.GROUNDED:
            continue
        grounded_count += 1
        for ev in rec.evidence:
            if ev.value_char_start is None:
                continue
            if not _oracle_ok(text, ev.value_char_start, ev.value_char_end):
                violations.append((text, quote, value))
    assert checked >= 1500
    assert grounded_count >= 100, "正例太少，模糊测试缺乏意义"
    assert violations == [], f"{len(violations)} 个 grounded 的值不在答案区，例如：{violations[:3]}"
