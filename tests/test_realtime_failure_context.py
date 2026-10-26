from __future__ import annotations

import asyncio
import json

import pytest
from test_realtime_context_ack import _running, _sent_message

from simple_harness_service.realtime.contracts import RealtimeError
from simple_harness_service.realtime.observability import RealtimeDiagnostics


@pytest.mark.asyncio
async def test_adapter_first_cause_survives_terminal_and_ignores_private_values() -> None:
    diagnostics = RealtimeDiagnostics(max_events=2)
    async with _running(diagnostics=diagnostics) as (connection, session):
        await connection.incoming.put(
            json.dumps(
                {
                    "type": "response.audio_transcript.done",
                    "event_id": "event_bad",
                    "item_id": "unknown",
                    "response_id": "",
                    "transcript": "private body",
                    "private body as key": "private value",
                }
            )
        )
        event = await asyncio.wait_for(anext(session.events()), 1)
        assert event.failure.stage == "adapter.decode"
        assert event.failure.event_type == "response.audio_transcript.done"
        assert event.failure.field_names == (
            "event_id",
            "item_id",
            "response_id",
            "transcript",
            "type",
        )
        terminal = diagnostics.snapshot().events[-1]
        assert terminal.failure == event.failure
        assert "private body" not in repr(diagnostics.snapshot())
        assert event.failure.generation == session.generation


@pytest.mark.asyncio
async def test_send_failure_records_client_type_before_terminal() -> None:
    async with _running() as (connection, session):
        connection.fail_once_types.add("input_audio_buffer.append")
        with pytest.raises(RealtimeError):
            await session.send_audio(b"\0\0")
        event = await asyncio.wait_for(anext(session.events()), 1)
        assert event.failure.stage == "provider.send"
        assert event.failure.event_type == "input_audio_buffer.append"
        assert "audio" in event.failure.field_names


@pytest.mark.asyncio
async def test_ack_timeout_records_pending_client_event() -> None:
    async with _running() as (connection, session):
        operation = asyncio.create_task(
            session.append_message("Pending context", trigger_response=True)
        )
        sent = await _sent_message(connection)
        with pytest.raises(RealtimeError):
            await operation
        event = await asyncio.wait_for(anext(session.events()), 1)
        assert event.failure.stage == "context.ack_timeout"
        assert event.failure.event_type == "conversation.item.create"
        assert event.failure.related_event_id == sent["event_id"]


@pytest.mark.asyncio
async def test_eof_has_receive_cause_instead_of_previous_provider_event() -> None:
    async with _running() as (connection, session):
        await connection.incoming.put(None)
        event = await asyncio.wait_for(anext(session.events()), 1)
        assert event.failure.stage == "provider.receive"
        assert event.failure.event_type == "transport.eof"


@pytest.mark.asyncio
async def test_tool_ack_timeout_points_to_original_client_result() -> None:
    from simple_harness_service.realtime.contracts import ToolCallRequested
    from simple_harness_service.realtime.ports import DecodedProviderEvent

    async with _running() as (_connection, session):
        session._responses.add("response_1")
        session._function_call_items.add(("response_1", "item_call", 0))
        call = ToolCallRequested("response_1", "call_1", "get_weather", '{"city":"Taipei"}')
        assert await session._apply(
            DecodedProviderEvent(
                "event_call", (call,), tool_call_identity=("response_1", "item_call", 0, "call_1")
            )
        )
        with pytest.raises(RealtimeError):
            await session.submit_tool_result("call_1", "done")
        events = session.events()
        event = await asyncio.wait_for(anext(events), 1)
        if event.kind == "ToolCallRequested":
            event = await asyncio.wait_for(anext(events), 1)
        assert event.failure.stage == "tool.ack_timeout"
        assert event.failure.event_type == "conversation.item.create"


@pytest.mark.asyncio
async def test_unknown_validation_reason_cannot_enter_failure_snapshot() -> None:
    from simple_harness_service.realtime.adapters.qwen_omni import QwenOmniAdapter
    from simple_harness_service.realtime.contracts import RealtimeErrorCode

    class Adapter(QwenOmniAdapter):
        def decode_server_event(self, payload):
            raise RealtimeError(
                RealtimeErrorCode.PROTOCOL_ERROR, validation_reason="private_body_in_reason"
            )

    async with _running(adapter=Adapter()) as (connection, session):
        await connection.incoming.put(
            json.dumps(
                {"type": "response.audio.done", "event_id": "event_invalid", "item_id": "unknown"}
            )
        )
        event = await asyncio.wait_for(anext(session.events()), 1)
        assert event.failure.validation_reason == "validation_failed"
        assert "private_body_in_reason" not in repr(session.diagnostics_snapshot())


@pytest.mark.asyncio
async def test_open_control_failure_preserves_relay_classification() -> None:
    from test_realtime_control_failures import control
    from test_realtime_session import FakeConnection, _open

    diagnostics = RealtimeDiagnostics()
    connection = FakeConnection()
    await connection.incoming.put(
        json.dumps(
            control(code="unavailable", retryable=True, message_class="upstream_unavailable")
        )
    )
    with pytest.raises(RealtimeError) as error:
        await _open(connection, diagnostics=diagnostics)
    assert error.value.failure.stage == "control.open"
    assert error.value.failure.control_code == "unavailable"
    assert error.value.failure.message_class == "upstream_unavailable"
    assert diagnostics.snapshot().events[-1].failure == error.value.failure
