"""Provider-neutral local JSON and 24-byte PCM framing."""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from .adapters._shared import strict_json_object
from .contracts import (
    OutputAudioCompleted,
    OutputAudioStarted,
    OutputText,
    RealtimeError,
    RealtimeErrorCode,
    RealtimeEvent,
    ResponseFinished,
    ResponseStarted,
    SessionExpiring,
    SpeechStarted,
    SpeechStopped,
    ToolCallRequested,
    TranscriptCompleted,
    TranscriptDelta,
)

LOCAL_PROTOCOL_VERSION = "2026-08-27.1"
LOCAL_WEBSOCKET_PATH = "/ws/realtime-voice"
PCM_MAGIC = b"SHRT"
PCM_VERSION = 1
PCM_HEADER = struct.Struct("!4sBBHIQI")


class AudioDirection(IntEnum):
    INPUT = 1
    OUTPUT = 2


@dataclass(frozen=True, slots=True)
class LocalPcmFrame:
    direction: AudioDirection
    generation: int
    sequence: int
    payload: bytes


_MESSAGE_FIELDS: dict[str, frozenset[str]] = {
    "local.auth": frozenset({"type", "version", "secret"}),
    "local.hello": frozenset({"type", "version", "generation", "correlation"}),
    "call.start": frozenset({"type", "generation", "instructions", "required_features"}),
    "call.ready": frozenset({"type", "generation", "correlation", "input_audio", "output_audio"}),
    "call.barge_in": frozenset({"type", "generation"}),
    "call.stop": frozenset({"type", "generation", "reason"}),
    "call.state": frozenset({"type", "generation", "state"}),
    "call.event": frozenset({"type", "generation", "event"}),
    "call.audio_ack": frozenset(
        {"type", "generation", "direction", "highest_contiguous_sequence"}
    ),
    "call.error": frozenset({"type", "generation", "code", "retryable"}),
    "call.closed": frozenset({"type", "generation", "reason"}),
}


def decode_local_message(payload: str) -> dict[str, Any]:
    value = strict_json_object(payload)
    message_type = value.get("type")
    if not isinstance(message_type, str) or message_type not in _MESSAGE_FIELDS:
        raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "unknown local message")
    if frozenset(value) != _MESSAGE_FIELDS[message_type]:
        raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "local fields do not match")
    generation = value.get("generation")
    if "generation" in value and (
        not isinstance(generation, int) or isinstance(generation, bool) or generation <= 0
    ):
        raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "invalid generation")
    if message_type.startswith("local.") and value.get("version") != LOCAL_PROTOCOL_VERSION:
        raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "local version mismatch")
    return value


def encode_local_message(value: dict[str, object]) -> str:
    message_type = value.get("type")
    if not isinstance(message_type, str) or message_type not in _MESSAGE_FIELDS:
        raise RealtimeError(RealtimeErrorCode.INVALID_REQUEST, "unknown local message")
    if frozenset(value) != _MESSAGE_FIELDS[message_type]:
        raise RealtimeError(RealtimeErrorCode.INVALID_REQUEST, "local fields do not match")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def encode_pcm_frame(frame: LocalPcmFrame) -> bytes:
    if frame.generation <= 0 or frame.sequence <= 0:
        raise RealtimeError(RealtimeErrorCode.INVALID_REQUEST, "invalid PCM identity")
    if len(frame.payload) == 0 or len(frame.payload) % 2:
        raise RealtimeError(RealtimeErrorCode.INVALID_REQUEST, "PCM must contain whole samples")
    limit = 65_536 if frame.direction is AudioDirection.INPUT else 262_144
    if len(frame.payload) > limit:
        raise RealtimeError(RealtimeErrorCode.INVALID_REQUEST, "PCM frame is too large")
    return PCM_HEADER.pack(
        PCM_MAGIC,
        PCM_VERSION,
        int(frame.direction),
        0,
        frame.generation,
        frame.sequence,
        len(frame.payload),
    ) + frame.payload


def decode_pcm_frame(payload: bytes) -> LocalPcmFrame:
    if len(payload) < PCM_HEADER.size:
        raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "PCM header is truncated")
    magic, version, raw_direction, flags, generation, sequence, size = PCM_HEADER.unpack(
        payload[: PCM_HEADER.size]
    )
    if magic != PCM_MAGIC or version != PCM_VERSION or flags != 0:
        raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "invalid PCM header")
    try:
        direction = AudioDirection(raw_direction)
    except ValueError as error:
        raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "invalid PCM direction") from error
    body = payload[PCM_HEADER.size :]
    if size != len(body):
        raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "PCM length mismatch")
    if generation <= 0 or sequence <= 0 or len(body) == 0 or len(body) % 2:
        raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "invalid PCM frame")
    limit = 65_536 if direction is AudioDirection.INPUT else 262_144
    if len(body) > limit:
        raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "PCM frame is too large")
    return LocalPcmFrame(direction, generation, sequence, body)


class SequenceWindow:
    """Tracks one generation/direction and rejects gaps beyond the authority window."""

    def __init__(self, generation: int, direction: AudioDirection, *, window: int = 64) -> None:
        self.generation = generation
        self.direction = direction
        self.window = window
        self.highest_contiguous = 0
        self._pending: set[int] = set()

    def accept(self, frame: LocalPcmFrame) -> bool:
        if frame.generation != self.generation:
            return False
        if frame.direction is not self.direction:
            raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "wrong PCM direction")
        if frame.sequence <= self.highest_contiguous or frame.sequence in self._pending:
            return False
        if frame.sequence > self.highest_contiguous + self.window:
            raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "PCM sequence gap")
        self._pending.add(frame.sequence)
        while self.highest_contiguous + 1 in self._pending:
            self._pending.remove(self.highest_contiguous + 1)
            self.highest_contiguous += 1
        return True


def encode_domain_event(generation: int, event: RealtimeEvent) -> str:
    if generation <= 0:
        raise RealtimeError(RealtimeErrorCode.INVALID_REQUEST, "invalid generation")
    event_value = _domain_event_value(event)
    return encode_local_message(
        {"type": "call.event", "generation": generation, "event": event_value}
    )


def _domain_event_value(event: RealtimeEvent) -> dict[str, object]:
    if isinstance(event, SpeechStarted | SpeechStopped):
        return {"kind": event.kind, "turn_id": event.turn_id}
    if isinstance(event, TranscriptDelta | TranscriptCompleted):
        return {"kind": event.kind, "turn_id": event.turn_id, "text": event.text}
    if isinstance(event, ResponseStarted):
        return {
            "kind": event.kind,
            "turn_id": event.turn_id,
            "response_id": event.response_id,
        }
    if isinstance(event, OutputText):
        return {
            "kind": event.kind,
            "response_id": event.response_id,
            "item_id": event.item_id,
            "output_index": event.output_index,
            "content_index": event.content_index,
            "text": event.text,
            "is_delta": event.is_delta,
        }
    if isinstance(event, OutputAudioStarted | OutputAudioCompleted):
        return {
            "kind": event.kind,
            "response_id": event.response_id,
            "item_id": event.item_id,
            "output_index": event.output_index,
            "content_index": event.content_index,
        }
    if isinstance(event, ToolCallRequested):
        return {
            "kind": event.kind,
            "response_id": event.response_id,
            "call_id": event.call_id,
            "name": event.name,
            "arguments_json": event.arguments_json,
        }
    if isinstance(event, ResponseFinished):
        return {
            "kind": event.kind,
            "response_id": event.response_id,
            "status": event.status.value,
            "local": event.local,
        }
    if isinstance(event, SessionExpiring):
        return {"kind": event.kind, "remaining_ms": event.remaining_ms}
    raise RealtimeError(
        RealtimeErrorCode.INVALID_REQUEST,
        "event is not a local call.event variant",
    )
