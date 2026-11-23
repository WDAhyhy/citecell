"""模型后端（ARCH §7）：OpenAI 兼容客户端、请求哈希缓存、测试后端与档位预设。

不包含任何真实模型下载或服务启动逻辑；远端后端默认禁止（ADR-012）。
"""

from citecell.backends.cache import (
    DEFAULT_CACHE_ROOT,
    CachedBackend,
    FileResponseCache,
    request_sha256,
)
from citecell.backends.fake import FakeBackend
from citecell.backends.openai_compat import (
    OpenAICompatibleBackend,
    RemoteBackendRefused,
    is_local_host,
)
from citecell.backends.oracle import NoisyOracleBackend, OracleBackend
from citecell.backends.profiles import PROFILE_NAMES, get_profile, profile_doc

__all__ = [
    "DEFAULT_CACHE_ROOT",
    "PROFILE_NAMES",
    "CachedBackend",
    "FakeBackend",
    "FileResponseCache",
    "NoisyOracleBackend",
    "OpenAICompatibleBackend",
    "OracleBackend",
    "RemoteBackendRefused",
    "get_profile",
    "is_local_host",
    "profile_doc",
    "request_sha256",
]
