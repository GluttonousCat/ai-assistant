import pytest

from core.json_response import parse_json_response


@pytest.mark.parametrize(
    "text,expected",
    [
        ('{"内容":"中文", "n": 1}', {"内容": "中文", "n": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('说明文字\n{"a": {"b": 2}}\n结束', {"a": {"b": 2}}),
        ("[1, 2]", [1, 2]),
        ("null", None),
        ("", None),
        ("{broken}", None),
        ('{"a":1} {"b":2}', None),
    ],
)
def test_parser_preserves_values_and_greedy_object_fallback(text, expected):
    assert parse_json_response(text) == expected


def test_global_fence_removal_is_explicit_for_legacy_sql_responses():
    text = '{"value":"```jsonliteral```"}'
    assert parse_json_response(text) == {"value": "```jsonliteral```"}
    assert parse_json_response(text, fences="all") == {"value": "literal"}
    assert parse_json_response("```json\n[1]\n```", fences="raw") is None


def test_legacy_parser_entry_points_remain_available():
    from skills.report.skill import ReportSkill
    from tools.finance.report_meta_analysis import _parse_json
    from tools.cninfo.repair import _parse_json as parse_object

    assert ReportSkill._parse_json('```json\n{"a":1}\n```') == {"a": 1}
    assert _parse_json("[1]") == [1]
    assert parse_object("[1]") is None
