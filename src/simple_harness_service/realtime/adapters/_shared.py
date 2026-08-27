"""Provider-neutral validation helpers shared by native Realtime adapters."""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from collections.abc import Mapping
from typing import Any

from ..contracts import RealtimeError, RealtimeErrorCode, ResponseUsage


def require_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, f"{name} is required")
    return value


def require_nonnegative_integer(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, f"{name} must be an integer")
    return value


def require_object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, f"{name} must be an object")
    return value


def new_event_id() -> str:
    return f"event_{uuid.uuid4().hex}"


def strict_json_object(payload: str) -> dict[str, Any]:
    try:
        value = json.loads(
            payload,
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("non-finite")),
        )
    except RealtimeError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "invalid JSON") from error
    if not isinstance(value, dict):
        raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "event must be an object")
    return value


def decode_base64_pcm(value: object, *, limit: int) -> bytes:
    if not isinstance(value, str) or not value:
        raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "audio must be base64")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as error:
        raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "invalid base64 audio") from error
    if len(decoded) == 0 or len(decoded) % 2:
        raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "PCM must contain whole samples")
    if len(decoded) > limit:
        raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "PCM event is too large")
    return decoded


def parse_terminal_usage(
    response: Mapping[str, object],
    *,
    input_details_field: str,
    output_details_field: str,
) -> ResponseUsage | None:
    raw = response.get("usage")
    if raw is None:
        return None
    usage = require_object(raw, "usage")
    input_details = require_object(usage.get(input_details_field), input_details_field)
    output_details = require_object(usage.get(output_details_field), output_details_field)
    input_tokens = require_nonnegative_integer(usage.get("input_tokens"), "input_tokens")
    output_tokens = require_nonnegative_integer(usage.get("output_tokens"), "output_tokens")
    total_tokens = require_nonnegative_integer(usage.get("total_tokens"), "total_tokens")
    input_text = require_nonnegative_integer(
        input_details.get("text_tokens"), "input text tokens"
    )
    input_audio = require_nonnegative_integer(
        input_details.get("audio_tokens"), "input audio tokens"
    )
    output_text = require_nonnegative_integer(
        output_details.get("text_tokens"), "output text tokens"
    )
    output_audio = require_nonnegative_integer(
        output_details.get("audio_tokens"), "output audio tokens"
    )
    if input_text + input_audio != input_tokens:
        raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "input usage total mismatch")
    if output_text + output_audio != output_tokens:
        raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "output usage total mismatch")
    if input_tokens + output_tokens != total_tokens:
        raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "usage total mismatch")
    return ResponseUsage(input_text, input_audio, output_text, output_audio)


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "duplicate JSON key")
        value[key] = item
    return value
