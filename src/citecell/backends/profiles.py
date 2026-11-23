"""模型档位预设（ARCH §7.1，ADR-016）。

档位只是 ``RunConfig`` 的一组预设值，**不新增代码路径**；档位名记入 ``RunConfig.profile``。

注意：下列所有数值（预算、键数）都是 **初始假设，待实测校准**。模型尺寸与各后端能否加载均未核实。
本模块**不包含**任何模型下载地址或真实模型仓库名；模型名由调用方通过 ``BackendInfo.model`` 传入。
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

from citecell.models import BackendInfo, RunConfig

# ---------------------------------------------------------------------------
# 初始假设（待实测校准）。改动任何数值前须先完成真实实测并记录在 M1_RESULTS / README。
# ---------------------------------------------------------------------------
CPU_LITE_BUDGET_TOKENS: Final = 3000  # 待实测校准（初始假设）
CPU_STD_BUDGET_TOKENS: Final = 6000  # 待实测校准（初始假设）

# 消融用的旋钮取值（不作默认）。原因：用合成短文档检查发现，prefilter="always"
# 加上每窗口键数上限，会使总输入 token 约为一次问完的 2.2～3.5 倍（启发式估算，
# 未用真实分词器校准），反而增加预填充成本。因此 cpu-* 档默认 prefilter="auto"、不限键数。
ABLATION_MAX_KEYS_PER_WINDOW: Final = 3  # 仅消融用

GPU_STD_BUDGET_TOKENS: Final = 12000  # 待实测校准（初始假设）
GPU_REF_BUDGET_TOKENS: Final = 12000  # 待实测校准（初始假设）

_PRESETS: Final[dict[str, dict[str, object]]] = {
    "cpu-lite": {
        "context_budget_tokens": CPU_LITE_BUDGET_TOKENS,
        "prefilter": "auto",
        "max_keys_per_window": None,
    },
    "cpu-std": {
        "context_budget_tokens": CPU_STD_BUDGET_TOKENS,
        "prefilter": "auto",
        "max_keys_per_window": None,
    },
    "gpu-std": {
        "context_budget_tokens": GPU_STD_BUDGET_TOKENS,
        "prefilter": "auto",
        "max_keys_per_window": None,
    },
    "gpu-ref": {
        "context_budget_tokens": GPU_REF_BUDGET_TOKENS,
        "prefilter": "auto",
        "max_keys_per_window": None,
    },
}

PROFILE_DOCS: Final = MappingProxyType(
    {
        "cpu-lite": "约 8GB 内存、无独显的最低档：只求能跑；难键依赖模板规则与人工复核。",
        "cpu-std": "约 16GB 内存、无独显的默认推荐档（普通电脑）。",
        "gpu-std": "8～16GB 显存档：速度与准确率更好。",
        "gpu-ref": "24GB 以上显存或租用的大模型档。仅作评测参考上限，不作为默认部署档。",
    }
)

PROFILE_NAMES: Final[tuple[str, ...]] = tuple(_PRESETS)


def profile_doc(name: str) -> str:
    """返回档位的说明文字（含 gpu-ref 的"仅作评测参考"声明）。未知档位报错。"""
    if name not in PROFILE_DOCS:
        raise ValueError(_unknown_message(name))
    return PROFILE_DOCS[name]


def _unknown_message(name: str) -> str:
    return f"未知档位 {name!r}，可用档位: {', '.join(PROFILE_NAMES)}"


def get_profile(name: str, backend: BackendInfo) -> RunConfig:
    """返回指定档位的 ``RunConfig``（其余字段取 RunConfig 默认值）。

    * 四个预设均为 ``prefilter="auto"``、``max_keys_per_window=None``（见模块注释的原因）。
    * 消融时可由调用方显式传入 ``prefilter="always"`` 与 ``max_keys_per_window``，
      本函数不覆盖它们，那些旋钮仍保留在 RunConfig 中。
    * ``gpu-ref`` 仅作评测参考上限（见 ``profile_doc``）。
    未知档位名 → ValueError，并列出可用档位。
    """
    if name not in _PRESETS:
        raise ValueError(_unknown_message(name))
    preset = _PRESETS[name]
    return RunConfig(
        backend=backend,
        profile=name,
        context_budget_tokens=preset["context_budget_tokens"],  # type: ignore[arg-type]
        prefilter=preset["prefilter"],  # type: ignore[arg-type]
        max_keys_per_window=preset["max_keys_per_window"],  # type: ignore[arg-type]
    )


__all__ = [
    "PROFILE_DOCS",
    "PROFILE_NAMES",
    "get_profile",
    "profile_doc",
]
