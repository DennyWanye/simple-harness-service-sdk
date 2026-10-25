from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from typing import Any

import pytest
from test_realtime_context_ack import _responses, _running, _sent_message

from simple_harness_service.realtime.adapters.qwen_omni import QwenOmniAdapter
from simple_harness_service.realtime.contracts import (
    OutputText,
    RealtimeError,
    RealtimeErrorCode,
    RealtimeOpenRequest,
    SpeechStarted,
    TranscriptCompleted,
    TranscriptDelta,
)
from simple_harness_service.realtime.observability import (
    RealtimeDiagnostics,
    RealtimeDiagnosticStage,
)


def _decode(adapter: QwenOmniAdapter, value: dict[str, Any]) -> Any:
    return adapter.decode_server_event(json.dumps(value))


def _confirmed_context(
    adapter: QwenOmniAdapter, *, role: str = "user", identity: str = "item_provider_context"
) -> str:
    request = adapter.append_message("Earlier user context", role=role)
    item = deepcopy(request["item"])
    item.update(id=identity, status="completed")
    ack = _decode(
        adapter,
        {"event_id": f"event_ack_{identity}", "type": "conversation.item.created", "item": item},
    )
    assert ack.context_ack_id == identity
    assert ack.context_request_id == request["event_id"]
    return identity


def _sideband(
    identity: str, *, text: Any = "", delta: bool = False, event: str = "event_sideband"
) -> dict[str, Any]:
    return {
        "event_id": event,
        "type": "response.audio_transcript.delta" if delta else "response.audio_transcript.done",
        "item_id": identity,
        "output_index": 0,
        "content_index": 0,
        "delta" if delta else "transcript": text,
    }


@pytest.mark.parametrize("source", ["context", "speech"])
@pytest.mark.parametrize("delta", [False, True])
@pytest.mark.parametrize("text", ["", "A real user request"])
def test_known_user_side_transcript(source: str, delta: bool, text: str) -> None:
    adapter = QwenOmniAdapter()
    identity = "item_provider_context"
    if source == "context":
        _confirmed_context(adapter)
    else:
        started = _decode(
            adapter,
            {
                "event_id": "event_speech",
                "type": "input_audio_buffer.speech_started",
                "item_id": identity,
            },
        )
        assert started.events == (SpeechStarted(identity),)
    decoded = _decode(adapter, _sideband(identity, delta=delta, text=text))
    expected = TranscriptDelta if delta else TranscriptCompleted
    assert decoded.events == ((expected(identity, text),) if text else ())
    assert decoded.user_side_transcript_ignored is (not text)
    assert decoded.context_ack_id is None and decoded.context_request_id is None
    assert decoded.introduced_response_id is None


@pytest.mark.parametrize("source", ["unknown", "pending", "system", "assistant", "invalid_ack"])
@pytest.mark.parametrize("delta", [False, True])
def test_untrusted_item_still_requires_response_id(source: str, delta: bool) -> None:
    adapter = QwenOmniAdapter()
    identity = "item_untrusted"
    if source == "pending":
        identity = str(adapter.append_message("Pending context")["item"]["id"])
    elif source == "system":
        _confirmed_context(adapter, role="system", identity=identity)
    elif source == "assistant":
        _decode(
            adapter,
            {
                "event_id": "event_response",
                "type": "response.created",
                "response": {"id": "response_1"},
            },
        )
        _decode(
            adapter,
            {
                "event_id": "event_output",
                "type": "response.output_item.added",
                "response_id": "response_1",
                "output_index": 0,
                "item": {"id": identity, "type": "message", "role": "assistant"},
            },
        )
    elif source == "invalid_ack":
        request = adapter.append_message("Pending context")
        item = deepcopy(request["item"])
        item.update(id=identity, status="in_progress")
        with pytest.raises(RealtimeError):
            _decode(
                adapter,
                {
                    "event_id": "event_bad_ack",
                    "type": "conversation.item.created",
                    "item": item,
                },
            )
    with pytest.raises(RealtimeError, match="response_id is required") as error:
        _decode(adapter, _sideband(identity, delta=delta))
    assert error.value.code is RealtimeErrorCode.PROTOCOL_ERROR


@pytest.mark.parametrize("text", [None, 7, [], {}])
@pytest.mark.parametrize("delta", [False, True])
def test_known_user_transcript_must_be_a_string(text: Any, delta: bool) -> None:
    adapter = QwenOmniAdapter()
    identity = _confirmed_context(adapter)
    with pytest.raises(RealtimeError) as error:
        _decode(adapter, _sideband(identity, delta=delta, text=text))
    assert error.value.code is RealtimeErrorCode.PROTOCOL_ERROR


@pytest.mark.parametrize("response_id", [None, ""])
def test_present_but_invalid_response_id_is_not_user_side(response_id: Any) -> None:
    adapter = QwenOmniAdapter()
    identity = _confirmed_context(adapter)
    value = _sideband(identity)
    value["response_id"] = response_id
    with pytest.raises(RealtimeError, match="response_id is required"):
        _decode(adapter, value)


@pytest.mark.parametrize("delta", [False, True])
def test_scoped_assistant_transcript_is_preserved(delta: bool) -> None:
    adapter = QwenOmniAdapter()
    identity = _confirmed_context(adapter)
    value = _sideband(identity, text="A real answer", delta=delta)
    value["response_id"] = "response_1"
    decoded = _decode(adapter, value)
    assert decoded.events == (OutputText("response_1", identity, 0, 0, "A real answer", delta),)


def test_normal_user_audio_transcription_is_preserved() -> None:
    decoded = _decode(
        QwenOmniAdapter(),
        {
            "event_id": "event_normal_user",
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "fresh_user_item",
            "transcript": "A real user request",
        },
    )
    assert decoded.events == (TranscriptCompleted("fresh_user_item", "A real user request"),)


@pytest.mark.parametrize("source", ["context", "speech"])
def test_new_session_does_not_trust_previous_user_item(source: str) -> None:
    adapter = QwenOmniAdapter()
    identity = "item_old"
    if source == "context":
        _confirmed_context(adapter, identity=identity)
    else:
        _decode(
            adapter,
            {
                "event_id": "event_old_speech",
                "type": "input_audio_buffer.speech_started",
                "item_id": identity,
            },
        )
    adapter.session_update(RealtimeOpenRequest("next_session", "instructions"))
    with pytest.raises(RealtimeError, match="response_id is required"):
        _decode(adapter, _sideband(identity))


@pytest.mark.asyncio
async def test_context_sideband_keeps_session_alive_for_next_user_turn() -> None:
    diagnostics = RealtimeDiagnostics()
    async with _running(diagnostics=diagnostics) as (connection, session):
        operation = asyncio.create_task(
            session.append_message("Earlier user context", trigger_response=False)
        )
        request = await _sent_message(connection)
        item = deepcopy(request["item"])
        item.update(id="item_provider_context", status="completed")
        await connection.incoming.put(
            json.dumps(
                {
                    "event_id": "event_context_ack",
                    "type": "conversation.item.created",
                    "item": item,
                }
            )
        )
        await asyncio.wait_for(operation, 1)
        generation = session.generation
        empty = _sideband(item["id"])
        empty["private_fixture"] = "private-fixture-content"
        for _ in range(2):
            await connection.incoming.put(json.dumps(empty))
        await connection.incoming.put(
            json.dumps(
                {
                    "event_id": "event_fresh_user",
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": "fresh_user_item",
                    "transcript": "Continue the conversation",
                }
            )
        )
        stream = session.events()
        assert await asyncio.wait_for(anext(stream), 1) == TranscriptCompleted(
            "fresh_user_item", "Continue the conversation"
        )
        assert not session._terminal_published.is_set()
        assert not connection.closed
        assert session.generation == generation
        assert session.context_ack_id == item["id"]
        assert not _responses(connection)
        assert session.duplicate_event_count == 1
        snapshot = diagnostics.snapshot()
        ignored = [e for e in snapshot.events if e.operation_kind == "user_side_transcript_ignored"]
        assert len(ignored) == 1
        assert ignored[0].stage is RealtimeDiagnosticStage.PROVIDER_EVENT_RECEIVED
        assert not any(
            e.stage is RealtimeDiagnosticStage.PROVIDER_EVENT_DECODE_FAILED for e in snapshot.events
        )
        assert "private-fixture-content" not in repr(snapshot)
        assert item["id"] not in repr(snapshot)


@pytest.mark.asyncio
async def test_user_side_transcripts_interleave_with_assistant_output_and_deduplicate() -> None:
    async with _running() as (connection, session):
        await connection.incoming.put(
            json.dumps(
                {
                    "event_id": "event_speech",
                    "type": "input_audio_buffer.speech_started",
                    "item_id": "user_1",
                }
            )
        )
        for value in [
            {
                "event_id": "event_response",
                "type": "response.created",
                "response": {"id": "response_1"},
            },
            {
                "event_id": "event_output",
                "type": "response.output_item.added",
                "response_id": "response_1",
                "output_index": 0,
                "item": {"id": "assistant_1", "type": "message", "role": "assistant"},
            },
            {
                "event_id": "event_content",
                "type": "response.content_part.added",
                "response_id": "response_1",
                "item_id": "assistant_1",
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "text"},
            },
            _sideband("user_1", text="A user delta", delta=True, event="event_delta"),
            _sideband("user_1", text="A user delta", delta=True, event="event_delta"),
            dict(
                _sideband("assistant_1", text="An assistant answer", event="event_assistant"),
                response_id="response_1",
            ),
            _sideband("user_1", text="A user request", event="event_user_done"),
        ]:
            await connection.incoming.put(json.dumps(value))
        stream = session.events()
        expected = [
            SpeechStarted("user_1"),
            TranscriptDelta("user_1", "A user delta"),
            OutputText("response_1", "assistant_1", 0, 0, "An assistant answer", False),
            TranscriptCompleted("user_1", "A user request"),
        ]
        actual = [await asyncio.wait_for(anext(stream), 1) for _ in range(5)]
        assert [e for e in actual if e.kind != "ResponseStarted"] == expected
        assert session.duplicate_event_count == 1
        assert not session._terminal_published.is_set() and not _responses(connection)


@pytest.mark.asyncio
async def test_old_generation_user_transcript_is_not_published() -> None:
    current = True
    async with _running(is_current_generation=lambda: current) as (connection, session):
        await connection.incoming.put(
            json.dumps(
                {
                    "event_id": "event_speech",
                    "type": "input_audio_buffer.speech_started",
                    "item_id": "user_1",
                }
            )
        )
        stream = session.events()
        assert await asyncio.wait_for(anext(stream), 1) == SpeechStarted("user_1")
        current = False
        await connection.incoming.put(json.dumps(_sideband("user_1", text="Late user text")))
        for _ in range(30):
            await asyncio.sleep(0)
        assert session._events.empty()
        assert session.late_event_count == 1
        assert not _responses(connection)


@pytest.mark.parametrize("item_id", [None, "", [], {}])
def test_malformed_user_side_item_id_remains_protocol_error(item_id: Any) -> None:
    adapter = QwenOmniAdapter()
    _confirmed_context(adapter)
    with pytest.raises(RealtimeError) as error:
        _decode(adapter, _sideband(item_id))
    assert error.value.code is RealtimeErrorCode.PROTOCOL_ERROR


def test_missing_user_side_transcript_remains_protocol_error() -> None:
    adapter = QwenOmniAdapter()
    identity = _confirmed_context(adapter)
    value = _sideband(identity)
    value.pop("transcript")
    with pytest.raises(RealtimeError) as error:
        _decode(adapter, value)
    assert error.value.code is RealtimeErrorCode.PROTOCOL_ERROR


def test_only_acknowledged_provider_id_is_trusted() -> None:
    adapter = QwenOmniAdapter()
    request = adapter.append_message("Earlier user context")
    item = deepcopy(request["item"])
    item.update(id="provider_replacement", status="completed")
    _decode(adapter, {"event_id": "event_ack", "type": "conversation.item.created", "item": item})
    with pytest.raises(RealtimeError, match="response_id is required"):
        _decode(adapter, _sideband(request["item"]["id"]))


@pytest.mark.asyncio
async def test_stale_connection_cannot_seed_next_generation_user_items() -> None:
    current = True
    adapter = QwenOmniAdapter()
    async with _running(adapter=adapter, is_current_generation=lambda: current) as (
        connection,
        session,
    ):
        current = False
        adapter.session_update(RealtimeOpenRequest("next_session", "instructions"))
        await connection.incoming.put(
            json.dumps(
                {
                    "event_id": "event_late_speech",
                    "type": "input_audio_buffer.speech_started",
                    "item_id": "old_user",
                }
            )
        )
        for _ in range(30):
            await asyncio.sleep(0)
        assert session.late_event_count == 1
        with pytest.raises(RealtimeError, match="response_id is required"):
            _decode(adapter, _sideband("old_user"))
