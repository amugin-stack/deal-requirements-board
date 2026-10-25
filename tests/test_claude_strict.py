"""The Claude tool schema must satisfy strict-mode requirements."""

from deal_board.extractors.claude import INPUT_SCHEMA, TOOL
from deal_board.schema import ExtractedFields


def test_tool_is_strict() -> None:
    assert TOOL["strict"] is True


def test_tool_sends_the_strict_input_schema() -> None:
    assert TOOL["input_schema"] is INPUT_SCHEMA


def test_all_properties_required_and_closed() -> None:
    schema = INPUT_SCHEMA
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(ExtractedFields.model_json_schema()["properties"])


def test_timeline_enum_preserved() -> None:
    timeline = INPUT_SCHEMA["$defs"]["Timeline"]
    assert timeline["enum"] == ["within_30_days", "31_to_90_days", "over_90_days"]
