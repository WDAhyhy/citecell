"""OpenAI 兼容 ``/v1/chat/completions`` 后端（ARCH §7）。

* 只用 httpx，不引入 openai SDK。
* 传输重试（429 / 5xx / 超时 / 连接错误）在本类内部，指数退避；400 等客户端错误不重试。
* 语义重试（校验失败重问）不在这里，属于 Producer（T8）。
* ``context`` 字段绝不发送给服务端。
* 结构化输出的 ``response_format`` 结构可配置（``response_format_builder``），
  各后端的确切写法未核实，由 ``scripts/probe_backend.py`` 确认。
* 远端后端默认禁止（ADR-012）：``base_url`` 的主机不是 localhost/127.0.0.1/::1 时，
  构造即报错，除非显式设置 ``allow_remote_backend=True``。
* 日志只记录尝试次数与 HTTP 状态码，不记录请求体或响应正文。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from citecell.interfaces import BackendError
from citecell.models import BackendInfo, ChatRequest, ChatResponse

log = logging.getLogger("citecell.backends")

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
RETRYABLE_STATUS = frozenset({429}) | frozenset(range(500, 600))

ResponseFormatBuilder = Callable[[dict[str, Any]], dict[str, Any]]


class RemoteBackendRefused(BackendError):
    """base_url 指向非本机地址，且未设置 allow_remote_backend。"""


def is_local_host(hostname: str | None) -> bool:
    return hostname is not None and hostname.lower() in LOCAL_HOSTS


def default_json_schema_format(schema: dict[str, Any]) -> dict[str, Any]:
    """默认的 json_schema 结构（OpenAI 形式）。各服务端是否接受未核实，见 probe 脚本。"""
    return {
        "type": "json_schema",
        "json_schema": {"name": "citecell_output", "schema": schema, "strict": False},
    }


def _split_base_url(base_url: str) -> tuple[str, int]:
    parts = urlsplit(base_url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise BackendError("base_url 必须是带主机名的 http(s) 地址")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.hostname, port


class OpenAICompatibleBackend:
    """对 OpenAI 兼容服务端的单次 chat 调用封装（含传输重试）。"""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        backend_id: str = "openai-compatible",
        allow_remote_backend: bool = False,
        max_concurrency: int = 2,
        max_transport_retries: int = 3,
        backoff_base_s: float = 0.5,
        structured_output: Literal["json_schema", "json_object", "none"] = "json_schema",
        response_format_builder: ResponseFormatBuilder | None = None,
        extra_params: dict[str, Any] | None = None,
        thinking: bool = False,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        host, port = _split_base_url(base_url)
        if not is_local_host(host) and not allow_remote_backend:
            raise RemoteBackendRefused(
                "后端地址不是 localhost/127.0.0.1/::1；文档会发往远端（数据出境）。"
                "如确需使用，请显式设置 allow_remote_backend=True（ADR-012）"
            )
        if max_concurrency < 1 or max_transport_retries < 0:
            raise ValueError("max_concurrency 必须 >= 1，max_transport_retries 必须 >= 0")

        self.backend_id = backend_id
        self.model = model
        self.allow_remote_backend = allow_remote_backend
        self.structured_output = structured_output
        self.thinking = thinking
        self.max_transport_retries = max_transport_retries
        self.max_concurrency = max_concurrency
        self.http_attempts = 0  # 实际发出的 HTTP 请求数（含重试），供测试与审计计数

        self._endpoint = _endpoint_url(base_url)
        self._host = host
        self._port = port
        self._backoff_base_s = backoff_base_s
        self._builder = response_format_builder or default_json_schema_format
        self._extra_params = dict(extra_params or {})
        self._api_key = api_key
        self._transport = transport
        self._sleep = sleep
        self._semaphore = asyncio.Semaphore(max_concurrency)

    # ------------------------------------------------------------------
    def info(self) -> BackendInfo:
        """后端快照（只含 host:port，不含凭据与路径）。温度等请求级参数由调用方在清单中另记。"""
        host = f"[{self._host}]" if ":" in self._host else self._host
        return BackendInfo(
            backend_id=self.backend_id,
            provider="openai_compatible",
            base_url_host=f"{host}:{self._port}",
            model=self.model,
            structured_output=self.structured_output,
            thinking=self.thinking,
            max_concurrency=self.max_concurrency,
            extra_params={k: v for k, v in self._extra_params.items() if isinstance(v, (str, int, bool))},
        )

    def build_body(self, request: ChatRequest) -> dict[str, Any]:
        """构造发往服务端的 JSON 请求体。不含 ``context``。"""
        body: dict[str, Any] = {}
        body.update(self._extra_params)
        body.update(request.extra)
        body.pop("context", None)  # 双保险：context 永不发送
        body.update(
            {
                "model": self.model,
                "messages": [m.model_dump() for m in request.messages],
                "temperature": request.temperature,
                "max_tokens": request.max_tokens,
                "stream": False,
            }
        )
        if request.top_p is not None:
            body["top_p"] = request.top_p
        if request.seed is not None:
            body["seed"] = request.seed
        if request.json_schema is not None:
            if self.structured_output == "json_schema":
                body["response_format"] = self._builder(request.json_schema)
            elif self.structured_output == "json_object":
                body["response_format"] = {"type": "json_object"}
        return body

    async def chat(self, request: ChatRequest) -> ChatResponse:
        body = self.build_body(request)
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        timeout = httpx.Timeout(request.timeout_s)
        last_reason = "未知"

        async with self._semaphore:
            for attempt in range(self.max_transport_retries + 1):
                self.http_attempts += 1
                started = time.perf_counter()
                try:
                    async with httpx.AsyncClient(transport=self._transport, timeout=timeout) as client:
                        resp = await client.post(self._endpoint, json=body, headers=headers)
                except httpx.TimeoutException:
                    last_reason = "超时"
                except httpx.TransportError:
                    last_reason = "连接错误"
                else:
                    status = resp.status_code
                    if status == 200:
                        latency_ms = (time.perf_counter() - started) * 1000.0
                        return _parse_response(resp, latency_ms)
                    if status not in RETRYABLE_STATUS:
                        log.warning("后端返回 HTTP %d，不重试", status)
                        raise BackendError(f"后端返回 HTTP {status}（客户端错误，不重试）")
                    last_reason = f"HTTP {status}"

                log.info("传输失败（%s），第 %d 次尝试", last_reason, attempt + 1)
                if attempt < self.max_transport_retries:
                    await self._sleep(self._backoff_base_s * (2**attempt))

        raise BackendError(f"传输失败，已尝试 {self.max_transport_retries + 1} 次（最后原因：{last_reason}）")


def _endpoint_url(base_url: str) -> str:
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        return root + "/chat/completions"
    return root + "/v1/chat/completions"


def _parse_response(resp: httpx.Response, latency_ms: float) -> ChatResponse:
    try:
        data = resp.json()
    except ValueError as exc:
        raise BackendError("后端响应不是合法 JSON") from exc
    try:
        choice = data["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise BackendError("后端响应结构不符合 OpenAI 兼容格式") from exc
    if not isinstance(content, str):
        raise BackendError("后端响应 content 不是字符串")
    usage = data.get("usage") or {}
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    model_reported = data.get("model")
    return ChatResponse(
        content=content,
        finish_reason=choice.get("finish_reason"),
        prompt_tokens=prompt_tokens if isinstance(prompt_tokens, int) else None,
        completion_tokens=completion_tokens if isinstance(completion_tokens, int) else None,
        latency_ms=latency_ms,
        model_reported=model_reported if isinstance(model_reported, str) else None,
        cache_hit=False,
    )
