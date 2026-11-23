"""验收 8：可选的真实模型冒烟。

仅当同时设置了环境变量 ``CITECELL_E2E_BASE_URL`` 与 ``CITECELL_E2E_MODEL`` 时运行（否则跳过）。
用 2 份合成文档跑一遍真实的 OpenAI 兼容后端，只断言：输出合法（每个键都有状态、记录完整）、
清单完整。**不断言准确率**（模型准确率不属于 M1 验收）。

远端地址默认被禁止（ADR-012）；本测试不会自动放行远端后端。若服务不在 localhost，
需要用户显式设置 CITECELL_E2E_ALLOW_REMOTE=1（数据出境，由用户自行承担）。
"""

from __future__ import annotations

import os

import pytest

from citecell.backends import OpenAICompatibleBackend
from citecell.models import RunConfig, Status

BASE_URL = os.environ.get("CITECELL_E2E_BASE_URL")
MODEL = os.environ.get("CITECELL_E2E_MODEL")
ALLOW_REMOTE = os.environ.get("CITECELL_E2E_ALLOW_REMOTE") == "1"

pytestmark = pytest.mark.skipif(
    not (BASE_URL and MODEL),
    reason="未设置 CITECELL_E2E_BASE_URL / CITECELL_E2E_MODEL，跳过真实模型冒烟",
)


def test_real_backend_output_is_well_formed(corpus, keyset, run_pipeline) -> None:  # noqa: ANN001
    backend = OpenAICompatibleBackend(base_url=BASE_URL, model=MODEL, allow_remote_backend=ALLOW_REMOTE)
    config = RunConfig(backend=backend.info(), allow_remote_backend=ALLOW_REMOTE)
    paths = corpus.paths[:2]
    result = run_pipeline(backend, config, paths, keyset, run_id="smoke")

    assert len(result.documents) == 2
    assert len(result.manifest.documents) == 2
    assert result.manifest.config_sha256 and result.manifest.dependencies
    valid = {s.value for s in Status}
    for dr in result.documents:
        assert {r.key for r in dr.records} == {k.key for k in keyset.keys}
        for rec in dr.records:
            assert rec.status.value in valid
