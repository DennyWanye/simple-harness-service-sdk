"""Realtime session lifecycle, ordering, backpressure and terminal ownership."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass

from .contracts import (
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
    RealtimeOpenRequest,
    ResponseFinished,
    ResponseStatus,
    SessionClosed,
    SessionFailed,
    SessionReady,
    ToolCallRequested,
    ToolCallState,
)
from .observability import (
    RealtimeDiagnostics,
    RealtimeDiagnosticSnapshot,
    RealtimeDiagnosticStage,
)
from .ports import DecodedProviderEvent, RealtimeConnection, RealtimeProviderAdapter
from .relay_control import RelayControlCodec


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
    ) -> None:
        if write_timeout <= 0 or tool_ack_timeout <= 0 or close_timeout <= 0:
            raise ValueError("Realtime timeouts must be positive")
        self._connection = connection
        self._adapter = adapter
        self._control = control
        self._credential = credential
        self._request = request
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

    async def start(self) -> None:
        update = self._adapter.session_update(self._request)
        await asyncio.wait_for(
            self._connection.send_text(self._adapter.encode_client_event(update)),
            timeout=self._write_timeout,
        )
        self._receiver = asyncio.create_task(self._receive_loop())

    async def __aenter__(self) -> ManagedRealtimeSession:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.close()

    def diagnostics_snapshot(self) -> RealtimeDiagnosticSnapshot:
        return self._diagnostics.snapshot()

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
                raise RealtimeError(
                    RealtimeErrorCode.TIMEOUT,
                    "tool result send is ambiguous; retry the same output",
                    retryable=True,
                ) from error
            except Exception as error:
                raise RealtimeError(
                    RealtimeErrorCode.UNAVAILABLE,
                    "tool result send is ambiguous; retry the same output",
                    retryable=True,
                ) from error
            assert record.ack is not None
            try:
                await asyncio.wait_for(record.ack.wait(), timeout=self._tool_ack_timeout)
            except TimeoutError as error:
                await self._fail(RealtimeErrorCode.PROTOCOL_ERROR, retryable=False)
                raise RealtimeError(
                    RealtimeErrorCode.PROTOCOL_ERROR,
                    "ambiguous tool result acknowledgement",
                ) from error
        if record.state is ToolCallState.RESULT_ACKED:
            followup = self._adapter.followup_response(call_id)
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
                raise RealtimeError(
                    RealtimeErrorCode.TIMEOUT,
                    "followup response send is ambiguous; retry the same output",
                    retryable=True,
                ) from error
            except Exception as error:
                raise RealtimeError(
                    RealtimeErrorCode.UNAVAILABLE,
                    "followup response send is ambiguous; retry the same output",
                    retryable=True,
                ) from error

    async def _send_runtime_event(self, event: Mapping[str, object]) -> None:
        try:
            await asyncio.wait_for(
                self._connection.send_text(self._adapter.encode_client_event(event)),
                timeout=self._write_timeout,
            )
        except TimeoutError as error:
            await self._fail(RealtimeErrorCode.TIMEOUT, retryable=True)
            raise RealtimeError(
                RealtimeErrorCode.TIMEOUT,
                "provider write timed out",
                retryable=True,
            ) from error
        except Exception as error:
            await self._fail(RealtimeErrorCode.UNAVAILABLE, retryable=True)
            raise RealtimeError(
                RealtimeErrorCode.UNAVAILABLE,
                "provider write failed",
                retryable=True,
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
        self._pending_close_event_id = close_event_id
        self._close_ack = asyncio.get_running_loop().create_future()
        acknowledgement: SessionClosed | None = None
        try:
            await asyncio.wait_for(
                self._connection.send_text(close_payload),
                timeout=self._close_timeout,
            )
        except TimeoutError:
            self._record_close_ack_timeout()
        except Exception:
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
            self._queued_output_bytes = max(
                0, self._queued_output_bytes - self._event_size(item)
            )
            yield item

    async def _receive_loop(self) -> None:
        try:
            while not self._terminal_published.is_set():
                try:
                    payload = await self._connection.receive_text()
                    if payload is None:
                        if self._closing:
                            return
                        await self._fail(RealtimeErrorCode.UNAVAILABLE, retryable=True)
                        return
                    control_event = self._control.decode_runtime_event(payload)
                    if control_event is not None:
                        if self._closing:
                            if isinstance(control_event, SessionClosed):
                                valid_ack = (
                                    control_event.related_event_id
                                    == self._pending_close_event_id
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
                            await self._finish_terminal(
                                control_event,
                                CloseInitiator.RELAY,
                                CloseDisposition.RETRYABLE
                                if control_event.retryable
                                else CloseDisposition.FATAL,
                            )
                            return
                        if isinstance(control_event, SessionClosed):
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
                    decoded = self._adapter.decode_server_event(payload)
                    if decoded.event_id in self._seen_event_ids:
                        self.duplicate_event_count += 1
                        continue
                    self._seen_event_ids.add(decoded.event_id)
                    if not await self._apply(decoded):
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
        terminal_batch = any(isinstance(event, SessionFailed) for event in decoded.events)
        if decoded.provider_ready and not self._ready:
            self._ready = True
            if not await self._emit(
                SessionReady(
                    self.generation,
                    self.correlation,
                    self.capability.input_audio,
                    self.capability.output_audio,
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
            response_id, item_id, output_index, item_type = (
                decoded.introduced_item_detail
            )
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
                    or (response_id, item_id, output_index)
                    not in self._function_call_items
                    or len(event.arguments_json.encode("utf-8"))
                    > self.capability.limits.tool_payload_bytes
                ):
                    await self._fail(RealtimeErrorCode.PROTOCOL_ERROR, retryable=False)
                    return False
                if event.call_id in self._tools:
                    await self._fail(RealtimeErrorCode.PROTOCOL_ERROR, retryable=False)
                    return False
                self._tools[event.call_id] = _ToolRecord(ToolCallState.REQUESTED)
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
        if self._terminal_owner is not None or self._closing:
            raise RealtimeError(RealtimeErrorCode.CANCELLED, "session is closed")

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
        self._diagnostics.emit(
            correlation=self.correlation,
            stage=RealtimeDiagnosticStage.SESSION_TERMINAL,
            stable_code=code,
            close_class=disposition,
            generation=self.generation,
            duration_ms=_duration_ms(self._created_ns),
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
