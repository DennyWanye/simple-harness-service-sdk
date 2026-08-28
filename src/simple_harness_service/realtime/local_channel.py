"""SDK-owned local channel FSM bridging products to one Realtime session."""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections import deque
from collections.abc import AsyncIterator
from typing import Protocol, cast

from .contracts import (
    CloseDisposition,
    CloseInitiator,
    OutputAudio,
    OutputAudioCompleted,
    OutputAudioStarted,
    RealtimeAudioFormat,
    RealtimeError,
    RealtimeErrorCode,
    RealtimeEvent,
    RealtimeFeature,
    RealtimeOpenRequest,
    ResponseFinished,
    SessionClosed,
    SessionFailed,
    SessionReady,
)
from .local import (
    AudioDirection,
    LocalPcmFrame,
    decode_local_message,
    decode_pcm_frame,
    encode_domain_event,
    encode_local_message,
    encode_pcm_frame,
)
from .observability import (
    RealtimeDiagnostics,
    RealtimeDiagnosticSnapshot,
    RealtimeDiagnosticStage,
)
from .ports import RealtimeSession

_CORRELATION = re.compile(r"^corr_[0-9A-HJKMNP-TV-Z]{26}$")
_WINDOW_FRAMES = 64
_WINDOW_BYTES = 1_048_576
_ACK_EVERY_FRAMES = 16
_ACK_MAX_DELAY_SECONDS = 0.1
# The managed provider transport has its own two-second write deadline. Keep the
# local product deadline outside it so the provider layer can publish the exact
# transport failure instead of losing the race and collapsing it to local busy.
_PRODUCER_BLOCK_TIMEOUT_SECONDS = 3.0
_OUTPUT_PCM_CHUNK_BYTES = 262_144


class LocalChannel(Protocol):
    async def receive(self) -> str | bytes: ...

    async def send(self, payload: str | bytes) -> None: ...

    def bind_session(self, session: RealtimeSession) -> None: ...

    async def close(self) -> None: ...


class CorrelatedRealtimeSessionOpener(Protocol):
    async def _open_with_correlation(
        self, request: RealtimeOpenRequest, correlation: str
    ) -> RealtimeSession: ...


class _ControlledCloseSession(Protocol):
    async def close(self, *, reason: str = "client_hangup") -> None: ...


class _InputWindow:
    def __init__(self, generation: int) -> None:
        self.generation = generation
        self.highest_contiguous = 0
        self._pending: dict[int, bytes] = {}
        self._pending_bytes = 0

    def accept(self, frame: LocalPcmFrame) -> list[bytes]:
        if frame.generation != self.generation:
            return []
        if frame.direction is not AudioDirection.INPUT:
            raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "wrong PCM direction")
        if frame.sequence <= self.highest_contiguous or frame.sequence in self._pending:
            return []
        if frame.sequence > self.highest_contiguous + _WINDOW_FRAMES:
            raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "PCM sequence gap")
        if self._pending_bytes + len(frame.payload) > _WINDOW_BYTES:
            raise RealtimeError(RealtimeErrorCode.BUSY, "input window is full")
        self._pending[frame.sequence] = frame.payload
        self._pending_bytes += len(frame.payload)
        ready: list[bytes] = []
        while self.highest_contiguous + 1 in self._pending:
            sequence = self.highest_contiguous + 1
            payload = self._pending.pop(sequence)
            self._pending_bytes -= len(payload)
            self.highest_contiguous = sequence
            ready.append(payload)
        return ready


class LocalRealtimeChannelController:
    """Own the authenticated local call lifecycle and bounded audio flow control."""

    def __init__(
        self,
        opener: CorrelatedRealtimeSessionOpener,
        *,
        diagnostics: RealtimeDiagnostics | None = None,
        ack_every_frames: int = _ACK_EVERY_FRAMES,
        ack_max_delay_seconds: float = _ACK_MAX_DELAY_SECONDS,
        producer_block_timeout_seconds: float = _PRODUCER_BLOCK_TIMEOUT_SECONDS,
        close_timeout_seconds: float = 5.0,
    ) -> None:
        if (
            ack_every_frames <= 0
            or ack_max_delay_seconds <= 0
            or producer_block_timeout_seconds <= 0
            or close_timeout_seconds <= 0
        ):
            raise ValueError("local controller bounds must be positive")
        self._opener = opener
        self._diagnostics = diagnostics or RealtimeDiagnostics()
        self._created_ns = time.monotonic_ns()
        self._ack_every_frames = ack_every_frames
        self._ack_max_delay_seconds = ack_max_delay_seconds
        self._producer_block_timeout_seconds = producer_block_timeout_seconds
        self._close_timeout_seconds = close_timeout_seconds
        self._channel: LocalChannel | None = None
        self._session: RealtimeSession | None = None
        self._generation = 0
        self._correlation = ""
        self._ready = False
        self._started = False
        self._requested_stop_reason: str | None = None
        self._terminal_lock = asyncio.Lock()
        self._terminal = False
        self._input_window: _InputWindow | None = None
        self._input_since_ack = 0
        self._ack_timer: asyncio.Task[None] | None = None
        self._event_task: asyncio.Task[None] | None = None
        self._output_sequence = 0
        self._output_acked = 0
        self._output_pending: deque[tuple[int, int]] = deque()
        self._output_pending_bytes = 0
        self._output_condition = asyncio.Condition()
        self._active_output: tuple[str, str, int, int] | None = None
        self._completed_outputs: set[tuple[str, str, int, int]] = set()
        self._completed_output_order: deque[tuple[str, str, int, int]] = deque()
        self._terminal_responses: set[str] = set()
        self._terminal_response_order: deque[str] = deque()
        self.late_frame_count = 0
        self.duplicate_frame_count = 0

    def diagnostics_snapshot(self) -> RealtimeDiagnosticSnapshot:
        return self._diagnostics.snapshot()

    async def run(self, channel: LocalChannel) -> None:
        if self._channel is not None:
            raise RuntimeError("controller instances serve exactly one channel")
        self._channel = channel
        try:
            await self._receive_hello()
            while not self._terminal:
                payload = await channel.receive()
                await self._handle_product_message(payload)
        except asyncio.CancelledError:
            raise
        except (EOFError, ConnectionError, asyncio.IncompleteReadError):
            await self._close_session()
        except RealtimeError as error:
            await self._fail(error.code, error.retryable)
        except (UnicodeError, ValueError):
            await self._fail(RealtimeErrorCode.PROTOCOL_ERROR, False)
        except Exception:
            await self._fail(RealtimeErrorCode.INTERNAL, False)
        finally:
            if self._ack_timer is not None:
                self._ack_timer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._ack_timer
            if self._event_task is not None and self._event_task is not asyncio.current_task():
                self._event_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._event_task
            await self._close_session()
            await channel.close()

    async def _receive_hello(self) -> None:
        assert self._channel is not None
        payload = await self._channel.receive()
        if not isinstance(payload, str):
            raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "local.hello must be text")
        value = decode_local_message(payload)
        if value.get("type") != "local.hello":
            raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "local.hello required")
        generation = value.get("generation")
        correlation = value.get("correlation")
        if generation != 1 or not isinstance(correlation, str) or not _CORRELATION.fullmatch(
            correlation
        ):
            raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "invalid local.hello")
        self._generation = generation
        self._correlation = correlation
        self._input_window = _InputWindow(generation)

    async def _handle_product_message(self, payload: str | bytes) -> None:
        if isinstance(payload, bytes):
            await self._handle_input_audio(payload)
            return
        value = decode_local_message(payload)
        message_type = value.get("type")
        generation = value.get("generation")
        if generation != self._generation:
            self.late_frame_count += 1
            return
        if message_type == "call.start":
            await self._start_call(value)
        elif message_type == "call.stop":
            await self._stop_call(value)
        elif message_type == "call.barge_in":
            if not self._ready or self._session is None:
                raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "call is not active")
            await self._session.cancel_response()
        elif message_type == "call.audio_ack":
            await self._handle_output_ack(value)
        else:
            raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "unexpected local message")

    async def _start_call(self, value: dict[str, object]) -> None:
        if self._started:
            raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "duplicate call.start")
        instructions = value.get("instructions")
        raw_features = value.get("required_features")
        if not isinstance(instructions, str) or not instructions or not isinstance(
            raw_features, list
        ):
            raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "invalid call.start")
        try:
            features = frozenset(RealtimeFeature(item) for item in raw_features)
        except (TypeError, ValueError) as error:
            raise RealtimeError(
                RealtimeErrorCode.PROTOCOL_ERROR, "invalid required_features"
            ) from error
        if len(features) != len(raw_features):
            raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "duplicate required feature")
        self._started = True
        await self._send_json(
            {"type": "call.state", "generation": self._generation, "state": "connecting"}
        )
        self._record_local_state(RealtimeDiagnosticStage.LOCAL_CONNECTING)
        request = RealtimeOpenRequest(
            external_session_id=f"local-generation-{self._generation}",
            instructions=instructions,
            required_features=features,
        )
        try:
            session = await self._opener._open_with_correlation(
                request, self._correlation
            )
        except RealtimeError as error:
            await self._fail(error.code, error.retryable)
            return
        self._session = session
        assert self._channel is not None
        self._channel.bind_session(session)
        self._event_task = asyncio.create_task(self._forward_events(session.events()))

    async def _stop_call(self, value: dict[str, object]) -> None:
        if not self._started:
            raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "call.start required")
        reason = value.get("reason")
        if reason not in {"client_hangup", "app_shutdown"}:
            raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "invalid stop reason")
        assert isinstance(reason, str)
        self._requested_stop_reason = reason
        await self._send_json(
            {"type": "call.state", "generation": self._generation, "state": "closing"}
        )
        self._record_local_state(RealtimeDiagnosticStage.LOCAL_CLOSING)
        event_task = self._event_task
        await self._close_session(reason=reason)
        if event_task is not None and not event_task.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(event_task), timeout=self._close_timeout_seconds
                )
            except TimeoutError:
                self._record_local_timeout(RealtimeErrorCode.TIMEOUT)
                await self._fail(RealtimeErrorCode.TIMEOUT, True)

    async def _handle_input_audio(self, payload: bytes) -> None:
        if not self._ready or self._session is None or self._input_window is None:
            raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "binary before call.ready")
        frame = decode_pcm_frame(payload)
        if frame.generation != self._generation:
            self.late_frame_count += 1
            return
        previous = self._input_window.highest_contiguous
        ready = self._input_window.accept(frame)
        if not ready and frame.sequence <= max(previous, self._input_window.highest_contiguous):
            self.duplicate_frame_count += 1
            return
        for pcm in ready:
            try:
                await asyncio.wait_for(
                    self._session.send_audio(pcm),
                    timeout=self._producer_block_timeout_seconds,
                )
            except TimeoutError:
                self._record_local_timeout(RealtimeErrorCode.BUSY)
                await self._cancel_active_response()
                await self._fail(RealtimeErrorCode.BUSY, False)
                return
            self._input_since_ack += 1
        if self._input_since_ack >= self._ack_every_frames:
            await self._send_input_ack()
        elif ready and self._ack_timer is None:
            self._ack_timer = asyncio.create_task(self._delayed_input_ack())

    async def _delayed_input_ack(self) -> None:
        try:
            await asyncio.sleep(self._ack_max_delay_seconds)
            await self._send_input_ack()
        finally:
            self._ack_timer = None

    async def _send_input_ack(self) -> None:
        assert self._input_window is not None
        if self._input_since_ack == 0:
            return
        self._input_since_ack = 0
        await self._send_json(
            {
                "type": "call.audio_ack",
                "generation": self._generation,
                "direction": "input",
                "highest_contiguous_sequence": self._input_window.highest_contiguous,
            }
        )

    async def _handle_output_ack(self, value: dict[str, object]) -> None:
        if not self._ready:
            raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "call is not active")
        if value.get("direction") != "output":
            raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "invalid ACK direction")
        highest = value.get("highest_contiguous_sequence")
        if not isinstance(highest, int) or isinstance(highest, bool) or highest < 0:
            raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "invalid ACK sequence")
        if highest > self._output_sequence:
            raise RealtimeError(RealtimeErrorCode.PROTOCOL_ERROR, "ACK exceeds sent output")
        async with self._output_condition:
            if highest <= self._output_acked:
                return
            self._output_acked = highest
            while self._output_pending and self._output_pending[0][0] <= highest:
                _sequence, size = self._output_pending.popleft()
                self._output_pending_bytes -= size
            self._output_condition.notify_all()

    async def _forward_events(self, stream: AsyncIterator[RealtimeEvent]) -> None:
        try:
            async for event in stream:
                if self._terminal:
                    return
                if isinstance(event, SessionReady):
                    if self._ready:
                        await self._fail(RealtimeErrorCode.PROTOCOL_ERROR, False)
                        return
                    if event.correlation != self._correlation:
                        await self._fail(RealtimeErrorCode.PROTOCOL_ERROR, False)
                        return
                    self._ready = True
                    await self._send_json(
                        {
                            "type": "call.ready",
                            "generation": self._generation,
                            "correlation": event.correlation,
                            "input_audio": _audio_json(event.input_audio),
                            "output_audio": _audio_json(event.output_audio),
                        }
                    )
                    await self._send_json(
                        {
                            "type": "call.state",
                            "generation": self._generation,
                            "state": "active",
                        }
                    )
                    self._record_local_state(RealtimeDiagnosticStage.LOCAL_ACTIVE)
                elif isinstance(event, OutputAudioStarted):
                    identity = _output_identity(event)
                    if event.response_id in self._terminal_responses:
                        self.late_frame_count += 1
                        continue
                    if self._active_output is not None and self._active_output != identity:
                        await self._fail(RealtimeErrorCode.PROTOCOL_ERROR, False)
                        return
                    if identity in self._completed_outputs:
                        self.late_frame_count += 1
                        continue
                    self._active_output = identity
                    await self._send_channel(encode_domain_event(self._generation, event))
                elif isinstance(event, OutputAudio):
                    identity = _output_identity(event)
                    if (
                        event.response_id in self._terminal_responses
                        or identity in self._completed_outputs
                    ):
                        self.late_frame_count += 1
                        continue
                    if identity != self._active_output:
                        await self._fail(RealtimeErrorCode.PROTOCOL_ERROR, False)
                        return
                    await self._send_output_audio(event.data)
                elif isinstance(event, OutputAudioCompleted):
                    identity = _output_identity(event)
                    if (
                        event.response_id in self._terminal_responses
                        or identity in self._completed_outputs
                    ):
                        self.late_frame_count += 1
                        continue
                    if identity != self._active_output:
                        await self._fail(RealtimeErrorCode.PROTOCOL_ERROR, False)
                        return
                    self._active_output = None
                    self._remember_completed_output(identity)
                    await self._send_channel(encode_domain_event(self._generation, event))
                elif isinstance(event, ResponseFinished):
                    self._remember_terminal_response(event.response_id)
                    if (
                        self._active_output is not None
                        and self._active_output[0] == event.response_id
                    ):
                        active = self._active_output
                        self._active_output = None
                        self._remember_completed_output(active)
                    await self._send_channel(encode_domain_event(self._generation, event))
                elif isinstance(event, SessionFailed):
                    await self._fail(event.code, event.retryable)
                    return
                elif isinstance(event, SessionClosed):
                    if await self._claim_terminal():
                        reason = self._requested_stop_reason or _closed_reason(event)
                        await self._try_send_json(
                            {
                                "type": "call.closed",
                                "generation": self._generation,
                                "reason": reason,
                            }
                        )
                        self._record_local_state(
                            RealtimeDiagnosticStage.LOCAL_CLOSED,
                            close_class=event.disposition,
                        )
                        await self._close_channel()
                    return
                else:
                    await self._send_channel(encode_domain_event(self._generation, event))
            if not self._terminal:
                await self._fail(RealtimeErrorCode.INTERNAL, False)
        except asyncio.CancelledError:
            raise
        except RealtimeError as error:
            await self._fail(error.code, error.retryable)
        except Exception:
            await self._fail(RealtimeErrorCode.INTERNAL, False)

    async def _send_output_audio(self, pcm: bytes) -> None:
        if not pcm or len(pcm) % 2:
            await self._fail(RealtimeErrorCode.PROTOCOL_ERROR, False)
            return
        for offset in range(0, len(pcm), _OUTPUT_PCM_CHUNK_BYTES):
            await self._send_output_chunk(pcm[offset : offset + _OUTPUT_PCM_CHUNK_BYTES])
            if self._terminal:
                return

    async def _send_output_chunk(self, pcm: bytes) -> None:
        async def wait_for_capacity() -> None:
            async with self._output_condition:
                await self._output_condition.wait_for(
                    lambda: self._terminal
                    or (
                        len(self._output_pending) < _WINDOW_FRAMES
                        and self._output_pending_bytes + len(pcm) <= _WINDOW_BYTES
                    )
                )

        if (
            len(self._output_pending) >= _WINDOW_FRAMES
            or self._output_pending_bytes + len(pcm) > _WINDOW_BYTES
        ):
            try:
                await asyncio.wait_for(
                    wait_for_capacity(), timeout=self._producer_block_timeout_seconds
                )
            except TimeoutError:
                self._record_local_timeout(RealtimeErrorCode.BUSY)
                await self._cancel_active_response()
                await self._fail(RealtimeErrorCode.BUSY, False)
                return
        if self._terminal:
            return
        self._output_sequence += 1
        sequence = self._output_sequence
        self._output_pending.append((sequence, len(pcm)))
        self._output_pending_bytes += len(pcm)
        await self._send_channel(
            encode_pcm_frame(
                LocalPcmFrame(
                    AudioDirection.OUTPUT,
                    self._generation,
                    sequence,
                    pcm,
                )
            )
        )

    def _remember_completed_output(self, identity: tuple[str, str, int, int]) -> None:
        self._completed_outputs.add(identity)
        self._completed_output_order.append(identity)
        while len(self._completed_output_order) > _WINDOW_FRAMES:
            expired = self._completed_output_order.popleft()
            self._completed_outputs.discard(expired)

    def _remember_terminal_response(self, response_id: str) -> None:
        if response_id in self._terminal_responses:
            return
        self._terminal_responses.add(response_id)
        self._terminal_response_order.append(response_id)
        while len(self._terminal_response_order) > _WINDOW_FRAMES:
            expired = self._terminal_response_order.popleft()
            self._terminal_responses.discard(expired)

    async def _fail(self, code: RealtimeErrorCode, retryable: bool) -> None:
        if not await self._claim_terminal():
            return
        close_class = (
            CloseDisposition.RETRYABLE if retryable else CloseDisposition.FATAL
        )
        self._record_local_state(
            RealtimeDiagnosticStage.LOCAL_ERROR,
            stable_code=code,
            close_class=close_class,
        )
        await self._close_session()
        await self._try_send_json(
            {"type": "call.state", "generation": self._generation, "state": "error"}
        )
        await self._try_send_json(
            {
                "type": "call.error",
                "generation": self._generation,
                "code": code.value,
                "retryable": retryable,
            }
        )
        await self._try_send_json(
            {
                "type": "call.closed",
                "generation": self._generation,
                "reason": _closed_reason_for_error(code),
            }
        )
        self._record_local_state(
            RealtimeDiagnosticStage.LOCAL_CLOSED,
            stable_code=code,
            close_class=close_class,
        )
        await self._close_channel()

    async def _claim_terminal(self) -> bool:
        async with self._terminal_lock:
            if self._terminal:
                return False
            self._terminal = True
        async with self._output_condition:
            self._output_condition.notify_all()
        return True

    async def _close_session(self, *, reason: str | None = None) -> None:
        session = self._session
        self._session = None
        if session is not None:
            try:
                if reason is not None:
                    close_awaitable = cast(_ControlledCloseSession, session).close(
                        reason=reason
                    )
                else:
                    close_awaitable = session.close()
                await asyncio.wait_for(
                    close_awaitable,
                    timeout=self._close_timeout_seconds,
                )
            except TimeoutError:
                self._record_local_timeout(RealtimeErrorCode.TIMEOUT)
            except Exception:
                pass

    async def _close_channel(self) -> None:
        if self._channel is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    self._channel.close(), timeout=self._close_timeout_seconds
                )

    async def _send_json(self, value: dict[str, object]) -> None:
        await self._send_channel(encode_local_message(value))

    async def _try_send_json(self, value: dict[str, object]) -> None:
        with contextlib.suppress(Exception):
            await self._send_channel(encode_local_message(value), cancel_on_timeout=False)

    async def _send_channel(
        self, payload: str | bytes, *, cancel_on_timeout: bool = True
    ) -> None:
        assert self._channel is not None
        try:
            await asyncio.wait_for(
                self._channel.send(payload),
                timeout=self._producer_block_timeout_seconds,
            )
        except TimeoutError:
            self._record_local_timeout(RealtimeErrorCode.BUSY)
            if cancel_on_timeout:
                await self._cancel_active_response()
            raise RealtimeError(
                RealtimeErrorCode.BUSY,
                "local product write timeout",
            ) from None

    async def _cancel_active_response(self) -> None:
        session = self._session
        if session is None:
            return
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                session.cancel_response(),
                timeout=self._producer_block_timeout_seconds,
            )

    def _record_local_state(
        self,
        stage: RealtimeDiagnosticStage,
        *,
        stable_code: RealtimeErrorCode | None = None,
        close_class: CloseDisposition | None = None,
    ) -> None:
        if not self._correlation:
            return
        self._diagnostics.emit(
            correlation=self._correlation,
            stage=stage,
            stable_code=stable_code,
            close_class=close_class,
            generation=self._generation,
            duration_ms=_duration_ms(self._created_ns),
        )

    def _record_local_timeout(self, code: RealtimeErrorCode) -> None:
        self._record_local_state(
            RealtimeDiagnosticStage.LOCAL_TIMEOUT,
            stable_code=code,
        )


def _audio_json(value: RealtimeAudioFormat) -> dict[str, object]:
    return {
        "codec": value.codec,
        "sample_rate": value.sample_rate,
        "channels": value.channels,
    }


def _output_identity(
    event: OutputAudioStarted | OutputAudio | OutputAudioCompleted,
) -> tuple[str, str, int, int]:
    return (
        event.response_id,
        event.item_id,
        event.output_index,
        event.content_index,
    )


def _closed_reason_for_error(code: RealtimeErrorCode) -> str:
    direct = {
        RealtimeErrorCode.BUSY: "busy",
        RealtimeErrorCode.INTERNAL: "internal",
        RealtimeErrorCode.PROTOCOL_ERROR: "protocol_error",
        RealtimeErrorCode.SESSION_EXPIRED: "session_expired",
        RealtimeErrorCode.TIMEOUT: "timeout",
    }
    if code in direct:
        return direct[code]
    if code in {
        RealtimeErrorCode.FORBIDDEN,
        RealtimeErrorCode.RATE_LIMITED,
        RealtimeErrorCode.UNAUTHENTICATED,
        RealtimeErrorCode.UNAVAILABLE,
        RealtimeErrorCode.BILLING_REJECTED,
    }:
        return "provider_error"
    if code in {
        RealtimeErrorCode.INVALID_REQUEST,
        RealtimeErrorCode.UNSUPPORTED,
    }:
        return "protocol_error"
    return "internal"


def _closed_reason(event: SessionClosed) -> str:
    allowed = {
        "client_hangup",
        "app_shutdown",
        "session_expired",
        "provider_error",
        "network_error",
        "timeout",
        "protocol_error",
        "busy",
        "internal",
    }
    if event.reason in allowed:
        return event.reason
    if event.disposition is CloseDisposition.CLEAN_EXPIRED:
        return "session_expired"
    mapping = {
        CloseInitiator.CLIENT: "client_hangup",
        CloseInitiator.NETWORK: "network_error",
        CloseInitiator.PROVIDER: "provider_error",
        CloseInitiator.RELAY: "provider_error",
        CloseInitiator.SHUTDOWN: "app_shutdown",
        CloseInitiator.TIMEOUT: "timeout",
    }
    return mapping[event.initiator]


def _duration_ms(started_ns: int) -> int:
    return max(0, (time.monotonic_ns() - started_ns) // 1_000_000)
