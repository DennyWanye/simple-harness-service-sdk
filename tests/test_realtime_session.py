from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from simple_harness_service.realtime.adapters.qwen_omni import (
    QWEN_CAPABILITY,
    QwenOmniAdapter,
)
from simple_harness_service.realtime.client import RealtimeClient
from simple_harness_service.realtime.contracts import (
    CloseDisposition,
    CloseInitiator,
    MintedRealtimeCredential,
    OutputAudio,
    OutputAudioStarted,
    RealtimeError,
    RealtimeErrorCode,
    RealtimeEvent,
    RealtimeOpenRequest,
    RealtimeProfile,
    ResponseFinished,
    ResponseStarted,
    ResponseStatus,
    SessionClosed,
    SessionFailed,
    SessionReady,
    ToolCallRequested,
)
from simple_harness_service.realtime.observability import (
    RealtimeDiagnostics,
    RealtimeDiagnosticStage,
)
from simple_harness_service.realtime.relay_control import RelayControlCodec

ROOT = Path(__file__).parents[1]
QWEN = ROOT / "ARCHITECTURE/protocols/qwen-native-2026-08-28.2"
CONTROL = ROOT / "ARCHITECTURE/protocols/tokenseller-realtime-control-2026-08-28.2"


class FakeConnection:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[str | None] = asyncio.Queue()
        self.sent: list[str] = []
        self.closed = 0
        self.block_audio: asyncio.Event | None = None
        self.block_close: asyncio.Event | None = None
        self.bind_created_to_open = True
        self.auto_close_ack = True
        self.mismatched_close_ack = False
        self.block_types: dict[str, asyncio.Event] = {}
        self.fail_once_types: set[str] = set()

    async def send_text(self, payload: str) -> None:
        self.sent.append(payload)
        value = json.loads(payload)
        event_type = value.get("type")
        if event_type in self.fail_once_types:
            self.fail_once_types.remove(event_type)
            raise OSError("fixture write failure")
        blocker = self.block_types.get(str(event_type))
        if blocker is not None:
            await blocker.wait()
        if value.get("type") == "tokenseller.session.close" and self.auto_close_ack:
            acknowledgement = json.loads(
                (CONTROL / "server-session-closed.json").read_text()
            )
            acknowledgement["related_event_id"] = (
                "ctl_close_mismatch"
                if self.mismatched_close_ack
                else value["event_id"]
            )
            await self.incoming.put(json.dumps(acknowledgement))
        if (
            value.get("type") == "input_audio_buffer.append"
            and self.block_audio is not None
        ):
            await self.block_audio.wait()

    async def receive_text(self) -> str | None:
        payload = await self.incoming.get()
        if payload is None or not self.bind_created_to_open:
            return payload
        value = json.loads(payload)
        if value.get("type") != "tokenseller.session.created":
            return payload
        open_event = next(
            (
                json.loads(sent)
                for sent in reversed(self.sent)
                if json.loads(sent).get("type") == "tokenseller.session.open"
            ),
            None,
        )
        if open_event is not None:
            value["related_event_id"] = open_event["event_id"]
        return json.dumps(value)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        del code, reason
        self.closed += 1
        if self.block_close is not None:
            await self.block_close.wait()


class FakeTransport:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.calls: list[tuple[str, str]] = []

    async def connect(self, websocket_path: str, bearer_token: str) -> FakeConnection:
        self.calls.append((websocket_path, bearer_token))
        return self.connection


class FakeMinter:
    def __init__(self, credential: MintedRealtimeCredential) -> None:
        self.credential = credential
        self.correlations: list[str] = []

    async def mint(
        self,
        profile: RealtimeProfile,
        request: RealtimeOpenRequest,
        correlation: str,
    ) -> MintedRealtimeCredential:
        del profile, request
        self.correlations.append(correlation)
        return self.credential


def _profile() -> RealtimeProfile:
    return RealtimeProfile(
        "qwen-production",
        "qwen",
        "qwen-native",
        "2026-08-28.2",
        "qwen3.5-omni-realtime",
        "Tina",
        QWEN_CAPABILITY,
    )


def _credential() -> MintedRealtimeCredential:
    return RelayControlCodec().parse_mint_response(
        (CONTROL / "mint-response.json").read_text(), _profile()
    )


async def _open(
    connection: FakeConnection,
    *,
    tool_ack_timeout: float = 5.0,
    close_timeout: float = 5.0,
    open_timeout: float = 5.0,
    write_timeout: float = 2.0,
    diagnostics: RealtimeDiagnostics | None = None,
) -> Any:
    await connection.incoming.put((CONTROL / "server-session-created.json").read_text())
    client = RealtimeClient(
        _profile(),
        FakeMinter(_credential()),
        FakeTransport(connection),
        QwenOmniAdapter(),
        diagnostics=diagnostics,
        open_timeout=open_timeout,
        write_timeout=write_timeout,
        tool_ack_timeout=tool_ack_timeout,
        close_timeout=close_timeout,
    )
    return await client.open(RealtimeOpenRequest("session", "instructions"))


async def _ready(connection: FakeConnection, session: Any, stream: Any) -> SessionReady:
    lifecycle = json.loads((QWEN / "server-lifecycle-sequence.json").read_text())
    updated = lifecycle["scenarios"][0]["events"][0]
    await connection.incoming.put(json.dumps(updated))
    event = await asyncio.wait_for(anext(stream), 1)
    assert isinstance(event, SessionReady)
    assert event.generation == session.generation
    return event


async def _request_tool_call(
    connection: FakeConnection,
    stream: Any,
    *,
    call_id: str,
    arguments: str,
) -> ToolCallRequested:
    suffix = call_id.replace("_", "-")
    await connection.incoming.put(
        json.dumps(
            {
                "event_id": f"event_tool_response_{suffix}",
                "type": "response.created",
                "response": {"id": "resp_tool", "status": "in_progress"},
            }
        )
    )
    await connection.incoming.put(
        json.dumps(
            {
                "event_id": f"event_tool_item_{suffix}",
                "type": "response.output_item.added",
                "response_id": "resp_tool",
                "output_index": 0,
                "item": {
                    "id": "item_tool",
                    "type": "function_call",
                    "call_id": call_id,
                },
            }
        )
    )
    await connection.incoming.put(
        json.dumps(
            {
                "event_id": f"event_tool_request_{suffix}",
                "type": "response.function_call_arguments.done",
                "response_id": "resp_tool",
                "item_id": "item_tool",
                "output_index": 0,
                "call_id": call_id,
                "name": "get_weather",
                "arguments": arguments,
            }
        )
    )
    assert isinstance(await asyncio.wait_for(anext(stream), 1), ResponseStarted)
    requested = await asyncio.wait_for(anext(stream), 1)
    assert isinstance(requested, ToolCallRequested)
    return requested


@pytest.mark.asyncio
async def test_client_open_send_audio_and_idempotent_clean_close() -> None:
    connection = FakeConnection()
    session = await _open(connection)
    stream = session.events()
    await _ready(connection, session, stream)

    await session.send_audio(b"\x00\x00" * 320)
    assert any(
        json.loads(payload).get("type") == "input_audio_buffer.append"
        for payload in connection.sent
    )
    await session.close()
    terminal = await asyncio.wait_for(anext(stream), 1)
    assert isinstance(terminal, SessionClosed)
    await session.close()
    assert connection.closed == 1


@pytest.mark.asyncio
async def test_client_session_observability_wires_lifecycle_audio_and_terminal() -> None:
    diagnostics = RealtimeDiagnostics()
    connection = FakeConnection()
    session = await _open(connection, diagnostics=diagnostics)
    stream = session.events()
    await _ready(connection, session, stream)
    await session.send_audio(b"\x00\x00" * 320)

    lifecycle = json.loads((QWEN / "server-lifecycle-sequence.json").read_text())
    scenario = next(
        item
        for item in lifecycle["scenarios"]
        if item["name"] == "audio_response_completed"
    )
    for event in scenario["events"]:
        await connection.incoming.put(json.dumps(event))
    observed: list[RealtimeEvent] = []
    while not observed or not isinstance(observed[-1], ResponseFinished):
        observed.append(await asyncio.wait_for(anext(stream), 1))

    await session.close()
    assert isinstance(await asyncio.wait_for(anext(stream), 1), SessionClosed)
    snapshot = diagnostics.snapshot()
    stages = [event.stage for event in snapshot.events]
    for expected in (
        RealtimeDiagnosticStage.OPEN_STARTED,
        RealtimeDiagnosticStage.MINT_COMPLETED,
        RealtimeDiagnosticStage.CONNECT_COMPLETED,
        RealtimeDiagnosticStage.OPEN_COMPLETED,
        RealtimeDiagnosticStage.SESSION_READY,
        RealtimeDiagnosticStage.INPUT_AUDIO,
        RealtimeDiagnosticStage.OUTPUT_AUDIO,
        RealtimeDiagnosticStage.CONTROLLED_CLOSE_STARTED,
        RealtimeDiagnosticStage.SESSION_TERMINAL,
        RealtimeDiagnosticStage.CONTROLLED_CLOSE_COMPLETED,
    ):
        assert expected in stages
    input_event = next(
        event
        for event in snapshot.events
        if event.stage is RealtimeDiagnosticStage.INPUT_AUDIO
    )
    output_event = next(
        event
        for event in snapshot.events
        if event.stage is RealtimeDiagnosticStage.OUTPUT_AUDIO
    )
    assert (input_event.frame_count, input_event.byte_count) == (1, 640)
    assert output_event.frame_count >= 1
    assert output_event.byte_count >= 2
    assert {event.correlation for event in snapshot.events} == {session.correlation}
    assert "instructions" not in repr(snapshot)


@pytest.mark.asyncio
async def test_observability_sink_failure_does_not_change_session_lifecycle() -> None:
    class FailingSink:
        def emit(self, event: object) -> None:
            raise RuntimeError("secret exception body")

    diagnostics = RealtimeDiagnostics(FailingSink())
    connection = FakeConnection()
    session = await _open(connection, diagnostics=diagnostics)
    stream = session.events()
    await _ready(connection, session, stream)
    await session.close()

    assert isinstance(await asyncio.wait_for(anext(stream), 1), SessionClosed)
    assert diagnostics.flush(0.5)
    snapshot = diagnostics.snapshot()
    assert snapshot.sink_failure_count == snapshot.emitted_count
    assert snapshot.emitted_count > 0
    assert "secret exception body" not in repr(snapshot)
    assert diagnostics.close(0.5)


@pytest.mark.asyncio
async def test_terminal_sequence_remains_observable_with_one_output_slot() -> None:
    connection = FakeConnection()
    session = await _open(connection)
    session.capability = replace(
        session.capability,
        limits=replace(session.capability.limits, output_queue_frames=1),
    )
    stream = session.events()
    await _ready(connection, session, stream)
    await connection.incoming.put(
        json.dumps(
            {
                "event_id": "event_single_slot_response",
                "type": "response.created",
                "response": {"id": "resp_single_slot", "status": "in_progress"},
            }
        )
    )
    assert isinstance(await asyncio.wait_for(anext(stream), 1), ResponseStarted)

    await session.close()

    response_terminal = await asyncio.wait_for(anext(stream), 1)
    assert response_terminal == ResponseFinished(
        "resp_single_slot", ResponseStatus.CANCELLED, None, local=True
    )
    assert isinstance(await asyncio.wait_for(anext(stream), 1), SessionClosed)
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


@pytest.mark.asyncio
async def test_connection_close_and_receiver_shutdown_are_bounded_and_idempotent() -> None:
    connection = FakeConnection()
    connection.block_close = asyncio.Event()
    session = await _open(connection, close_timeout=0.01)
    stream = session.events()
    await _ready(connection, session, stream)

    await asyncio.wait_for(
        asyncio.gather(session.close(), session.close()),
        timeout=0.2,
    )

    assert connection.closed == 1
    assert isinstance(await asyncio.wait_for(anext(stream), 1), SessionClosed)
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


@pytest.mark.asyncio
async def test_controlled_close_ack_timeout_remains_clean_and_is_observable() -> None:
    diagnostics = RealtimeDiagnostics()
    connection = FakeConnection()
    connection.auto_close_ack = False
    session = await _open(
        connection, close_timeout=0.01, diagnostics=diagnostics
    )
    stream = session.events()
    await _ready(connection, session, stream)

    await session.close()

    assert await asyncio.wait_for(anext(stream), 1) == SessionClosed(
        "client_hangup",
        initiator=CloseInitiator.CLIENT,
        disposition=CloseDisposition.CLEAN,
    )
    assert session.close_ack_timeout_count == 1
    assert session.close_diagnostics == {
        "close_ack_timeout": 1,
        "close_ack_mismatch": 0,
    }
    timeout_event = next(
        event
        for event in diagnostics.snapshot().events
        if event.stage is RealtimeDiagnosticStage.CONTROLLED_CLOSE_TIMEOUT
    )
    assert timeout_event.stable_code is RealtimeErrorCode.TIMEOUT
    assert timeout_event.frame_count == 1
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


@pytest.mark.asyncio
async def test_controlled_close_ignores_mismatched_ack_then_times_out_clean() -> None:
    connection = FakeConnection()
    connection.mismatched_close_ack = True
    session = await _open(connection, close_timeout=0.05)
    stream = session.events()
    await _ready(connection, session, stream)

    await session.close()

    assert isinstance(await asyncio.wait_for(anext(stream), 1), SessionClosed)
    assert session.close_ack_timeout_count == 1
    assert session.close_ack_mismatch_count == 1


@pytest.mark.asyncio
async def test_physical_close_after_clean_intent_cannot_steal_terminal() -> None:
    connection = FakeConnection()
    connection.auto_close_ack = False
    session = await _open(connection, close_timeout=0.1)
    stream = session.events()
    await _ready(connection, session, stream)
    close_task = asyncio.create_task(session.close())
    while not any(
        json.loads(payload).get("type") == "tokenseller.session.close"
        for payload in connection.sent
    ):
        await asyncio.sleep(0)
    await connection.incoming.put(None)

    await asyncio.wait_for(close_task, 0.2)

    assert isinstance(await asyncio.wait_for(anext(stream), 1), SessionClosed)
    assert session.close_ack_timeout_count == 1


@pytest.mark.asyncio
async def test_physical_close_before_clean_intent_is_retryable_unavailable() -> None:
    connection = FakeConnection()
    session = await _open(connection)
    stream = session.events()
    await _ready(connection, session, stream)

    await connection.incoming.put(None)

    assert await asyncio.wait_for(anext(stream), 1) == SessionFailed(
        RealtimeErrorCode.UNAVAILABLE, True
    )


@pytest.mark.asyncio
async def test_active_response_terminals_wait_for_matching_close_ack() -> None:
    connection = FakeConnection()
    connection.auto_close_ack = False
    session = await _open(connection, close_timeout=0.1)
    stream = session.events()
    await _ready(connection, session, stream)
    await connection.incoming.put(
        json.dumps(
            {
                "event_id": "event_active_before_close",
                "type": "response.created",
                "response": {"id": "resp_active_close", "status": "in_progress"},
            }
        )
    )
    assert isinstance(await asyncio.wait_for(anext(stream), 1), ResponseStarted)

    close_task = asyncio.create_task(session.close())
    while not any(
        json.loads(payload).get("type") == "tokenseller.session.close"
        for payload in connection.sent
    ):
        await asyncio.sleep(0)
    await asyncio.sleep(0.01)
    assert session._events.empty()
    close_frame = next(
        json.loads(payload)
        for payload in connection.sent
        if json.loads(payload).get("type") == "tokenseller.session.close"
    )
    acknowledgement = json.loads((CONTROL / "server-session-closed.json").read_text())
    acknowledgement["related_event_id"] = close_frame["event_id"]
    await connection.incoming.put(json.dumps(acknowledgement))
    await close_task

    assert await asyncio.wait_for(anext(stream), 1) == ResponseFinished(
        "resp_active_close",
        ResponseStatus.CANCELLED,
        None,
        local=True,
    )
    assert isinstance(await asyncio.wait_for(anext(stream), 1), SessionClosed)


@pytest.mark.asyncio
async def test_app_shutdown_rejects_wrong_ack_owner_but_stays_clean_on_timeout() -> None:
    connection = FakeConnection()
    connection.auto_close_ack = False
    session = await _open(connection, close_timeout=0.01)
    stream = session.events()
    await _ready(connection, session, stream)
    close_task = asyncio.create_task(session.close(reason="app_shutdown"))
    while not any(
        json.loads(payload).get("type") == "tokenseller.session.close"
        for payload in connection.sent
    ):
        await asyncio.sleep(0)
    close_frame = next(
        json.loads(payload)
        for payload in connection.sent
        if json.loads(payload).get("type") == "tokenseller.session.close"
    )
    acknowledgement = json.loads((CONTROL / "server-session-closed.json").read_text())
    acknowledgement.update(
        {
            "related_event_id": close_frame["event_id"],
            "initiator": "provider",
            "disposition": "retryable",
        }
    )
    await connection.incoming.put(json.dumps(acknowledgement))
    await close_task

    terminal = await asyncio.wait_for(anext(stream), 1)
    assert terminal == SessionClosed(
        "app_shutdown",
        CloseInitiator.CLIENT,
        CloseDisposition.CLEAN,
    )
    assert session.close_ack_mismatch_count == 1
    assert session.close_ack_timeout_count == 1


@pytest.mark.asyncio
async def test_local_controller_correlation_reaches_mint_open_and_session() -> None:
    correlation = "corr_0123456789ABCDEFGHJKMNPQRS"
    connection = FakeConnection()
    await connection.incoming.put((CONTROL / "server-session-created.json").read_text())
    minter = FakeMinter(_credential())
    client = RealtimeClient(
        _profile(), minter, FakeTransport(connection), QwenOmniAdapter()
    )

    session = await client._open_with_correlation(
        RealtimeOpenRequest("session", "instructions"), correlation
    )

    assert minter.correlations == [correlation]
    assert json.loads(connection.sent[0])["correlation"] == correlation
    assert session.correlation == correlation
    await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "correlation",
    [
        "",
        "corr_short",
        "corr_0123456789ABCDEFGHJKMNPQRU",
        "corr_0123456789abcdefghjkmnpqrs",
        "trace_0123456789ABCDEFGHJKMNPQRS",
    ],
)
async def test_local_controller_correlation_rejects_invalid_values(
    correlation: str,
) -> None:
    connection = FakeConnection()
    minter = FakeMinter(_credential())
    client = RealtimeClient(
        _profile(), minter, FakeTransport(connection), QwenOmniAdapter()
    )

    with pytest.raises(RealtimeError) as raised:
        await client._open_with_correlation(
            RealtimeOpenRequest("session", "instructions"), correlation
        )

    assert raised.value.code is RealtimeErrorCode.INVALID_REQUEST
    assert minter.correlations == []


@pytest.mark.asyncio
async def test_local_controller_correlation_cannot_be_reused() -> None:
    correlation = "corr_0123456789ABCDEFGHJKMNPQRS"
    connection = FakeConnection()
    await connection.incoming.put((CONTROL / "server-session-created.json").read_text())
    minter = FakeMinter(_credential())
    client = RealtimeClient(
        _profile(), minter, FakeTransport(connection), QwenOmniAdapter()
    )
    request = RealtimeOpenRequest("session", "instructions")

    session = await client._open_with_correlation(request, correlation)
    with pytest.raises(RealtimeError) as raised:
        await client._open_with_correlation(request, correlation)

    assert raised.value.code is RealtimeErrorCode.INVALID_REQUEST
    assert minter.correlations == [correlation]
    await session.close()


@pytest.mark.asyncio
async def test_client_rejects_created_for_a_different_open_event() -> None:
    connection = FakeConnection()
    connection.bind_created_to_open = False
    await connection.incoming.put((CONTROL / "server-session-created.json").read_text())

    with pytest.raises(RealtimeError) as caught:
        await _open(connection)

    assert caught.value.code is RealtimeErrorCode.PROTOCOL_ERROR
    assert connection.closed == 1


@pytest.mark.asyncio
async def test_unknown_response_audio_fails_once_instead_of_cross_binding() -> None:
    connection = FakeConnection()
    session = await _open(connection)
    stream = session.events()
    await _ready(connection, session, stream)
    await connection.incoming.put((QWEN / "server-audio-delta.json").read_text())
    terminal = await asyncio.wait_for(anext(stream), 1)
    assert terminal == SessionFailed(RealtimeErrorCode.PROTOCOL_ERROR, False)
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


@pytest.mark.asyncio
async def test_response_identity_tombstone_drops_duplicate_terminal_and_late_audio() -> None:
    connection = FakeConnection()
    session = await _open(connection)
    stream = session.events()
    await _ready(connection, session, stream)
    lifecycle = json.loads((QWEN / "server-lifecycle-sequence.json").read_text())
    scenario = next(
        item
        for item in lifecycle["scenarios"]
        if item["name"] == "audio_response_completed"
    )
    for event in scenario["events"]:
        await connection.incoming.put(json.dumps(event))

    observed: list[RealtimeEvent] = []
    while not observed or not isinstance(observed[-1], ResponseFinished):
        observed.append(await asyncio.wait_for(anext(stream), 1))
    assert isinstance(observed[0], ResponseStarted)
    assert any(isinstance(event, OutputAudioStarted) for event in observed)
    assert any(isinstance(event, OutputAudio) for event in observed)
    assert observed[-1].status is ResponseStatus.COMPLETED

    done = json.loads((QWEN / "server-response-done.json").read_text())
    done["event_id"] = "event_duplicate_terminal_new_id"
    done["response"]["id"] = "resp_audio_1"
    await connection.incoming.put(json.dumps(done))
    late = json.loads((QWEN / "server-audio-delta.json").read_text())
    late["event_id"] = "event_late_audio_new_id"
    late.update({"response_id": "resp_audio_1", "item_id": "item_audio_1"})
    await connection.incoming.put(json.dumps(late))
    for _ in range(100):
        if session.duplicate_event_count >= 1 and session.late_event_count >= 1:
            break
        await asyncio.sleep(0)
    assert session.duplicate_event_count >= 1
    assert session.late_event_count >= 1
    await session.close()


@pytest.mark.asyncio
async def test_failed_response_orders_response_terminal_before_session_terminal() -> None:
    connection = FakeConnection()
    session = await _open(connection)
    stream = session.events()
    await _ready(connection, session, stream)
    await connection.incoming.put(
        json.dumps(
            {
                "event_id": "event_response_created",
                "type": "response.created",
                "response": {"id": "resp_failed", "status": "in_progress"},
            }
        )
    )
    assert isinstance(await asyncio.wait_for(anext(stream), 1), ResponseStarted)
    await connection.incoming.put(
        json.dumps(
            {
                "event_id": "event_response_failed",
                "type": "response.done",
                "response": {"id": "resp_failed", "status": "failed", "usage": None},
            }
        )
    )
    response_terminal = await asyncio.wait_for(anext(stream), 1)
    session_terminal = await asyncio.wait_for(anext(stream), 1)
    assert response_terminal == ResponseFinished("resp_failed", ResponseStatus.FAILED)
    assert session_terminal == SessionFailed(RealtimeErrorCode.PROTOCOL_ERROR, False)


@pytest.mark.asyncio
async def test_qwen_incomplete_projects_cancelled_only_with_local_cancel_owner() -> None:
    connection = FakeConnection()
    session = await _open(connection)
    stream = session.events()
    await _ready(connection, session, stream)
    await connection.incoming.put(
        json.dumps(
            {
                "event_id": "event_cancel_response_created",
                "type": "response.created",
                "response": {"id": "resp_cancel", "status": "in_progress"},
            }
        )
    )
    assert isinstance(await asyncio.wait_for(anext(stream), 1), ResponseStarted)
    await session.cancel_response()
    terminal_matrix = json.loads(
        (QWEN / "server-response-terminal-matrix.json").read_text()
    )
    cancelled = next(
        case for case in terminal_matrix["cases"] if case["name"] == "client_cancelled"
    )["wire_events"][0]
    cancelled["event_id"] = "event_cancel_terminal"
    cancelled["response"]["id"] = "resp_cancel"
    await connection.incoming.put(json.dumps(cancelled))
    terminal = await asyncio.wait_for(anext(stream), 1)
    assert isinstance(terminal, ResponseFinished)
    assert terminal.response_id == "resp_cancel"
    assert terminal.status is ResponseStatus.CANCELLED
    await session.close()


@pytest.mark.asyncio
async def test_barge_in_allows_one_successor_but_rejects_a_third_live_response() -> None:
    connection = FakeConnection()
    session = await _open(connection)
    stream = session.events()
    await _ready(connection, session, stream)
    await connection.incoming.put(
        json.dumps(
            {
                "event_id": "event_barge_old",
                "type": "response.created",
                "response": {"id": "resp_old", "status": "in_progress"},
            }
        )
    )
    assert isinstance(await asyncio.wait_for(anext(stream), 1), ResponseStarted)
    await session.cancel_response()
    await connection.incoming.put(
        json.dumps(
            {
                "event_id": "event_barge_successor",
                "type": "response.created",
                "response": {"id": "resp_successor", "status": "in_progress"},
            }
        )
    )
    successor = await asyncio.wait_for(anext(stream), 1)
    assert isinstance(successor, ResponseStarted)
    assert successor.response_id == "resp_successor"
    await connection.incoming.put(
        json.dumps(
            {
                "event_id": "event_barge_third",
                "type": "response.created",
                "response": {"id": "resp_third", "status": "in_progress"},
            }
        )
    )
    assert await asyncio.wait_for(anext(stream), 1) == SessionFailed(
        RealtimeErrorCode.PROTOCOL_ERROR, False
    )


@pytest.mark.asyncio
async def test_output_queue_overflow_fails_busy_without_unbounded_buffering() -> None:
    connection = FakeConnection()
    session = await _open(connection)
    stream = session.events()
    await _ready(connection, session, stream)
    for index in range(QWEN_CAPABILITY.limits.output_queue_frames + 1):
        await connection.incoming.put(
            json.dumps(
                {
                    "event_id": f"event_transcript_{index}",
                    "type": "conversation.item.input_audio_transcription.delta",
                    "item_id": "item_user",
                    "content_index": 0,
                    "text": "x",
                }
            )
        )
    for _ in range(1_000):
        if session._terminal_owner is not None:
            break
        await asyncio.sleep(0)
    assert await asyncio.wait_for(anext(stream), 1) == SessionFailed(
        RealtimeErrorCode.BUSY, False
    )


@pytest.mark.asyncio
async def test_transcript_json_counts_toward_output_byte_limit() -> None:
    connection = FakeConnection()
    session = await _open(connection)
    session.capability = replace(
        session.capability,
        limits=replace(session.capability.limits, output_queue_bytes=300),
    )
    stream = session.events()
    await _ready(connection, session, stream)

    await connection.incoming.put(
        json.dumps(
            {
                "event_id": "event_large_transcript",
                "type": "conversation.item.input_audio_transcription.delta",
                "item_id": "item_user",
                "content_index": 0,
                "text": "x" * 400,
            }
        )
    )

    assert await asyncio.wait_for(anext(stream), 1) == SessionFailed(
        RealtimeErrorCode.BUSY, False
    )


@pytest.mark.asyncio
async def test_client_close_wins_race_with_late_provider_error() -> None:
    connection = FakeConnection()
    session = await _open(connection)
    stream = session.events()
    await _ready(connection, session, stream)
    close_task = asyncio.create_task(session.close())
    while not any(
        json.loads(payload).get("type") == "tokenseller.session.close"
        for payload in connection.sent
    ):
        await asyncio.sleep(0)
    await connection.incoming.put(
        json.dumps(
            {
                "event_id": "event_late_provider_error",
                "type": "error",
                "error": {"type": "ModelServingError", "code": "ModelServingError"},
            }
        )
    )
    await close_task
    assert isinstance(await asyncio.wait_for(anext(stream), 1), SessionClosed)
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


@pytest.mark.asyncio
async def test_input_queue_backpressure_is_bounded() -> None:
    connection = FakeConnection()
    connection.block_audio = asyncio.Event()
    session = await _open(connection)
    stream = session.events()
    await _ready(connection, session, stream)
    pending = [
        asyncio.create_task(session.send_audio(b"\x00\x00"))
        for _ in range(QWEN_CAPABILITY.limits.input_queue_frames)
    ]
    while sum(
        json.loads(payload).get("type") == "input_audio_buffer.append"
        for payload in connection.sent
    ) < QWEN_CAPABILITY.limits.input_queue_frames:
        await asyncio.sleep(0)
    with pytest.raises(RealtimeError) as caught:
        await session.send_audio(b"\x00\x00")
    assert caught.value.code is RealtimeErrorCode.BUSY
    connection.block_audio.set()
    await asyncio.gather(*pending)
    await session.close()


@pytest.mark.asyncio
async def test_tool_result_waits_for_ack_then_requests_followup() -> None:
    connection = FakeConnection()
    session = await _open(connection)
    stream = session.events()
    await _ready(connection, session, stream)
    requested = await _request_tool_call(
        connection,
        stream,
        call_id="call_1",
        arguments='{"city":"Hangzhou"}',
    )
    assert isinstance(requested, ToolCallRequested)
    submission = asyncio.create_task(session.submit_tool_result("call_1", '{"temp":21}'))
    while not any(
        json.loads(payload).get("type") == "conversation.item.create"
        for payload in connection.sent
    ):
        await asyncio.sleep(0)
    await connection.incoming.put(
        json.dumps(
            {
                "event_id": "event_tool_ack",
                "type": "conversation.item.created",
                "item": {
                    "id": "item_result",
                    "type": "function_call_output",
                    "status": "completed",
                    "call_id": "call_1",
                    "output": '{"temp":21}',
                },
            }
        )
    )
    await asyncio.wait_for(submission, 1)
    assert any(json.loads(payload).get("type") == "response.create" for payload in connection.sent)
    await session.close()


@pytest.mark.asyncio
async def test_concurrent_identical_tool_submissions_share_one_result_and_followup() -> None:
    connection = FakeConnection()
    session = await _open(connection)
    stream = session.events()
    await _ready(connection, session, stream)
    requested = await _request_tool_call(
        connection,
        stream,
        call_id="call_concurrent",
        arguments="{}",
    )
    assert isinstance(requested, ToolCallRequested)
    submissions = [
        asyncio.create_task(session.submit_tool_result("call_concurrent", "{}"))
        for _ in range(2)
    ]
    while sum(
        json.loads(payload).get("type") == "conversation.item.create"
        for payload in connection.sent
    ) < 1:
        await asyncio.sleep(0)
    await connection.incoming.put(
        json.dumps(
            {
                "event_id": "event_tool_ack_concurrent",
                "type": "conversation.item.created",
                "item": {
                    "id": "item_result",
                    "type": "function_call_output",
                    "status": "completed",
                    "call_id": "call_concurrent",
                    "output": "{}",
                },
            }
        )
    )

    await asyncio.wait_for(asyncio.gather(*submissions), 1)

    sent_types = [json.loads(payload).get("type") for payload in connection.sent]
    assert sent_types.count("conversation.item.create") == 1
    assert sent_types.count("response.create") == 1
    await session.close()


@pytest.mark.asyncio
async def test_ambiguous_tool_ack_timeout_is_fatal_and_does_not_resend() -> None:
    connection = FakeConnection()
    session = await _open(connection, tool_ack_timeout=0.01)
    stream = session.events()
    await _ready(connection, session, stream)
    requested = await _request_tool_call(
        connection,
        stream,
        call_id="call_timeout",
        arguments="{}",
    )
    assert isinstance(requested, ToolCallRequested)
    with pytest.raises(RealtimeError) as caught:
        await session.submit_tool_result("call_timeout", "{}")
    assert caught.value.code is RealtimeErrorCode.PROTOCOL_ERROR
    terminal = await asyncio.wait_for(anext(stream), 1)
    assert terminal == SessionFailed(RealtimeErrorCode.PROTOCOL_ERROR, False)


@pytest.mark.asyncio
async def test_response_before_negotiated_session_update_fails_closed() -> None:
    connection = FakeConnection()
    session = await _open(connection)
    stream = session.events()
    await connection.incoming.put(
        json.dumps(
            {
                "event_id": "event_response_before_ready",
                "type": "response.created",
                "response": {"id": "resp_early", "status": "in_progress"},
            }
        )
    )

    assert await asyncio.wait_for(anext(stream), 1) == SessionFailed(
        RealtimeErrorCode.PROTOCOL_ERROR,
        False,
    )


@pytest.mark.asyncio
async def test_item_done_before_added_fails_closed() -> None:
    connection = FakeConnection()
    session = await _open(connection)
    stream = session.events()
    await _ready(connection, session, stream)
    await connection.incoming.put(
        json.dumps(
            {
                "event_id": "event_response_for_early_done",
                "type": "response.created",
                "response": {"id": "resp_early_done", "status": "in_progress"},
            }
        )
    )
    assert isinstance(await asyncio.wait_for(anext(stream), 1), ResponseStarted)
    await connection.incoming.put(
        json.dumps(
            {
                "event_id": "event_item_done_before_added",
                "type": "response.output_item.done",
                "response_id": "resp_early_done",
                "output_index": 0,
                "item": {"id": "item_early", "type": "message"},
            }
        )
    )

    assert await asyncio.wait_for(anext(stream), 1) == SessionFailed(
        RealtimeErrorCode.PROTOCOL_ERROR,
        False,
    )


@pytest.mark.asyncio
async def test_output_text_requires_introduced_content_identity() -> None:
    connection = FakeConnection()
    session = await _open(connection)
    stream = session.events()
    await _ready(connection, session, stream)
    await connection.incoming.put(
        json.dumps(
            {
                "event_id": "event_response_for_text",
                "type": "response.created",
                "response": {"id": "resp_text_guard", "status": "in_progress"},
            }
        )
    )
    await connection.incoming.put(
        json.dumps(
            {
                "event_id": "event_item_for_text",
                "type": "response.output_item.added",
                "response_id": "resp_text_guard",
                "output_index": 0,
                "item": {"id": "item_text_guard", "type": "message"},
            }
        )
    )
    assert isinstance(await asyncio.wait_for(anext(stream), 1), ResponseStarted)
    await connection.incoming.put(
        json.dumps(
            {
                "event_id": "event_text_without_content",
                "type": "response.text.delta",
                "response_id": "resp_text_guard",
                "item_id": "item_text_guard",
                "output_index": 0,
                "content_index": 0,
                "delta": "unsafe ordering",
            }
        )
    )

    assert await asyncio.wait_for(anext(stream), 1) == SessionFailed(
        RealtimeErrorCode.PROTOCOL_ERROR,
        False,
    )


@pytest.mark.asyncio
async def test_content_done_before_added_fails_closed() -> None:
    connection = FakeConnection()
    session = await _open(connection)
    stream = session.events()
    await _ready(connection, session, stream)
    await connection.incoming.put(
        json.dumps(
            {
                "event_id": "event_response_for_content_done",
                "type": "response.created",
                "response": {"id": "resp_content_done", "status": "in_progress"},
            }
        )
    )
    await connection.incoming.put(
        json.dumps(
            {
                "event_id": "event_item_for_content_done",
                "type": "response.output_item.added",
                "response_id": "resp_content_done",
                "output_index": 0,
                "item": {"id": "item_content_done", "type": "message"},
            }
        )
    )
    assert isinstance(await asyncio.wait_for(anext(stream), 1), ResponseStarted)
    await connection.incoming.put(
        json.dumps(
            {
                "event_id": "event_content_done_before_added",
                "type": "response.content_part.done",
                "response_id": "resp_content_done",
                "item_id": "item_content_done",
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "text", "text": ""},
            }
        )
    )

    assert await asyncio.wait_for(anext(stream), 1) == SessionFailed(
        RealtimeErrorCode.PROTOCOL_ERROR,
        False,
    )


@pytest.mark.asyncio
async def test_duplicate_tool_request_with_new_event_id_fails_closed() -> None:
    connection = FakeConnection()
    session = await _open(connection)
    stream = session.events()
    await _ready(connection, session, stream)
    await _request_tool_call(connection, stream, call_id="call_duplicate", arguments="{}")
    await connection.incoming.put(
        json.dumps(
            {
                "event_id": "event_tool_request_duplicate_new_id",
                "type": "response.function_call_arguments.done",
                "response_id": "resp_tool",
                "item_id": "item_tool",
                "output_index": 0,
                "call_id": "call_duplicate",
                "name": "get_weather",
                "arguments": "{}",
            }
        )
    )

    assert await asyncio.wait_for(anext(stream), 1) == SessionFailed(
        RealtimeErrorCode.PROTOCOL_ERROR,
        False,
    )


@pytest.mark.asyncio
async def test_oversize_tool_arguments_fail_closed() -> None:
    connection = FakeConnection()
    session = await _open(connection)
    stream = session.events()
    await _ready(connection, session, stream)
    await connection.incoming.put(
        json.dumps(
            {
                "event_id": "event_tool_response_oversize",
                "type": "response.created",
                "response": {"id": "resp_tool", "status": "in_progress"},
            }
        )
    )
    await connection.incoming.put(
        json.dumps(
            {
                "event_id": "event_tool_item_oversize",
                "type": "response.output_item.added",
                "response_id": "resp_tool",
                "output_index": 0,
                "item": {"id": "item_tool", "type": "function_call"},
            }
        )
    )
    assert isinstance(await asyncio.wait_for(anext(stream), 1), ResponseStarted)
    await connection.incoming.put(
        json.dumps(
            {
                "event_id": "event_tool_request_oversize",
                "type": "response.function_call_arguments.done",
                "response_id": "resp_tool",
                "item_id": "item_tool",
                "output_index": 0,
                "call_id": "call_oversize",
                "name": "get_weather",
                "arguments": "x" * (QWEN_CAPABILITY.limits.tool_payload_bytes + 1),
            }
        )
    )

    assert await asyncio.wait_for(anext(stream), 1) == SessionFailed(
        RealtimeErrorCode.PROTOCOL_ERROR,
        False,
    )


@pytest.mark.asyncio
async def test_tool_ack_must_match_frozen_output_identity() -> None:
    connection = FakeConnection()
    session = await _open(connection, tool_ack_timeout=0.02)
    stream = session.events()
    await _ready(connection, session, stream)
    await _request_tool_call(connection, stream, call_id="call_ack_guard", arguments="{}")
    submission = asyncio.create_task(
        session.submit_tool_result("call_ack_guard", '{"ok":true}')
    )
    while not any(
        json.loads(payload).get("type") == "conversation.item.create"
        for payload in connection.sent
    ):
        await asyncio.sleep(0)
    await connection.incoming.put(
        json.dumps(
            {
                "event_id": "event_tool_ack_wrong_output",
                "type": "conversation.item.created",
                "item": {
                    "id": "item_result_wrong",
                    "type": "function_call_output",
                    "status": "completed",
                    "call_id": "call_ack_guard",
                    "output": '{"ok":false}',
                },
            }
        )
    )

    assert await asyncio.wait_for(anext(stream), 1) == SessionFailed(
        RealtimeErrorCode.PROTOCOL_ERROR,
        False,
    )
    with pytest.raises(RealtimeError) as caught:
        await submission
    assert caught.value.code is RealtimeErrorCode.PROTOCOL_ERROR


@pytest.mark.asyncio
async def test_tool_result_send_retry_reuses_exact_payload_identity() -> None:
    connection = FakeConnection()
    connection.fail_once_types.add("conversation.item.create")
    session = await _open(connection)
    stream = session.events()
    await _ready(connection, session, stream)
    await _request_tool_call(connection, stream, call_id="call_retry", arguments="{}")

    with pytest.raises(RealtimeError) as first:
        await session.submit_tool_result("call_retry", "{}")
    assert first.value.code is RealtimeErrorCode.UNAVAILABLE

    retry = asyncio.create_task(session.submit_tool_result("call_retry", "{}"))
    while sum(
        json.loads(payload).get("type") == "conversation.item.create"
        for payload in connection.sent
    ) < 2:
        await asyncio.sleep(0)
    await connection.incoming.put(
        json.dumps(
            {
                "event_id": "event_tool_ack_retry",
                "type": "conversation.item.created",
                "item": {
                    "id": "item_result_retry",
                    "type": "function_call_output",
                    "status": "completed",
                    "call_id": "call_retry",
                    "output": "{}",
                },
            }
        )
    )
    await asyncio.wait_for(retry, 1)
    result_payloads = [
        payload
        for payload in connection.sent
        if json.loads(payload).get("type") == "conversation.item.create"
    ]
    assert len(result_payloads) == 2
    assert result_payloads[0] == result_payloads[1]
    await session.close()


@pytest.mark.asyncio
async def test_followup_send_retry_reuses_exact_payload_identity() -> None:
    connection = FakeConnection()
    connection.fail_once_types.add("response.create")
    session = await _open(connection)
    stream = session.events()
    await _ready(connection, session, stream)
    await _request_tool_call(connection, stream, call_id="call_followup", arguments="{}")
    first = asyncio.create_task(session.submit_tool_result("call_followup", "{}"))
    while not any(
        json.loads(payload).get("type") == "conversation.item.create"
        for payload in connection.sent
    ):
        await asyncio.sleep(0)
    await connection.incoming.put(
        json.dumps(
            {
                "event_id": "event_tool_ack_followup",
                "type": "conversation.item.created",
                "item": {
                    "id": "item_result_followup",
                    "type": "function_call_output",
                    "status": "completed",
                    "call_id": "call_followup",
                    "output": "{}",
                },
            }
        )
    )
    with pytest.raises(RealtimeError) as failed:
        await first
    assert failed.value.code is RealtimeErrorCode.UNAVAILABLE

    await session.submit_tool_result("call_followup", "{}")
    followups = [
        payload
        for payload in connection.sent
        if json.loads(payload).get("type") == "response.create"
    ]
    assert len(followups) == 2
    assert followups[0] == followups[1]
    await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_type", ["tokenseller.session.open", "session.update"])
async def test_open_writes_are_bounded_and_close_on_timeout(blocked_type: str) -> None:
    connection = FakeConnection()
    connection.block_types[blocked_type] = asyncio.Event()

    with pytest.raises(RealtimeError) as caught:
        await _open(
            connection,
            open_timeout=0.01,
            write_timeout=0.01,
            close_timeout=0.01,
        )

    assert caught.value.code is RealtimeErrorCode.TIMEOUT
    assert connection.closed == 1


@pytest.mark.asyncio
async def test_runtime_audio_write_timeout_is_bounded_and_terminal() -> None:
    connection = FakeConnection()
    session = await _open(connection, write_timeout=0.01, close_timeout=0.01)
    stream = session.events()
    await _ready(connection, session, stream)
    connection.block_types["input_audio_buffer.append"] = asyncio.Event()

    with pytest.raises(RealtimeError) as caught:
        await session.send_audio(b"\x00\x00")

    assert caught.value.code is RealtimeErrorCode.TIMEOUT
    assert await asyncio.wait_for(anext(stream), 1) == SessionFailed(
        RealtimeErrorCode.TIMEOUT,
        True,
    )
