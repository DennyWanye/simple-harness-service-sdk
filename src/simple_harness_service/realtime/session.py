"""Realtime session lifecycle, ordering, backpressure and terminal ownership."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import json
import re
import time
from collections.abc import AsyncIterator, Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Literal, cast

from .adapters._shared import strict_json_object
from .contracts import (
    _FAILURE_REASONS,
    _SAFE_FAILURE_FIELDS,
    CloseDisposition,
    CloseInitiator,
    MintedRealtimeCredential,
    OutputAudio,
    OutputAudioCompleted,
    OutputAudioStarted,
    OutputText,
    RealtimeCapability,
    RealtimeError,
    RealtimeErrorCode,
    RealtimeEvent,
    RealtimeFailureContext,
    RealtimeOpenRequest,
    ResponseFinished,
    ResponseStatus,
    SessionClosed,
    SessionFailed,
    SessionReady,
    ToolCallRequested,
    ToolCallState,
    TranscriptCompleted,
)
from .observability import (
    RealtimeDiagnostics,
    RealtimeDiagnosticSnapshot,
    RealtimeDiagnosticStage,
)
from .ports import DecodedProviderEvent, RealtimeConnection, RealtimeProviderAdapter, ToolDispatcher
from .recovery import RealtimeIdentityTable, RealtimeRecoveryState
from .relay_control import RelayControlCodec
from .tool_schema import matches_schema, parse_arguments, validate_tools

_DIAGNOSTIC_EVENT_KIND = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_SECRET_MARKER = re.compile(
    r"(?:sk-|api[_-]?key|authorization|bearer|cookie|password|token)", re.IGNORECASE
)


@dataclass(slots=True)
class _ToolRecord:
    state: ToolCallState
    output: str | None = None
    ack: asyncio.Event | None = None
    result_payload: str | None = None
    followup_payload: str | None = None
    attempt: asyncio.Task[None] | None = None
    lock: asyncio.Lock = dataclasses.field(default_factory=asyncio.Lock)


class ManagedRealtimeSession:
    """One SDK-owned media session with exactly one session terminal."""

    def __init__(
        self,
        *,
        connection: RealtimeConnection,
        adapter: RealtimeProviderAdapter,
        control: RelayControlCodec,
        credential: MintedRealtimeCredential,
        request: RealtimeOpenRequest,
        correlation: str,
        generation: int,
        diagnostics: RealtimeDiagnostics | None = None,
        write_timeout: float = 2.0,
        tool_ack_timeout: float = 5.0,
        close_timeout: float = 5.0,
        tool_dispatcher: ToolDispatcher | None = None,
        seen_tool_calls: set[str] | None = None,
        is_current_generation: Callable[[], bool] | None = None,
        tool_dispatch_timeout: float = 30.0,
        recovery_state: RealtimeRecoveryState | None = None,
        client_events: Mapping[str, str] | None = None,
    ) -> None:
        if write_timeout <= 0 or tool_ack_timeout <= 0 or close_timeout <= 0:
            raise ValueError("Realtime timeouts must be positive")
        self._connection = connection
        self._adapter = deepcopy(adapter)
        self._control = control
        self._credential = credential
        self._request = deepcopy(request)
        self._tool_dispatcher = tool_dispatcher
        self._recovery_state = recovery_state or RealtimeRecoveryState()
        self._identity_table = self._recovery_state.identity_table(request.external_session_id)
        generation = self._recovery_state.begin_session(request.external_session_id, generation)
        self._seen_tool_calls = self._recovery_state.tool_calls(request.external_session_id)
        if seen_tool_calls is not None:
            self._seen_tool_calls.update(seen_tool_calls)
        self._external_seen_tool_calls = seen_tool_calls
        self._completed_inputs = self._recovery_state.completed_inputs(request.external_session_id)
        current: Callable[[], bool] = is_current_generation or (lambda: True)
        self._is_current_generation: Callable[[], bool] = lambda: (
            current() and self._identity_table.generation == generation
        )
        binder = getattr(self._adapter, "bind_identity_table", None)
        if callable(binder):
            binder(self._identity_table)
        if tool_dispatch_timeout <= 0:
            raise ValueError("tool dispatch timeout must be positive")
        self._tool_dispatch_timeout = tool_dispatch_timeout
        self._tool_tasks: set[asyncio.Task[None]] = set()
        self._context_lock = asyncio.Lock()
        self._context_pending: dict[str, asyncio.Future[None]] = {}
        self._context_ack_id: str | None = None
        self.correlation = correlation
        self.generation = generation
        self._diagnostics = diagnostics or RealtimeDiagnostics()
        self._created_ns = time.monotonic_ns()
        self._controlled_close_started_ns: int | None = None
        self.capability: RealtimeCapability = credential.capability
        self._write_timeout = write_timeout
        self._tool_ack_timeout = tool_ack_timeout
        self._close_timeout = close_timeout
        self._events: asyncio.Queue[RealtimeEvent] = asyncio.Queue()
        self._queued_output_frames = 0
        self._queued_output_bytes = 0
        self._input_pending_frames = 0
        self._input_pending_bytes = 0
        self._input_frame_count = 0
        self._input_byte_count = 0
        self._output_frame_count = 0
        self._output_byte_count = 0
        self._input_lock = asyncio.Lock()
        self._terminal_lock = asyncio.Lock()
        self._terminal_owner: tuple[CloseInitiator, CloseDisposition] | None = None
        self._terminal_sequence_started = False
        self._terminal_published = asyncio.Event()
        self._shutdown_lock = asyncio.Lock()
        self._shutdown_complete = False
        self._close_start_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None
        self._close_reason: str | None = None
        self._close_ack: asyncio.Future[SessionClosed] | None = None
        self._pending_close_event_id: str | None = None
        self._closing = False
        self._receiver: asyncio.Task[None] | None = None
        self._ready = False
        self._seen_event_ids: set[str] = set()
        self._responses: set[str] = set()
        self._response_order: list[str] = []
        self._terminal_responses: set[str] = set()
        self._cancelled_responses: set[str] = set()
        self._items: set[tuple[str, str]] = set()
        self._function_call_items: set[tuple[str, str, int]] = set()
        self._completed_items: set[tuple[str, str]] = set()
        self._contents: set[tuple[str, str, int, int]] = set()
        self._completed_contents: set[tuple[str, str, int, int]] = set()
        self._tools: dict[str, _ToolRecord] = {}
        self._followup_waiting: list[str] = []
        self.late_event_count = 0
        self.duplicate_event_count = 0
        self.close_ack_timeout_count = 0
        self.close_ack_mismatch_count = 0
        self._diagnostic_event_kinds: set[str] = set()
        self._terminal_error_code: RealtimeErrorCode | None = None
        self._failure_context: RealtimeFailureContext | None = None
        self._last_event_type = "session.start"
        self._last_field_names: tuple[str, ...] = ()
        self._sent_event_types: dict[str, str] = dict(client_events or {})
        self._first_user_transcript = False

    async def start(self) -> None:
        try:
            validate_tools(self._request.tools, limit=self.capability.limits.tool_payload_bytes)
        except (ValueError, TypeError) as error:
            raise RealtimeError(
                RealtimeErrorCode.INVALID_REQUEST, "invalid tool catalog"
            ) from error
        if self._request.tools and not self.capability.features.tool_calling:
            raise RealtimeError(RealtimeErrorCode.UNSUPPORTED)
        update = self._adapter.session_update(self._request)
        self._remember_client_event(update)
        self._diagnostics.emit(
            correlation=self.correlation,
            generation=self.generation,
            stage=RealtimeDiagnosticStage.RECOVERY,
            operation_kind="recovery.restored" if self.generation > 1 else "recovery.started",
        )
        await self._send_runtime_event(update)
        self._receiver = asyncio.create_task(self._receive_loop())

    async def __aenter__(self) -> ManagedRealtimeSession:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.close()

    def diagnostics_snapshot(self) -> RealtimeDiagnosticSnapshot:
        return self._diagnostics.snapshot()

    @property
    def is_current_generation(self) -> bool:
        return self._is_current_generation()

    @property
    def context_ack_id(self) -> str | None:
        return self._context_ack_id

    @property
    def identity_table(self) -> RealtimeIdentityTable:
        return self._identity_table

    async def send_audio(self, pcm: bytes) -> None:
        self._require_active()
        if not self._ready:
            raise RealtimeError(RealtimeErrorCode.BUSY, "session is not ready")
        if len(pcm) == 0 or len(pcm) % 2:
            raise RealtimeError(RealtimeErrorCode.INVALID_REQUEST, "PCM must contain whole samples")
        limits = self.capability.limits
        if len(pcm) > limits.input_audio_event_bytes:
            raise RealtimeError(RealtimeErrorCode.INVALID_REQUEST, "PCM event is too large")
        async with self._input_lock:
            if self._input_pending_frames + 1 > limits.input_queue_frames:
                raise RealtimeError(RealtimeErrorCode.BUSY, "input frame queue is full")
            if self._input_pending_bytes + len(pcm) > limits.input_queue_bytes:
                raise RealtimeError(RealtimeErrorCode.BUSY, "input byte queue is full")
            self._input_pending_frames += 1
            self._input_pending_bytes += len(pcm)
        try:
            event = self._adapter.audio_append(pcm)
            await self._send_runtime_event(event)
            self._input_frame_count += 1
            self._input_byte_count += len(pcm)
            if _sample_audio_diagnostic(self._input_frame_count):
                self._diagnostics.emit(
                    correlation=self.correlation,
                    stage=RealtimeDiagnosticStage.INPUT_AUDIO,
                    generation=self.generation,
                    frame_count=self._input_frame_count,
                    byte_count=self._input_byte_count,
                    duration_ms=_duration_ms(self._created_ns),
                )
        finally:
            async with self._input_lock:
                self._input_pending_frames -= 1
                self._input_pending_bytes -= len(pcm)

    async def cancel_response(self) -> None:
        self._require_active()
        if not self.capability.features.cancel_response:
            raise RealtimeError(RealtimeErrorCode.UNSUPPORTED)
        response_id = self._current_response()
        if response_id is None:
            return
        self._cancelled_responses.add(response_id)
        event = self._adapter.cancel_response(response_id)
        await self._send_runtime_event(event)

    async def truncate_output(
        self,
        item_id: str,
        content_index: int,
        audio_end_ms: int,
    ) -> None:
        self._require_active()
        if not self.capability.features.truncate_output:
            raise RealtimeError(RealtimeErrorCode.UNSUPPORTED)
        event = self._adapter.truncate_output(item_id, content_index, audio_end_ms)
        await self._send_runtime_event(event)

    async def submit_tool_result(self, call_id: str, output: str) -> None:
        self._require_active()
        if not self.capability.features.tool_calling:
            raise RealtimeError(RealtimeErrorCode.UNSUPPORTED)
        if not output or len(output.encode("utf-8")) > self.capability.limits.tool_payload_bytes:
            raise RealtimeError(RealtimeErrorCode.INVALID_REQUEST, "invalid tool output")
        record = self._tools.get(call_id)
        if record is None:
            raise RealtimeError(RealtimeErrorCode.INVALID_REQUEST, "unknown tool call")
        async with record.lock:
            self._require_active()
            if record.output is not None and record.output != output:
                raise RealtimeError(RealtimeErrorCode.INVALID_REQUEST, "tool output changed")
            if record.attempt is None or record.attempt.done():
                record.attempt = asyncio.create_task(
                    self._submit_tool_result_attempt(record, call_id, output)
                )
            attempt = record.attempt
        await asyncio.shield(attempt)

    async def append_message(
        self,
        text: str,
        *,
        role: Literal["user", "system"] = "user",
        trigger_response: bool,
        idempotency_key: str | None = None,
    ) -> None:
        self._validate_context(text)
        if role not in {"user", "system"}:
            raise RealtimeError(RealtimeErrorCode.INVALID_REQUEST, "invalid message role")
        async with self._context_lock:
            if idempotency_key is not None:
                digest = hashlib.sha256(
                    json.dumps([text, role, trigger_response]).encode()
                ).hexdigest()
                prior = self._recovery_state.claim_message(
                    self._request.external_session_id, idempotency_key, digest
                )
                if prior is not None:
                    self._context_ack_id = prior
                    self._diagnostics.emit(
                        correlation=self.correlation,
                        generation=self.generation,
                        stage=RealtimeDiagnosticStage.RECOVERY,
                        operation_kind="recovery.message_deduplicated",
                    )
                    return
            await self._inject_context(
                self._adapter.append_message(text, role=role),
                trigger_response=trigger_response,
                idempotency_key=idempotency_key,
            )

    async def update_instructions(self, text: str, *, trigger_response: bool) -> None:
        self._validate_context(text)
        async with self._context_lock:
            await self._inject_context(
                self._adapter.update_instructions(text), trigger_response=trigger_response
            )

    async def out_of_band_response(self, text: str) -> None:
        self._validate_context(text)
        async with self._context_lock:
            await self._inject_context(
                self._adapter.out_of_band_response(text), trigger_response=False
            )

    def _validate_context(self, text: str) -> None:
        self._require_active()
        if not self._ready:
            raise RealtimeError(RealtimeErrorCode.BUSY, "session is not ready")
        if (
            not isinstance(text, str)
            or not text.strip()
            or "\x00" in text
            or len(text.encode("utf-8")) > self.capability.limits.tool_payload_bytes
        ):
            raise RealtimeError(RealtimeErrorCode.INVALID_REQUEST, "invalid context text")

    async def _inject_context(
        self,
        event: Mapping[str, object],
        *,
        trigger_response: bool,
        idempotency_key: str | None = None,
    ) -> None:
        self._require_active()
        identity = cast(str, event["event_id"])
        acknowledgement: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._context_pending[identity] = acknowledgement
        try:
            await self._send_runtime_event(event)
            await asyncio.wait_for(acknowledgement, timeout=self._tool_ack_timeout)
            self._require_active()
            if idempotency_key is not None and self._context_ack_id is not None:
                self._recovery_state.confirm_message(
                    self._request.external_session_id, idempotency_key, self._context_ack_id
                )
            if trigger_response and event["type"] != "response.create":
                await self._send_runtime_event(self._adapter.followup_response(""))
        except TimeoutError as error:
            self._capture_failure("context.ack_timeout", event=event, related_event_id=identity)
            self._record_provider_diagnostic(
                RealtimeDiagnosticStage.PROVIDER_EVENT_APPLY_FAILED,
                operation_kind="context.ack_timeout",
                stable_code=RealtimeErrorCode.TIMEOUT,
            )
            await self._fail(RealtimeErrorCode.TIMEOUT, retryable=False)
            raise RealtimeError(
                RealtimeErrorCode.TIMEOUT,
                "context acknowledgement timed out",
                failure=self._failure_context,
            ) from error
        finally:
            self._context_pending.pop(identity, None)

    async def _dispatch_tool(self, call: ToolCallRequested, *, invalid: bool) -> None:
        output: str
        try:
            self._require_active()
            if invalid:
                output = '{"error":"invalid_arguments"}'
            else:
                assert self._tool_dispatcher is not None
                try:
                    output = await asyncio.wait_for(
                        self._tool_dispatcher.dispatch(call), timeout=self._tool_dispatch_timeout
                    )
                    if (
                        not isinstance(output, str)
                        or not output
                        or len(output.encode("utf-8")) > self.capability.limits.tool_payload_bytes
                    ):
                        output = '{"error":"invalid_tool_output"}'
                except TimeoutError:
                    output = '{"error":"tool_timeout"}'
                except Exception:
                    output = '{"error":"tool_failed"}'
            self._require_active()
            await self.submit_tool_result(call.call_id, output)
        except RealtimeError as error:
            self._record_provider_diagnostic(
                RealtimeDiagnosticStage.PROVIDER_EVENT_APPLY_FAILED,
                operation_kind="tool.dispatch",
                stable_code=error.code,
            )

    def _invalid_tool_arguments(self, call: ToolCallRequested) -> bool:
        if len(call.arguments_json.encode("utf-8")) > self.capability.limits.tool_payload_bytes:
            return True
        try:
            arguments = parse_arguments(call.arguments_json)
            schema = next(
                (tool["parameters"] for tool in self._request.tools if tool["name"] == call.name),
                None,
            )
            return schema is None or not matches_schema(
                arguments, cast(Mapping[str, object], schema)
            )
        except (ValueError, TypeError, RecursionError, OverflowError):
            return True

    async def _submit_tool_result_attempt(
        self,
        record: _ToolRecord,
        call_id: str,
        output: str,
    ) -> None:
        if record.state is ToolCallState.REQUESTED:
            event, _item_identity = self._adapter.tool_result(call_id, output)
            record.output = output
            record.ack = asyncio.Event()
            self._remember_client_event(event)
            record.result_payload = self._adapter.encode_client_event(event)
            record.state = ToolCallState.RESULT_SENT
        if record.state is ToolCallState.RESULT_SENT:
            assert record.result_payload is not None
            try:
                await asyncio.wait_for(
                    self._connection.send_text(record.result_payload),
                    timeout=self._write_timeout,
                )
            except TimeoutError as error:
                failure = self._record_attempt_failure(
                    record.result_payload, RealtimeErrorCode.TIMEOUT
                )
                raise RealtimeError(
                    RealtimeErrorCode.TIMEOUT,
                    "tool result send is ambiguous; retry the same output",
                    retryable=True,
                    failure=failure,
                ) from error
            except Exception as error:
                failure = self._record_attempt_failure(
                    record.result_payload, RealtimeErrorCode.UNAVAILABLE
                )
                raise RealtimeError(
                    RealtimeErrorCode.UNAVAILABLE,
                    "tool result send is ambiguous; retry the same output",
                    retryable=True,
                    failure=failure,
                ) from error
            assert record.ack is not None
            try:
                await asyncio.wait_for(record.ack.wait(), timeout=self._tool_ack_timeout)
            except TimeoutError as error:
                result_event = json.loads(record.result_payload)
                self._capture_failure(
                    "tool.ack_timeout",
                    event=result_event,
                    related_event_id=result_event.get("event_id"),
                )
                self._record_provider_diagnostic(
                    RealtimeDiagnosticStage.PROVIDER_EVENT_APPLY_FAILED,
                    operation_kind="tool.ack_timeout",
                    stable_code=RealtimeErrorCode.PROTOCOL_ERROR,
                )
                await self._fail(RealtimeErrorCode.PROTOCOL_ERROR, retryable=False)
                raise RealtimeError(
                    RealtimeErrorCode.PROTOCOL_ERROR,
                    "ambiguous tool result acknowledgement",
                    failure=self._failure_context,
                ) from error
        if record.state is ToolCallState.RESULT_ACKED:
            followup = self._adapter.followup_response(call_id)
            self._remember_client_event(followup)
            record.followup_payload = self._adapter.encode_client_event(followup)
            record.state = ToolCallState.FOLLOWUP_REQUESTED
            if call_id not in self._followup_waiting:
                self._followup_waiting.append(call_id)
        if record.state is ToolCallState.FOLLOWUP_REQUESTED:
            assert record.followup_payload is not None
            try:
                await asyncio.wait_for(
                    self._connection.send_text(record.followup_payload),
                    timeout=self._write_timeout,
                )
            except TimeoutError as error:
                failure = self._record_attempt_failure(
                    record.followup_payload, RealtimeErrorCode.TIMEOUT
                )
                raise RealtimeError(
                    RealtimeErrorCode.TIMEOUT,
                    "followup response send is ambiguous; retry the same output",
                    retryable=True,
                    failure=failure,
                ) from error
            except Exception as error:
                failure = self._record_attempt_failure(
                    record.followup_payload, RealtimeErrorCode.UNAVAILABLE
                )
                raise RealtimeError(
                    RealtimeErrorCode.UNAVAILABLE,
                    "followup response send is ambiguous; retry the same output",
                    retryable=True,
                    failure=failure,
                ) from error

    async def _send_runtime_event(self, event: Mapping[str, object]) -> None:
        self._remember_client_event(event)
        try:
            await asyncio.wait_for(
                self._connection.send_text(self._adapter.encode_client_event(event)),
                timeout=self._write_timeout,
            )
        except TimeoutError as error:
            self._capture_failure("provider.send", event=event)
            self._record_provider_diagnostic(
                RealtimeDiagnosticStage.PROVIDER_SEND_FAILED,
                operation_kind="transport.send.timeout",
                stable_code=RealtimeErrorCode.TIMEOUT,
            )
            await self._fail(RealtimeErrorCode.TIMEOUT, retryable=True)
            raise RealtimeError(
                RealtimeErrorCode.TIMEOUT,
                "provider write timed out",
                retryable=True,
                failure=self._failure_context,
            ) from error
        except RealtimeError as error:
            self._capture_failure("provider.send", event=event, error=error)
            self._record_provider_diagnostic(
                RealtimeDiagnosticStage.PROVIDER_SEND_FAILED,
                operation_kind=_transport_failure_kind(error),
                stable_code=error.code,
            )
            await self._fail(error.code, retryable=error.retryable)
            error.failure = self._failure_context
            raise
        except Exception as error:
            self._capture_failure("provider.send", event=event)
            self._record_provider_diagnostic(
                RealtimeDiagnosticStage.PROVIDER_SEND_FAILED,
                operation_kind=_transport_failure_kind(error),
                stable_code=RealtimeErrorCode.UNAVAILABLE,
            )
            await self._fail(RealtimeErrorCode.UNAVAILABLE, retryable=True)
            raise RealtimeError(
                RealtimeErrorCode.UNAVAILABLE,
                "provider write failed",
                retryable=True,
                failure=self._failure_context,
            ) from error

    async def close(self, *, reason: str = "client_hangup") -> None:
        async with self._close_start_lock:
            if self._close_task is None:
                self._close_reason = reason
                self._close_task = asyncio.create_task(self._controlled_close(reason))
            elif reason != self._close_reason:
                raise RealtimeError(
                    RealtimeErrorCode.INVALID_REQUEST,
                    "close reason cannot change",
                )
            close_task = self._close_task
        await asyncio.shield(close_task)

    async def _controlled_close(self, reason: str) -> None:
        if not await self._claim_clean_close_intent():
            return
        self._controlled_close_started_ns = time.monotonic_ns()
        self._diagnostics.emit(
            correlation=self.correlation,
            stage=RealtimeDiagnosticStage.CONTROLLED_CLOSE_STARTED,
            generation=self.generation,
        )
        close_payload, close_event_id = self._control._build_bound_session_close(reason)
        self._remember_client_event(json.loads(close_payload))
        self._pending_close_event_id = close_event_id
        self._close_ack = asyncio.get_running_loop().create_future()
        acknowledgement: SessionClosed | None = None
        try:
            await asyncio.wait_for(
                self._connection.send_text(close_payload),
                timeout=self._close_timeout,
            )
        except TimeoutError:
            self._capture_failure(
                "provider.send", event=json.loads(close_payload), related_event_id=close_event_id
            )
            self._record_close_ack_timeout()
        except Exception:
            self._capture_failure(
                "provider.send", event=json.loads(close_payload), related_event_id=close_event_id
            )
            self._record_close_ack_timeout()
        else:
            try:
                acknowledgement = await asyncio.wait_for(
                    asyncio.shield(self._close_ack),
                    timeout=self._close_timeout,
                )
            except TimeoutError:
                self._record_close_ack_timeout()
        if self._close_ack is not None and not self._close_ack.done():
            self._close_ack.cancel()
        response_id = self._current_response()
        if response_id is not None and response_id not in self._terminal_responses:
            await self._emit_terminal_safe(
                ResponseFinished(response_id, ResponseStatus.CANCELLED, None, local=True)
            )
            self._terminal_responses.add(response_id)
        terminal = (
            dataclasses.replace(acknowledgement, reason=reason)
            if acknowledgement is not None
            else SessionClosed(reason, CloseInitiator.CLIENT, CloseDisposition.CLEAN)
        )
        terminal = dataclasses.replace(terminal, failure=self._failure_context)
        await self._emit_terminal_safe(terminal)
        self._record_terminal_diagnostic(terminal.disposition)
        self._diagnostics.emit(
            correlation=self.correlation,
            stage=RealtimeDiagnosticStage.CONTROLLED_CLOSE_COMPLETED,
            close_class=terminal.disposition,
            generation=self.generation,
            duration_ms=_duration_ms(self._controlled_close_started_ns),
        )
        self._terminal_published.set()
        await self._shutdown_connection()

    @property
    def close_diagnostics(self) -> dict[str, int]:
        """Content-free counters for controlled-close observability."""

        return {
            "close_ack_timeout": self.close_ack_timeout_count,
            "close_ack_mismatch": self.close_ack_mismatch_count,
        }

    def _record_close_ack_timeout(self) -> None:
        self._capture_failure(
            "close.ack_timeout",
            event={"type": "tokenseller.session.close"},
            related_event_id=self._pending_close_event_id,
        )
        self.close_ack_timeout_count += 1
        started_ns = self._controlled_close_started_ns or self._created_ns
        self._diagnostics.emit(
            correlation=self.correlation,
            stage=RealtimeDiagnosticStage.CONTROLLED_CLOSE_TIMEOUT,
            stable_code=RealtimeErrorCode.TIMEOUT,
            generation=self.generation,
            frame_count=self.close_ack_timeout_count,
            duration_ms=_duration_ms(started_ns),
        )

    async def events(self) -> AsyncIterator[RealtimeEvent]:
        while not (self._terminal_published.is_set() and self._events.empty()):
            item = await self._events.get()
            self._queued_output_frames = max(0, self._queued_output_frames - 1)
            self._queued_output_bytes = max(0, self._queued_output_bytes - self._event_size(item))
            if not self._is_current_generation() and not isinstance(
                item, (SessionClosed, SessionFailed)
            ):
                self.late_event_count += 1
                continue
            yield item

    async def _receive_loop(self) -> None:
        try:
            while not self._terminal_published.is_set():
                operation_kind = "unknown_provider_event"
                fingerprint: str | None = None
                try:
                    try:
                        payload = await self._connection.receive_text()
                    except RealtimeError as error:
                        self._capture_failure(
                            "provider.receive", event={"type": "transport.receive"}, error=error
                        )
                        self._record_provider_diagnostic(
                            RealtimeDiagnosticStage.PROVIDER_RECEIVE_FAILED,
                            operation_kind=_transport_failure_kind(error),
                            stable_code=error.code,
                        )
                        raise
                    except Exception as error:
                        self._capture_failure(
                            "provider.receive", event={"type": "transport.receive"}
                        )
                        self._record_provider_diagnostic(
                            RealtimeDiagnosticStage.PROVIDER_RECEIVE_FAILED,
                            operation_kind=_transport_failure_kind(error),
                            stable_code=RealtimeErrorCode.INTERNAL,
                        )
                        raise
                    if payload is None:
                        if self._closing:
                            return
                        self._capture_failure("provider.receive", event={"type": "transport.eof"})
                        await self._fail(RealtimeErrorCode.UNAVAILABLE, retryable=True)
                        return
                    if not self._is_current_generation() and not self._closing:
                        self.late_event_count += 1
                        continue
                    self._last_event_type, self._last_field_names = _safe_event_fields(payload)
                    operation_kind, fingerprint = _provider_event_metadata(payload)
                    if operation_kind not in self._diagnostic_event_kinds:
                        self._diagnostic_event_kinds.add(operation_kind)
                        self._record_provider_diagnostic(
                            RealtimeDiagnosticStage.PROVIDER_EVENT_RECEIVED,
                            operation_kind=operation_kind,
                            fingerprint=fingerprint,
                        )
                    try:
                        control_event = self._control.decode_runtime_event(payload)
                    except RealtimeError as error:
                        related, client_type = self._known_client_reference(payload)
                        self._capture_failure(
                            "control.decode",
                            error=error,
                            related_event_id=related,
                            client_event_type=client_type,
                        )
                        self._record_provider_diagnostic(
                            RealtimeDiagnosticStage.CONTROL_EVENT_DECODE_FAILED,
                            operation_kind="control.validation_failed",
                            stable_code=error.code,
                        )
                        raise
                    if control_event is not None:
                        if self._closing:
                            if isinstance(control_event, SessionClosed):
                                valid_ack = (
                                    control_event.related_event_id == self._pending_close_event_id
                                    and control_event.initiator is CloseInitiator.CLIENT
                                    and control_event.disposition is CloseDisposition.CLEAN
                                )
                                if valid_ack:
                                    if self._close_ack is not None and not self._close_ack.done():
                                        self._close_ack.set_result(control_event)
                                else:
                                    self.close_ack_mismatch_count += 1
                                continue
                            self.late_event_count += 1
                            continue
                        if isinstance(control_event, SessionFailed):
                            related = control_event.related_event_id
                            client_type = (
                                self._sent_event_types.get(related) if related is not None else None
                            )
                            self._capture_failure(
                                "control.terminal",
                                related_event_id=related,
                                client_event_type=client_type,
                                control_code=control_event.control_code,
                                message_class=control_event.message_class,
                                retryable=control_event.retryable,
                                fatal=control_event.fatal,
                                retry_after_ms=control_event.retry_after_ms,
                            )
                            control_event = dataclasses.replace(
                                control_event, client_event_type=client_type
                            )
                            self._record_provider_diagnostic(
                                RealtimeDiagnosticStage.CONTROL_TERMINATED,
                                operation_kind="control.terminal",
                                stable_code=control_event.code,
                            )
                            await self._finish_terminal(
                                control_event,
                                CloseInitiator.RELAY,
                                CloseDisposition.RETRYABLE
                                if control_event.retryable
                                else CloseDisposition.FATAL,
                            )
                            return
                        if isinstance(control_event, SessionClosed):
                            client_type = self._sent_event_types.get(
                                control_event.related_event_id or ""
                            )
                            self._capture_failure(
                                "control.closed",
                                related_event_id=control_event.related_event_id,
                                client_event_type=client_type,
                            )
                            control_event = dataclasses.replace(
                                control_event, client_event_type=client_type
                            )
                            await self._finish_terminal(
                                control_event,
                                control_event.initiator,
                                control_event.disposition,
                            )
                            return
                        if not await self._emit(control_event):
                            await self._fail(RealtimeErrorCode.BUSY, retryable=False)
                            return
                        continue
                    if self._closing:
                        self.late_event_count += 1
                        continue
                    try:
                        decoded = self._adapter.decode_server_event(payload)
                    except RealtimeError as error:
                        self._capture_failure("adapter.decode", error=error)
                        self._record_provider_diagnostic(
                            RealtimeDiagnosticStage.PROVIDER_EVENT_DECODE_FAILED,
                            operation_kind=operation_kind,
                            fingerprint=fingerprint,
                            stable_code=error.code,
                        )
                        raise
                    except Exception:
                        self._capture_failure("adapter.decode")
                        self._record_provider_diagnostic(
                            RealtimeDiagnosticStage.PROVIDER_EVENT_DECODE_FAILED,
                            operation_kind=operation_kind,
                            fingerprint=fingerprint,
                            stable_code=RealtimeErrorCode.INTERNAL,
                        )
                        raise
                    if decoded.event_id in self._seen_event_ids:
                        self.duplicate_event_count += 1
                        continue
                    self._seen_event_ids.add(decoded.event_id)
                    if not await self._apply(decoded):
                        if self._terminal_error_code is not None:
                            self._record_provider_diagnostic(
                                RealtimeDiagnosticStage.PROVIDER_EVENT_APPLY_FAILED,
                                operation_kind=operation_kind,
                                fingerprint=fingerprint,
                                stable_code=self._terminal_error_code,
                            )
                        if self._closing and not self._terminal_published.is_set():
                            continue
                        return
                except RealtimeError as error:
                    if self._closing:
                        self.late_event_count += 1
                        continue
                    await self._fail(error.code, retryable=error.retryable)
                    return
                except Exception:
                    if self._closing:
                        self.late_event_count += 1
                        continue
                    await self._fail(RealtimeErrorCode.INTERNAL, retryable=False)
                    return
        except asyncio.CancelledError:
            raise

    async def _apply(self, decoded: DecodedProviderEvent) -> bool:
        if not self._is_current_generation():
            self.late_event_count += 1
            return True
        if decoded.item_role_metadata_mismatch is not None:
            item_id, declared_role = decoded.item_role_metadata_mismatch
            self._diagnostics.emit(
                correlation=self.correlation,
                generation=self.generation,
                stage=RealtimeDiagnosticStage.PROVIDER_EVENT_RECEIVED,
                operation_kind="item_role_metadata_mismatch",
                provider_event_type="conversation.item.created",
                item_id=item_id,
                declared_role=declared_role,
            )
        if decoded.user_side_response_event_ignored is not None:
            self._record_provider_diagnostic(
                RealtimeDiagnosticStage.PROVIDER_EVENT_RECEIVED,
                operation_kind="user_side_response_event_ignored",
                provider_event_type=decoded.user_side_response_event_ignored,
            )
        elif decoded.user_side_transcript_ignored:
            self._record_provider_diagnostic(
                RealtimeDiagnosticStage.PROVIDER_EVENT_RECEIVED,
                operation_kind="user_side_transcript_ignored",
            )
        if decoded.tool_catalog_normalized:
            self._record_provider_diagnostic(
                RealtimeDiagnosticStage.PROVIDER_EVENT_RECEIVED,
                operation_kind="session.tools.normalized",
            )
        if decoded.rejected_event_id in self._context_pending:
            assert decoded.rejected_event_id is not None
            pending = self._context_pending[decoded.rejected_event_id]
            if not pending.done():
                pending.set_exception(RealtimeError(RealtimeErrorCode.UNSUPPORTED))
            self._record_provider_diagnostic(
                RealtimeDiagnosticStage.PROVIDER_EVENT_APPLY_FAILED,
                operation_kind="context.unsupported",
                stable_code=RealtimeErrorCode.UNSUPPORTED,
            )
            return True
        context_request_id = decoded.context_request_id or decoded.context_ack_id
        if context_request_id in self._context_pending:
            assert context_request_id is not None
            pending = self._context_pending[context_request_id]
            if not pending.done():
                if (
                    decoded.context_ack_id is not None
                    and not decoded.session_acknowledged
                    and decoded.introduced_response_id is None
                ):
                    self._context_ack_id = decoded.context_ack_id
                pending.set_result(None)
                self._diagnostics.emit(
                    correlation=self.correlation,
                    generation=self.generation,
                    stage=RealtimeDiagnosticStage.RECOVERY,
                    operation_kind="recovery.context_acknowledged",
                )
        terminal_batch = any(isinstance(event, SessionFailed) for event in decoded.events)
        if decoded.provider_ready and not self._ready:
            self._ready = True
            if not await self._emit(
                SessionReady(
                    self.generation,
                    self.correlation,
                    self.capability.input_audio,
                    self.capability.output_audio,
                    tools_acknowledged=decoded.tools_acknowledged,
                )
            ):
                await self._fail(RealtimeErrorCode.BUSY, retryable=False)
                return False
            self._diagnostics.emit(
                correlation=self.correlation,
                stage=RealtimeDiagnosticStage.SESSION_READY,
                generation=self.generation,
                duration_ms=_duration_ms(self._created_ns),
            )
        elif not self._ready and not decoded.session_acknowledged:
            await self._fail(RealtimeErrorCode.PROTOCOL_ERROR, retryable=False)
            return False
        if decoded.session_acknowledged:
            return True
        if decoded.introduced_response_id is not None:
            response_id = decoded.introduced_response_id
            if response_id in self._responses or response_id in self._terminal_responses:
                await self._fail(RealtimeErrorCode.PROTOCOL_ERROR, retryable=False)
                return False
            live_responses = self._responses - self._terminal_responses
            if len(live_responses) >= 2 or (
                live_responses and not live_responses.issubset(self._cancelled_responses)
            ):
                await self._fail(RealtimeErrorCode.PROTOCOL_ERROR, retryable=False)
                return False
            self._responses.add(response_id)
            self._response_order.append(response_id)
            if self._followup_waiting:
                call_id = self._followup_waiting.pop(0)
                self._tools[call_id].state = ToolCallState.FOLLOWUP_STARTED
        if decoded.introduced_item is not None:
            if decoded.introduced_item[0] not in self._responses:
                await self._fail(RealtimeErrorCode.PROTOCOL_ERROR, retryable=False)
                return False
            if decoded.introduced_item in self._items:
                await self._fail(RealtimeErrorCode.PROTOCOL_ERROR, retryable=False)
                return False
            self._items.add(decoded.introduced_item)
            if decoded.introduced_item_detail is None:
                await self._fail(RealtimeErrorCode.PROTOCOL_ERROR, retryable=False)
                return False
            response_id, item_id, output_index, item_type = decoded.introduced_item_detail
            if (response_id, item_id) != decoded.introduced_item:
                await self._fail(RealtimeErrorCode.PROTOCOL_ERROR, retryable=False)
                return False
            if item_type == "function_call":
                self._function_call_items.add((response_id, item_id, output_index))
        if decoded.introduced_content is not None:
            response_id, item_id, _output_index, _content_index = decoded.introduced_content
            if (response_id, item_id) not in self._items:
                await self._fail(RealtimeErrorCode.PROTOCOL_ERROR, retryable=False)
                return False
            if decoded.introduced_content in self._contents:
                await self._fail(RealtimeErrorCode.PROTOCOL_ERROR, retryable=False)
                return False
            self._contents.add(decoded.introduced_content)
        if decoded.tool_ack_identity is not None:
            _item_id, item_type, status, call_id, output = decoded.tool_ack_identity
            record = self._tools.get(call_id)
            if (
                record is None
                or record.output != output
                or item_type != "function_call_output"
                or status != "completed"
            ):
                await self._fail(RealtimeErrorCode.PROTOCOL_ERROR, retryable=False)
                return False
            if record.state is ToolCallState.RESULT_SENT:
                record.state = ToolCallState.RESULT_ACKED
                assert record.ack is not None
                record.ack.set()
            else:
                self.duplicate_event_count += 1
        if decoded.completed_content is not None and (
            decoded.completed_content not in self._contents
            or decoded.completed_content in self._completed_contents
        ):
            await self._fail(RealtimeErrorCode.PROTOCOL_ERROR, retryable=False)
            return False
        if decoded.completed_item is not None and (
            decoded.completed_item not in self._items
            or decoded.completed_item in self._completed_items
        ):
            await self._fail(RealtimeErrorCode.PROTOCOL_ERROR, retryable=False)
            return False
        for event in decoded.events:
            if isinstance(event, TranscriptCompleted):
                user_identity = self.identity_table.lookup(event.turn_id)
                if user_identity is not None and user_identity.source in {
                    "speech_started",
                    "committed",
                    "transcript",
                }:
                    if event.turn_id in self._completed_inputs:
                        self.duplicate_event_count += 1
                        continue
                    self._completed_inputs.add(event.turn_id)
                if (
                    not self._first_user_transcript
                    and user_identity is not None
                    and user_identity.source
                    in {"speech_started", "committed", "transcript", "input_audio_item"}
                    and user_identity.generation == self.generation
                ):
                    self._first_user_transcript = True
                    self._diagnostics.emit(
                        correlation=self.correlation,
                        generation=self.generation,
                        stage=RealtimeDiagnosticStage.RECOVERY,
                        operation_kind="recovery.first_user_transcript",
                    )
            if isinstance(event, ToolCallRequested):
                identity = decoded.tool_call_identity
                if identity is None:
                    await self._fail(RealtimeErrorCode.PROTOCOL_ERROR, retryable=False)
                    return False
                response_id, item_id, output_index, call_id = identity
                if (
                    response_id != event.response_id
                    or call_id != event.call_id
                    or response_id not in self._responses
                    or response_id in self._terminal_responses
                    or (response_id, item_id, output_index) not in self._function_call_items
                    or (event.generation not in {0, self.generation})
                ):
                    await self._fail(RealtimeErrorCode.PROTOCOL_ERROR, retryable=False)
                    return False
                if event.call_id in self._tools:
                    await self._fail(RealtimeErrorCode.PROTOCOL_ERROR, retryable=False)
                    return False
                if event.call_id in self._seen_tool_calls:
                    self.duplicate_event_count += 1
                    continue
                self._tools[event.call_id] = _ToolRecord(ToolCallState.REQUESTED)
                self._seen_tool_calls.add(event.call_id)
                if self._external_seen_tool_calls is not None:
                    self._external_seen_tool_calls.add(event.call_id)
                event = dataclasses.replace(event, generation=self.generation)
                invalid_arguments = self._invalid_tool_arguments(event)
                if self._tool_dispatcher is not None or invalid_arguments:
                    task = asyncio.create_task(
                        self._dispatch_tool(event, invalid=invalid_arguments)
                    )
                    self._tool_tasks.add(task)
                    task.add_done_callback(self._tool_tasks.discard)
                if invalid_arguments:
                    continue
            if isinstance(
                event,
                (OutputText, OutputAudioStarted, OutputAudio, OutputAudioCompleted),
            ):
                output_identity = (
                    event.response_id,
                    event.item_id,
                    event.output_index,
                    event.content_index,
                )
                if (
                    event.response_id in self._terminal_responses
                    or event.response_id in self._cancelled_responses
                ):
                    self.late_event_count += 1
                    continue
                if event.response_id not in self._responses:
                    await self._fail(RealtimeErrorCode.PROTOCOL_ERROR, retryable=False)
                    return False
                if output_identity not in self._contents:
                    await self._fail(RealtimeErrorCode.PROTOCOL_ERROR, retryable=False)
                    return False
                if output_identity in self._completed_contents:
                    self.late_event_count += 1
                    continue
            if isinstance(event, ResponseFinished):
                if (
                    event.status is ResponseStatus.INCOMPLETE
                    and event.response_id in self._cancelled_responses
                ):
                    event = dataclasses.replace(event, status=ResponseStatus.CANCELLED)
                if event.response_id not in self._responses:
                    await self._fail(RealtimeErrorCode.PROTOCOL_ERROR, retryable=False)
                    return False
                if event.response_id in self._terminal_responses:
                    self.duplicate_event_count += 1
                    continue
                self._terminal_responses.add(event.response_id)
            if isinstance(event, SessionFailed):
                await self._finish_terminal(
                    event,
                    CloseInitiator.PROVIDER,
                    CloseDisposition.RETRYABLE if event.retryable else CloseDisposition.FATAL,
                )
                return False
            if isinstance(event, ResponseFinished) and terminal_batch:
                await self._emit_terminal_safe(event)
                continue
            if not await self._emit(event):
                await self._fail(RealtimeErrorCode.BUSY, retryable=False)
                return False
            if isinstance(event, OutputAudio):
                self._output_frame_count += 1
                self._output_byte_count += len(event.data)
                if _sample_audio_diagnostic(self._output_frame_count):
                    self._diagnostics.emit(
                        correlation=self.correlation,
                        stage=RealtimeDiagnosticStage.OUTPUT_AUDIO,
                        generation=self.generation,
                        frame_count=self._output_frame_count,
                        byte_count=self._output_byte_count,
                        duration_ms=_duration_ms(self._created_ns),
                    )
        if decoded.completed_content is not None:
            self._completed_contents.add(decoded.completed_content)
        if decoded.completed_item is not None:
            self._completed_items.add(decoded.completed_item)
        return True

    async def _emit(self, event: RealtimeEvent) -> bool:
        size = self._event_size(event)
        if self._queued_output_frames + 1 > self.capability.limits.output_queue_frames:
            return False
        if self._queued_output_bytes + size > self.capability.limits.output_queue_bytes:
            return False
        self._events.put_nowait(event)
        self._queued_output_frames += 1
        self._queued_output_bytes += size
        return True

    async def _emit_terminal_safe(self, event: RealtimeEvent) -> None:
        if not self._terminal_sequence_started:
            self._terminal_sequence_started = True
            if await self._emit(event):
                return
            while not self._events.empty():
                self._events.get_nowait()
            self._queued_output_frames = 0
            self._queued_output_bytes = 0
        self._events.put_nowait(event)
        self._queued_output_frames += 1
        self._queued_output_bytes += self._event_size(event)

    async def _fail(self, code: RealtimeErrorCode, *, retryable: bool) -> None:
        await self._finish_terminal(
            SessionFailed(code, retryable),
            CloseInitiator.PROVIDER,
            CloseDisposition.RETRYABLE if retryable else CloseDisposition.FATAL,
        )

    async def _finish_terminal(
        self,
        event: SessionClosed | SessionFailed,
        initiator: CloseInitiator,
        disposition: CloseDisposition,
    ) -> None:
        if not await self._claim_terminal(initiator, disposition):
            return
        self._terminal_error_code = event.code if isinstance(event, SessionFailed) else None
        if isinstance(event, SessionFailed):
            self._capture_failure("adapter.apply")
        event = dataclasses.replace(event, failure=self._failure_context)
        await self._emit_terminal_safe(event)
        self._record_terminal_diagnostic(
            disposition,
            event.code if isinstance(event, SessionFailed) else None,
        )
        self._terminal_published.set()
        await self._shutdown_connection()

    async def _claim_clean_close_intent(self) -> bool:
        async with self._terminal_lock:
            if self._terminal_owner is not None:
                return False
            self._closing = True
            self._terminal_owner = (CloseInitiator.CLIENT, CloseDisposition.CLEAN)
            return True

    async def _claim_terminal(
        self,
        initiator: CloseInitiator,
        disposition: CloseDisposition,
    ) -> bool:
        async with self._terminal_lock:
            if self._terminal_owner is not None:
                return False
            self._terminal_owner = (initiator, disposition)
            return True

    async def _shutdown_connection(self) -> None:
        async with self._shutdown_lock:
            if self._shutdown_complete:
                return
            self._shutdown_complete = True
            current = asyncio.current_task()
            for pending in self._context_pending.values():
                if not pending.done():
                    pending.set_exception(
                        RealtimeError(RealtimeErrorCode.CANCELLED, failure=self._failure_context)
                    )
            for task in self._tool_tasks:
                if task is not current:
                    task.cancel()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    self._connection.close(1000, "session terminal"),
                    timeout=self._close_timeout,
                )
            current = asyncio.current_task()
            if self._receiver is not None and self._receiver is not current:
                self._receiver.cancel()
                with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(
                        self._receiver,
                        timeout=self._close_timeout,
                    )

    def _require_active(self) -> None:
        if self._terminal_owner is not None or self._closing or not self._is_current_generation():
            raise RealtimeError(
                RealtimeErrorCode.CANCELLED, "session is closed", failure=self._failure_context
            )

    def _current_response(self) -> str | None:
        for response_id in reversed(self._response_order):
            if response_id not in self._terminal_responses:
                return response_id
        return None

    def _record_terminal_diagnostic(
        self,
        disposition: CloseDisposition,
        code: RealtimeErrorCode | None = None,
    ) -> None:
        duration_ms = _duration_ms(self._created_ns)
        self._diagnostics.emit(
            correlation=self.correlation,
            stage=RealtimeDiagnosticStage.INPUT_AUDIO_SUMMARY,
            generation=self.generation,
            frame_count=self._input_frame_count,
            byte_count=self._input_byte_count,
            duration_ms=duration_ms,
        )
        self._diagnostics.emit(
            correlation=self.correlation,
            stage=RealtimeDiagnosticStage.OUTPUT_AUDIO_SUMMARY,
            generation=self.generation,
            frame_count=self._output_frame_count,
            byte_count=self._output_byte_count,
            duration_ms=duration_ms,
        )
        self._diagnostics.emit(
            correlation=self.correlation,
            stage=RealtimeDiagnosticStage.SESSION_TERMINAL,
            failure=self._failure_context,
            stable_code=code,
            close_class=disposition,
            generation=self.generation,
            duration_ms=duration_ms,
        )

    def _record_attempt_failure(
        self, payload: str, code: RealtimeErrorCode
    ) -> RealtimeFailureContext:
        event_type, fields = _safe_event_fields(payload)
        failure = RealtimeFailureContext(
            "provider.send", event_type, self.generation, fields, client_event_type=event_type
        )
        self._diagnostics.emit(
            correlation=self.correlation,
            generation=self.generation,
            stage=RealtimeDiagnosticStage.PROVIDER_SEND_FAILED,
            stable_code=code,
            operation_kind="tool.send_ambiguous",
            failure=failure,
        )
        return failure

    def _known_client_reference(self, payload: str) -> tuple[str | None, str | None]:
        try:
            value = strict_json_object(payload)
        except RealtimeError:
            return None, None
        related = value.get("related_event_id")
        if isinstance(related, str) and related in self._sent_event_types:
            return related, self._sent_event_types[related]
        return None, None

    def _remember_client_event(self, event: Mapping[str, object]) -> None:
        identity = event.get("event_id")
        event_type, _ = _safe_event_fields(event)
        if isinstance(identity, str):
            self._sent_event_types[identity] = event_type

    def _capture_failure(
        self,
        stage: str,
        *,
        event: Mapping[str, object] | None = None,
        error: RealtimeError | None = None,
        related_event_id: str | None = None,
        client_event_type: str | None = None,
        control_code: RealtimeErrorCode | None = None,
        message_class: str | None = None,
        retryable: bool | None = None,
        fatal: bool | None = None,
        retry_after_ms: int | None = None,
    ) -> None:
        if self._failure_context is not None:
            return
        event_type, fields = (
            _safe_event_fields(event)
            if event is not None
            else (self._last_event_type, self._last_field_names)
        )
        if related_event_id is None and event is not None:
            event_id = event.get("event_id")
            if isinstance(event_id, str) and event_id in self._sent_event_types:
                related_event_id = event_id
        validation_field = error.validation_field if error is not None else None
        if validation_field not in _SAFE_FAILURE_FIELDS | {
            "fields",
            "response.id",
            "item.id",
            "item.call_id",
            "session.id",
        }:
            validation_field = None
        self._failure_context = RealtimeFailureContext(
            stage,
            event_type,
            self.generation,
            fields,
            validation_field=validation_field,
            validation_reason=(
                error.validation_reason
                if error.validation_reason in _FAILURE_REASONS
                else "validation_failed"
            )
            if error
            else None,
            related_event_id=related_event_id,
            client_event_type=client_event_type
            or (
                event_type
                if event is not None
                and stage
                in {"provider.send", "context.ack_timeout", "tool.ack_timeout", "close.ack_timeout"}
                else None
            ),
            control_code=control_code,
            message_class=message_class,
            retryable=retryable,
            fatal=fatal,
            retry_after_ms=retry_after_ms,
        )

    def _record_provider_diagnostic(
        self,
        stage: RealtimeDiagnosticStage,
        *,
        operation_kind: str,
        fingerprint: str | None = None,
        stable_code: RealtimeErrorCode | None = None,
        provider_event_type: str | None = None,
    ) -> None:
        self._diagnostics.emit(
            correlation=self.correlation,
            stage=stage,
            stable_code=stable_code,
            generation=self.generation,
            operation_kind=operation_kind,
            fingerprint=fingerprint,
            duration_ms=_duration_ms(self._created_ns),
            provider_event_type=provider_event_type,
            failure=self._failure_context,
        )

    @staticmethod
    def _event_size(event: RealtimeEvent) -> int:
        if isinstance(event, OutputAudio):
            return len(event.data)
        try:
            payload = dataclasses.asdict(event)
            return len(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
        except (TypeError, ValueError):
            return 256


def _duration_ms(started_ns: int) -> int:
    return max(0, (time.monotonic_ns() - started_ns) // 1_000_000)


def _sample_audio_diagnostic(frame_count: int) -> bool:
    return frame_count == 1 or frame_count % 250 == 0


def _provider_event_metadata(payload: str) -> tuple[str, str]:
    """Return content-free event identity and JSON shape fingerprint."""

    try:
        value = json.loads(payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        return "invalid_provider_event", hashlib.sha256(b"invalid-json").hexdigest()
    event_type = value.get("type") if isinstance(value, dict) else None
    if isinstance(event_type, str) and event_type.startswith("tokenseller."):
        event_type = event_type.replace("tokenseller.", "relay.", 1)
    operation_kind = (
        event_type
        if isinstance(event_type, str)
        and _DIAGNOSTIC_EVENT_KIND.fullmatch(event_type) is not None
        and _SECRET_MARKER.search(event_type) is None
        else "unknown_provider_event"
    )
    try:
        shape = _json_shape(value)
        encoded = json.dumps(shape, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (RecursionError, TypeError, ValueError):
        encoded = b"shape-unavailable"
    return operation_kind, hashlib.sha256(encoded).hexdigest()


def _transport_failure_kind(error: Exception) -> str:
    diagnostic_kind = getattr(error, "diagnostic_kind", None)
    if (
        isinstance(diagnostic_kind, str)
        and _DIAGNOSTIC_EVENT_KIND.fullmatch(diagnostic_kind) is not None
    ):
        return diagnostic_kind
    close_code = getattr(error, "close_code", None)
    if (
        isinstance(close_code, int)
        and not isinstance(close_code, bool)
        and 1000 <= close_code <= 4999
    ):
        return f"transport.close.{close_code}"
    transport_code = getattr(error, "transport_code", None)
    if isinstance(transport_code, str):
        candidate = f"transport.{transport_code}"
        if _DIAGNOSTIC_EVENT_KIND.fullmatch(candidate) is not None:
            return candidate
    return "transport.failure"


def _json_shape(value: object) -> object:
    if isinstance(value, dict):
        return {key: _json_shape(item) for key, item in value.items()}
    if isinstance(value, list):
        return [len(value), *(_json_shape(item) for item in value[:4])]
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, str):
        return "string-empty" if not value else "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return "unknown"


def _safe_event_fields(value: object) -> tuple[str, tuple[str, ...]]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            value = None
    if not isinstance(value, Mapping):
        return "invalid_provider_event", ()
    kind = value.get("type")
    event_type = (
        kind
        if isinstance(kind, str)
        and _DIAGNOSTIC_EVENT_KIND.fullmatch(kind)
        and (
            _SECRET_MARKER.search(kind) is None
            or kind
            in {
                "tokenseller.error",
                "tokenseller.session.closed",
                "tokenseller.session.expiring",
                "tokenseller.session.open",
                "tokenseller.session.close",
                "tokenseller.session.created",
            }
        )
        else "unknown_provider_event"
    )
    return event_type, tuple(sorted(key for key in value if key in _SAFE_FAILURE_FIELDS))
