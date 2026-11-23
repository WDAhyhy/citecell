"""输出 Schema 与 parse_llm_output 的测试。"""

import json

from citecell.models import IssueCode, Severity
from citecell.prompting.output import build_output_schema, parse_llm_output

KEYS = ["合同金额", "付款日期", "联系电话"]
VALID_ONE = {
    "fields": [
        {
            "key": "合同金额",
            "evidence": [{"block_id": "p2-b1", "quote": "12,000 元"}],
            "value": "12,000 元",
        }
    ]
}


def _keyspecs(t8):
    return [t8.make_key(k) for k in KEYS]


def _walk(node, found):
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("$ref", "$defs"):
                found.append(k)
            _walk(v, found)
    elif isinstance(node, list):
        for v in node:
            _walk(v, found)
    return found


# ---------------------------------------------------------------- schema


def test_schema_field_order_and_key_enum(t8):
    schema = build_output_schema(_keyspecs(t8), None)
    item = schema["properties"]["fields"]["items"]
    assert list(item["properties"]) == ["key", "evidence", "value"]
    assert item["properties"]["key"]["enum"] == KEYS


def test_schema_has_no_refs(t8):
    schema = build_output_schema(_keyspecs(t8), ["p1-b1", "p2-b1"])
    assert _walk(schema, []) == []
    assert "$defs" not in schema


def test_schema_block_id_enum_only_when_requested(t8):
    unconstrained = build_output_schema(_keyspecs(t8), None)
    ev = unconstrained["properties"]["fields"]["items"]["properties"]["evidence"]["items"]["properties"]
    assert "enum" not in ev["block_id"]

    constrained = build_output_schema(_keyspecs(t8), ["p1-b1", "p2-b1"])
    ev = constrained["properties"]["fields"]["items"]["properties"]["evidence"]["items"]["properties"]
    assert ev["block_id"]["enum"] == ["p1-b1", "p2-b1"]


def test_schema_only_lists_asked_keys(t8):
    schema = build_output_schema([t8.make_key("付款日期")], None)
    item = schema["properties"]["fields"]["items"]
    assert item["properties"]["key"]["enum"] == ["付款日期"]


# ---------------------------------------------------------------- parsing


def test_pure_json_parses():
    out, issues = parse_llm_output(json.dumps(VALID_ONE, ensure_ascii=False), KEYS)
    assert issues == [] or all(i.code == IssueCode.KEY_MISSING_IN_OUTPUT for i in issues)
    assert out is not None and [f.key for f in out.fields] == ["合同金额"]


def test_fenced_json_parses():
    text = "```json\n" + json.dumps(VALID_ONE, ensure_ascii=False) + "\n```"
    out, _ = parse_llm_output(text, ["合同金额"])
    assert out is not None and out.fields[0].value == "12,000 元"


def test_think_prefix_is_stripped():
    text = "<think>先看第二段，金额在那里。</think>\n" + json.dumps(VALID_ONE, ensure_ascii=False)
    out, issues = parse_llm_output(text, ["合同金额"])
    assert issues == []
    assert out is not None and out.fields[0].evidence[0].block_id == "p2-b1"


def test_trailing_text_after_json_is_ignored():
    text = json.dumps(VALID_ONE, ensure_ascii=False) + "\n以上为抽取结果，请核对。"
    out, issues = parse_llm_output(text, ["合同金额"])
    assert issues == []
    assert out is not None and len(out.fields) == 1


def test_leading_prose_before_json_is_ignored():
    text = "结果如下：" + json.dumps(VALID_ONE, ensure_ascii=False)
    out, _ = parse_llm_output(text, ["合同金额"])
    assert out is not None


def test_invalid_input_is_schema_invalid():
    for bad in ["这不是 JSON", "", "{not json}", "[1, 2, 3]"]:
        out, issues = parse_llm_output(bad, ["合同金额"])
        assert out is None, bad
        assert [i.code for i in issues] == [IssueCode.OUTPUT_SCHEMA_INVALID], bad
        assert issues[0].severity == Severity.ERROR


def test_wrong_structure_is_schema_invalid():
    out, issues = parse_llm_output('{"fields": "不是列表"}', ["合同金额"])
    assert out is None
    assert [i.code for i in issues] == [IssueCode.OUTPUT_SCHEMA_INVALID]


def test_one_missing_key_issue_per_key():
    out, issues = parse_llm_output(json.dumps(VALID_ONE, ensure_ascii=False), KEYS)
    assert out is not None
    missing = [i for i in issues if i.code == IssueCode.KEY_MISSING_IN_OUTPUT]
    assert len(missing) == 2
    assert all(i.severity == Severity.WARNING for i in missing)
    assert "付款日期" in missing[0].message and "联系电话" in missing[1].message


def test_unknown_key_is_dropped_with_warning():
    obj = {
        "fields": [
            {"key": "合同金额", "evidence": [], "value": None},
            {"key": "不存在的键", "evidence": [], "value": None},
        ]
    }
    out, issues = parse_llm_output(json.dumps(obj, ensure_ascii=False), ["合同金额"])
    assert out is not None
    assert [f.key for f in out.fields] == ["合同金额"]
    assert any(i.severity == Severity.WARNING for i in issues)


def test_duplicate_key_entries_are_all_kept():
    obj = {
        "fields": [
            {"key": "合同金额", "evidence": [{"block_id": "p2-b1", "quote": "12,000"}], "value": "12,000"},
            {"key": "合同金额", "evidence": [{"block_id": "p3-b1", "quote": "一万"}], "value": "一万"},
        ]
    }
    out, _ = parse_llm_output(json.dumps(obj, ensure_ascii=False), ["合同金额"])
    assert out is not None
    assert [f.value for f in out.fields] == ["12,000", "一万"]


def test_accepts_keyspec_list(t8):
    out, issues = parse_llm_output(json.dumps(VALID_ONE, ensure_ascii=False), _keyspecs(t8))
    assert out is not None
    assert {i.code for i in issues} == {IssueCode.KEY_MISSING_IN_OUTPUT}
