"""Strict codec for the frozen Qwen Omni Realtime native wire contract."""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping
from typing import Any

from ..contracts import RealtimeError, RealtimeErrorCode
from ._shared import decode_base64_pcm, strict_json_object

QWEN_CLIENT_EVENTS = frozenset(
    {
        "session.update",
        "input_audio_buffer.append",
        "input_audio_buffer.commit",
        "input_audio_buffer.clear",
        "response.create",
        "response.cancel",
        "conversation.item.create",
    }
)
QWEN_SERVER_EVENTS = frozenset(
    {
        "error",
        "session.updated",
        "input_audio_buffer.speech_started",
        "input_audio_buffer.speech_stopped",
        "input_audio_buffer.committed",
        "conversation.item.created",
        "conversation.item.input_audio_transcription.delta",
        "conversation.item.input_audio_transcription.completed",
        "conversation.item.input_audio_transcription.failed",
        "response.created",
        "response.done",
        "response.text.delta",
        "response.text.done",
        "response.audio.delta",
        "response.audio.done",
        "response.audio_transcript.delta",
        "response.audio_transcript.done",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
        "response.output_item.added",
        "response.output_item.done",
        "response.content_part.added",
        "response.content_part.done",
    }
)
_QWEN_EVENT_ID = re.compile(r"^event_[A-Za-z0-9_-]{1,120}$")


def _event_identity(value: Mapping[str, object]) -> tuple[str, str]:
    event_type = value.get("type")
    event_id = value.get("event_id")
    if not isinstance(event_type, str) or not isinstance(event_id, str) or not event_id:
        raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "event type/id is required")
    if not _QWEN_EVENT_ID.fullmatch(event_id):
        raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "invalid Qwen event_id")
    return event_type, event_id


class QwenWireCodec:
    input_audio_limit = 65_536
    output_audio_limit = 262_144

    def encode_client_event(self, value: Mapping[str, object]) -> str:
        event_type, _ = _event_identity(value)
        if event_type not in QWEN_CLIENT_EVENTS:
            raise RealtimeError(RealtimeErrorCode.INVALID_REQUEST, "unknown Qwen client event")
        if event_type == "input_audio_buffer.append":
            decode_base64_pcm(value.get("audio"), limit=self.input_audio_limit)
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def decode_server_event(self, payload: str) -> dict[str, Any]:
        if len(payload.encode("utf-8")) > 1_048_576:
            raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "Qwen frame is too large")
        value = strict_json_object(payload)
        event_type, _ = _event_identity(value)
        if event_type not in QWEN_SERVER_EVENTS:
            raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "unknown Qwen server event")
        if event_type == "response.audio.delta":
            decode_base64_pcm(value.get("delta"), limit=self.output_audio_limit)
        return value

    def encode_audio(self, event_id: str, pcm: bytes) -> dict[str, object]:
        if len(pcm) == 0 or len(pcm) % 2:
            raise RealtimeError(RealtimeErrorCode.INVALID_REQUEST, "PCM must contain whole samples")
        if len(pcm) > self.input_audio_limit:
            raise RealtimeError(RealtimeErrorCode.INVALID_REQUEST, "PCM event is too large")
        return {
            "event_id": event_id,
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(pcm).decode("ascii"),
        }
