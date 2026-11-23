"""请求哈希缓存（ARCH §7、§11；ADR-011）。

* 请求哈希 = 规范 JSON {model, messages, json_schema, temperature, top_p, seed, max_tokens, extra}
  的 sha256。**不含** ``context`` 与 ``timeout_s``。
* 缓存文件：``<root>/<sha[:2]>/<sha>.json``。内容含正文，永不提交；根目录应位于被 .gitignore 忽略的
  ``outputs/`` 之下（默认 ``outputs/cache/llm``）。
* ``replay_only=True`` 时只读缓存，未命中即 BackendError（复现实验）。
* 日志不输出缓存内容。
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from citecell.canonical import canonical_sha256
from citecell.interfaces import BackendError, ExtractionBackend
from citecell.models import ChatRequest, ChatResponse

log = logging.getLogger("citecell.backends")

DEFAULT_CACHE_ROOT = Path("outputs/cache/llm")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def request_sha256(request: ChatRequest, model: str) -> str:
    """请求的缓存键。``context`` 与 ``timeout_s`` 不参与哈希。"""
    payload = {
        "model": model,
        "messages": [m.model_dump() for m in request.messages],
        "json_schema": request.json_schema,
        "temperature": request.temperature,
        "top_p": request.top_p,
        "seed": request.seed,
        "max_tokens": request.max_tokens,
        "extra": request.extra,
    }
    return canonical_sha256(payload)


class FileResponseCache:
    """基于文件系统的 ``ResponseCache`` 实现。"""

    def __init__(self, root: Path | str = DEFAULT_CACHE_ROOT) -> None:
        self.root = Path(root)

    def _path(self, request_sha256: str) -> Path:
        if not _SHA_RE.match(request_sha256):
            raise ValueError("缓存键必须是 64 位小写十六进制 sha256")
        return self.root / request_sha256[:2] / f"{request_sha256}.json"

    def get(self, request_sha256: str) -> ChatResponse | None:
        path = self._path(request_sha256)
        if not path.exists():
            return None
        try:
            return ChatResponse.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            # 损坏的缓存不静默覆盖，交由人工处理
            raise BackendError(f"缓存文件损坏: {request_sha256[:12]}…") from exc

    def put(self, request_sha256: str, response: ChatResponse) -> None:
        path = self._path(request_sha256)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        data = json.dumps(response.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp, path)


class CachedBackend:
    """在任意 ``ExtractionBackend`` 外包一层请求哈希缓存。"""

    def __init__(
        self,
        inner: ExtractionBackend,
        cache: FileResponseCache,
        *,
        replay_only: bool = False,
    ) -> None:
        self.inner = inner
        self.cache = cache
        self.replay_only = replay_only
        self.backend_id = inner.backend_id

    def info(self):  # noqa: ANN201 - 与 ExtractionBackend 协议一致
        return self.inner.info()

    def key_for(self, request: ChatRequest) -> str:
        return request_sha256(request, self.inner.info().model)

    async def chat(self, request: ChatRequest) -> ChatResponse:
        key = self.key_for(request)
        hit = self.cache.get(key)
        if hit is not None:
            log.debug("缓存命中 %s…", key[:12])
            return hit.model_copy(update={"cache_hit": True})
        if self.replay_only:
            raise BackendError(f"replay_only 模式下缓存未命中: {key[:12]}…")
        response = await self.inner.chat(request)
        self.cache.put(key, response)
        return response
