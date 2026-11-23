"""CLI 冒烟测试：--help、synth 生成、extract 在无服务时的失败路径（不访问网络：连接本机未监听端口）。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from citecell.cli import main

ROOT = Path(__file__).resolve().parents[2]
KEYSET = ROOT / "configs" / "keysets" / "procurement_v1.json"
MEDIUM = ROOT / "configs" / "synth" / "medium.json"


def test_module_help_runs():
    proc = subprocess.run([sys.executable, "-m", "citecell", "--help"], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "extract" in proc.stdout and "synth" in proc.stdout and "eval" in proc.stdout


def test_extract_help_runs():
    proc = subprocess.run(
        [sys.executable, "-m", "citecell", "extract", "--help"], capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, proc.stderr
    assert "--base-url" in proc.stdout


def test_synth_then_extract_without_server_writes_failed_outputs(tmp_path, capsys):
    docs = tmp_path / "docs"
    assert main(["synth", "--keyset", str(KEYSET), "--difficulty", str(MEDIUM), "--seed", "3",
                 "--n-docs", "1", "--formats", "docx", "--out", str(docs)]) == 0
    gold = json.loads((docs / "gold.json").read_text(encoding="utf-8"))
    assert isinstance(gold, list) and len(gold) == 1
    doc_path = next(p for p in docs.iterdir() if p.suffix == ".docx")

    out = tmp_path / "runs"
    code = main([
        "extract", str(doc_path),
        "--keyset", str(KEYSET),
        "--base-url", "http://127.0.0.1:9/v1",  # 本机未监听的端口：连接被拒绝
        "--model", "test-model",
        "--max-transport-retries", "0",
        "--no-cache",
        "--out", str(out),
        "--run-id", "cli-smoke",
    ])
    assert code == 0
    run_dir = out / "cli-smoke"
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    results = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
    assert manifest["run_id"] == "cli-smoke"
    assert (run_dir / "results_values.csv").exists()
    assert (run_dir / "results_evidence.csv").exists()
    assert (run_dir / "results.xlsx").exists()
    statuses = {r["status"] for d in results["documents"] for r in d["records"]}
    assert statuses == {"failed"}

    printed = capsys.readouterr().out
    assert "状态:" in printed
    assert "HT-" not in printed  # 输出不含文档内容

    # eval 子命令：对同一份 results.json 与金标准运行（全部失败，指标应可计算而不崩溃）。
    eval_out = tmp_path / "eval"
    assert main([
        "eval",
        "--results", str(run_dir / "results.json"),
        "--gold", str(docs / "gold.json"),
        "--keyset", str(KEYSET),
        "--docs-dir", str(docs),
        "--out", str(eval_out),
    ]) == 0
    assert (eval_out / "eval.json").exists()
    assert (eval_out / "eval.md").exists()
