from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from typing import Any

import pytest
from test_realtime_session import (
    CONTROL,
    QWEN,
    FakeConnection,
    FakeMinter,
    FakeTransport,
    _credential,
    _profile,
)

from simple_harness_service.realtime.adapters.openai import OpenAIRealtimeAdapter
from simple_harness_service.realtime.adapters.qwen_omni import QwenOmniAdapter
from simple_harness_service.realtime.client import RealtimeClient
from simple_harness_service.realtime.contracts import (
    OutputAudio,
    RealtimeError,
    RealtimeErrorCode,
    RealtimeOpenRequest,
    SessionFailed,
    ToolCallRequested,
)
from simple_harness_service.realtime.local import decode_local_message
from simple_harness_service.realtime.local_channel import LocalRealtimeChannelController
from simple_harness_service.realtime.ports import DecodedProviderEvent
from simple_harness_service.realtime.relay_control import RelayControlCodec
from simple_harness_service.realtime.session import ManagedRealtimeSession

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "get_weather",
        "description": "Look up weather",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
            "additionalProperties": False,
        },
    }
]


class FakeOpener:
    async def _open_with_correlation(self, request: RealtimeOpenRequest, correlation: str) -> Any:
        raise AssertionError("not used")


class FakeChannel:
    def __init__(self) -> None:
        self.sent: list[str | bytes] = []

    async def send(self, payload: str | bytes) -> None:
        self.sent.append(payload)


def _ack() -> dict[str, Any]:
    return json.loads((QWEN / "server-lifecycle-sequence.json").read_text())["scenarios"][0][
        "events"
    ][0]


@pytest.mark.parametrize("adapter", [QwenOmniAdapter(), OpenAIRealtimeAdapter()])
def test_tool_catalog_is_encoded(adapter: Any) -> None:
    request = RealtimeOpenRequest("session", "instructions", tools=deepcopy(TOOLS))
    assert adapter.session_update(request)["session"]["tools"] == TOOLS


@pytest.mark.parametrize("mutation", ["name", "parameters", "required", "unknown", "duplicate"])
def test_invalid_tool_catalog_is_rejected(mutation: str) -> None:
    tools = deepcopy(TOOLS)
    if mutation == "name":
        tools[0]["name"] = ""
    elif mutation == "parameters":
        tools[0]["parameters"] = {"type": "invalid"}
    elif mutation == "required":
        tools[0]["parameters"]["required"] = [3]
    elif mutation == "unknown":
        tools[0]["parameters"]["$ref"] = "https://example.invalid/schema"
    else:
        tools.append(deepcopy(tools[0]))
    with pytest.raises(ValueError):
        RealtimeOpenRequest("session", "instructions", tools=tools)


@pytest.mark.parametrize("echo", ["equal", "missing", "different"])
def test_qwen_tool_catalog_acknowledgement(echo: str) -> None:
    adapter = QwenOmniAdapter()
    adapter.session_update(RealtimeOpenRequest("session", "instructions", tools=TOOLS))
    ack = _ack()
    if echo != "missing":
        ack["session"]["tools"] = TOOLS if echo == "equal" else []
    if echo == "equal":
        assert adapter.decode_server_event(json.dumps(ack)).tools_acknowledged
    else:
        with pytest.raises(RealtimeError) as caught:
            adapter.decode_server_event(json.dumps(ack))
        assert caught.value.code is RealtimeErrorCode.UNSUPPORTED


@pytest.mark.parametrize("adapter", [QwenOmniAdapter(), OpenAIRealtimeAdapter()])
def test_context_encoding(adapter: Any) -> None:
    message = adapter.append_message("task finished", role="system")
    assert message["type"] == "conversation.item.create"
    assert message["item"]["role"] == "system"
    assert message["item"]["content"] == [{"type": "input_text", "text": "task finished"}]
    update = adapter.update_instructions("new instructions")
    if isinstance(adapter, QwenOmniAdapter):
        assert update["session"] == {"instructions": "new instructions"}
        with pytest.raises(RealtimeError) as caught:
            adapter.out_of_band_response("brief summary")
        assert caught.value.code is RealtimeErrorCode.UNSUPPORTED
    else:
        assert update["session"]["instructions"] == "new instructions"
        assert adapter.followup_response("")["response"]["instructions"] == "new instructions"
        assert adapter.out_of_band_response("brief summary")["response"] == {
            "instructions": "brief summary",
            "conversation": "none",
        }
    assert json.loads(adapter.encode_client_event(message))["item"] == message["item"]


class Dispatcher:
    def __init__(self) -> None:
        self.calls: list[ToolCallRequested] = []

    async def dispatch(self, call: ToolCallRequested) -> str:
        self.calls.append(call)
        return "sunny"


def _session(connection: FakeConnection, **kwargs: Any) -> ManagedRealtimeSession:
    return ManagedRealtimeSession(
        connection=connection,
        adapter=kwargs.pop("adapter", QwenOmniAdapter()),
        control=RelayControlCodec(),
        credential=_credential(),
        request=RealtimeOpenRequest("session", "instructions", tools=deepcopy(TOOLS)),
        correlation="corr_0123456789ABCDEFGHJKMNPQRS",
        generation=1,
        close_timeout=0.01,
        tool_ack_timeout=0.1,
        **kwargs,
    )


async def _settle() -> None:
    for _ in range(30):
        await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments,executed",
    [
        ('{"city":"Taipei"}', True),
        ('{"city":3}', False),
        ("{bad", False),
        ('{"city":"Taipei","extra":1}', False),
        ('{"city":NaN}', False),
        ('{"city":"A","city":"B"}', False),
        ("[]", False),
        ("x" * 65537, False),
    ],
)
async def test_automatic_tool_loop_validates_before_dispatch(
    arguments: str, executed: bool
) -> None:
    connection = FakeConnection()
    dispatcher = Dispatcher()
    session = _session(connection, tool_dispatcher=dispatcher)
    session._ready = True
    session._responses.add("response")
    session._function_call_items.add(("response", "item", 0))
    call = ToolCallRequested("response", "call", "get_weather", arguments)
    assert await session._apply(
        DecodedProviderEvent(
            "event_call", (call,), tool_call_identity=("response", "item", 0, "call")
        )
    )
    await _settle()
    assert bool(dispatcher.calls) is executed
    result = json.loads(connection.sent[0])["item"]
    assert result["output"] == "sunny" if executed else "error" in json.loads(result["output"])
    assert not any(json.loads(event)["type"] == "response.create" for event in connection.sent)
    assert await session._apply(
        DecodedProviderEvent(
            "event_ack",
            tool_ack_identity=(
                "result",
                "function_call_output",
                "completed",
                "call",
                result["output"],
            ),
        )
    )
    await _settle()
    assert json.loads(connection.sent[-1])["type"] == "response.create"
    await session.close()


@pytest.mark.asyncio
async def test_old_generation_and_reconnect_replay_never_dispatch() -> None:
    dispatcher = Dispatcher()
    seen: set[str] = set()
    for current in [False, True, True]:
        connection = FakeConnection()
        session = _session(
            connection,
            tool_dispatcher=dispatcher,
            seen_tool_calls=seen,
            is_current_generation=lambda current=current: current,
        )
        session._ready = True
        session._responses.add("response")
        session._function_call_items.add(("response", "item", 0))
        await session._apply(
            DecodedProviderEvent(
                "event_call",
                (ToolCallRequested("response", "call", "get_weather", '{"city":"Taipei"}'),),
                tool_call_identity=("response", "item", 0, "call"),
            )
        )
        await _settle()
        assert len(dispatcher.calls) <= 1
        await session.close()
    assert len(dispatcher.calls) == 1
    assert dispatcher.calls[0].generation == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["append_message", "update_instructions"])
@pytest.mark.parametrize("adapter", [QwenOmniAdapter(), OpenAIRealtimeAdapter()])
async def test_injection_rejection_is_unsupported_and_diagnostic(method: str, adapter: Any) -> None:
    connection = FakeConnection()
    session = _session(connection, adapter=adapter)
    await session.start()
    session._ready = True
    operation = asyncio.create_task(getattr(session, method)("context", trigger_response=True))
    await _settle()
    event = json.loads(connection.sent[-1])
    await connection.incoming.put(
        json.dumps(
            {
                "type": "error",
                "event_id": "event_rejected",
                "error": {
                    "type": "invalid_request_error",
                    "code": "invalid_event",
                    "event_id": event["event_id"],
                },
            }
        )
    )
    with pytest.raises(RealtimeError) as caught:
        await asyncio.wait_for(operation, 1)
    assert caught.value.code is RealtimeErrorCode.UNSUPPORTED
    assert "response.create" not in [json.loads(e)["type"] for e in connection.sent]
    assert any(
        event.operation_kind == "context.unsupported"
        and event.stable_code is RealtimeErrorCode.UNSUPPORTED
        for event in session.diagnostics_snapshot().events
    )
    await session.close()


@pytest.mark.asyncio
async def test_tool_catalog_limit_uses_negotiated_limits() -> None:
    session = _session(FakeConnection())
    session.capability = replace(
        session.capability, limits=replace(session.capability.limits, tool_payload_bytes=32)
    )
    with pytest.raises(RealtimeError) as caught:
        await session.start()
    assert caught.value.code is RealtimeErrorCode.INVALID_REQUEST


@pytest.mark.asyncio
async def test_playback_position_and_sent_statistics() -> None:
    controller = LocalRealtimeChannelController(FakeOpener())
    controller._channel = FakeChannel()
    controller._generation = 1
    controller._ready = True
    await controller._send_output_audio(b"\0\0" * 2400)
    ack = {
        "type": "call.audio_ack",
        "generation": 1,
        "direction": "output",
        "highest_contiguous_sequence": 1,
        "played_audio_ms": 75,
    }
    assert decode_local_message(json.dumps(ack))["played_audio_ms"] == 75
    await controller._handle_output_ack(ack)
    snapshot = controller.playback_snapshot()
    assert snapshot.sent_audio_bytes == 4800
    assert snapshot.sent_audio_ms == 100
    assert snapshot.played_audio_ms == 75
    ack["played_audio_ms"] = 101
    with pytest.raises(RealtimeError):
        await controller._handle_output_ack(ack)


@pytest.mark.asyncio
async def test_stale_output_generation_is_not_sent() -> None:
    controller = LocalRealtimeChannelController(FakeOpener())
    channel = FakeChannel()
    controller._channel = channel
    controller._generation = 2
    await controller._send_output_audio(b"\0\0", generation=1)
    assert not channel.sent


def test_qwen_unrelated_error_keeps_original_code() -> None:
    adapter = QwenOmniAdapter()
    event = adapter.decode_server_event(
        json.dumps(
            {
                "type": "error",
                "event_id": "event_unrelated",
                "error": {"type": "invalid_request_error", "code": "invalid_event"},
            }
        )
    )
    assert event.events == (SessionFailed(RealtimeErrorCode.INVALID_REQUEST, False),)


def test_qwen_uncorrelated_unsupported_error_during_single_context_operation() -> None:
    adapter = QwenOmniAdapter()
    request = adapter.append_message("context")
    event = adapter.decode_server_event(
        json.dumps(
            {
                "type": "error",
                "event_id": "event_unsupported",
                "error": {"type": "invalid_request_error", "code": "invalid_event"},
            }
        )
    )
    assert event.rejected_event_id == request["event_id"]
    assert event.events == (SessionFailed(RealtimeErrorCode.UNSUPPORTED, False),)


def test_qwen_auth_failure_during_context_is_not_unsupported() -> None:
    adapter = QwenOmniAdapter()
    adapter.append_message("context")
    event = adapter.decode_server_event(
        json.dumps(
            {
                "type": "error",
                "event_id": "event_auth",
                "error": {"type": "authentication_error", "code": "invalid_api_key"},
            }
        )
    )
    assert event.events == (SessionFailed(RealtimeErrorCode.UNAUTHENTICATED, False),)


@pytest.mark.parametrize("adapter", [QwenOmniAdapter(), OpenAIRealtimeAdapter()])
def test_empty_tool_arguments_reach_validator(adapter: Any) -> None:
    decoded = adapter.decode_server_event(
        json.dumps(
            {
                "type": "response.function_call_arguments.done",
                "event_id": "event_empty",
                "response_id": "response",
                "item_id": "item",
                "output_index": 0,
                "call_id": "call",
                "name": "get_weather",
                "arguments": "",
            }
        )
    )
    assert decoded.events[0].arguments_json == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["append_message", "update_instructions"])
@pytest.mark.parametrize("trigger", [True, False])
@pytest.mark.parametrize("adapter", [QwenOmniAdapter(), OpenAIRealtimeAdapter()])
async def test_context_waits_for_ack_before_optional_response(
    method: str, trigger: bool, adapter: Any
) -> None:
    connection = FakeConnection()
    session = _session(connection, adapter=adapter)
    await session.start()
    session._ready = True
    operation = asyncio.create_task(getattr(session, method)("context", trigger_response=trigger))
    await _settle()
    request = json.loads(connection.sent[-1])
    assert not operation.done()
    if method == "append_message":
        ack = {
            "type": "conversation.item.created",
            "event_id": "event_context_ack",
            "item": {**request["item"], "status": "completed"},
        }
    else:
        ack = {
            "type": "session.updated",
            "event_id": "event_context_ack",
            "session": {"instructions": "context"},
        }
    await connection.incoming.put(json.dumps(ack))
    await asyncio.wait_for(operation, 1)
    followups = [e for e in connection.sent if json.loads(e)["type"] == "response.create"]
    assert len(followups) == int(trigger)
    await session.close()


@pytest.mark.asyncio
async def test_invalid_call_is_not_exposed_to_manual_dispatch() -> None:
    session = _session(FakeConnection())
    session._ready = True
    session._responses.add("response")
    session._function_call_items.add(("response", "item", 0))
    await session._apply(
        DecodedProviderEvent(
            "event_call",
            (ToolCallRequested("response", "bad", "get_weather", "{}"),),
            tool_call_identity=("response", "item", 0, "bad"),
        )
    )
    assert session._events.empty()
    await session.close()


@pytest.mark.asyncio
async def test_played_position_cannot_exceed_acknowledged_frames() -> None:
    controller = LocalRealtimeChannelController(FakeOpener())
    controller._channel = FakeChannel()
    controller._generation = 1
    controller._ready = True
    await controller._send_output_audio(b"\0\0" * 2400)
    with pytest.raises(RealtimeError):
        await controller._handle_output_ack(
            {
                "direction": "output",
                "highest_contiguous_sequence": 0,
                "played_audio_ms": 50,
            }
        )


@pytest.mark.asyncio
async def test_generation_change_while_dispatching_drops_result() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    current = True

    class SlowDispatcher:
        async def dispatch(self, call: ToolCallRequested) -> str:
            started.set()
            await release.wait()
            return "finished"

    connection = FakeConnection()
    session = _session(
        connection, tool_dispatcher=SlowDispatcher(), is_current_generation=lambda: current
    )
    session._ready = True
    session._responses.add("response")
    session._function_call_items.add(("response", "item", 0))
    await session._apply(
        DecodedProviderEvent(
            "event_call",
            (ToolCallRequested("response", "call", "get_weather", '{"city":"Taipei"}'),),
            tool_call_identity=("response", "item", 0, "call"),
        )
    )
    await asyncio.wait_for(started.wait(), 1)
    current = False
    release.set()
    await _settle()
    assert not connection.sent
    await session.close()


@pytest.mark.asyncio
async def test_context_timeout_is_terminal_without_retry() -> None:
    connection = FakeConnection()
    session = _session(connection)
    session._tool_ack_timeout = 0.001
    session._ready = True
    with pytest.raises(RealtimeError) as caught:
        await session.append_message("context", trigger_response=True)
    assert caught.value.code is RealtimeErrorCode.TIMEOUT
    assert len(connection.sent) == 1
    assert session._terminal_published.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "behavior,expected",
    [
        ("exception", "tool_failed"),
        ("timeout", "tool_timeout"),
        ("oversize", "invalid_tool_output"),
    ],
)
async def test_dispatch_failures_return_stable_tool_error(behavior: str, expected: str) -> None:
    class FailingDispatcher:
        async def dispatch(self, call: ToolCallRequested) -> str:
            if behavior == "timeout":
                await asyncio.Event().wait()
            if behavior == "exception":
                raise RuntimeError("private exception detail")
            return "x" * 65537

    connection = FakeConnection()
    session = _session(connection, tool_dispatcher=FailingDispatcher(), tool_dispatch_timeout=0.001)
    session._ready = True
    session._responses.add("response")
    session._function_call_items.add(("response", "item", 0))
    await session._apply(
        DecodedProviderEvent(
            "event_call",
            (ToolCallRequested("response", "call", "get_weather", '{"city":"Taipei"}'),),
            tool_call_identity=("response", "item", 0, "call"),
        )
    )

    async def wait_for_output() -> dict[str, Any]:
        while not connection.sent:
            await asyncio.sleep(0)
        return dict(json.loads(connection.sent[0])["item"])

    item = await asyncio.wait_for(wait_for_output(), 1)
    assert json.loads(item["output"]) == {"error": expected}
    assert "private exception detail" not in str(session.diagnostics_snapshot())
    assert len(connection.sent) == 1
    await session.close()


@pytest.mark.asyncio
async def test_reconnect_dedup_is_scoped_to_external_session() -> None:
    dispatcher = Dispatcher()
    transport = FakeTransport(FakeConnection())
    client = RealtimeClient(
        _profile(),
        FakeMinter(_credential()),
        transport,
        QwenOmniAdapter(),
        tool_dispatcher=dispatcher,
        close_timeout=0.01,
    )
    for external_id in ["same", "same", "different"]:
        connection = FakeConnection()
        transport.connection = connection
        await connection.incoming.put((CONTROL / "server-session-created.json").read_text())
        session = await client.open(RealtimeOpenRequest(external_id, "instructions", tools=TOOLS))
        session._ready = True
        session._responses.add("response")
        session._function_call_items.add(("response", "item", 0))
        await session._apply(
            DecodedProviderEvent(
                "event_call",
                (ToolCallRequested("response", "call", "get_weather", '{"city":"Taipei"}'),),
                tool_call_identity=("response", "item", 0, "call"),
            )
        )
        await _settle()
        await session.close()
    assert len(dispatcher.calls) == 2


@pytest.mark.asyncio
async def test_expired_session_drops_audio_already_queued() -> None:
    session = _session(FakeConnection(), is_current_generation=lambda: False)
    await session._events.put(OutputAudio("response", "item", 0, 0, b"\0\0"))
    terminal = SessionFailed(RealtimeErrorCode.CANCELLED, False)
    await session._events.put(terminal)
    stream = session.events()
    assert await asyncio.wait_for(anext(stream), 1) == terminal
    assert session.late_event_count == 1
    await stream.aclose()
    await session.close()


@pytest.mark.asyncio
async def test_local_channel_drops_frame_when_provider_generation_expires() -> None:
    session = _session(FakeConnection(), is_current_generation=lambda: False)
    controller = LocalRealtimeChannelController(FakeOpener())
    channel = FakeChannel()
    controller._channel = channel
    controller._session = session
    controller._generation = 1
    await controller._send_output_audio(b"\0\0", generation=1)
    assert not channel.sent
    assert not session.is_current_generation
    await session.close()


@pytest.mark.parametrize("adapter", [QwenOmniAdapter(), OpenAIRealtimeAdapter()])
def test_context_ack_must_echo_requested_message(adapter: Any) -> None:
    request = adapter.append_message("context", role="system")
    item = deepcopy(request["item"])
    item["content"] = [{"type": "input_text", "text": "different"}]
    item["status"] = "completed"
    with pytest.raises(RealtimeError) as caught:
        adapter.decode_server_event(
            json.dumps(
                {
                    "type": "conversation.item.created",
                    "event_id": "event_wrong_context",
                    "item": item,
                }
            )
        )
    assert caught.value.code is RealtimeErrorCode.PROTOCOL_ERROR
