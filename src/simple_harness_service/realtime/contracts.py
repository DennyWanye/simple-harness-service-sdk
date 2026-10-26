"""Provider-neutral contracts for full-duplex Realtime sessions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypeAlias

from .tool_schema import validate_tools


class RealtimeErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    UNAUTHENTICATED = "unauthenticated"
    FORBIDDEN = "forbidden"
    UNSUPPORTED = "unsupported"
    BUSY = "busy"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    PROTOCOL_ERROR = "protocol_error"
    SESSION_EXPIRED = "session_expired"
    BILLING_REJECTED = "billing_rejected"
    CANCELLED = "cancelled"
    INTERNAL = "internal"


_SAFE_FAILURE_FIELDS = frozenset(
    {
        "type",
        "event_id",
        "related_event_id",
        "response_id",
        "item_id",
        "call_id",
        "id",
        "output_index",
        "content_index",
        "session",
        "response",
        "item",
        "part",
        "content",
        "role",
        "status",
        "code",
        "retryable",
        "fatal",
        "message_class",
        "retry_after_ms",
        "transcript",
        "delta",
        "audio",
        "text",
        "instructions",
        "tools",
        "arguments",
        "name",
        "initiator",
        "disposition",
        "active_response",
        "settlement",
        "remaining_ms",
        "error",
        "control_version",
        "sdk_protocol_version",
        "relay_session_id",
        "provider",
        "wire_protocol",
        "wire_version",
        "capability",
        "capability_digest",
        "expires_at_ms",
        "reason",
        "usage",
    }
)
_FAILURE_REASONS = frozenset(
    {
        "validation_failed",
        "missing_identity",
        "invalid_integer",
        "invalid_object",
        "invalid_string",
        "invalid_identity",
        "invalid_range",
        "unknown_enum",
        "non_terminal_error",
        "field_set",
        "missing_field",
        "unresolved_item_missing_response_id",
        "suspected_assistant_missing_response_id",
    }
)
_STABLE_METADATA = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")


@dataclass(frozen=True, slots=True)
class RealtimeFailureContext:
    stage: str
    event_type: str
    generation: int
    field_names: tuple[str, ...] = ()
    validation_field: str | None = None
    validation_reason: str | None = None
    related_event_id: str | None = None
    client_event_type: str | None = None
    control_code: RealtimeErrorCode | None = None
    message_class: str | None = None
    retryable: bool | None = None
    fatal: bool | None = None
    retry_after_ms: int | None = None

    def __post_init__(self) -> None:
        for value in (
            self.stage,
            self.event_type,
            self.validation_reason,
            self.client_event_type,
            self.message_class,
        ):
            if value is not None and (
                not isinstance(value, str) or _STABLE_METADATA.fullmatch(value) is None
            ):
                raise ValueError("invalid failure metadata")
        if (
            not isinstance(self.generation, int)
            or isinstance(self.generation, bool)
            or self.generation < 1
        ):
            raise ValueError("invalid failure generation")
        if not isinstance(self.field_names, tuple) or any(
            v not in _SAFE_FAILURE_FIELDS for v in self.field_names
        ):
            raise ValueError("invalid failure field names")
        if (
            self.validation_field is not None
            and self.validation_field
            not in _SAFE_FAILURE_FIELDS
            | {"fields", "response.id", "item.id", "item.call_id", "session.id"}
        ):
            raise ValueError("invalid validation field")
        if self.related_event_id is not None and (
            not isinstance(self.related_event_id, str)
            or re.fullmatch(r"[!-~]{1,128}", self.related_event_id) is None
        ):
            raise ValueError("invalid related event identity")
        for flag in (self.retryable, self.fatal):
            if flag is not None and not isinstance(flag, bool):
                raise ValueError("invalid control boolean")
        if self.retry_after_ms is not None and (
            not isinstance(self.retry_after_ms, int)
            or isinstance(self.retry_after_ms, bool)
            or not 0 <= self.retry_after_ms <= 3_600_000
        ):
            raise ValueError("invalid retry interval")
        if self.control_code is not None:
            object.__setattr__(self, "control_code", RealtimeErrorCode(self.control_code))


class RealtimeError(RuntimeError):
    """Stable error raised before open, or for invalid product operations."""

    def __init__(
        self,
        code: RealtimeErrorCode,
        message: str | None = None,
        *,
        retryable: bool = False,
        validation_field: str | None = None,
        validation_reason: str | None = None,
        failure: RealtimeFailureContext | None = None,
    ) -> None:
        self.validation_field = validation_field
        self.validation_reason = validation_reason
        self.failure = failure
        self.code = RealtimeErrorCode(code)
        self.retryable = retryable
        super().__init__(message or self.code.value)


class RealtimeFeature(StrEnum):
    SERVER_TURN_DETECTION = "server_turn_detection"
    AUTOMATIC_RESPONSE = "automatic_response"
    INTERRUPTION = "interruption"
    INPUT_TRANSCRIPTION = "input_transcription"
    TEXT_OUTPUT = "text_output"
    AUDIO_OUTPUT = "audio_output"
    CANCEL_RESPONSE = "cancel_response"
    TRUNCATE_OUTPUT = "truncate_output"
    TOOL_CALLING = "tool_calling"
    RESUME = "resume"


class ResponseStatus(StrEnum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    INCOMPLETE = "incomplete"
    FAILED = "failed"


class CloseInitiator(StrEnum):
    CLIENT = "client"
    PROVIDER = "provider"
    RELAY = "relay"
    NETWORK = "network"
    TIMEOUT = "timeout"
    SHUTDOWN = "shutdown"


class CloseDisposition(StrEnum):
    CLEAN = "clean"
    RETRYABLE = "retryable"
    FATAL = "fatal"
    CLEAN_EXPIRED = "clean_expired"


class ToolCallState(StrEnum):
    REQUESTED = "requested"
    RESULT_SENT = "result_sent"
    RESULT_ACKED = "result_acked"
    FOLLOWUP_REQUESTED = "followup_requested"
    FOLLOWUP_STARTED = "followup_started"


def _required_text(value: str, name: str, *, max_bytes: int = 65_536) -> None:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} is required")
    if len(value.encode("utf-8")) > max_bytes:
        raise ValueError(f"{name} is too large")


@dataclass(frozen=True, slots=True)
class RealtimeAudioFormat:
    codec: str = "pcm_s16le"
    sample_rate: int = 16_000
    channels: int = 1

    def __post_init__(self) -> None:
        if self.codec != "pcm_s16le":
            raise ValueError("only raw pcm_s16le is supported")
        if self.sample_rate <= 0 or self.channels <= 0:
            raise ValueError("sample_rate and channels must be positive")


@dataclass(frozen=True, slots=True)
class RealtimeFeatureSet:
    server_turn_detection: bool = False
    automatic_response: bool = False
    interruption: bool = False
    input_transcription: bool = False
    text_output: bool = False
    audio_output: bool = False
    cancel_response: bool = False
    truncate_output: bool = False
    tool_calling: bool = False
    resume: bool = False

    def supports(self, required: frozenset[RealtimeFeature]) -> bool:
        return all(bool(getattr(self, feature.value)) for feature in required)

    def enabled(self) -> frozenset[RealtimeFeature]:
        return frozenset(feature for feature in RealtimeFeature if getattr(self, feature.value))


@dataclass(frozen=True, slots=True)
class RealtimeLimits:
    json_frame_bytes: int = 1_048_576
    input_audio_event_bytes: int = 65_536
    output_audio_event_bytes: int = 262_144
    input_queue_frames: int = 256
    input_queue_bytes: int = 4_194_304
    output_queue_frames: int = 512
    output_queue_bytes: int = 16_777_216
    tool_payload_bytes: int = 65_536

    def __post_init__(self) -> None:
        if any(
            value <= 0
            for value in (
                self.json_frame_bytes,
                self.input_audio_event_bytes,
                self.output_audio_event_bytes,
                self.input_queue_frames,
                self.input_queue_bytes,
                self.output_queue_frames,
                self.output_queue_bytes,
                self.tool_payload_bytes,
            )
        ):
            raise ValueError("Realtime limits must be positive")


@dataclass(frozen=True, slots=True)
class RealtimeCapability:
    control_version: str
    sdk_protocol_version: str
    provider: str
    wire_protocol: str
    wire_version: str
    input_audio: RealtimeAudioFormat
    output_audio: RealtimeAudioFormat
    features: RealtimeFeatureSet
    limits: RealtimeLimits = field(default_factory=RealtimeLimits)

    def __post_init__(self) -> None:
        for name in (
            "control_version",
            "sdk_protocol_version",
            "provider",
            "wire_protocol",
            "wire_version",
        ):
            _required_text(str(getattr(self, name)), name, max_bytes=256)


@dataclass(frozen=True, slots=True)
class RealtimeProfile:
    """Composition-time Provider selection; products never branch on its fields."""

    name: str
    provider: str
    wire_protocol: str
    wire_version: str
    public_model: str
    voice: str
    capability: RealtimeCapability

    def __post_init__(self) -> None:
        for name in ("name", "provider", "wire_protocol", "wire_version", "public_model", "voice"):
            _required_text(str(getattr(self, name)), name, max_bytes=256)


@dataclass(frozen=True, slots=True)
class RealtimeOpenRequest:
    external_session_id: str
    instructions: str
    required_features: frozenset[RealtimeFeature] = field(default_factory=frozenset)
    input_audio: RealtimeAudioFormat = field(default_factory=RealtimeAudioFormat)
    output_audio: RealtimeAudioFormat = field(
        default_factory=lambda: RealtimeAudioFormat(sample_rate=24_000)
    )
    tools: list[dict[str, object]] = field(default_factory=list)

    def __post_init__(self) -> None:
        _required_text(self.external_session_id, "external_session_id", max_bytes=512)
        _required_text(self.instructions, "instructions")
        validate_tools(self.tools)
        object.__setattr__(
            self,
            "required_features",
            frozenset(RealtimeFeature(feature) for feature in self.required_features),
        )


@dataclass(frozen=True, slots=True, repr=False)
class MintedRealtimeCredential:
    secret: str
    expires_at_ms: int
    websocket_path: str
    capability: RealtimeCapability
    capability_document: dict[str, object]
    capability_digest: str

    def __post_init__(self) -> None:
        _required_text(self.secret, "secret", max_bytes=4096)
        _required_text(self.websocket_path, "websocket_path", max_bytes=512)
        _required_text(self.capability_digest, "capability_digest", max_bytes=64)
        if self.expires_at_ms <= 0:
            raise ValueError("expires_at_ms must be positive")

    def __repr__(self) -> str:
        return (
            "MintedRealtimeCredential("
            "secret=<redacted>, "
            f"expires_at_ms={self.expires_at_ms!r}, "
            f"websocket_path={self.websocket_path!r}, "
            f"capability={self.capability!r}, "
            f"capability_document={self.capability_document!r}, "
            f"capability_digest={self.capability_digest!r})"
        )


@dataclass(frozen=True, slots=True)
class ResponseUsage:
    input_text_tokens: int
    input_audio_tokens: int
    output_text_tokens: int
    output_audio_tokens: int

    def __post_init__(self) -> None:
        if any(
            value < 0
            for value in (
                self.input_text_tokens,
                self.input_audio_tokens,
                self.output_text_tokens,
                self.output_audio_tokens,
            )
        ):
            raise ValueError("token counts must be non-negative")


@dataclass(frozen=True, slots=True)
class SessionReady:
    generation: int
    correlation: str
    input_audio: RealtimeAudioFormat
    output_audio: RealtimeAudioFormat
    tools_acknowledged: bool = False
    kind: str = field(default="SessionReady", init=False)


@dataclass(frozen=True, slots=True)
class SpeechStarted:
    turn_id: str
    kind: str = field(default="SpeechStarted", init=False)


@dataclass(frozen=True, slots=True)
class SpeechStopped:
    turn_id: str
    kind: str = field(default="SpeechStopped", init=False)


@dataclass(frozen=True, slots=True)
class TranscriptDelta:
    turn_id: str
    text: str
    kind: str = field(default="TranscriptDelta", init=False)


@dataclass(frozen=True, slots=True)
class TranscriptCompleted:
    turn_id: str
    text: str
    kind: str = field(default="TranscriptCompleted", init=False)


@dataclass(frozen=True, slots=True)
class ResponseStarted:
    turn_id: str
    response_id: str
    kind: str = field(default="ResponseStarted", init=False)


@dataclass(frozen=True, slots=True)
class OutputText:
    response_id: str
    item_id: str
    output_index: int
    content_index: int
    text: str
    is_delta: bool
    kind: str = field(default="OutputText", init=False)


@dataclass(frozen=True, slots=True)
class OutputAudioStarted:
    response_id: str
    item_id: str
    output_index: int
    content_index: int
    kind: str = field(default="OutputAudioStarted", init=False)


@dataclass(frozen=True, slots=True)
class OutputAudio:
    response_id: str
    item_id: str
    output_index: int
    content_index: int
    data: bytes
    kind: str = field(default="OutputAudio", init=False)


@dataclass(frozen=True, slots=True)
class OutputAudioCompleted:
    response_id: str
    item_id: str
    output_index: int
    content_index: int
    kind: str = field(default="OutputAudioCompleted", init=False)


@dataclass(frozen=True, slots=True)
class ToolCallRequested:
    response_id: str
    call_id: str
    name: str
    arguments_json: str
    generation: int = 0
    kind: str = field(default="ToolCallRequested", init=False)


@dataclass(frozen=True, slots=True)
class RealtimePlaybackStats:
    sent_audio_bytes: int
    sent_audio_ms: float
    played_audio_ms: int


@dataclass(frozen=True, slots=True)
class ResponseFinished:
    response_id: str
    status: ResponseStatus
    usage: ResponseUsage | None = None
    local: bool = False
    kind: str = field(default="ResponseFinished", init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", ResponseStatus(self.status))


@dataclass(frozen=True, slots=True)
class SessionExpiring:
    remaining_ms: int
    kind: str = field(default="SessionExpiring", init=False)


@dataclass(frozen=True, slots=True)
class SessionClosed:
    reason: str
    initiator: CloseInitiator
    disposition: CloseDisposition
    active_response: str = "none"
    settlement: str = "none"
    related_event_id: str | None = None
    kind: str = field(default="SessionClosed", init=False)
    control_code: RealtimeErrorCode | None = field(default=None, compare=False)
    message_class: str | None = field(default=None, compare=False)
    client_event_type: str | None = field(default=None, compare=False)
    failure: RealtimeFailureContext | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "initiator", CloseInitiator(self.initiator))
        object.__setattr__(self, "disposition", CloseDisposition(self.disposition))
        if self.active_response not in {"none", "completed", "cancelled"}:
            raise ValueError("invalid active response disposition")
        if self.settlement not in {"none", "settled", "released", "deferred"}:
            raise ValueError("invalid settlement disposition")


@dataclass(frozen=True, slots=True)
class SessionFailed:
    code: RealtimeErrorCode
    retryable: bool
    kind: str = field(default="SessionFailed", init=False)
    control_code: RealtimeErrorCode | None = field(default=None, compare=False)
    message_class: str | None = field(default=None, compare=False)
    related_event_id: str | None = field(default=None, compare=False)
    client_event_type: str | None = field(default=None, compare=False)
    fatal: bool | None = field(default=None, compare=False)
    retry_after_ms: int | None = field(default=None, compare=False)
    failure: RealtimeFailureContext | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", RealtimeErrorCode(self.code))


RealtimeEvent: TypeAlias = (
    SessionReady
    | SpeechStarted
    | SpeechStopped
    | TranscriptDelta
    | TranscriptCompleted
    | ResponseStarted
    | OutputText
    | OutputAudioStarted
    | OutputAudio
    | OutputAudioCompleted
    | ToolCallRequested
    | ResponseFinished
    | SessionExpiring
    | SessionClosed
    | SessionFailed
)
