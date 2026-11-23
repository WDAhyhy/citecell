"""模型档位预设（ARCH §7.1）与探测脚本的 --help。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from citecell.backends import PROFILE_NAMES, get_profile, profile_doc
from citecell.canonical import canonical_sha256
from citecell.models import BackendInfo, RunConfig

BACKEND = BackendInfo(backend_id="local-test", provider="openai_compatible", model="caller-supplied-model")
PROFILES = ["cpu-lite", "cpu-std", "gpu-std", "gpu-ref"]


@pytest.mark.parametrize("name", PROFILES)
def test_preset_validates_as_run_config(name):
    cfg = get_profile(name, BACKEND)
    RunConfig.model_validate(cfg.model_dump())
    assert isinstance(cfg, RunConfig)


@pytest.mark.parametrize("name", PROFILES)
def test_all_presets_use_auto_prefilter_and_unlimited_keys(name):
    cfg = get_profile(name, BACKEND)
    assert cfg.prefilter == "auto"
    assert cfg.max_keys_per_window is None


def test_explicit_ablation_knobs_still_validate():
    # 旋钮没有被删掉：消融时可显式传入 always 与键数上限
    cfg = RunConfig(
        backend=BACKEND,
        profile="cpu-std",
        prefilter="always",
        max_keys_per_window=3,
    )
    RunConfig.model_validate(cfg.model_dump())
    assert cfg.prefilter == "always" and cfg.max_keys_per_window == 3


def test_gpu_ref_is_marked_as_evaluation_reference_only():
    assert "仅作评测参考" in profile_doc("gpu-ref")
    assert "仅作评测参考" in (get_profile.__doc__ or "")


def test_profile_name_recorded_in_config():
    for name in PROFILES:
        assert get_profile(name, BACKEND).profile == name


def test_same_profile_twice_has_same_canonical_hash():
    a = get_profile("cpu-std", BACKEND)
    b = get_profile("cpu-std", BACKEND)
    assert canonical_sha256(a) == canonical_sha256(b)


def test_different_profiles_have_different_hashes():
    hashes = {canonical_sha256(get_profile(n, BACKEND)) for n in PROFILES}
    assert len(hashes) == len(PROFILES)


def test_unknown_profile_lists_available_names():
    with pytest.raises(ValueError) as exc:
        get_profile("tpu-huge", BACKEND)
    for name in PROFILE_NAMES:
        assert name in str(exc.value)


def test_presets_contain_no_download_urls_or_repo_names():
    source = Path(__file__).resolve().parents[2] / "src" / "citecell" / "backends" / "profiles.py"
    text = source.read_text(encoding="utf-8").lower()
    for forbidden in ("http://", "https://", "huggingface", "modelscope", "qwen", "llama", ".gguf"):
        assert forbidden not in text, forbidden


def test_model_name_comes_from_caller():
    cfg = get_profile("cpu-lite", BACKEND)
    assert cfg.backend.model == "caller-supplied-model"


def test_probe_script_help_runs():
    script = Path(__file__).resolve().parents[2] / "scripts" / "probe_backend.py"
    proc = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "--base-url" in proc.stdout
