"""后端探测脚本：确认某个 OpenAI 兼容服务端对结构化输出与思考模式开关的支持情况。

用法（需要用户在本地运行，结果写入 src/citecell/backends/README.md）：

    .venv/bin/python scripts/probe_backend.py --base-url http://127.0.0.1:8080 --model <本地模型名>

只打印每项探测的状态（是否成功、是否得到可解析的 JSON、错误类别），**不打印模型输出正文**。
默认只允许 localhost；远端地址需显式 ``--allow-remote``（数据出境，ADR-012）。
本脚本不进入 CI，也不下载任何模型。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from citecell.interfaces import BackendError
from citecell.backends.openai_compat import OpenAICompatibleBackend, RemoteBackendRefused
from citecell.models import ChatMessage, ChatRequest

PROBE_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
}

MODES = ("json_schema", "json_object", "none")

# 思考模式关闭的候选写法（各后端是否支持未核实，由本脚本确认）
THINKING_OFF_VARIANTS: dict[str, dict] = {
    "none": {},
    "chat_template_kwargs": {"chat_template_kwargs": {"enable_thinking": False}},
}


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="探测 OpenAI 兼容后端的结构化输出与思考模式开关支持情况。")
    p.add_argument("--base-url", required=True, help="服务端根地址，如 http://127.0.0.1:8080")
    p.add_argument("--model", required=True, help="服务端上的模型名（由调用方提供，脚本不下载模型）")
    p.add_argument("--allow-remote", action="store_true", help="允许非 localhost 地址（数据出境，需谨慎）")
    p.add_argument("--timeout-s", type=float, default=60.0, help="单次探测超时（秒）")
    p.add_argument("--modes", nargs="+", choices=MODES, default=list(MODES), help="要探测的结构化输出模式")
    return p


async def _probe_one(args: argparse.Namespace, mode: str, variant: str, extra: dict) -> dict:
    result = {"structured_output": mode, "thinking_off": variant, "ok": False, "json_ok": False, "error": None}
    try:
        backend = OpenAICompatibleBackend(
            base_url=args.base_url,
            model=args.model,
            allow_remote_backend=args.allow_remote,
            max_transport_retries=0,
            structured_output=mode,
        )
    except RemoteBackendRefused:
        result["error"] = "远端地址被拒绝（需 --allow-remote）"
        return result
    request = ChatRequest(
        messages=[
            ChatMessage(role="system", content="只输出 JSON。"),
            ChatMessage(role="user", content='返回 {"answer": "ok"}'),
        ],
        json_schema=PROBE_SCHEMA,
        temperature=0.0,
        max_tokens=64,
        timeout_s=args.timeout_s,
        extra=extra,
    )
    try:
        resp = await backend.chat(request)
    except BackendError as exc:
        result["error"] = str(exc)  # BackendError 文本不含响应正文
        return result
    result["ok"] = True
    try:
        json.loads(resp.content)
        result["json_ok"] = True
    except ValueError:
        result["json_ok"] = False
    return result


async def _run(args: argparse.Namespace) -> list[dict]:
    rows: list[dict] = []
    for mode in args.modes:
        for variant, extra in THINKING_OFF_VARIANTS.items():
            rows.append(await _probe_one(args, mode, variant, extra))
    return rows


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    rows = asyncio.run(_run(args))
    for row in rows:
        print(
            f"structured_output={row['structured_output']:<12} thinking_off={row['thinking_off']:<20} "
            f"ok={row['ok']} json_ok={row['json_ok']} error={row['error'] or '-'}"
        )
    return 0 if any(r["ok"] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
