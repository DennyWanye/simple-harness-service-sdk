from __future__ import annotations

import asyncio
import importlib
import json
from copy import deepcopy
from typing import Any

import pytest
from test_realtime_context_ack import _running, _sent_message
from test_realtime_user_side_response import _event


def recovery() -> Any:
    return importlib.import_module(
        "simple_harness_service.realtime.recovery"
    ).RealtimeRecoveryState()


async def confirm(
    connection: Any,
    session: Any,
    text: str = "Earlier context",
    *,
    trigger_response: bool = False,
    **kwargs: Any,
) -> str:
    task = asyncio.create_task(
        session.append_message(text, trigger_response=trigger_response, **kwargs)
    )
    request = await _sent_message(connection)
    item = deepcopy(request["item"])
    item.update(id="item_server_context", status="completed")
    await connection.incoming.put(
        json.dumps({"event_id": "event_ack", "type": "conversation.item.created", "item": item})
    )
    await asyncio.wait_for(task, 1)
    return str(request["item"]["id"])


@pytest.mark.asyncio
async def test_ack_aliases_and_audio_input_share_queryable_table() -> None:
    state = recovery()
    async with _running(recovery_state=state) as (connection, session):
        original = await confirm(connection, session)
        table = session.identity_table
        assert table.lookup(original).canonical_id == "item_server_context"
        assert table.lookup("item_server_context").role == "user"
        assert table.lookup("item_server_context").confirmed
        await connection.incoming.put(
            json.dumps(
                {
                    "event_id": "event_speech",
                    "type": "input_audio_buffer.speech_started",
                    "item_id": "item_audio",
                }
            )
        )
        assert (await asyncio.wait_for(anext(session.events()), 1)).kind == "SpeechStarted"
        assert table.lookup("item_audio").source == "speech_started"


@pytest.mark.asyncio
async def test_nested_completed_user_output_is_registered_before_sidecar() -> None:
    async with _running() as (connection, session):
        await connection.incoming.put(
            json.dumps(
                _event(
                    "response.output_item.done",
                    response_id="response_1",
                    item={
                        "id": "nested_user",
                        "type": "message",
                        "role": "user",
                        "status": "completed",
                    },
                )
            )
        )
        await connection.incoming.put(
            json.dumps(
                _event(
                    "response.audio_transcript.done",
                    "nested_user",
                    response_id="",
                    transcript="A user transcript",
                )
            )
        )
        event = await asyncio.wait_for(anext(session.events()), 1)
        assert event.kind == "TranscriptCompleted"
        assert session.identity_table.lookup("nested_user").role == "user"
        assert not session._terminal_published.is_set()


@pytest.mark.asyncio
async def test_completed_audio_message_confirms_input_identity_before_reconnect() -> None:
    state = recovery()
    async with _running(recovery_state=state) as (connection, session):
        await connection.incoming.put(
            json.dumps(
                {
                    "event_id": "event_speech",
                    "type": "input_audio_buffer.speech_started",
                    "item_id": "audio_user",
                }
            )
        )
        assert (await asyncio.wait_for(anext(session.events()), 1)).kind == "SpeechStarted"
        await connection.incoming.put(
            json.dumps(
                {
                    "event_id": "event_audio_item",
                    "type": "conversation.item.created",
                    "item": {
                        "id": "audio_user",
                        "type": "message",
                        "role": "user",
                        "status": "completed",
                        "content": [{"type": "input_audio"}],
                    },
                }
            )
        )
        await connection.incoming.put(
            json.dumps(
                {
                    "event_id": "event_barrier",
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": "barrier_user",
                    "transcript": "Continue",
                }
            )
        )
        assert (await asyncio.wait_for(anext(session.events()), 1)).kind == "TranscriptCompleted"
        assert session.identity_table.lookup("audio_user").confirmed
    async with _running(recovery_state=state) as (connection, session):
        assert session.identity_table.lookup("audio_user").source == "input_audio_item"
