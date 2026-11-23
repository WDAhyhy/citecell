"""脚本化测试后端：按预设顺序返回响应或抛出错误。不访问网络。"""

from __future__ import annotations

from collections.abc import Iterable

from citecell.interfaces import BackendError
from citecell.models import BackendInfo, ChatRequest, ChatResponse

Scripted = str | ChatResponse | BaseException


class FakeBackend:
    """按脚本依次返回：``str`` 视为 content；``ChatResponse`` 原样返回；异常则抛出。

    脚本耗尽后：``repeat_last=True`` 时重复最后一条，否则抛 BackendError。
    所有收到的请求保存在 ``requests`` 中，便于断言（不得含 context 之外的额外副作用）。
    """

    def __init__(
        self,
        script: Iterable[Scripted],
        *,
        backend_id: str = "fake",
        model: str = "fake-model",
        repeat_last: bool = False,
    ) -> None:
        self._script = list(script)
        if not self._script:
            raise ValueError("FakeBackend 至少需要一条脚本响应")
        self._pos = 0
        self._repeat_last = repeat_last
        self.backend_id = backend_id
        self.model = model
        self.requests: list[ChatRequest] = []

    def info(self) -> BackendInfo:
        return BackendInfo(backend_id=self.backend_id, provider="fake", model=self.model)

    async def chat(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        if self._pos >= len(self._script):
            if not self._repeat_last:
                raise BackendError("FakeBackend 脚本响应已用完")
            item = self._script[-1]
        else:
            item = self._script[self._pos]
            self._pos += 1
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, ChatResponse):
            return item
        return ChatResponse(content=item)
