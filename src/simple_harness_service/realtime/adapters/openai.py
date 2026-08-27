"""Offline-only semantic adapter for the frozen OpenAI Realtime fixtures."""

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
)
from ..ports import DecodedProviderEvent
from ._shared import decode_base64_pcm, parse_terminal_usage
from ._shared import new_event_id as _event_id
from ._shared import require_nonnegative_integer as _integer
from ._shared import require_object as _object
from ._shared import require_string as _string
from .openai_wire import OpenAIWireCodec

OPENAI_OFFLINE_CAPABILITY = RealtimeCapability(
    control_version="2026-08-27.1",
    sdk_protocol_version="simple-harness-realtime/1",
    provider="openai",
    wire_protocol="openai-native",
    wire_version="2026-08-27.1",
    input_audio=RealtimeAudioFormat(sample_rate=24_000),
    output_audio=RealtimeAudioFormat(sample_rate=24_000),
    features=RealtimeFeatureSet(
        server_turn_detection=True,
        automatic_response=True,
        interruption=True,
        input_transcription=True,
        text_output=True,
        audio_output=True,
        cancel_response=True,
        truncate_output=True,
        tool_calling=True,
        resume=False,
    ),
    limits=RealtimeLimits(),
)


class OpenAIRealtimeAdapter:
    """Fixture adapter. Live OpenAI execution is deliberately unavailable."""

    capability = OPENAI_OFFLINE_CAPABILITY
    execution_enabled = False

    def __init__(self, wire: OpenAIWireCodec | None = None, *, enable_live: bool = False) -> None:
        if enable_live:
            raise RealtimeError(
                RealtimeErrorCode.UNSUPPORTED,
                "live OpenAI Realtime is outside this release",
            )
        self._wire = wire or OpenAIWireCodec()

    def session_update(self, request: RealtimeOpenRequest) -> Mapping[str, object]:
        return {
            "event_id": _event_id(),
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": request.instructions,
                "output_modalities": ["audio"],
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": 24_000},
                        "transcription": {"model": "gpt-realtime-whisper"},
                        "turn_detection": {
                            "type": "semantic_vad",
                            "create_response": True,
                            "interrupt_response": True,
                        },
                    },
                    "output": {
                        "format": {"type": "audio/pcm", "rate": 24_000},
                        "voice": "marin",
                    },
                },
            },
        }

    def audio_append(self, pcm: bytes) -> Mapping[str, object]:
        return self._wire.encode_audio(_event_id(), pcm)

    def cancel_response(self, response_id: str | None) -> Mapping[str, object]:
        value: dict[str, object] = {"event_id": _event_id(), "type": "response.cancel"}
        if response_id is not None:
            value["response_id"] = response_id
        return value

    def truncate_output(
        self,
        item_id: str,
        content_index: int,
        audio_end_ms: int,
    ) -> Mapping[str, object]:
        if content_index < 0 or audio_end_ms < 0:
            raise RealtimeError(RealtimeErrorCode.INVALID_REQUEST)
        return {
            "event_id": _event_id(),
            "type": "conversation.item.truncate",
            "item_id": item_id,
            "content_index": content_index,
            "audio_end_ms": audio_end_ms,
        }

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
        if event_type in {"session.created", "session.updated"}:
            self._validate_session_ack(value, event_type)
            return DecodedProviderEvent(
                event_id,
                provider_ready=event_type == "session.updated",
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
        if event_type == "conversation.item.input_audio_transcription.completed":
            turn_id = _string(value.get("item_id"), "item_id")
            return DecodedProviderEvent(
                event_id,
                (TranscriptCompleted(turn_id, _string(value.get("transcript"), "transcript")),),
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
                domain_events = (
                    (OutputAudioStarted(*identity),) if part.get("type") == "audio" else ()
                )
                return DecodedProviderEvent(
                    event_id, domain_events, introduced_content=identity
                )
            return DecodedProviderEvent(event_id, completed_content=identity)
        if event_type in {
            "response.output_audio_transcript.delta",
            "response.output_audio_transcript.done",
        }:
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
        if event_type == "response.output_audio.delta":
            identity = self._content_identity(value)
            audio = decode_base64_pcm(value.get("delta"), limit=262_144)
            return DecodedProviderEvent(event_id, (OutputAudio(*identity, audio),))
        if event_type == "response.output_audio.done":
            identity = self._content_identity(value)
            return DecodedProviderEvent(event_id, (OutputAudioCompleted(*identity),))
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
            response = _object(value.get("response"), "response")
            response_id = _string(response.get("id"), "response.id")
            try:
                status = ResponseStatus(_string(response.get("status"), "response.status"))
            except ValueError as error:
                raise RealtimeError(
                    RealtimeErrorCode.PROTOCOL_ERROR, "unknown response status"
                ) from error
            usage = parse_terminal_usage(
                response,
                input_details_field="input_token_details",
                output_details_field="output_token_details",
            )
            if status is not ResponseStatus.FAILED and usage is None:
                raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "terminal usage is required")
            terminal_events: list[RealtimeEvent] = [
                ResponseFinished(response_id, status, usage)
            ]
            if status is ResponseStatus.FAILED:
                terminal_events.append(SessionFailed(RealtimeErrorCode.UNAVAILABLE, True))
            return DecodedProviderEvent(event_id, tuple(terminal_events))
        if event_type == "error":
            error_payload = _object(value.get("error"), "error")
            code, retryable = self._map_error(error_payload)
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
        event_type: str,
    ) -> None:
        if set(value) != {"event_id", "type", "session"}:
            raise RealtimeError(
                RealtimeErrorCode.PROTOCOL_ERROR,
                "OpenAI session acknowledgement fields do not match",
            )
        session = _object(value.get("session"), "session")
        if set(session) != {"id", "type", "model", "output_modalities", "audio"}:
            raise RealtimeError(
                RealtimeErrorCode.PROTOCOL_ERROR,
                "OpenAI session fields do not match",
            )
        _string(session.get("id"), "session.id")
        if (
            session.get("type") != "realtime"
            or session.get("model") != "gpt-realtime-2.1"
            or session.get("output_modalities") != ["audio"]
        ):
            raise RealtimeError(
                RealtimeErrorCode.PROTOCOL_ERROR,
                "OpenAI session configuration mismatch",
            )
        audio = _object(session.get("audio"), "session.audio")
        if set(audio) != {"input", "output"}:
            raise RealtimeError(
                RealtimeErrorCode.PROTOCOL_ERROR,
                "OpenAI audio acknowledgement fields do not match",
            )
        input_audio = _object(audio.get("input"), "session.audio.input")
        output_audio = _object(audio.get("output"), "session.audio.output")
        expected_input: dict[str, object] = {
            "format": {"type": "audio/pcm", "rate": 24_000}
        }
        if event_type == "session.updated":
            expected_input["turn_detection"] = {
                "type": "semantic_vad",
                "create_response": True,
                "interrupt_response": True,
            }
        if input_audio != expected_input or output_audio != {
            "format": {"type": "audio/pcm", "rate": 24_000},
            "voice": "marin",
        }:
            raise RealtimeError(
                RealtimeErrorCode.PROTOCOL_ERROR,
                "OpenAI audio acknowledgement mismatch",
            )

    @staticmethod
    def _map_error(error: Mapping[str, object]) -> tuple[RealtimeErrorCode, bool]:
        code = error.get("code")
        error_type = error.get("type")
        if code == "invalid_api_key":
            return RealtimeErrorCode.UNAUTHENTICATED, False
        if code == "permission_denied":
            return RealtimeErrorCode.FORBIDDEN, False
        if code in {"rate_limit_exceeded", "insufficient_quota"}:
            return RealtimeErrorCode.RATE_LIMITED, True
        if code == "server_error":
            return RealtimeErrorCode.UNAVAILABLE, True
        if code == "session_expired":
            return RealtimeErrorCode.SESSION_EXPIRED, True
        if error_type == "invalid_request_error" and code in {
            "invalid_value",
            "missing_required_parameter",
            "invalid_event",
        }:
            return RealtimeErrorCode.INVALID_REQUEST, False
        return RealtimeErrorCode.PROTOCOL_ERROR, False
