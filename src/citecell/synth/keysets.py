"""读取仓库 configs/ 下的 KeySet 与难度预设（JSON）。"""

from __future__ import annotations

import json
from pathlib import Path

from citecell.models import DifficultyConfig, KeySet

# src/citecell/synth/keysets.py → 仓库根目录
REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_ROOT = REPO_ROOT / "configs"


def _read_json(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"配置文件不存在: {path.relative_to(REPO_ROOT)}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_keyset(name: str = "procurement_v1") -> KeySet:
    """读取 configs/keysets/<name>.json。"""
    return KeySet.model_validate(_read_json(CONFIG_ROOT / "keysets" / f"{name}.json"))


def load_preset(name: str) -> DifficultyConfig:
    """读取 configs/synth/<name>.json（easy / medium / hard）。"""
    return DifficultyConfig.model_validate(_read_json(CONFIG_ROOT / "synth" / f"{name}.json"))
