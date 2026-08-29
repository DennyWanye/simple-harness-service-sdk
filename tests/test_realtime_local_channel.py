from __future__ import annotations

import asyncio
import json
import os
import struct
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from simple_harness_service.realtime.contracts import (
    CloseDisposition,
    CloseInitiator,
    OutputAudio,
    OutputAudioCompleted,
    OutputAudioStarted,
    OutputText,
    RealtimeAudioFormat,
    RealtimeError,
    RealtimeErrorCode,
    RealtimeEvent,
    RealtimeOpenRequest,
    ResponseFinished,
    ResponseStarted,
    ResponseStatus,
    ResponseUsage,
    SessionClosed,
    SessionExpiring,
    SessionFailed,
    SessionReady,
    SpeechStarted,
    SpeechStopped,
    ToolCallRequested,
    TranscriptCompleted,
    TranscriptDelta,
)
from simple_harness_service.realtime.local import (
    AudioDirection,
    LocalPcmFrame,
    decode_pcm_frame,
    encode_domain_event,
    encode_pcm_frame,
)
from simple_harness_service.realtime.local_channel import LocalRealtimeChannelController
from simple_harness_service.realtime.observability import (
    RealtimeDiagnostics,
    RealtimeDiagnosticStage,
)
from simple_harness_service.realtime.transports.loopback_websocket import (
    LOCAL_REALTIME_PATH,
    LOCAL_REALTIME_VERSION,
    LoopbackWebSocketRealtimeHost,
)
from simple_harness_service.realtime.transports.unix_local import (
    UnixRealtimeChannel,
    UnixRealtimeHost,
)

ROOT = Path(__file__).parents[1]
LOCAL = ROOT / "ARCHITECTURE/protocols/realtime-local-2026-08-27.1"
CORRELATION = "corr_0123456789ABCDEFGHJKMNPQRS"


class FakeSession:
    def __init__(self, actions: list[str] | None = None) -> None:
        self.incoming: asyncio.Queue[RealtimeEvent | None] = asyncio.Queue()
        self.audio: list[bytes] = []
        self.cancelled = 0
        self.closed = False
        self.close_events: list[RealtimeEvent] = []
        self.close_reasons: list[str] = []
        self.actions = actions

    async def events(self) -> AsyncIterator[RealtimeEvent]:
        while True:
            event = await self.incoming.get()
            if event is None:
                return
            yield event

    async def send_audio(self, pcm: bytes) -> None:
        self.audio.append(pcm)

    async def cancel_response(self) -> None:
        self.cancelled += 1

    async def truncate_output(
        self, item_id: str, content_index: int, audio_end_ms: int
    ) -> None:
        raise AssertionError("not used")

    async def submit_tool_result(self, call_id: str, output: str) -> None:
        raise AssertionError("not used")

    async def close(self, *, reason: str = "client_hangup") -> None:
        if self.closed:
            return
        self.closed = True
        self.close_reasons.append(reason)
        if self.actions is not None:
            self.actions.append("session")
        for event in self.close_events:
            await self.incoming.put(event)
        await self.incoming.put(
            SessionClosed(
                "session_closed",
                CloseInitiator.SHUTDOWN
                if reason == "app_shutdown"
                else CloseInitiator.CLIENT,
                CloseDisposition.CLEAN,
            )
        )
        await self.incoming.put(None)

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.close()


class FakeOpener:
    def __init__(self, session: FakeSession) -> None:
        self.session = session
        self.calls: list[tuple[RealtimeOpenRequest, str]] = []
        self.called = asyncio.Event()

    async def _open_with_correlation(
        self, request: RealtimeOpenRequest, correlation: str
    ) -> FakeSession:
        self.calls.append((request, correlation))
        self.called.set()
        return self.session


class FakeChannel:
    def __init__(self, actions: list[str] | None = None) -> None:
        self.incoming: asyncio.Queue[str | bytes | None] = asyncio.Queue()
        self.sent: list[str | bytes] = []
        self.bound: FakeSession | None = None
        self.closed = False
        self.actions = actions

    async def receive(self) -> str | bytes:
        value = await self.incoming.get()
        if value is None:
            raise EOFError
        return value

    async def send(self, payload: str | bytes) -> None:
        self.sent.append(payload)

    def bind_session(self, session: object) -> None:
        self.bound = session  # type: ignore[assignment]

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.actions is not None:
            self.actions.append("channel")
        await self.incoming.put(None)


class QueueSocket:
    def __init__(self, actions: list[str] | None = None) -> None:
        self.incoming: asyncio.Queue[str | bytes] = asyncio.Queue()
        self.sent: list[str | bytes] = []
        self.closed: list[tuple[int, str]] = []
        self.actions = actions

    async def recv(self) -> str | bytes:
        return await self.incoming.get()

    async def send(self, message: str | bytes) -> None:
        self.sent.append(message)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.actions is not None:
            self.actions.append("channel")
        self.closed.append((code, reason))


def _fixture(name: str) -> str:
    return (LOCAL / name).read_text()


def _ready() -> SessionReady:
    return SessionReady(
        1,
        CORRELATION,
        RealtimeAudioFormat(sample_rate=16_000),
        RealtimeAudioFormat(sample_rate=24_000),
    )


async def _wait_for(predicate: object, *, timeout: float = 0.5) -> None:
    async def wait() -> None:
        while not predicate():  # type: ignore[operator]
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), timeout=timeout)


@pytest.mark.asyncio
async def test_authority_lifecycle_correlation_ack_chunking_and_session_first_stop() -> None:
    actions: list[str] = []
    session = FakeSession(actions)
    opener = FakeOpener(session)
    channel = FakeChannel(actions)
    controller = LocalRealtimeChannelController(opener, ack_every_frames=1)
    task = asyncio.create_task(controller.run(channel))

    await channel.incoming.put(_fixture("client-hello.json"))
    await channel.incoming.put(_fixture("client-call-start.json"))
    await asyncio.wait_for(opener.called.wait(), timeout=0.5)
    assert len(opener.calls) == 1
    assert opener.calls[0][1] == CORRELATION
    await session.incoming.put(_ready())
    await _wait_for(
        lambda: any(
            isinstance(item, str) and json.loads(item).get("type") == "call.ready"
            for item in channel.sent
        )
    )

    input_vector = json.loads(_fixture("input-audio-frame.json"))
    await channel.incoming.put(bytes.fromhex(input_vector["wire_hex"]))
    await _wait_for(lambda: session.audio == [b"\x00\x00\x00\x00"])
    await _wait_for(
        lambda: any(
            isinstance(item, str)
            and json.loads(item).get("type") == "call.audio_ack"
            and json.loads(item).get("direction") == "input"
            for item in channel.sent
        )
    )

    identity = ("response-1", "item-1", 0, 0)
    await session.incoming.put(OutputAudioStarted(*identity))
    await session.incoming.put(OutputAudio(*identity, b"\x00\x00" * 150_000))
    await _wait_for(lambda: len([item for item in channel.sent if isinstance(item, bytes)]) == 2)
    binary = [decode_pcm_frame(item) for item in channel.sent if isinstance(item, bytes)]
    assert [frame.sequence for frame in binary] == [1, 2]
    assert [len(frame.payload) for frame in binary] == [262_144, 37_856]

    await channel.incoming.put(
        json.dumps(
            {
                "type": "call.audio_ack",
                "generation": 1,
                "direction": "output",
                "highest_contiguous_sequence": 2,
            }
        )
    )
    await session.incoming.put(OutputAudioCompleted(*identity))
    await session.incoming.put(OutputAudio(*identity, b"\x00\x00"))
    await _wait_for(lambda: controller.late_frame_count == 1)
    assert len([item for item in channel.sent if isinstance(item, bytes)]) == 2
    await session.incoming.put(
        ResponseFinished("response-1", ResponseStatus.COMPLETED)
    )

    await channel.incoming.put(_fixture("client-call-stop.json"))
    await asyncio.wait_for(task, timeout=0.5)
    assert actions[:2] == ["session", "channel"]
    messages = [json.loads(item) for item in channel.sent if isinstance(item, str)]
    assert [item["state"] for item in messages if item["type"] == "call.state"] == [
        "connecting",
        "active",
        "closing",
    ]
    assert messages[-1] == {
        "type": "call.closed",
        "generation": 1,
        "reason": "client_hangup",
    }


@pytest.mark.asyncio
async def test_input_batch_waits_for_full_batch_then_acks_all_local_frames() -> None:
    session = FakeSession()
    opener = FakeOpener(session)
    channel = FakeChannel()
    controller = LocalRealtimeChannelController(
        opener,
        ack_every_frames=5,
        input_batch_frames=5,
    )
    task = asyncio.create_task(controller.run(channel))
    await channel.incoming.put(_fixture("client-hello.json"))
    await channel.incoming.put(_fixture("client-call-start.json"))
    await asyncio.wait_for(opener.called.wait(), timeout=0.5)
    await session.incoming.put(_ready())
    await _wait_for(
        lambda: any(
            isinstance(item, str) and json.loads(item).get("type") == "call.ready"
            for item in channel.sent
        )
    )

    frames = [
        encode_pcm_frame(
            LocalPcmFrame(AudioDirection.INPUT, 1, sequence, bytes([sequence, 0]) * 320)
        )
        for sequence in range(1, 6)
    ]
    for frame in frames[:4]:
        await channel.incoming.put(frame)
    await asyncio.sleep(0)
    assert session.audio == []
    assert not any(
        isinstance(item, str)
        and json.loads(item).get("type") == "call.audio_ack"
        and json.loads(item).get("direction") == "input"
        for item in channel.sent
    )

    await channel.incoming.put(frames[4])
    expected = b"".join(bytes([sequence, 0]) * 320 for sequence in range(1, 6))
    await _wait_for(lambda: session.audio == [expected])
    await _wait_for(
        lambda: any(
            isinstance(item, str)
            and json.loads(item).get("type") == "call.audio_ack"
            and json.loads(item).get("highest_contiguous_sequence") == 5
            for item in channel.sent
        )
    )

    await channel.incoming.put(_fixture("client-call-stop.json"))
    await asyncio.wait_for(task, timeout=0.5)


@pytest.mark.asyncio
async def test_stop_flushes_partial_input_batch_before_closing_session() -> None:
    actions: list[str] = []
    session = FakeSession(actions)
    opener = FakeOpener(session)
    channel = FakeChannel(actions)
    controller = LocalRealtimeChannelController(opener, input_batch_frames=5)
    task = asyncio.create_task(controller.run(channel))
    await channel.incoming.put(_fixture("client-hello.json"))
    await channel.incoming.put(_fixture("client-call-start.json"))
    await asyncio.wait_for(opener.called.wait(), timeout=0.5)
    await session.incoming.put(_ready())
    await _wait_for(
        lambda: any(
            isinstance(item, str) and json.loads(item).get("type") == "call.ready"
            for item in channel.sent
        )
    )

    first = b"\x01\x00" * 320
    second = b"\x02\x00" * 320
    await channel.incoming.put(
        encode_pcm_frame(LocalPcmFrame(AudioDirection.INPUT, 1, 1, first))
    )
    await channel.incoming.put(
        encode_pcm_frame(LocalPcmFrame(AudioDirection.INPUT, 1, 2, second))
    )
    await channel.incoming.put(_fixture("client-call-stop.json"))
    await asyncio.wait_for(task, timeout=0.5)

    assert session.audio == [first + second]
    assert actions[:2] == ["session", "channel"]


def test_input_batch_frames_must_be_positive() -> None:
    with pytest.raises(ValueError):
        LocalRealtimeChannelController(FakeOpener(FakeSession()), input_batch_frames=0)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["client_hangup", "app_shutdown"])
async def test_stop_drains_cancelled_response_before_closed_and_preserves_reason(
    reason: str,
) -> None:
    actions: list[str] = []
    session = FakeSession(actions)
    session.close_events.append(
        ResponseFinished("response-active", ResponseStatus.CANCELLED, local=True)
    )
    opener = FakeOpener(session)
    channel = FakeChannel(actions)
    controller = LocalRealtimeChannelController(opener)
    task = asyncio.create_task(controller.run(channel))
    await channel.incoming.put(_fixture("client-hello.json"))
    await channel.incoming.put(_fixture("client-call-start.json"))
    await asyncio.wait_for(opener.called.wait(), timeout=0.5)
    await session.incoming.put(_ready())
    await _wait_for(
        lambda: any(
            isinstance(item, str) and json.loads(item).get("type") == "call.ready"
            for item in channel.sent
        )
    )

    await channel.incoming.put(
        json.dumps({"type": "call.stop", "generation": 1, "reason": reason})
    )
    await asyncio.wait_for(task, timeout=0.5)

    messages = [json.loads(item) for item in channel.sent if isinstance(item, str)]
    closing_index = next(
        index
        for index, message in enumerate(messages)
        if message == {"type": "call.state", "generation": 1, "state": "closing"}
    )
    response_index = next(
        index
        for index, message in enumerate(messages)
        if message.get("type") == "call.event"
        and message["event"].get("kind") == "ResponseFinished"
    )
    closed_index = next(
        index for index, message in enumerate(messages) if message.get("type") == "call.closed"
    )
    assert closing_index < response_index < closed_index
    assert messages[response_index]["event"] == {
        "kind": "ResponseFinished",
        "response_id": "response-active",
        "status": "cancelled",
        "local": True,
    }
    assert messages[closed_index] == {
        "type": "call.closed",
        "generation": 1,
        "reason": reason,
    }
    assert session.close_reasons == [reason]
    assert actions[:2] == ["session", "channel"]


def test_all_authority_call_events_encode_exact_allowed_bytes() -> None:
    fixture = json.loads(_fixture("server-call-events.json"))
    manifest = json.loads(_fixture("manifest.json"))
    events: list[RealtimeEvent] = [
        SpeechStarted("turn_fixture_1"),
        SpeechStopped("turn_fixture_1"),
        TranscriptDelta("turn_fixture_1", "Fixture"),
        TranscriptCompleted("turn_fixture_1", "Fixture transcript."),
        ResponseStarted("turn_fixture_1", "response_fixture_1"),
        OutputText(
            "response_fixture_1", "item_fixture_1", 0, 0, "Fixture response.", True
        ),
        OutputAudioStarted("response_fixture_1", "item_fixture_1", 0, 0),
        OutputAudioCompleted("response_fixture_1", "item_fixture_1", 0, 0),
        ToolCallRequested(
            "response_fixture_tool_1",
            "call_fixture_1",
            "get_weather",
            '{"city":"Hangzhou"}',
        ),
        ResponseFinished(
            "response_fixture_1",
            ResponseStatus.COMPLETED,
            ResponseUsage(1, 2, 3, 4),
        ),
        SessionExpiring(10_000),
    ]
    assert len(events) == len(fixture["messages"])
    for event, expected in zip(events, fixture["messages"], strict=True):
        encoded = encode_domain_event(1, event)
        assert encoded == json.dumps(expected, ensure_ascii=False, separators=(",", ":"))
        actual = json.loads(encoded)
        allowed = manifest["domain_event_variants"][actual["event"]["kind"]]
        assert list(actual["event"]) == allowed


def test_non_authority_domain_event_is_rejected() -> None:
    with pytest.raises(RealtimeError) as captured:
        encode_domain_event(
            1,
            SessionClosed(
                "client_hangup", CloseInitiator.CLIENT, CloseDisposition.CLEAN
            ),
        )
    assert captured.value.code is RealtimeErrorCode.INVALID_REQUEST


@pytest.mark.asyncio
async def test_output_ack_timeout_cancels_and_emits_busy_terminal() -> None:
    session = FakeSession()
    opener = FakeOpener(session)
    channel = FakeChannel()
    controller = LocalRealtimeChannelController(
        opener, producer_block_timeout_seconds=0.01
    )
    task = asyncio.create_task(controller.run(channel))
    await channel.incoming.put(_fixture("client-hello.json"))
    await channel.incoming.put(_fixture("client-call-start.json"))
    await asyncio.wait_for(opener.called.wait(), timeout=0.5)
    await session.incoming.put(_ready())
    identity = ("response-1", "item-1", 0, 0)
    await session.incoming.put(OutputAudioStarted(*identity))
    for _ in range(5):
        await session.incoming.put(OutputAudio(*identity, b"\x00\x00" * 131_072))

    await asyncio.wait_for(task, timeout=0.5)
    assert session.cancelled == 1
    messages = [json.loads(item) for item in channel.sent if isinstance(item, str)]
    assert [item["type"] for item in messages[-3:]] == [
        "call.state",
        "call.error",
        "call.closed",
    ]
    assert messages[-2]["code"] == "busy"


@pytest.mark.asyncio
async def test_stalled_upstream_audio_write_cancels_and_emits_busy_terminal() -> None:
    send_started = asyncio.Event()
    send_cancelled = asyncio.Event()

    class BlockingAudioSession(FakeSession):
        async def send_audio(self, pcm: bytes) -> None:
            send_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                send_cancelled.set()

    session = BlockingAudioSession()
    opener = FakeOpener(session)
    channel = FakeChannel()
    controller = LocalRealtimeChannelController(
        opener, producer_block_timeout_seconds=0.01
    )
    task = asyncio.create_task(controller.run(channel))
    await channel.incoming.put(_fixture("client-hello.json"))
    await channel.incoming.put(_fixture("client-call-start.json"))
    await asyncio.wait_for(opener.called.wait(), timeout=0.5)
    await session.incoming.put(_ready())
    await _wait_for(
        lambda: any(
            isinstance(item, str) and json.loads(item).get("type") == "call.ready"
            for item in channel.sent
        )
    )

    input_vector = json.loads(_fixture("input-audio-frame.json"))
    await channel.incoming.put(bytes.fromhex(input_vector["wire_hex"]))
    await asyncio.wait_for(task, timeout=0.5)

    messages = [json.loads(item) for item in channel.sent if isinstance(item, str)]
    assert send_started.is_set()
    assert send_cancelled.is_set()
    assert session.cancelled == 1
    assert [message["type"] for message in messages[-3:]] == [
        "call.state",
        "call.error",
        "call.closed",
    ]
    assert messages[-2]["code"] == "busy"
    assert messages[-1]["reason"] == "busy"


@pytest.mark.asyncio
async def test_stalled_product_write_cancels_and_emits_busy_terminal() -> None:
    send_started = asyncio.Event()
    send_cancelled = asyncio.Event()

    class BlockingOnceChannel(FakeChannel):
        def __init__(self) -> None:
            super().__init__()
            self.block_next = False

        async def send(self, payload: str | bytes) -> None:
            if self.block_next:
                self.block_next = False
                send_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    send_cancelled.set()
            await super().send(payload)

    session = FakeSession()
    opener = FakeOpener(session)
    channel = BlockingOnceChannel()
    diagnostics = RealtimeDiagnostics()
    controller = LocalRealtimeChannelController(
        opener,
        diagnostics=diagnostics,
        producer_block_timeout_seconds=0.01,
    )
    task = asyncio.create_task(controller.run(channel))
    await channel.incoming.put(_fixture("client-hello.json"))
    await channel.incoming.put(_fixture("client-call-start.json"))
    await asyncio.wait_for(opener.called.wait(), timeout=0.5)
    await session.incoming.put(_ready())
    await _wait_for(
        lambda: any(
            isinstance(item, str) and json.loads(item).get("type") == "call.ready"
            for item in channel.sent
        )
    )

    channel.block_next = True
    await session.incoming.put(
        OutputAudioStarted("response-blocked", "item-blocked", 0, 0)
    )
    await asyncio.wait_for(task, timeout=0.5)

    messages = [json.loads(item) for item in channel.sent if isinstance(item, str)]
    assert send_started.is_set()
    assert send_cancelled.is_set()
    assert session.cancelled == 1
    assert [message["type"] for message in messages[-3:]] == [
        "call.state",
        "call.error",
        "call.closed",
    ]
    assert messages[-2]["code"] == "busy"
    assert messages[-1]["reason"] == "busy"
    stages = [event.stage for event in diagnostics.snapshot().events]
    assert stages == [
        RealtimeDiagnosticStage.LOCAL_CONNECTING,
        RealtimeDiagnosticStage.LOCAL_ACTIVE,
        RealtimeDiagnosticStage.LOCAL_TIMEOUT,
        RealtimeDiagnosticStage.LOCAL_ERROR,
        RealtimeDiagnosticStage.LOCAL_CLOSED,
    ]


@pytest.mark.asyncio
async def test_barge_in_terminal_releases_output_owner_for_successor_and_drops_old_audio() -> None:
    session = FakeSession()
    opener = FakeOpener(session)
    channel = FakeChannel()
    controller = LocalRealtimeChannelController(opener)
    task = asyncio.create_task(controller.run(channel))
    await channel.incoming.put(_fixture("client-hello.json"))
    await channel.incoming.put(_fixture("client-call-start.json"))
    await asyncio.wait_for(opener.called.wait(), timeout=0.5)
    await session.incoming.put(_ready())
    await _wait_for(
        lambda: any(
            isinstance(item, str) and json.loads(item).get("type") == "call.ready"
            for item in channel.sent
        )
    )

    predecessor = ("response-old", "item-old", 0, 0)
    successor = ("response-new", "item-new", 0, 0)
    await session.incoming.put(OutputAudioStarted(*predecessor))
    await session.incoming.put(OutputAudio(*predecessor, b"\x00\x00"))
    await _wait_for(lambda: len([item for item in channel.sent if isinstance(item, bytes)]) == 1)
    await channel.incoming.put(
        json.dumps({"type": "call.barge_in", "generation": 1})
    )
    await _wait_for(lambda: session.cancelled == 1)
    await session.incoming.put(
        ResponseFinished("response-old", ResponseStatus.CANCELLED)
    )
    await session.incoming.put(OutputAudioStarted(*successor))
    await session.incoming.put(OutputAudio(*successor, b"\x01\x00"))
    await _wait_for(lambda: len([item for item in channel.sent if isinstance(item, bytes)]) == 2)
    await session.incoming.put(OutputAudioCompleted(*successor))
    await session.incoming.put(
        ResponseFinished("response-new", ResponseStatus.COMPLETED)
    )

    await session.incoming.put(OutputAudio(*predecessor, b"\x02\x00"))
    await _wait_for(lambda: controller.late_frame_count == 1)
    assert len([item for item in channel.sent if isinstance(item, bytes)]) == 2
    assert not any(
        isinstance(item, str) and json.loads(item).get("type") == "call.error"
        for item in channel.sent
    )

    await channel.incoming.put(_fixture("client-call-stop.json"))
    await asyncio.wait_for(task, timeout=0.5)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "reason"),
    [
        (RealtimeErrorCode.INVALID_REQUEST, "protocol_error"),
        (RealtimeErrorCode.UNAUTHENTICATED, "provider_error"),
        (RealtimeErrorCode.FORBIDDEN, "provider_error"),
        (RealtimeErrorCode.UNSUPPORTED, "protocol_error"),
        (RealtimeErrorCode.BUSY, "busy"),
        (RealtimeErrorCode.RATE_LIMITED, "provider_error"),
        (RealtimeErrorCode.UNAVAILABLE, "provider_error"),
        (RealtimeErrorCode.TIMEOUT, "timeout"),
        (RealtimeErrorCode.PROTOCOL_ERROR, "protocol_error"),
        (RealtimeErrorCode.SESSION_EXPIRED, "session_expired"),
        (RealtimeErrorCode.CANCELLED, "internal"),
        (RealtimeErrorCode.INTERNAL, "internal"),
    ],
)
async def test_session_failure_maps_to_authority_closed_reason(
    code: RealtimeErrorCode, reason: str
) -> None:
    session = FakeSession()
    opener = FakeOpener(session)
    channel = FakeChannel()
    controller = LocalRealtimeChannelController(opener)
    task = asyncio.create_task(controller.run(channel))
    await channel.incoming.put(_fixture("client-hello.json"))
    await channel.incoming.put(_fixture("client-call-start.json"))
    await asyncio.wait_for(opener.called.wait(), timeout=0.5)
    await session.incoming.put(_ready())
    await session.incoming.put(SessionFailed(code, code is RealtimeErrorCode.UNAVAILABLE))

    await asyncio.wait_for(task, timeout=0.5)
    messages = [json.loads(item) for item in channel.sent if isinstance(item, str)]
    assert messages[-1]["type"] == "call.closed"
    assert messages[-1]["reason"] == reason


@pytest.mark.asyncio
async def test_session_closed_unknown_reason_uses_initiator_mapping() -> None:
    session = FakeSession()
    opener = FakeOpener(session)
    channel = FakeChannel()
    controller = LocalRealtimeChannelController(opener)
    task = asyncio.create_task(controller.run(channel))
    await channel.incoming.put(_fixture("client-hello.json"))
    await channel.incoming.put(_fixture("client-call-start.json"))
    await asyncio.wait_for(opener.called.wait(), timeout=0.5)
    await session.incoming.put(_ready())
    await session.incoming.put(
        SessionClosed("session_closed", CloseInitiator.NETWORK, CloseDisposition.RETRYABLE)
    )

    await asyncio.wait_for(task, timeout=0.5)
    messages = [json.loads(item) for item in channel.sent if isinstance(item, str)]
    assert messages[-1]["reason"] == "network_error"


@pytest.mark.asyncio
async def test_loopback_host_run_uses_controller_owned_fsm() -> None:
    actions: list[str] = []
    session = FakeSession(actions)
    opener = FakeOpener(session)
    controller = LocalRealtimeChannelController(opener)
    socket = QueueSocket(actions)
    host = LoopbackWebSocketRealtimeHost("local-secret", ["tauri://localhost"])
    await socket.incoming.put(
        json.dumps(
            {
                "type": "local.auth",
                "version": LOCAL_REALTIME_VERSION,
                "secret": "local-secret",
            }
        )
    )
    await socket.incoming.put(_fixture("client-hello.json"))
    await socket.incoming.put(_fixture("client-call-start.json"))
    task = asyncio.create_task(
        host.run(
            socket,
            path=LOCAL_REALTIME_PATH,
            origin="tauri://localhost",
            peer_host="127.0.0.1",
            handler=controller.run,
        )
    )
    await asyncio.wait_for(opener.called.wait(), timeout=0.5)
    await session.incoming.put(_ready())
    await _wait_for(
        lambda: any(
            isinstance(item, str) and json.loads(item).get("type") == "call.ready"
            for item in socket.sent
        )
    )
    await socket.incoming.put(_fixture("client-call-stop.json"))
    await asyncio.wait_for(task, timeout=0.5)
    await host.close()
    assert actions[:2] == ["session", "channel"]
    assert len(opener.calls) == 1


@pytest.fixture
def short_socket_dir() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="shrt-fsm-", dir="/tmp") as value:
        path = Path(value)
        os.chmod(path, 0o700)
        yield path


async def _unix_send(writer: asyncio.StreamWriter, payload: str | bytes) -> None:
    encoded = payload.encode() if isinstance(payload, str) else payload
    writer.write(struct.pack("!I", len(encoded)) + encoded)
    await writer.drain()


async def _unix_receive(reader: asyncio.StreamReader) -> str | bytes:
    size = struct.unpack("!I", await reader.readexactly(4))[0]
    payload = await reader.readexactly(size)
    return payload if payload.startswith(b"SHRT") else payload.decode()


@pytest.mark.asyncio
async def test_real_unix_accepted_channel_runs_controller_lifecycle(
    short_socket_dir: Path,
) -> None:
    actions: list[str] = []
    session = FakeSession(actions)
    opener = FakeOpener(session)
    controller = LocalRealtimeChannelController(opener)
    socket_path = short_socket_dir / "voice.sock"

    async def handler(channel: UnixRealtimeChannel) -> None:
        await controller.run(channel)

    host = UnixRealtimeHost(socket_path, handler)
    await host.start()
    reader, writer = await asyncio.open_unix_connection(socket_path)
    await _unix_send(writer, _fixture("client-hello.json"))
    await _unix_send(writer, _fixture("client-call-start.json"))
    assert json.loads(await _unix_receive(reader))["state"] == "connecting"
    await asyncio.wait_for(opener.called.wait(), timeout=0.5)
    await session.incoming.put(_ready())
    assert json.loads(await _unix_receive(reader))["type"] == "call.ready"
    assert json.loads(await _unix_receive(reader))["state"] == "active"
    await _unix_send(writer, _fixture("client-call-stop.json"))
    assert json.loads(await _unix_receive(reader))["state"] == "closing"
    assert json.loads(await _unix_receive(reader))["type"] == "call.closed"
    assert await asyncio.wait_for(reader.read(), timeout=0.5) == b""
    await host.close()
    writer.close()
    await writer.wait_closed()
    assert actions[0] == "session"
