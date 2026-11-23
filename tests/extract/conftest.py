"""抽取编排测试的共享夹具。所有测试文档在运行时生成，不提交二进制样例（ADR / TASKS §0.4）。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from docx import Document

from citecell.models import KeySet, KeySpec, ValueType
from citecell.parsing import build_default_registry

PARAGRAPHS = (
    "采购合同",
    "合同编号：HT-2024-001。",
    "甲方：示例科技有限公司。",
    "合同金额为人民币120万元。",
)


@pytest.fixture
def make_docx(tmp_path: Path) -> Callable[..., Path]:
    def _make(name: str = "contract.docx", paragraphs: tuple[str, ...] = PARAGRAPHS) -> Path:
        path = tmp_path / name
        doc = Document()
        for text in paragraphs:
            doc.add_paragraph(text)
        doc.save(str(path))
        return path

    return _make


@pytest.fixture
def parse() -> Callable[[Path], object]:
    registry = build_default_registry()

    def _parse(path: Path):  # noqa: ANN202
        return registry.for_path(path).parse(path)

    return _parse


@pytest.fixture
def block_id_of() -> Callable[[object, str], str]:
    def _find(doc, text: str) -> str:  # noqa: ANN001
        hits = [b.block_id for b in doc.blocks if text in b.text]
        assert len(hits) == 1, f"期望唯一命中，实际 {len(hits)} 处"
        return hits[0]

    return _find


@pytest.fixture
def keyset_one() -> KeySet:
    return KeySet(name="t-one", keys=[KeySpec(key="合同编号", value_type=ValueType.TEXT)])


@pytest.fixture
def keyset_three() -> KeySet:
    return KeySet(
        name="t-three",
        keys=[
            KeySpec(key="合同编号", value_type=ValueType.TEXT),
            KeySpec(key="甲方", value_type=ValueType.TEXT),
            KeySpec(key="合同金额", value_type=ValueType.MONEY),
        ],
    )


