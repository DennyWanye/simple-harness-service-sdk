"""Bounded, offline JSON Schema subset for function tool contracts."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

_TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}
_KEYWORDS = {
    "type",
    "description",
    "title",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "const",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "anyOf",
    "oneOf",
    "allOf",
}


def json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def validate_schema(schema: object, *, depth: int = 0) -> None:
    if depth > 32 or not isinstance(schema, dict) or set(schema) - _KEYWORDS:
        raise ValueError("unsupported tool schema")
    kind = schema.get("type")
    kinds = kind if isinstance(kind, list) else [kind]
    if kind is not None and (
        not kinds or any(not isinstance(k, str) or k not in _TYPES for k in kinds)
    ):
        raise ValueError("invalid schema type")
    for key in ("description", "title"):
        if key in schema and not isinstance(schema[key], str):
            raise ValueError("invalid schema annotation")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict) or not all(isinstance(k, str) for k in properties):
        raise ValueError("invalid schema properties")
    for child in properties.values():
        validate_schema(child, depth=depth + 1)
    required = schema.get("required", [])
    if (
        not isinstance(required, list)
        or not all(isinstance(k, str) for k in required)
        or len(set(required)) != len(required)
    ):
        raise ValueError("invalid schema required")
    additional = schema.get("additionalProperties", True)
    if not isinstance(additional, bool):
        validate_schema(additional, depth=depth + 1)
    if "items" in schema:
        validate_schema(schema["items"], depth=depth + 1)
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            children = schema[key]
            if not isinstance(children, list) or not children:
                raise ValueError("invalid schema alternatives")
            for child in children:
                validate_schema(child, depth=depth + 1)
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        raise ValueError("invalid schema enum")
    for key in ("minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems"):
        if key in schema:
            bound = schema[key]
            if not isinstance(bound, (int, float)) or isinstance(bound, bool):
                raise ValueError("invalid schema bound")
            if key not in {"minimum", "maximum"} and (not isinstance(bound, int) or bound < 0):
                raise ValueError("invalid schema size bound")


def validate_tools(tools: Sequence[Mapping[str, object]], *, limit: int = 65_536) -> None:
    if not isinstance(tools, list):
        raise ValueError("tool catalog must be a list")
    try:
        payload_size = len(json_bytes(tools))
    except (ValueError, TypeError, RecursionError) as error:
        raise ValueError("tool catalog must contain bounded JSON") from error
    if payload_size > limit:
        raise ValueError("tool catalog is too large")
    names: set[str] = set()
    for tool in tools:
        if not isinstance(tool, dict) or set(tool) - {
            "type",
            "name",
            "description",
            "parameters",
            "strict",
        }:
            raise ValueError("invalid function tool")
        name = tool.get("name")
        if (
            tool.get("type") != "function"
            or not isinstance(name, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) is None
            or name in names
        ):
            raise ValueError("invalid or duplicate function name")
        names.add(name)
        if "description" in tool and not isinstance(tool["description"], str):
            raise ValueError("invalid function description")
        if "strict" in tool and not isinstance(tool["strict"], bool):
            raise ValueError("invalid function strict flag")
        parameters = tool.get("parameters")
        validate_schema(parameters)
        if not isinstance(parameters, dict) or parameters.get("type") != "object":
            raise ValueError("function parameters must be an object schema")


def _has_type(value: object, kind: str) -> bool:
    if kind == "null":
        return value is None
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "string":
        return isinstance(value, str)
    if kind == "object":
        return isinstance(value, dict)
    if kind == "array":
        return isinstance(value, list)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return kind == "number" or (
        kind == "integer" and (isinstance(value, int) or value.is_integer())
    )


def _json_equal(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _json_equal(v, right[k]) for k, v in left.items()
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _json_equal(a, b) for a, b in zip(left, right, strict=True)
        )
    return bool(left == right)


def matches_schema(value: Any, schema: Mapping[str, Any], *, depth: int = 0) -> bool:
    if depth > 32:
        return False
    kind = schema.get("type")
    if kind is not None and not any(
        _has_type(value, k) for k in (kind if isinstance(kind, list) else [kind])
    ):
        return False
    if "enum" in schema and not any(_json_equal(value, v) for v in schema["enum"]):
        return False
    if "const" in schema and not _json_equal(value, schema["const"]):
        return False
    for keyword in ("anyOf", "oneOf", "allOf"):
        if keyword in schema:
            results = [matches_schema(value, child, depth=depth + 1) for child in schema[keyword]]
            if (
                (keyword == "anyOf" and not any(results))
                or (keyword == "oneOf" and sum(results) != 1)
                or (keyword == "allOf" and not all(results))
            ):
                return False
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if any(key not in value for key in schema.get("required", [])):
            return False
        for key, child in value.items():
            child_schema = properties.get(key, schema.get("additionalProperties", True))
            if child_schema is False or (
                isinstance(child_schema, dict)
                and not matches_schema(child, child_schema, depth=depth + 1)
            ):
                return False
    if isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", math.inf):
            return False
        if "items" in schema and not all(
            matches_schema(child, schema["items"], depth=depth + 1) for child in value
        ):
            return False
    if isinstance(value, str) and not (
        schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", math.inf)
    ):
        return False
    return not (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and not (schema.get("minimum", -math.inf) <= value <= schema.get("maximum", math.inf))
    )


def parse_arguments(payload: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate argument key")
            result[key] = value
        return result

    def invalid_constant(value: str) -> None:
        raise ValueError("nonfinite argument")

    value: Any = json.loads(payload, object_pairs_hook=pairs, parse_constant=invalid_constant)
    if not isinstance(value, dict):
        raise ValueError("tool arguments must be an object")
    json_bytes(value)
    return value
