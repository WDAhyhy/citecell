"""验收 7：许可证与导入隔离。

* ``scripts/check_licenses.py --default-only`` 退出码为 0；
* 在**独立子进程**中运行完整流水线（解析 docx/xlsx/pdf → Oracle → Verifier → 导出），
  之后 ``sys.modules`` 中不得出现 fitz / pymupdf（AGPL，ADR-005，TASKS §0.5）。
  使用子进程是为了不受同一 pytest 进程中其他测试导入模块的影响。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

PROBE = r"""
import asyncio, json, sys
from pathlib import Path
from citecell.backends import OracleBackend
from citecell.extract.pipeline import build_pipeline
from citecell.models import GoldDocument, KeySet, RunConfig
from citecell.parsing import build_default_registry

corpus_dir, keyset_path = Path(sys.argv[1]), Path(sys.argv[2])
keyset = KeySet.model_validate_json(keyset_path.read_text(encoding="utf-8"))
golds = [GoldDocument.model_validate(g) for g in json.loads((corpus_dir / "gold.json").read_text(encoding="utf-8"))]
paths = sorted(corpus_dir.glob("doc_*"))
registry = build_default_registry()
parsed = [registry.for_path(p).parse(p) for p in paths]
oracle = OracleBackend(golds, parsed)
config = RunConfig(backend=oracle.info())
result = asyncio.run(build_pipeline(oracle, config).run(paths, keyset, config, run_id="isolation"))
from citecell.export import values_table
values_table(result, keyset)
banned = sorted(m for m in sys.modules if m.split(".")[0] in ("fitz", "pymupdf"))
print("BANNED=" + json.dumps(banned))
print("DOCS=" + str(len(result.documents)))
"""


def test_default_licenses_check_passes() -> None:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_licenses.py"), "--default-only"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert proc.returncode == 0, f"check_licenses 退出码 {proc.returncode}：\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"


def test_full_pipeline_does_not_import_pymupdf(corpus, keyset, tmp_path) -> None:  # noqa: ANN001
    keyset_path = tmp_path / "keyset.json"
    keyset_path.write_text(keyset.model_dump_json(), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-c", PROBE, str(corpus.out_dir), str(keyset_path)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    assert proc.returncode == 0, f"子进程失败：\n{proc.stderr[-3000:]}"
    lines = {ln.split("=", 1)[0]: ln.split("=", 1)[1] for ln in proc.stdout.splitlines() if "=" in ln}
    assert lines.get("BANNED") == "[]", f"导入了禁止的模块：{lines.get('BANNED')}"
    assert lines.get("DOCS") == str(len(corpus.paths))
