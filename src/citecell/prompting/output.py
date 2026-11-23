"""输出 Schema 构建与模型输出解析。

解析顺序（契约见 docs/ARCHITECTURE.md §7）：剥离 ``<think>…</think>`` → 去代码围栏 →
取第一个能解码的顶层 JSON 对象 → pydantic 校验（LLMExtractionOutput）→ 键对账。

日志只记录数量与代码，不记录模型输出或文档正文。
"""

from __future__ import annotations

import copy
import json
import logging
import re
from collections.abc import Sequence
from typing import Any

from pydantic import ValidationError

from citecell.models import (
    Issue,
    IssueCode,
    KeySpec,
    LLMExtractionOutput,
    Severity,
)

logger = logging.getLogger("citecell.prompting")

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE = re.compile(r"```[A-Za-z0-9_-]*[ \t]*\r?\n?(.*?)```", re.DOTALL)
_DECODER = json.JSONDecoder()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def _inline_refs(node: Any, defs: dict[str, Any]) -> Any:
    """递归把 ``{"$ref": "#/$defs/X"}`` 替换为 X 的（已内联的）副本。"""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            name = ref[len("#/$defs/") :]
            if name not in defs:
                raise KeyError(f"未知的 schema 引用: {name}")
            return _inline_refs(copy.deepcopy(defs[name]), defs)
        return {k: _inline_refs(v, defs) for k, v in node.items() if k != "$defs"}
    if isinstance(node, list):
        return [_inline_refs(v, defs) for v in node]
    return node


def build_output_schema(keys: Sequence[KeySpec], block_ids: Sequence[str] | None) -> dict[str, Any]:
    """由 LLMExtractionOutput 生成内联（无 $ref/$defs）的 JSON Schema。

    * 字段顺序：key → evidence → value（LLMFieldOutput 的声明顺序）；
    * ``key`` 约束为所问键的枚举（保持 KeySet 顺序）；
    * ``block_ids`` 非空时，``evidence[].block_id`` 约束为该枚举。
    """
    if not keys:
        raise ValueError("output_schema 至少需要一个键")
    raw = LLMExtractionOutput.model_json_schema()
    defs = raw.get("$defs", {})
    schema = _inline_refs({k: v for k, v in raw.items() if k != "$defs"}, defs)

    field_schema = schema["properties"]["fields"]["items"]
    props = field_schema["properties"]
    key_names = [k.key for k in keys]
    props["key"] = {**props["key"], "type": "string", "enum": key_names}

    if block_ids is not None:
        if not block_ids:
            raise ValueError("block_ids 为空列表时无法构造枚举")
        ev_item = props["evidence"]["items"]
        ev_item["properties"]["block_id"] = {
            **ev_item["properties"]["block_id"],
            "type": "string",
            "enum": list(block_ids),
        }
    return schema


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _issue(code: IssueCode, severity: Severity, message: str) -> Issue:
    return Issue(code=code, severity=severity, message=message[:500])


def _strip_wrappers(text: str) -> str:
    text = _THINK_BLOCK.sub("", text)
    if "</think>" in text:  # 只有结束标签（思考前缀被截断）的情形
        text = text.split("</think>")[-1]
    m = _FENCE.search(text)
    if m:
        return m.group(1)
    return text


def _first_json_object(text: str) -> dict[str, Any] | None:
    """取第一个能被解码为 JSON 对象的位置；后面允许跟多余文字。"""
    start = text.find("{")
    while start != -1:
        try:
            obj, _end = _DECODER.raw_decode(text, start)
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
            continue
        return obj if isinstance(obj, dict) else None
    return None


def parse_llm_output(
    text: str,
    keys: Sequence[str] | Sequence[KeySpec],
) -> tuple[LLMExtractionOutput | None, list[Issue]]:
    """解析一次模型输出。

    返回 ``(输出, 问题列表)``：
    * 无法得到合法对象或 pydantic 校验失败 → ``(None, [OUTPUT_SCHEMA_INVALID])``；
    * 每个缺失的键 → 一条 ``KEY_MISSING_IN_OUTPUT``（WARNING）；
    * 未知键被丢弃，并记一条 ``OUTPUT_SCHEMA_INVALID``（WARNING）；
    * 同一键重复出现时全部保留，顺序与模型输出一致。
    """
    key_names = [k.key if isinstance(k, KeySpec) else str(k) for k in keys]
    known = set(key_names)

    obj = _first_json_object(_strip_wrappers(text or ""))
    if obj is None:
        return None, [_issue(IssueCode.OUTPUT_SCHEMA_INVALID, Severity.ERROR, "输出中没有可解析的 JSON 对象")]
    try:
        parsed = LLMExtractionOutput.model_validate(obj)
    except ValidationError as exc:
        n = len(exc.errors())
        return None, [
            _issue(IssueCode.OUTPUT_SCHEMA_INVALID, Severity.ERROR, f"JSON 不符合输出结构（{n} 处错误）")
        ]

    issues: list[Issue] = []
    produced = {f.key for f in parsed.fields}

    unknown = sorted({f.key for f in parsed.fields if f.key not in known})
    if unknown:
        issues.append(
            _issue(IssueCode.OUTPUT_SCHEMA_INVALID, Severity.WARNING, f"丢弃 {len(unknown)} 个未知键: {', '.join(unknown)}")
        )
        logger.debug("丢弃未知键 %d 个", len(unknown))
    kept = [f for f in parsed.fields if f.key in known]

    for name in key_names:
        if name not in produced:
            issues.append(
                _issue(IssueCode.KEY_MISSING_IN_OUTPUT, Severity.WARNING, f"输出中缺少键: {name}")
            )
            # KEY_MISSING_IN_OUTPUT 的 message 只含键名，不含正文。

    return LLMExtractionOutput(fields=kept), issues


__all__ = ["build_output_schema", "parse_llm_output"]
