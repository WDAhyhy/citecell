"""字节级确定性工具。

T0 结论（docs/VERIFICATION_T0.md）：python-docx 与 openpyxl 同内容写两次字节不同，原因是
zip 条目时间戳，以及 openpyxl 保存时把 docProps/core.xml 的 modified 覆盖为当前时间。
本模块在写出后统一重写 zip：固定条目时间、按名排序、固定压缩参数，并把 core.xml 的
created/modified 固定为 FIXED_DT。
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import datetime
from typing import Any

FIXED_DT = datetime(2024, 1, 1, 0, 0, 0)
"""所有文档属性时间与 zip 条目时间统一使用的固定时刻（zip 要求 >= 1980）。"""
_ZIP_DT = (2024, 1, 1, 0, 0, 0)
_W3CDTF = "2024-01-01T00:00:00Z"
_CORE_TIME_RE = re.compile(rb"(<dcterms:(?:created|modified)\b[^>]*>)[^<]*(</dcterms:(?:created|modified)>)")


def canonical_json(obj: Any) -> str:
    """规范 JSON（ARCHITECTURE §11）：排序键、不转义中文、紧凑分隔符。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def canonicalize_zip(data: bytes) -> bytes:
    """重写 OOXML zip 包，使其字节只由条目内容决定。"""
    with zipfile.ZipFile(io.BytesIO(data)) as zin:
        names = sorted(zin.namelist())  # "[Content_Types].xml" 排在最前
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zout:
            for name in names:
                raw = zin.read(name)
                if name == "docProps/core.xml":
                    raw = _CORE_TIME_RE.sub(rb"\g<1>" + _W3CDTF.encode() + rb"\g<2>", raw)
                info = zipfile.ZipInfo(name, date_time=_ZIP_DT)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o600 << 16
                zout.writestr(info, raw)
    return out.getvalue()
