"""Closed product-neutral service contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

MESSAGE_MAX_BYTES = 256 * 1024
FRAME_MAX_BYTES = 1024 * 1024


class ServiceErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    UNAUTHENTICATED = "unauthenticated"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    INTERNAL = "internal"


class ServiceError(RuntimeError):
    def __init__(self, code: ServiceErrorCode, message: str | None = None) -> None:
        self.code = ServiceErrorCode(code)
        super().__init__(message or self.code.value)


class CommandState(StrEnum):
    ACCEPTED = "accepted"
    CONTEXT_CALL_INTENT = "context_call_intent"
    CONTEXT_READY = "context_ready"
    APPLIED = "applied"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class OutputState(StrEnum):
    PENDING = "pending"
    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


def _text(value: str, name: str, *, max_bytes: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{name} is required")
    if len(value.encode()) > max_bytes:
        raise ValueError(f"{name} is too large")
    return value


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    serving: bool
    detail: str = "ready"


@dataclass(frozen=True, slots=True)
class StartRequest:
    external_session_id: str
    external_run_id: str
    external_command_id: str
    message: str
    profile_key: str = "agent.general"

    def __post_init__(self) -> None:
        for name in ("external_session_id", "external_run_id", "external_command_id"):
            _text(getattr(self, name), name)
        _text(self.message, "message", max_bytes=MESSAGE_MAX_BYTES)
        _text(self.profile_key, "profile_key")


@dataclass(frozen=True, slots=True)
class ContinueRequest:
    external_session_id: str
    external_run_id: str
    external_command_id: str
    external_continuation_id: str
    message: str

    def __post_init__(self) -> None:
        for name in (
            "external_session_id",
            "external_run_id",
            "external_command_id",
            "external_continuation_id",
        ):
            _text(getattr(self, name), name)
        _text(self.message, "message", max_bytes=MESSAGE_MAX_BYTES)


@dataclass(frozen=True, slots=True)
class GetRequest:
    external_command_id: str

    def __post_init__(self) -> None:
        _text(self.external_command_id, "external_command_id")


@dataclass(frozen=True, slots=True)
class CancelRequest:
    external_run_id: str
    external_command_id: str

    def __post_init__(self) -> None:
        _text(self.external_run_id, "external_run_id")
        _text(self.external_command_id, "external_command_id")


@dataclass(frozen=True, slots=True)
class CommandReceipt:
    command_id: str
    run_id: str
    accept_seq: int
    state: CommandState
    version: int


@dataclass(frozen=True, slots=True)
class CommandSnapshot:
    receipt: CommandReceipt
    output_state: OutputState
    output_text: str | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.output_state is OutputState.PRESENT and self.output_text is None:
            raise ValueError("present output requires text")
        if self.output_state is not OutputState.PRESENT and self.output_text is not None:
            raise ValueError("only present output may contain text")


JsonObject = dict[str, Any]

