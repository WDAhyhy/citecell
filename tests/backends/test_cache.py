"""请求哈希、文件缓存、replay_only 与规范 JSON。"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from citecell.backends import CachedBackend, FakeBackend, FileResponseCache, request_sha256
from citecell.canonical import canonical_json, canonical_sha256
from citecell.interfaces import BackendError
from citecell.models import ChatMessage, ChatRequest, ChatResponse, RequestContext

MODEL = "m"


def _req(**overrides) -> ChatRequest:
    base = dict(
        messages=[ChatMessage(role="system", content="s"), ChatMessage(role="user", content="u")],
        json_schema={"type": "object", "properties": {"a": {"type": "string"}}},
        temperature=0.0,
        seed=7,
        max_tokens=100,
        extra={"x": 1, "y": {"b": 2, "a": 1}},
    )
    base.update(overrides)
    return ChatRequest(**base)


def test_dict_key_order_does_not_change_hash():
    a = _req(extra={"x": 1, "y": {"b": 2, "a": 1}})
    b = _req(extra={"y": {"a": 1, "b": 2}, "x": 1})
    assert request_sha256(a, MODEL) == request_sha256(b, MODEL)


def test_context_does_not_change_hash():
    ctx = RequestContext(doc_id="abababababababab", window_id="w1", keys=["k"])
    assert request_sha256(_req(), MODEL) == request_sha256(_req(context=ctx), MODEL)


def test_timeout_does_not_change_hash():
    assert request_sha256(_req(timeout_s=5.0), MODEL) == request_sha256(_req(timeout_s=500.0), MODEL)


def test_temperature_changes_hash():
    assert request_sha256(_req(temperature=0.0), MODEL) != request_sha256(_req(temperature=0.7), MODEL)


def test_model_changes_hash():
    assert request_sha256(_req(), "m1") != request_sha256(_req(), "m2")


def test_hash_is_64_lowercase_hex():
    h = request_sha256(_req(), MODEL)
    assert len(h) == 64 and h == h.lower() and int(h, 16) >= 0


def test_canonical_json_sorts_and_keeps_unicode():
    assert canonical_json({"b": 1, "a": "中文"}) == '{"a":"中文","b":1}'


def test_canonical_decimal_as_string():
    assert canonical_json({"v": Decimal("1.50")}) == '{"v":"1.50"}'


def test_canonical_rejects_nan():
    with pytest.raises(ValueError):
        canonical_json({"v": float("nan")})


def test_canonical_sha256_order_independent():
    assert canonical_sha256({"a": 1, "b": [2]}) == canonical_sha256({"b": [2], "a": 1})


def test_file_cache_round_trip_and_layout(tmp_path):
    cache = FileResponseCache(tmp_path / "llm")
    key = request_sha256(_req(), MODEL)
    assert cache.get(key) is None
    cache.put(key, ChatResponse(content="{}", finish_reason="stop"))
    assert (tmp_path / "llm" / key[:2] / f"{key}.json").exists()
    assert cache.get(key).content == "{}"


def test_file_cache_rejects_non_sha_keys(tmp_path):
    cache = FileResponseCache(tmp_path)
    with pytest.raises(ValueError):
        cache.get("../../etc/passwd")


def test_cached_backend_hit_skips_inner_call(tmp_path):
    inner = FakeBackend(["first"], model=MODEL)
    cached = CachedBackend(inner, FileResponseCache(tmp_path))
    first = asyncio.run(cached.chat(_req()))
    second = asyncio.run(cached.chat(_req()))
    assert first.content == "first" and first.cache_hit is False
    assert second.content == "first" and second.cache_hit is True
    assert len(inner.requests) == 1


def test_replay_only_miss_is_backend_error(tmp_path):
    inner = FakeBackend(["x"], model=MODEL)
    cached = CachedBackend(inner, FileResponseCache(tmp_path), replay_only=True)
    with pytest.raises(BackendError):
        asyncio.run(cached.chat(_req()))
    assert inner.requests == []


def test_replay_only_hit_is_served_from_cache(tmp_path):
    cache = FileResponseCache(tmp_path)
    key = request_sha256(_req(), MODEL)
    cache.put(key, ChatResponse(content="cached"))
    inner = FakeBackend(["should-not-be-called"], model=MODEL)
    cached = CachedBackend(inner, cache, replay_only=True)
    resp = asyncio.run(cached.chat(_req()))
    assert resp.content == "cached" and resp.cache_hit is True
