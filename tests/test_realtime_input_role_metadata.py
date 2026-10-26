from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from test_realtime_context_ack import _responses, _running, _sent_message
from test_realtime_identity_table import recovery
from test_realtime_user_side_transcript import _decode

from simple_harness_service.realtime.adapters.qwen_omni import QwenOmniAdapter
from simple_harness_service.realtime.contracts import (
    RealtimeError,
    TranscriptCompleted,
)
from simple_harness_service.realtime.observability import RealtimeDiagnostics

FIXTURE = json.loads((Path(__file__).parent / "fixtures/qwen-input-role-metadata.json").read_text())
FIRST_RESPONSE = []
for frame in FIXTURE["stage0_events"]:
    FIRST_RESPONSE.append(frame)
    if frame["type"] == "response.done":
        break
RESPONSE_SUFFIX = FIRST_RESPONSE[
    next(i for i, frame in enumerate(FIRST_RESPONSE) if frame["type"] == "response.created") :
]


def created(
    identity: str, *, role: Any = "assistant", status: str = "in_progress"
) -> dict[str, Any]:
    return {
        "type": "conversation.item.created",
        "event_id": "event_input_item",
        "item": {
            "id": identity,
            "type": "message",
            "role": role,
            "status": status,
            "content": [{"type": "input_audio"}],
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", FIXTURE["live7_branches"], ids=lambda b: b["name"])
async def test_five_live7_sequences_continue_into_scoped_response(branch: dict[str, Any]) -> None:
    diagnostics = RealtimeDiagnostics()
    async with _running(diagnostics=diagnostics) as (connection, session):
        for frame in [*branch["events"], *RESPONSE_SUFFIX]:
            await connection.incoming.put(json.dumps(frame))
        stream = session.events()
        received = []
        while True:
            event = await asyncio.wait_for(anext(stream), 1)
            received.append(event)
            if event.kind == "ResponseFinished":
                break
        assert [e.kind for e in received].count("TranscriptCompleted") == 1
        assert any(e.kind == "ToolCallRequested" for e in received)
        identity = branch["events"][0]["item_id"]
        assert session.identity_table.lookup(identity).role == "user"
        assert session.identity_table.lookup(identity).origin == "speech_started"
        assert not session._terminal_published.is_set() and not connection.closed
        mismatch = [
            e
            for e in diagnostics.snapshot().events
            if e.operation_kind == "item_role_metadata_mismatch"
        ]
        assert len(mismatch) == 1
        assert mismatch[0].item_id == identity and mismatch[0].declared_role == "assistant"
        assert mismatch[0].provider_event_type == "conversation.item.created"
        assert not _responses(connection)
        assert "Fixture user request" not in repr(diagnostics.snapshot())


def test_stage0_complete_native_turn_preserves_user_and_assistant_events() -> None:
    adapter = QwenOmniAdapter()
    events = [
        event for frame in FIXTURE["stage0_events"] for event in _decode(adapter, frame).events
    ]
    assert any(e.kind == "TranscriptCompleted" for e in events)
    assert any(e.kind == "ToolCallRequested" for e in events)
    assert any(e.kind == "OutputAudio" for e in events)
    assert sum(e.kind == "ResponseFinished" for e in events) == 2
    identity = FIXTURE["stage0_events"][0]["item_id"]
    assert adapter.identity_table.lookup(identity).role == "user"


@pytest.mark.parametrize("source", ["speech_started", "committed"])
def test_input_source_cannot_be_relabelled_by_later_metadata(source: str) -> None:
    adapter = QwenOmniAdapter()
    _decode(
        adapter,
        {"type": "input_audio_buffer." + source, "event_id": "event_input", "item_id": "user_1"},
    )
    _decode(adapter, created("user_1"))
    _decode(
        adapter,
        {
            "type": "response.output_item.added",
            "event_id": "event_output",
            "response_id": "response_1",
            "output_index": 0,
            "item": {"id": "user_1", "type": "message", "role": "assistant"},
        },
    )
    assert adapter.identity_table.lookup("user_1").role == "user"
    assert adapter.identity_table.lookup("user_1").origin == source
    assert _decode(
        adapter,
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "event_id": "event_asr",
            "item_id": "user_1",
            "transcript": "Fixture user request",
        },
    ).events == (TranscriptCompleted("user_1", "Fixture user request"),)


@pytest.mark.parametrize("role", ["assistant", "system", None])
def test_input_audio_message_alone_establishes_user_origin(role: Any) -> None:
    adapter = QwenOmniAdapter()
    decoded = _decode(adapter, created("user_1", role=role))
    assert not decoded.events
    assert adapter.identity_table.lookup("user_1").role == "user"
    assert adapter.identity_table.lookup("user_1").origin == "input_audio_item"


@pytest.mark.asyncio
async def test_voice_metadata_does_not_consume_pending_context_ack() -> None:
    async with _running() as (connection, session):
        operation = asyncio.create_task(session.append_message("Context", trigger_response=False))
        request = await _sent_message(connection)
        for frame in [
            {
                "type": "input_audio_buffer.speech_started",
                "event_id": "event_speech",
                "item_id": "user_1",
            },
            created("user_1"),
        ]:
            await connection.incoming.put(json.dumps(frame))
        assert (await asyncio.wait_for(anext(session.events()), 1)).kind == "SpeechStarted"
        await asyncio.sleep(0)
        assert not operation.done() and not _responses(connection)
        echo = deepcopy(request["item"])
        echo.update(id="context_server", status="completed")
        await connection.incoming.put(
            json.dumps(
                {"type": "conversation.item.created", "event_id": "event_context_ack", "item": echo}
            )
        )
        await asyncio.wait_for(operation, 1)
        assert session.context_ack_id == "context_server"


@pytest.mark.asyncio
async def test_mismatch_diagnostics_deduplicate_and_use_only_role_enum() -> None:
    diagnostics = RealtimeDiagnostics()
    async with _running(diagnostics=diagnostics) as (connection, session):
        frame = created("user_1", role="private unexpected role")
        frame["item"]["content"].append({"type": "input_text", "text": "private context"})
        for value in [
            frame,
            frame,
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "event_id": "event_asr",
                "item_id": "user_1",
                "transcript": "Fixture user request",
            },
        ]:
            await connection.incoming.put(json.dumps(value))
        assert (await asyncio.wait_for(anext(session.events()), 1)).kind == "TranscriptCompleted"
        mismatch = [
            e
            for e in diagnostics.snapshot().events
            if e.operation_kind == "item_role_metadata_mismatch"
        ]
        assert len(mismatch) == 1 and mismatch[0].declared_role == "invalid"
        assert "private unexpected role" not in repr(diagnostics.snapshot())
        assert "private context" not in repr(diagnostics.snapshot())


def test_assistant_output_requires_scope_before_identity_registration() -> None:
    adapter = QwenOmniAdapter()
    with pytest.raises(RealtimeError):
        _decode(
            adapter,
            {
                "type": "response.output_item.added",
                "event_id": "event_assistant",
                "output_index": 0,
                "item": {"id": "assistant_1", "type": "message", "role": "assistant"},
            },
        )
    assert adapter.identity_table.lookup("assistant_1") is None


def test_real_assistant_origin_cannot_be_promoted_by_created_role() -> None:
    adapter = QwenOmniAdapter()
    _decode(
        adapter,
        {
            "type": "response.output_item.added",
            "event_id": "event_assistant",
            "response_id": "response_1",
            "output_index": 0,
            "item": {"id": "assistant_1", "type": "message", "role": "assistant"},
        },
    )
    with pytest.raises(RealtimeError):
        _decode(adapter, created("assistant_1", role="user"))
    assert adapter.identity_table.lookup("assistant_1").role == "assistant"


@pytest.mark.asyncio
async def test_origin_survives_snapshot_and_rebind() -> None:
    state = recovery()
    async with _running(recovery_state=state) as (connection, session):
        for frame in [
            {
                "type": "input_audio_buffer.speech_started",
                "event_id": "event_speech",
                "item_id": "user_1",
            },
            created("user_1"),
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "event_id": "event_asr",
                "item_id": "user_1",
                "transcript": "Fixture user request",
            },
        ]:
            await connection.incoming.put(json.dumps(frame))
        stream = session.events()
        assert (await asyncio.wait_for(anext(stream), 1)).kind == "SpeechStarted"
        assert (await asyncio.wait_for(anext(stream), 1)).kind == "TranscriptCompleted"
    snapshot = state.snapshot()
    restored = type(state).from_snapshot(json.loads(json.dumps(snapshot)))
    assert restored.identity_table("session").lookup("user_1").origin == "speech_started"
    for session_state in snapshot["sessions"].values():
        for entry in session_state["identities"]:
            entry.pop("origin", None)
    assert (
        type(state).from_snapshot(snapshot).identity_table("session").lookup("user_1").role
        == "user"
    )


def test_created_role_alone_does_not_establish_assistant_origin() -> None:
    adapter = QwenOmniAdapter()
    frame = created("unscoped_1")
    frame["item"]["content"] = [{"type": "text", "text": "Fixture text"}]
    _decode(adapter, frame)
    assert adapter.identity_table.lookup("unscoped_1") is None


@pytest.mark.parametrize("source", ["speech_started", "committed"])
def test_audio_message_origin_precedes_later_input_notifications(source: str) -> None:
    adapter = QwenOmniAdapter()
    _decode(adapter, created("user_1", role="user"))
    _decode(
        adapter,
        {
            "type": "input_audio_buffer." + source,
            "event_id": "event_later_input",
            "item_id": "user_1",
        },
    )
    assert adapter.identity_table.lookup("user_1").origin == "input_audio_item"
    assert adapter.identity_table.lookup("user_1").role == "user"


@pytest.mark.asyncio
async def test_stale_generation_metadata_does_not_publish_mismatch() -> None:
    diagnostics = RealtimeDiagnostics()
    current = True
    async with _running(diagnostics=diagnostics, is_current_generation=lambda: current) as (
        connection,
        session,
    ):
        await connection.incoming.put(
            json.dumps(
                {
                    "type": "input_audio_buffer.speech_started",
                    "event_id": "event_speech",
                    "item_id": "user_1",
                }
            )
        )
        assert (await asyncio.wait_for(anext(session.events()), 1)).kind == "SpeechStarted"
        current = False
        await connection.incoming.put(json.dumps(created("user_1")))
        for _ in range(30):
            await asyncio.sleep(0)
        assert session.late_event_count == 1
        assert not any(
            e.operation_kind == "item_role_metadata_mismatch" for e in diagnostics.snapshot().events
        )


def test_later_role_metadata_does_not_refresh_cached_input_generation() -> None:
    adapter = QwenOmniAdapter()
    _decode(
        adapter,
        {"type": "input_audio_buffer.committed", "event_id": "event_input", "item_id": "user_1"},
    )
    adapter.identity_table.begin_generation(2)
    _decode(
        adapter,
        {
            "type": "response.output_item.added",
            "event_id": "event_metadata",
            "response_id": "response_2",
            "output_index": 0,
            "item": {"id": "user_1", "type": "message", "role": "assistant"},
        },
    )
    assert adapter.identity_table.lookup("user_1").role == "user"
    assert adapter.identity_table.lookup("user_1").generation == 1
