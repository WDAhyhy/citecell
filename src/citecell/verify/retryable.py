"""可重试的问题代码集合（ARCH §6.2 步骤 7）。

语义重试只针对这些代码：LLMCandidateProducer（T8/T10）收到这些问题后，
只重问失败的键，附上结构化错误，最多 ``max_semantic_retries`` 次。
不在本集合中的代码（例如 CARDINALITY_UNSUPPORTED、UNIT_MISMATCH、CONFLICTING_CANDIDATES）
表示问题不是模型输出可修正的，重试没有意义。

本模块只导出常量，不含逻辑；集合内容与 ARCH §6.2 步骤 7 逐项对应，修改需同步文档。
"""

from __future__ import annotations

from citecell.models import IssueCode

RETRYABLE_ISSUE_CODES: frozenset[IssueCode] = frozenset(
    {
        IssueCode.BLOCK_NOT_FOUND,
        IssueCode.QUOTE_NOT_IN_BLOCK,
        IssueCode.QUOTE_FUZZY_ONLY,
        IssueCode.QUOTE_AMBIGUOUS,
        IssueCode.VALUE_NOT_IN_QUOTE,
        IssueCode.VALUE_CROSSES_QUOTE_BOUNDARY,
        IssueCode.VALUE_UNPARSEABLE,
        IssueCode.ENUM_OUT_OF_RANGE,
        IssueCode.EMPTY_EVIDENCE_WITH_VALUE,
        IssueCode.EVIDENCE_WITHOUT_VALUE,
        IssueCode.KEY_MISSING_IN_OUTPUT,
        IssueCode.OUTPUT_SCHEMA_INVALID,
    }
)
"""可重试的 IssueCode 集合（不可变，供 T10 的重试循环使用）。"""

__all__ = ["RETRYABLE_ISSUE_CODES"]
