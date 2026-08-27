"""Ports separating semantic Realtime behavior from credentials and transport."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Protocol

from .contracts import (
    MintedRealtimeCredential,
    RealtimeCapability,
    RealtimeEvent,
    RealtimeOpenRequest,
    RealtimeProfile,
)


@dataclass(frozen=True, slots=True)
class DecodedProviderEvent:
    event_id: str
    events: tuple[RealtimeEvent, ...] = ()
    provider_ready: bool = False
    session_acknowledged: bool = False
    introduced_response_id: str | None = None
    introduced_item: tuple[str, str] | None = None
    introduced_item_detail: tuple[str, str, int, str] | None = None
    introduced_content: tuple[str, str, int, int] | None = None
    completed_item: tuple[str, str] | None = None
    completed_content: tuple[str, str, int, int] | None = None
    tool_ack_call_id: str | None = None
    tool_ack_identity: tuple[str, str, str, str, str] | None = None
    tool_call_identity: tuple[str, str, int, str] | None = None


class CredentialMinter(Protocol):
    async def mint(
        self,
        profile: RealtimeProfile,
        request: RealtimeOpenRequest,
        correlation: str,
    ) -> MintedRealtimeCredential: ...


class RealtimeConnection(Protocol):
    async def send_text(self, payload: str) -> None: ...

    async def receive_text(self) -> str | None: ...

    async def close(self, code: int = 1000, reason: str = "") -> None: ...


class RealtimeTransport(Protocol):
    async def connect(self, websocket_path: str, bearer_token: str) -> RealtimeConnection: ...


class RealtimeProviderAdapter(Protocol):
    @property
    def capability(self) -> RealtimeCapability: ...

    def session_update(self, request: RealtimeOpenRequest) -> Mapping[str, object]: ...

    def audio_append(self, pcm: bytes) -> Mapping[str, object]: ...

    def cancel_response(self, response_id: str | None) -> Mapping[str, object]: ...

    def truncate_output(
        self,
        item_id: str,
        content_index: int,
        audio_end_ms: int,
    ) -> Mapping[str, object]: ...

    def tool_result(self, call_id: str, output: str) -> tuple[Mapping[str, object], str]: ...

    def followup_response(self, call_id: str) -> Mapping[str, object]: ...

    def encode_client_event(self, event: Mapping[str, object]) -> str: ...

    def decode_server_event(self, payload: str) -> DecodedProviderEvent: ...


class RealtimeSession(Protocol):
    async def __aenter__(self) -> RealtimeSession: ...

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None: ...

    async def send_audio(self, pcm: bytes) -> None: ...

    def events(self) -> AsyncIterator[RealtimeEvent]: ...

    async def cancel_response(self) -> None: ...

    async def truncate_output(
        self,
        item_id: str,
        content_index: int,
        audio_end_ms: int,
    ) -> None: ...

    async def submit_tool_result(self, call_id: str, output: str) -> None: ...

    async def close(self) -> None: ...
