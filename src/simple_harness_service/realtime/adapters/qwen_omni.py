"""Semantic adapter for Qwen3.5 Omni Realtime native events."""

from __future__ import annotations

from collections.abc import Mapping

from ..contracts import (
    OutputAudio,
    OutputAudioCompleted,
    OutputAudioStarted,
    OutputText,
    RealtimeAudioFormat,
    RealtimeCapability,
    RealtimeError,
    RealtimeErrorCode,
    RealtimeEvent,
    RealtimeFeatureSet,
    RealtimeLimits,
    RealtimeOpenRequest,
    ResponseFinished,
    ResponseStarted,
    ResponseStatus,
    SessionFailed,
    SpeechStarted,
    SpeechStopped,
    ToolCallRequested,
    TranscriptCompleted,
    TranscriptDelta,
)
from ..ports import DecodedProviderEvent
from ._shared import decode_base64_pcm, parse_terminal_usage
from ._shared import new_event_id as _event_id
from ._shared import require_nonnegative_integer as _integer
from ._shared import require_object as _object
from ._shared import require_string as _string
from .qwen_wire import QwenWireCodec

QWEN_CAPABILITY = RealtimeCapability(
    control_version="2026-08-28.3",
    sdk_protocol_version="simple-harness-realtime/1",
    provider="qwen",
    wire_protocol="qwen-native",
    wire_version="2026-08-28.3",
    input_audio=RealtimeAudioFormat(sample_rate=16_000),
    output_audio=RealtimeAudioFormat(sample_rate=24_000),
    features=RealtimeFeatureSet(
        server_turn_detection=True,
        automatic_response=True,
        interruption=True,
        input_transcription=True,
        text_output=True,
        audio_output=True,
        cancel_response=True,
        truncate_output=False,
        tool_calling=True,
        resume=False,
    ),
    limits=RealtimeLimits(),
)


class QwenOmniAdapter:
    capability = QWEN_CAPABILITY

    def __init__(self, wire: QwenWireCodec | None = None) -> None:
        self._wire = wire or QwenWireCodec()

    def session_update(self, request: RealtimeOpenRequest) -> Mapping[str, object]:
        return {
            "event_id": _event_id(),
            "type": "session.update",
            "session": {
                "modalities": ["text", "audio"],
                "model": "qwen3.5-omni-flash-realtime-2026-03-15",
                "voice": "Tina",
                "instructions": request.instructions,
                "audio": {
                    "input": {"format": {"type": "pcm", "sample_rate": 16_000}},
                    "output": {"format": {"type": "pcm", "sample_rate": 24_000}},
                },
                "turn_detection": {
                    "type": "semantic_vad",
                    "threshold": 0.5,
                    "silence_duration_ms": 800,
                },
                "input_audio_transcription": {"model": "qwen3-asr-flash-realtime"},
            },
        }

    def audio_append(self, pcm: bytes) -> Mapping[str, object]:
        return self._wire.encode_audio(_event_id(), pcm)

    def cancel_response(self, response_id: str | None) -> Mapping[str, object]:
        del response_id
        return {"event_id": _event_id(), "type": "response.cancel"}

    def truncate_output(
        self,
        item_id: str,
        content_index: int,
        audio_end_ms: int,
    ) -> Mapping[str, object]:
        del item_id, content_index, audio_end_ms
        raise RealtimeError(RealtimeErrorCode.UNSUPPORTED, "Qwen does not support truncate")

    def tool_result(self, call_id: str, output: str) -> tuple[Mapping[str, object], str]:
        item_event_id = _event_id()
        return (
            {
                "event_id": item_event_id,
                "type": "conversation.item.create",
                "item": {"type": "function_call_output", "call_id": call_id, "output": output},
            },
            item_event_id,
        )

    def followup_response(self, call_id: str) -> Mapping[str, object]:
        del call_id
        return {"event_id": _event_id(), "type": "response.create"}

    def encode_client_event(self, event: Mapping[str, object]) -> str:
        return self._wire.encode_client_event(event)

    def decode_server_event(self, payload: str) -> DecodedProviderEvent:
        value = self._wire.decode_server_event(payload)
        event_type = _string(value.get("type"), "type")
        event_id = _string(value.get("event_id"), "event_id")
        if event_type == "session.updated":
            self._validate_session_ack(value)
            return DecodedProviderEvent(
                event_id,
                provider_ready=True,
                session_acknowledged=True,
            )
        if event_type == "input_audio_buffer.committed":
            return DecodedProviderEvent(event_id)
        if event_type == "input_audio_buffer.speech_started":
            turn_id = _string(value.get("item_id"), "item_id")
            return DecodedProviderEvent(event_id, (SpeechStarted(turn_id),))
        if event_type == "input_audio_buffer.speech_stopped":
            turn_id = _string(value.get("item_id"), "item_id")
            return DecodedProviderEvent(event_id, (SpeechStopped(turn_id),))
        if event_type == "conversation.item.input_audio_transcription.delta":
            turn_id = _string(value.get("item_id"), "item_id")
            return DecodedProviderEvent(
                event_id,
                (TranscriptDelta(turn_id, _string(value.get("text"), "text")),),
            )
        if event_type == "conversation.item.input_audio_transcription.completed":
            turn_id = _string(value.get("item_id"), "item_id")
            return DecodedProviderEvent(
                event_id,
                (TranscriptCompleted(turn_id, _string(value.get("transcript"), "transcript")),),
            )
        if event_type == "conversation.item.input_audio_transcription.failed":
            return DecodedProviderEvent(
                event_id,
                (SessionFailed(RealtimeErrorCode.UNAVAILABLE, True),),
            )
        if event_type == "response.created":
            response = _object(value.get("response"), "response")
            response_id = _string(response.get("id"), "response.id")
            return DecodedProviderEvent(
                event_id,
                (ResponseStarted(f"turn_{response_id}", response_id),),
                introduced_response_id=response_id,
            )
        if event_type in {"response.output_item.added", "response.output_item.done"}:
            response_id = _string(value.get("response_id"), "response_id")
            item = _object(value.get("item"), "item")
            item_id = _string(item.get("id"), "item.id")
            item_type = _string(item.get("type"), "item.type")
            output_index = _integer(value.get("output_index"), "output_index")
            if event_type.endswith("added"):
                return DecodedProviderEvent(
                    event_id,
                    introduced_item=(response_id, item_id),
                    introduced_item_detail=(
                        response_id,
                        item_id,
                        output_index,
                        item_type,
                    ),
                )
            return DecodedProviderEvent(event_id, completed_item=(response_id, item_id))
        if event_type in {"response.content_part.added", "response.content_part.done"}:
            identity = self._content_identity(value)
            part = _object(value.get("part"), "part")
            if event_type.endswith("added"):
                events = (OutputAudioStarted(*identity),) if part.get("type") == "audio" else ()
                return DecodedProviderEvent(
                    event_id, events, introduced_content=identity
                )
            return DecodedProviderEvent(event_id, completed_content=identity)
        if event_type in {"response.text.delta", "response.text.done"}:
            identity = self._content_identity(value)
            key = "delta" if event_type.endswith("delta") else "text"
            return DecodedProviderEvent(
                event_id,
                (
                    OutputText(
                        *identity,
                        _string(value.get(key), key),
                        event_type.endswith("delta"),
                    ),
                ),
            )
        if event_type == "response.audio.delta":
            identity = self._content_identity(value)
            audio = decode_base64_pcm(value.get("delta"), limit=262_144)
            return DecodedProviderEvent(event_id, (OutputAudio(*identity, audio),))
        if event_type == "response.audio.done":
            identity = self._content_identity(value)
            return DecodedProviderEvent(event_id, (OutputAudioCompleted(*identity),))
        if event_type in {"response.audio_transcript.delta", "response.audio_transcript.done"}:
            identity = self._content_identity(value)
            key = "delta" if event_type.endswith("delta") else "transcript"
            return DecodedProviderEvent(
                event_id,
                (
                    OutputText(
                        *identity,
                        _string(value.get(key), key),
                        event_type.endswith("delta"),
                    ),
                ),
            )
        if event_type == "response.function_call_arguments.done":
            response_id = _string(value.get("response_id"), "response_id")
            item_id = _string(value.get("item_id"), "item_id")
            output_index = _integer(value.get("output_index"), "output_index")
            call_id = _string(value.get("call_id"), "call_id")
            return DecodedProviderEvent(
                event_id,
                (
                    ToolCallRequested(
                        response_id,
                        call_id,
                        _string(value.get("name"), "name"),
                        _string(value.get("arguments"), "arguments"),
                    ),
                ),
                tool_call_identity=(response_id, item_id, output_index, call_id),
            )
        if event_type == "conversation.item.created":
            item = _object(value.get("item"), "item")
            raw_call_id = item.get("call_id")
            if item.get("type") == "function_call_output":
                ack_identity = (
                    _string(item.get("id"), "item.id"),
                    _string(item.get("type"), "item.type"),
                    _string(item.get("status"), "item.status"),
                    _string(raw_call_id, "item.call_id"),
                    _string(item.get("output"), "item.output"),
                )
            else:
                ack_identity = None
            return DecodedProviderEvent(
                event_id,
                tool_ack_call_id=(
                    ack_identity[3] if ack_identity is not None else None
                ),
                tool_ack_identity=ack_identity,
            )
        if event_type == "response.done":
            return self._response_done(event_id, value)
        if event_type == "error":
            error = _object(value.get("error"), "error")
            code, retryable = self._map_error(error)
            return DecodedProviderEvent(event_id, (SessionFailed(code, retryable),))
        return DecodedProviderEvent(event_id)

    @staticmethod
    def _content_identity(value: Mapping[str, object]) -> tuple[str, str, int, int]:
        return (
            _string(value.get("response_id"), "response_id"),
            _string(value.get("item_id"), "item_id"),
            _integer(value.get("output_index"), "output_index"),
            _integer(value.get("content_index"), "content_index"),
        )

    @staticmethod
    def _validate_session_ack(
        value: Mapping[str, object],
    ) -> None:
        if set(value) != {"event_id", "type", "session"}:
            raise RealtimeError(
                RealtimeErrorCode.PROTOCOL_ERROR,
                "Qwen session acknowledgement fields do not match",
            )
        session = _object(value.get("session"), "session")
        expected_keys = {
            "id",
            "modalities",
            "instructions",
            "voice",
            "audio",
            "input_audio_transcription",
            "turn_detection",
        }
        if set(session) != expected_keys:
            raise RealtimeError(
                RealtimeErrorCode.PROTOCOL_ERROR,
                "Qwen session fields do not match",
            )
        _string(session.get("id"), "session.id")
        _string(session.get("instructions"), "session.instructions")
        expected = {
            "modalities": ["text", "audio"],
            "voice": "Tina",
            "audio": {
                "input": {"format": {"type": "pcm", "sample_rate": 16_000}},
                "output": {"format": {"type": "pcm", "sample_rate": 24_000}},
            },
            "input_audio_transcription": {"model": "qwen3-asr-flash-realtime"},
        }
        if any(session.get(key) != expected_value for key, expected_value in expected.items()):
            raise RealtimeError(
                RealtimeErrorCode.PROTOCOL_ERROR,
                "Qwen session configuration mismatch",
            )
        turn_detection = _object(session.get("turn_detection"), "session.turn_detection")
        expected_turn_detection: dict[str, object] = {
            "type": "semantic_vad",
            "threshold": 0.5,
            "silence_duration_ms": 800,
            "create_response": True,
            "interrupt_response": True,
        }
        if turn_detection != expected_turn_detection:
            raise RealtimeError(
                RealtimeErrorCode.PROTOCOL_ERROR,
                "Qwen turn detection acknowledgement mismatch",
            )

    @staticmethod
    def _response_done(event_id: str, value: Mapping[str, object]) -> DecodedProviderEvent:
        response = _object(value.get("response"), "response")
        response_id = _string(response.get("id"), "response.id")
        provider_status = _string(response.get("status"), "response.status")
        if provider_status == "completed":
            status = ResponseStatus.COMPLETED
        elif provider_status == "failed":
            status = ResponseStatus.FAILED
        elif provider_status == "incomplete":
            details = response.get("status_details")
            cancelled = isinstance(details, dict) and details.get("reason") in {
                "client_cancelled",
                "turn_detected",
            }
            status = ResponseStatus.CANCELLED if cancelled else ResponseStatus.INCOMPLETE
        elif provider_status == "cancelled":
            status = ResponseStatus.CANCELLED
        else:
            raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "unknown response status")
        usage = parse_terminal_usage(
            response,
            input_details_field="input_tokens_details",
            output_details_field="output_tokens_details",
        )
        if status not in {ResponseStatus.FAILED, ResponseStatus.CANCELLED} and usage is None:
            raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "terminal usage is required")
        events: list[RealtimeEvent] = [ResponseFinished(response_id, status, usage)]
        if status is ResponseStatus.FAILED:
            events.append(SessionFailed(RealtimeErrorCode.PROTOCOL_ERROR, False))
        return DecodedProviderEvent(event_id, tuple(events))

    @staticmethod
    def _map_error(error: Mapping[str, object]) -> tuple[RealtimeErrorCode, bool]:
        code = error.get("code")
        error_type = error.get("type")
        if code in {"InvalidApiKey", "invalid_api_key"}:
            return RealtimeErrorCode.UNAUTHENTICATED, False
        if code in {
            "AccessDenied",
            "access_denied",
            "Endpoint.AccessDenied",
            "AllocationQuota.FreeTierOnly",
            "Arrearage",
            "CommodityNotPurchased",
            "PrepaidBillOverdue",
            "PostpaidBillOverdue",
        }:
            return RealtimeErrorCode.FORBIDDEN, False
        if code in {
            "Throttling",
            "Throttling.RateQuota",
            "LimitRequests",
            "limit_requests",
            "ResourceExhausted",
            "Throttling.BurstRate",
            "limit_burst_rate",
            "Throttling.AllocationQuota",
            "insufficient_quota",
            "Throttling.Concurrency",
        }:
            return RealtimeErrorCode.RATE_LIMITED, True
        if code == "ModelServingError":
            return RealtimeErrorCode.UNAVAILABLE, True
        if error_type == "invalid_request_error" and code in {
            "invalid_value",
            "missing_required_parameter",
            "invalid_event",
            "audio_format_invalid",
        }:
            return RealtimeErrorCode.INVALID_REQUEST, False
        return RealtimeErrorCode.PROTOCOL_ERROR, False


def encode_qwen_event(adapter: QwenOmniAdapter, event: Mapping[str, object]) -> str:
    """Typed helper used by the session without widening the adapter protocol."""

    return adapter.encode_client_event(event)
