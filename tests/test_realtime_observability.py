from __future__ import annotations

import dataclasses
import threading
import time
from typing import Any, cast

import pytest

from simple_harness_service.realtime.contracts import (
    CloseDisposition,
    RealtimeErrorCode,
)
from simple_harness_service.realtime.observability import (
    RealtimeDiagnosticEvent,
    RealtimeDiagnostics,
    RealtimeDiagnosticStage,
)

CORRELATION = "corr_0123456789ABCDEFGHJKMNPQRS"


def test_diagnostic_event_is_immutable_and_has_exact_allowlist() -> None:
    event = RealtimeDiagnosticEvent(
        correlation=CORRELATION,
        stage=RealtimeDiagnosticStage.SESSION_TERMINAL,
        stable_code=RealtimeErrorCode.TIMEOUT,
        close_class=CloseDisposition.RETRYABLE,
        generation=1,
        frame_count=2,
        byte_count=4,
        duration_ms=8,
    )

    assert tuple(field.name for field in dataclasses.fields(event)) == (
        "correlation",
        "stage",
        "stable_code",
        "close_class",
        "generation",
        "frame_count",
        "byte_count",
        "duration_ms",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.byte_count = 5  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        event.secret = "forbidden"  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("generation", 0),
        ("generation", True),
        ("frame_count", -1),
        ("byte_count", True),
        ("duration_ms", -1),
    ],
)
def test_diagnostic_event_rejects_invalid_integer_metadata(
    field: str, value: object
) -> None:
    values: dict[str, object] = {
        "correlation": CORRELATION,
        "stage": RealtimeDiagnosticStage.SESSION_READY,
        field: value,
    }
    with pytest.raises(ValueError):
        RealtimeDiagnosticEvent(**values)  # type: ignore[arg-type]


def test_diagnostics_default_sink_is_noop_and_snapshot_is_bounded() -> None:
    diagnostics = RealtimeDiagnostics(max_events=2)
    for generation in (1, 2, 3):
        diagnostics.emit(
            correlation=CORRELATION,
            stage=RealtimeDiagnosticStage.LOCAL_ACTIVE,
            generation=generation,
        )

    snapshot = diagnostics.snapshot()
    assert tuple(event.generation for event in snapshot.events) == (2, 3)
    assert snapshot.emitted_count == 3
    assert snapshot.dropped_count == 1
    assert snapshot.sink_drop_count == 0
    assert snapshot.sink_failure_count == 0
    assert snapshot.pending_count == 0
    assert snapshot.worker_count == 0
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.emitted_count = 4  # type: ignore[misc]


def test_sink_failure_and_secret_exception_never_escape_or_enter_snapshot() -> None:
    class FailingSink:
        def emit(self, event: RealtimeDiagnosticEvent) -> None:
            raise RuntimeError("api-key bearer raw-audio transcript instructions")

    diagnostics = RealtimeDiagnostics(FailingSink())
    event = diagnostics.emit(
        correlation=CORRELATION,
        stage=RealtimeDiagnosticStage.MINT_FAILED,
        stable_code=RealtimeErrorCode.TIMEOUT,
        duration_ms=10,
    )

    assert event.stable_code is RealtimeErrorCode.TIMEOUT
    assert diagnostics.flush(0.5)
    snapshot = diagnostics.snapshot()
    assert snapshot.sink_failure_count == 1
    rendered = repr(snapshot)
    for forbidden in (
        "api-key",
        "bearer",
        "raw-audio",
        "transcript",
        "instructions",
    ):
        assert forbidden not in rendered
    assert diagnostics.close(0.5)


def test_slow_sink_never_blocks_emit_hot_path() -> None:
    started = threading.Event()
    release = threading.Event()

    class SlowSink:
        def emit(self, event: RealtimeDiagnosticEvent) -> None:
            started.set()
            release.wait(1)

    diagnostics = RealtimeDiagnostics(SlowSink())
    started_at = time.monotonic()
    diagnostics.emit(
        correlation=CORRELATION,
        stage=RealtimeDiagnosticStage.INPUT_AUDIO,
        generation=1,
        frame_count=1,
        byte_count=640,
    )
    duration = time.monotonic() - started_at

    assert duration < 0.05
    assert started.wait(0.5)
    release.set()
    assert diagnostics.flush(0.5)
    assert diagnostics.close(0.5)


def test_sink_queue_overflow_drops_without_blocking_producer() -> None:
    started = threading.Event()
    release = threading.Event()

    class BlockingSink:
        def emit(self, event: RealtimeDiagnosticEvent) -> None:
            started.set()
            release.wait(1)

    diagnostics = RealtimeDiagnostics(BlockingSink(), max_pending_events=1)
    diagnostics.emit(
        correlation=CORRELATION,
        stage=RealtimeDiagnosticStage.INPUT_AUDIO,
    )
    assert started.wait(0.5)
    diagnostics.emit(
        correlation=CORRELATION,
        stage=RealtimeDiagnosticStage.OUTPUT_AUDIO,
    )
    diagnostics.emit(
        correlation=CORRELATION,
        stage=RealtimeDiagnosticStage.SESSION_TERMINAL,
    )

    snapshot = diagnostics.snapshot()
    assert snapshot.emitted_count == 3
    assert snapshot.sink_drop_count == 1
    assert snapshot.pending_count == 2
    release.set()
    assert diagnostics.flush(0.5)
    assert diagnostics.close(0.5)


def test_diagnostics_close_releases_daemon_worker() -> None:
    observed: list[RealtimeDiagnosticEvent] = []

    class CollectingSink:
        def emit(self, event: RealtimeDiagnosticEvent) -> None:
            observed.append(event)

    diagnostics = RealtimeDiagnostics(CollectingSink())
    diagnostics.emit(
        correlation=CORRELATION,
        stage=RealtimeDiagnosticStage.OPEN_COMPLETED,
    )

    assert diagnostics.close(0.5)
    assert diagnostics.snapshot().worker_count == 0
    assert len(observed) == 1


def test_close_reserves_sentinel_slot_when_worker_and_data_queue_are_full() -> None:
    started = threading.Event()
    release = threading.Event()
    observed: list[RealtimeDiagnosticStage] = []

    class BlockingSink:
        def emit(self, event: RealtimeDiagnosticEvent) -> None:
            observed.append(event.stage)
            if len(observed) == 1:
                started.set()
                release.wait(1)

    diagnostics = RealtimeDiagnostics(BlockingSink(), max_pending_events=1)
    diagnostics.emit(
        correlation=CORRELATION,
        stage=RealtimeDiagnosticStage.INPUT_AUDIO,
    )
    assert started.wait(0.5)
    diagnostics.emit(
        correlation=CORRELATION,
        stage=RealtimeDiagnosticStage.OUTPUT_AUDIO,
    )

    close_results: list[bool] = []
    close_thread = threading.Thread(target=lambda: close_results.append(diagnostics.close()))
    close_thread.start()
    close_thread.join(0.5)

    assert not close_thread.is_alive()
    assert close_results == [False]
    assert diagnostics._queue is not None
    assert diagnostics._queue.qsize() == 2
    assert diagnostics.snapshot().sink_drop_count == 0

    release.set()
    assert diagnostics.flush(0.5)
    assert diagnostics.close(0.5)
    assert observed == [
        RealtimeDiagnosticStage.INPUT_AUDIO,
        RealtimeDiagnosticStage.OUTPUT_AUDIO,
    ]


def test_diagnostic_api_cannot_accept_content_or_secret_fields() -> None:
    diagnostics = RealtimeDiagnostics()
    emit = cast(Any, diagnostics.emit)
    for forbidden in ("secret", "api_key", "bearer", "audio", "text", "instructions"):
        with pytest.raises(TypeError):
            emit(
                correlation=CORRELATION,
                stage=RealtimeDiagnosticStage.OPEN_FAILED,
                **{forbidden: "do-not-store"},
            )
    with pytest.raises(ValueError, match="opaque correlation"):
        diagnostics.emit(
            correlation="corr_secret_bearer",
            stage=RealtimeDiagnosticStage.OPEN_FAILED,
        )
