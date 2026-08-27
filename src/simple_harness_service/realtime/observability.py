"""Provider-neutral, privacy-safe diagnostics for Realtime lifecycle metadata."""

from __future__ import annotations

import queue
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .contracts import CloseDisposition, RealtimeErrorCode

_CORRELATION = re.compile(r"^corr_[0-9A-HJKMNP-TV-Z]{26}$")
_STOP_DRAIN = object()


class RealtimeDiagnosticStage(StrEnum):
    """Closed diagnostic stages; values never contain Provider payload data."""

    MINT_STARTED = "mint_started"
    MINT_COMPLETED = "mint_completed"
    MINT_FAILED = "mint_failed"
    CONNECT_STARTED = "connect_started"
    CONNECT_COMPLETED = "connect_completed"
    CONNECT_FAILED = "connect_failed"
    OPEN_STARTED = "open_started"
    OPEN_COMPLETED = "open_completed"
    OPEN_FAILED = "open_failed"
    SESSION_READY = "session_ready"
    INPUT_AUDIO = "input_audio"
    OUTPUT_AUDIO = "output_audio"
    SESSION_TERMINAL = "session_terminal"
    CONTROLLED_CLOSE_STARTED = "controlled_close_started"
    CONTROLLED_CLOSE_COMPLETED = "controlled_close_completed"
    CONTROLLED_CLOSE_TIMEOUT = "controlled_close_timeout"
    LOCAL_CONNECTING = "local_connecting"
    LOCAL_ACTIVE = "local_active"
    LOCAL_CLOSING = "local_closing"
    LOCAL_ERROR = "local_error"
    LOCAL_CLOSED = "local_closed"
    LOCAL_TIMEOUT = "local_timeout"


@dataclass(frozen=True, slots=True)
class RealtimeDiagnosticEvent:
    """One immutable event containing only the diagnostic field allowlist."""

    correlation: str
    stage: RealtimeDiagnosticStage
    stable_code: RealtimeErrorCode | None = None
    close_class: CloseDisposition | None = None
    generation: int | None = None
    frame_count: int = 0
    byte_count: int = 0
    duration_ms: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.correlation, str) or _CORRELATION.fullmatch(
            self.correlation
        ) is None:
            raise ValueError("invalid opaque correlation")
        object.__setattr__(self, "stage", RealtimeDiagnosticStage(self.stage))
        if self.stable_code is not None:
            object.__setattr__(self, "stable_code", RealtimeErrorCode(self.stable_code))
        if self.close_class is not None:
            object.__setattr__(self, "close_class", CloseDisposition(self.close_class))
        if self.generation is not None:
            _positive_integer(self.generation, "generation")
        _non_negative_integer(self.frame_count, "frame_count")
        _non_negative_integer(self.byte_count, "byte_count")
        _non_negative_integer(self.duration_ms, "duration_ms")


class RealtimeDiagnosticSink(Protocol):
    """Synchronous sink invoked outside recorder locks."""

    def emit(self, event: RealtimeDiagnosticEvent) -> None: ...


class NullRealtimeDiagnosticSink:
    """Default sink with no external side effects."""

    __slots__ = ()

    def emit(self, event: RealtimeDiagnosticEvent) -> None:
        return None


@dataclass(frozen=True, slots=True)
class RealtimeDiagnosticSnapshot:
    events: tuple[RealtimeDiagnosticEvent, ...]
    emitted_count: int
    dropped_count: int
    sink_drop_count: int
    sink_failure_count: int
    pending_count: int
    worker_count: int


class RealtimeDiagnostics:
    """Bounded diagnostic recorder whose sink can never fail a session."""

    def __init__(
        self,
        sink: RealtimeDiagnosticSink | None = None,
        *,
        max_events: int = 256,
        max_pending_events: int = 256,
    ) -> None:
        _positive_integer(max_events, "max_events")
        _positive_integer(max_pending_events, "max_pending_events")
        self._sink = sink or NullRealtimeDiagnosticSink()
        self._events: deque[RealtimeDiagnosticEvent] = deque(maxlen=max_events)
        self._emitted_count = 0
        self._sink_drop_count = 0
        self._sink_failure_count = 0
        self._lock = threading.Lock()
        self._state_changed = threading.Condition(self._lock)
        self._pending_count = 0
        self._max_queued_events = max_pending_events
        self._queued_event_count = 0
        self._closed = False
        self._queue: queue.Queue[RealtimeDiagnosticEvent | object] | None = None
        self._worker: threading.Thread | None = None
        self._stop_enqueued = False
        if sink is not None and not isinstance(sink, NullRealtimeDiagnosticSink):
            # Data admission is limited explicitly; the extra physical slot belongs
            # exclusively to the stop sentinel, so close never races a full queue.
            self._queue = queue.Queue(maxsize=max_pending_events + 1)
            self._worker = threading.Thread(
                target=self._drain,
                name="simple-harness-realtime-diagnostics",
                daemon=True,
            )
            self._worker.start()

    def emit(
        self,
        *,
        correlation: str,
        stage: RealtimeDiagnosticStage,
        stable_code: RealtimeErrorCode | None = None,
        close_class: CloseDisposition | None = None,
        generation: int | None = None,
        frame_count: int = 0,
        byte_count: int = 0,
        duration_ms: int = 0,
    ) -> RealtimeDiagnosticEvent:
        event = RealtimeDiagnosticEvent(
            correlation=correlation,
            stage=stage,
            stable_code=stable_code,
            close_class=close_class,
            generation=generation,
            frame_count=frame_count,
            byte_count=byte_count,
            duration_ms=duration_ms,
        )
        with self._state_changed:
            self._events.append(event)
            self._emitted_count += 1
            if self._queue is not None:
                if self._closed or self._queued_event_count >= self._max_queued_events:
                    self._sink_drop_count += 1
                else:
                    self._queue.put_nowait(event)
                    self._queued_event_count += 1
                    self._pending_count += 1
        return event

    def snapshot(self) -> RealtimeDiagnosticSnapshot:
        with self._lock:
            events = tuple(self._events)
            emitted_count = self._emitted_count
            sink_drop_count = self._sink_drop_count
            sink_failure_count = self._sink_failure_count
            pending_count = self._pending_count
            worker = self._worker
        return RealtimeDiagnosticSnapshot(
            events=events,
            emitted_count=emitted_count,
            dropped_count=emitted_count - len(events),
            sink_drop_count=sink_drop_count,
            sink_failure_count=sink_failure_count,
            pending_count=pending_count,
            worker_count=int(worker is not None and worker.is_alive()),
        )

    def flush(self, timeout_seconds: float = 0.0) -> bool:
        """Wait at most the explicit bound for queued sink work to finish."""

        _non_negative_timeout(timeout_seconds)
        deadline = time.monotonic() + timeout_seconds
        with self._state_changed:
            while self._pending_count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._state_changed.wait(remaining)
            return True

    def close(self, timeout_seconds: float = 0.0) -> bool:
        """Stop accepting sink work; optional waiting is bounded and explicit."""

        _non_negative_timeout(timeout_seconds)
        deadline = time.monotonic() + timeout_seconds
        with self._state_changed:
            self._closed = True
            self._state_changed.notify_all()
            worker = self._worker
            sink_queue = self._queue
            should_signal = worker is not None and not self._stop_enqueued
            if should_signal:
                self._stop_enqueued = True
        if should_signal and sink_queue is not None:
            if timeout_seconds > 0:
                self.flush(max(0.0, deadline - time.monotonic()))
            sink_queue.put_nowait(_STOP_DRAIN)
        if (
            worker is not None
            and worker is not threading.current_thread()
            and timeout_seconds > 0
        ):
            worker.join(max(0.0, deadline - time.monotonic()))
        return worker is None or not worker.is_alive()

    def _drain(self) -> None:
        assert self._queue is not None
        for queued in iter(self._queue.get, _STOP_DRAIN):
            assert isinstance(queued, RealtimeDiagnosticEvent)
            with self._state_changed:
                self._queued_event_count -= 1
            try:
                self._sink.emit(queued)
            except Exception:
                with self._state_changed:
                    self._sink_failure_count += 1
            finally:
                with self._state_changed:
                    self._pending_count -= 1
                    self._state_changed.notify_all()
                self._queue.task_done()
        self._queue.task_done()


def _positive_integer(value: int, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _non_negative_integer(value: int, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _non_negative_timeout(value: float) -> None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or value < 0
        or value == float("inf")
        or value != value
    ):
        raise ValueError("timeout_seconds must be finite and non-negative")
