from __future__ import annotations

import pytest

from simple_harness_service.realtime.contracts import RealtimeOpenRequest
from simple_harness_service.realtime.tool_schema import matches_schema, parse_arguments


@pytest.mark.parametrize(
    "schema",
    [
        {"type": {}},
        {"type": [None]},
        {"type": "object", "required": [True]},
        {"type": "object", "patternProperties": {}},
        {"type": "object", "minLength": -1},
        {"type": "object", "properties": {"x": {"minimum": float("nan")}}},
    ],
)
def test_malformed_schema_is_value_error(schema: object) -> None:
    with pytest.raises(ValueError):
        RealtimeOpenRequest(
            "session",
            "instructions",
            tools=[
                {
                    "type": "function",
                    "name": "task",
                    "parameters": schema,
                }
            ],
        )


@pytest.mark.parametrize("value,expected", [(True, False), (1, True), (1.0, True), (1.5, False)])
def test_integer_schema_does_not_accept_boolean(value: object, expected: bool) -> None:
    assert matches_schema(value, {"type": "integer"}) is expected


def test_enum_uses_json_semantics() -> None:
    assert matches_schema(1.0, {"enum": [1]})
    assert matches_schema({"b": 2, "a": 1}, {"enum": [{"a": 1, "b": 2}]})
    assert not matches_schema(True, {"enum": [1]})


@pytest.mark.parametrize("payload", ['{"a":1,"a":2}', '{"a":NaN}', '{"a":1e999}', "[]"])
def test_json_arguments_reject_ambiguous_or_nonfinite_data(payload: str) -> None:
    with pytest.raises(ValueError):
        parse_arguments(payload)


def test_nested_schema_bounds_and_alternatives() -> None:
    schema = {
        "type": "array",
        "minItems": 1,
        "maxItems": 2,
        "items": {
            "oneOf": [{"type": "string", "minLength": 2}, {"type": "integer", "minimum": 1}],
        },
    }
    assert matches_schema(["ok", 2], schema)
    assert not matches_schema([], schema)
    assert not matches_schema(["x"], schema)
    assert not matches_schema([0], schema)
