"""The Claude tool schema must satisfy strict-mode requirements."""
from extractors.claude import TOOL
from schema import ExtractedFields


def test_tool_is_strict():
    assert TOOL["strict"] is True


def test_all_properties_required_and_closed():
    schema = TOOL["input_schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(ExtractedFields.model_json_schema()["properties"])


def test_timeline_enum_preserved():
    timeline = TOOL["input_schema"]["$defs"]["Timeline"]
    assert timeline["enum"] == ["within_30_days", "31_to_90_days", "over_90_days"]
