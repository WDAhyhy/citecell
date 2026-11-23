"""OpenAI 兼容后端：用 httpx.MockTransport 模拟服务端，不访问网络。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from citecell.backends import OpenAICompatibleBackend, RemoteBackendRefused, is_local_host
from citecell.interfaces import BackendError
from citecell.models import ChatMessage, ChatRequest, RequestContext

OK_BODY = {
    "model": "mock-model",
    "choices": [{"message": {"content": '{"fields": []}'}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 12, "completion_tokens": 5},
}


def _request(**overrides) -> ChatRequest:
    base = dict(
        messages=[ChatMessage(role="user", content="hi")],
        json_schema={"type": "object"},
        timeout_s=5.0,
    )
    base.update(overrides)
    return ChatRequest(**base)


def _backend(handler, **kwargs) -> tuple[OpenAICompatibleBackend, list[float]]:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    kwargs.setdefault("base_url", "http://127.0.0.1:8000")
    kwargs.setdefault("model", "mock-model")
    kwargs.setdefault("max_transport_retries", 3)
    backend = OpenAICompatibleBackend(
        transport=httpx.MockTransport(handler), sleep=fake_sleep, **kwargs
    )
    return backend, sleeps


def test_200_returns_content_and_counts_one_attempt():
    backend, sleeps = _backend(lambda req: httpx.Response(200, json=OK_BODY))
    resp = asyncio.run(backend.chat(_request()))
    assert resp.content == '{"fields": []}'
    assert resp.prompt_tokens == 12 and resp.completion_tokens == 5
    assert resp.model_reported == "mock-model"
    assert resp.cache_hit is False
    assert backend.http_attempts == 1
    assert sleeps == []


def test_500_then_200_retries_and_counts_two_attempts():
    responses = iter([httpx.Response(500, text="boom"), httpx.Response(200, json=OK_BODY)])
    backend, sleeps = _backend(lambda req: next(responses))
    resp = asyncio.run(backend.chat(_request()))
    assert resp.content == '{"fields": []}'
    assert backend.http_attempts == 2
    assert sleeps == [0.5]  # 首次退避 = backoff_base_s * 2**0


def test_429_is_retried_with_exponential_backoff():
    responses = iter(
        [httpx.Response(429), httpx.Response(429), httpx.Response(200, json=OK_BODY)]
    )
    backend, sleeps = _backend(lambda req: next(responses), backoff_base_s=1.0)
    asyncio.run(backend.chat(_request()))
    assert backend.http_attempts == 3
    assert sleeps == [1.0, 2.0]


def test_persistent_5xx_gives_backend_error_after_all_retries():
    backend, _ = _backend(lambda req: httpx.Response(503), max_transport_retries=2)
    with pytest.raises(BackendError):
        asyncio.run(backend.chat(_request()))
    assert backend.http_attempts == 3


def test_timeout_raises_backend_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    backend, _ = _backend(handler, max_transport_retries=0)
    with pytest.raises(BackendError):
        asyncio.run(backend.chat(_request()))
    assert backend.http_attempts == 1


def test_400_is_not_retried():
    backend, sleeps = _backend(lambda req: httpx.Response(400, json={"error": "bad"}))
    with pytest.raises(BackendError):
        asyncio.run(backend.chat(_request()))
    assert backend.http_attempts == 1
    assert sleeps == []


def test_remote_host_refused_at_construction():
    with pytest.raises(RemoteBackendRefused):
        OpenAICompatibleBackend(base_url="http://10.0.0.5:8000", model="m", allow_remote_backend=False)


def test_remote_host_allowed_only_when_explicit():
    backend = OpenAICompatibleBackend(
        base_url="http://10.0.0.5:8000", model="m", allow_remote_backend=True
    )
    assert backend.info().base_url_host == "10.0.0.5:8000"


@pytest.mark.parametrize(
    "url", ["http://localhost:8000", "http://127.0.0.1:8000", "http://[::1]:8000"]
)
def test_local_hosts_allowed(url):
    OpenAICompatibleBackend(base_url=url, model="m")


def test_is_local_host_table():
    assert is_local_host("localhost") and is_local_host("127.0.0.1") and is_local_host("::1")
    assert not is_local_host("10.0.0.5") and not is_local_host(None)


def test_request_body_never_contains_context():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=OK_BODY)

    backend, _ = _backend(handler)
    req = _request(
        context=RequestContext(doc_id="abababababababab", window_id="w1", keys=["合同金额"]),
        extra={"chat_template_kwargs": {"enable_thinking": False}},
    )
    asyncio.run(backend.chat(req))
    body = seen[0]
    assert "context" not in body
    assert "timeout_s" not in body
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["stream"] is False and body["model"] == "mock-model"


def test_response_format_json_schema_default_structure():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=OK_BODY)

    backend, _ = _backend(handler, structured_output="json_schema")
    asyncio.run(backend.chat(_request(json_schema={"type": "object", "title": "T"})))
    rf = seen[0]["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["schema"] == {"type": "object", "title": "T"}


def test_response_format_is_configurable_and_modes_differ():
    def body_for(**kwargs) -> dict:
        backend = OpenAICompatibleBackend(base_url="http://localhost:1", model="m", **kwargs)
        return backend.build_body(_request())

    assert body_for(structured_output="json_object")["response_format"] == {"type": "json_object"}
    assert "response_format" not in body_for(structured_output="none")
    custom = body_for(
        structured_output="json_schema",
        response_format_builder=lambda schema: {"type": "grammar", "schema": schema},
    )
    assert custom["response_format"] == {"type": "grammar", "schema": {"type": "object"}}


def test_malformed_success_body_is_backend_error():
    backend, _ = _backend(lambda req: httpx.Response(200, json={"nope": 1}))
    with pytest.raises(BackendError):
        asyncio.run(backend.chat(_request()))


def test_info_hides_credentials_and_path():
    backend = OpenAICompatibleBackend(
        base_url="http://127.0.0.1:8080/v1",
        model="m",
        api_key="SECRET-TOKEN",
    )
    info = backend.info()
    assert info.base_url_host == "127.0.0.1:8080"
    assert "SECRET" not in info.model_dump_json()
