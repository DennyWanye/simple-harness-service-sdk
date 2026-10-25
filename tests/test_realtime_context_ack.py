from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from test_realtime_session import FakeConnection
from test_realtime_stage2 import _session

from simple_harness_service.realtime.adapters.qwen_omni import QwenOmniAdapter
from simple_harness_service.realtime.contracts import RealtimeError

FIXTURES = json.loads((Path(__file__).parent / "fixtures/qwen-context-ack.json").read_text())


@asynccontextmanager
async def _running(**kwargs: Any) -> Any:
    connection = FakeConnection()
    session = _session(connection, **kwargs)
    session._tool_ack_timeout = 0.03
    await session.start()
    session._ready = True
    try:
        yield connection, session
    finally:
        await session.close()


async def _sent_message(connection: FakeConnection, number: int = 1) -> dict[str, Any]:
    async def wait() -> dict[str, Any]:
        while True:
            messages = [
                json.loads(s)
                for s in connection.sent
                if json.loads(s).get("type") == "conversation.item.create"
            ]
            if len(messages) >= number:
                return dict(messages[number - 1])
            await asyncio.sleep(0)

    return await asyncio.wait_for(wait(), 1)


def _responses(connection: FakeConnection) -> list[dict[str, Any]]:
    return [json.loads(s) for s in connection.sent if json.loads(s)["type"] == "response.create"]


@pytest.mark.asyncio
async def test_provider_id_confirms_history_without_response() -> None:
    async with _running() as (connection, session):
        fixture = FIXTURES["history"]
        operation = asyncio.create_task(
            session.append_message(fixture["text"], trigger_response=False)
        )
        request = await _sent_message(connection)
        assert request["item"]["id"] != fixture["echo"]["item"]["id"]
        assert not operation.done() and not _responses(connection)
        await connection.incoming.put(json.dumps(fixture["echo"]))
        await asyncio.wait_for(operation, 1)
        assert not _responses(connection)
        assert session.context_ack_id == fixture["echo"]["item"]["id"]


@pytest.mark.asyncio
async def test_provider_id_confirms_announcement_before_one_response() -> None:
    async with _running() as (connection, session):
        fixture = FIXTURES["announcement"]
        operation = asyncio.create_task(
            session.append_message(fixture["text"], trigger_response=True)
        )
        await _sent_message(connection)
        assert not operation.done() and not _responses(connection)
        await connection.incoming.put(json.dumps(fixture["echo"]))
        await asyncio.wait_for(operation, 1)
        assert len(_responses(connection)) == 1


@pytest.mark.asyncio
async def test_original_id_confirms_history_and_announcement() -> None:
    async with _running() as (connection, session):
        for index, name in enumerate(["history", "announcement"], 1):
            fixture = FIXTURES[name]
            operation = asyncio.create_task(
                session.append_message(fixture["text"], trigger_response=name == "announcement")
            )
            request = await _sent_message(connection, index)
            echo = deepcopy(fixture["echo"])
            echo["item"]["id"] = request["item"]["id"]
            await connection.incoming.put(json.dumps(echo))
            await asyncio.wait_for(operation, 1)
            assert len(_responses(connection)) == int(name == "announcement")
            assert session.context_ack_id == echo["item"]["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt", ["text", "role", "status"])
async def test_wrong_echo_never_authorizes_response(corrupt: str) -> None:
    async with _running() as (connection, session):
        fixture = FIXTURES["announcement"]
        operation = asyncio.create_task(
            session.append_message(fixture["text"], trigger_response=True)
        )
        await _sent_message(connection)
        echo = deepcopy(fixture["echo"])
        if corrupt == "text":
            echo["item"]["content"][0]["text"] = "A different message"
        elif corrupt == "role":
            echo["item"]["role"] = "system"
        else:
            echo["item"]["status"] = "in_progress"
        await connection.incoming.put(json.dumps(echo))
        with pytest.raises(RealtimeError):
            await asyncio.wait_for(operation, 1)
        assert not _responses(connection)


def _echo(text: str, identity: str, event: str) -> str:
    value = deepcopy(FIXTURES["announcement"]["echo"])
    value["event_id"] = event
    value["item"]["id"] = identity
    value["item"]["content"][0]["text"] = text
    return json.dumps(value)


def test_duplicate_ids_and_events_do_not_confirm_same_content_next_message() -> None:
    adapter = QwenOmniAdapter()
    first = adapter.append_message("Repeated context")
    decoded = adapter.decode_server_event(_echo("Repeated context", "item_first", "event_first"))
    assert decoded.context_ack_id == "item_first"
    assert decoded.context_request_id == first["event_id"]
    second = adapter.append_message("Repeated context")
    for identity, event in [
        ("item_first", "event_replayed"),
        (first["item"]["id"], "event_original_id_replayed"),
        ("item_duplicate", "event_first"),
    ]:
        duplicate = adapter.decode_server_event(_echo("Repeated context", identity, event))
        assert duplicate.context_ack_id is None
    decoded = adapter.decode_server_event(_echo("Repeated context", "item_second", "event_second"))
    assert decoded.context_request_id == second["event_id"]
    assert decoded.context_ack_id == "item_second"


def test_ack_cannot_skip_oldest_pending_message() -> None:
    adapter = QwenOmniAdapter()
    first = adapter.append_message("First context")
    second = adapter.append_message("Second context")
    with pytest.raises(RealtimeError):
        adapter.decode_server_event(_echo("Second context", "item_second", "event_out_of_order"))
    first_ack = adapter.decode_server_event(_echo("First context", "item_first", "event_first"))
    assert first_ack.context_request_id == first["event_id"]
    second_ack = adapter.decode_server_event(_echo("Second context", "item_second", "event_second"))
    assert second_ack.context_request_id == second["event_id"]


@pytest.mark.asyncio
async def test_late_ack_from_previous_generation_never_authorizes_response() -> None:
    current = True
    async with _running(is_current_generation=lambda: current) as (connection, session):
        fixture = FIXTURES["announcement"]
        operation = asyncio.create_task(
            session.append_message(fixture["text"], trigger_response=True)
        )
        await _sent_message(connection)
        current = False
        await connection.incoming.put(json.dumps(fixture["echo"]))
        with pytest.raises(RealtimeError):
            await asyncio.wait_for(operation, 1)
        assert not _responses(connection)


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", [False, True])
async def test_instructions_ack_matches_trimmed_content(trigger: bool) -> None:
    async with _running() as (connection, session):
        operation = asyncio.create_task(
            session.update_instructions("  Answer briefly.\n", trigger_response=trigger)
        )

        async def wait_for_update() -> None:
            while (
                len([s for s in connection.sent if json.loads(s)["type"] == "session.update"]) < 2
            ):
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_update(), 1)
        assert not operation.done() and not _responses(connection)
        await connection.incoming.put(
            json.dumps(
                {
                    "type": "session.updated",
                    "event_id": "event_instructions_ack",
                    "session": {"instructions": "Answer briefly.  "},
                }
            )
        )
        await asyncio.wait_for(operation, 1)
        assert len(_responses(connection)) == int(trigger)
        assert len([s for s in connection.sent if json.loads(s)["type"] == "session.update"]) == 2


def test_later_original_id_cannot_confirm_earlier_identical_message() -> None:
    adapter = QwenOmniAdapter()
    adapter.append_message("Repeated context")
    second = adapter.append_message("Repeated context")
    with pytest.raises(RealtimeError):
        adapter.decode_server_event(_echo("Repeated context", second["item"]["id"], "event_second"))


def test_tool_result_and_audio_item_do_not_consume_message_confirmation() -> None:
    adapter = QwenOmniAdapter()
    message = adapter.append_message("Pending context")
    result = adapter.decode_server_event(
        json.dumps(
            {
                "type": "conversation.item.created",
                "event_id": "event_tool_result_ack",
                "item": {
                    "id": "item_tool_result",
                    "type": "function_call_output",
                    "status": "completed",
                    "call_id": "call_tool",
                    "output": "done",
                },
            }
        )
    )
    assert result.tool_ack_call_id == "call_tool"
    assert result.context_ack_id is None
    audio = adapter.decode_server_event(
        json.dumps(
            {
                "type": "conversation.item.created",
                "event_id": "event_user_audio",
                "item": {
                    "id": "item_user_audio",
                    "type": "message",
                    "status": "completed",
                    "role": "user",
                    "content": [{"type": "input_audio"}],
                },
            }
        )
    )
    assert audio.context_ack_id is None
    decoded = adapter.decode_server_event(_echo("Pending context", "item_context", "event_context"))
    assert decoded.context_request_id == message["event_id"]


@pytest.mark.asyncio
async def test_instructions_mismatch_never_authorizes_response() -> None:
    async with _running() as (connection, session):
        operation = asyncio.create_task(
            session.update_instructions("Answer briefly.", trigger_response=True)
        )

        async def wait_for_update() -> None:
            while (
                len([s for s in connection.sent if json.loads(s)["type"] == "session.update"]) < 2
            ):
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_update(), 1)
        await connection.incoming.put(
            json.dumps(
                {
                    "type": "session.updated",
                    "event_id": "event_wrong_instructions",
                    "session": {"instructions": "Answer in detail."},
                }
            )
        )
        with pytest.raises(RealtimeError):
            await asyncio.wait_for(operation, 1)
        assert not _responses(connection)


def test_message_body_is_not_trimmed_when_matching() -> None:
    adapter = QwenOmniAdapter()
    adapter.append_message("  Exact context  ")
    with pytest.raises(RealtimeError):
        adapter.decode_server_event(_echo("Exact context", "item_context", "event_context"))


def test_duplicate_instructions_ack_does_not_confirm_next_update() -> None:
    adapter = QwenOmniAdapter()
    first = adapter.update_instructions(" First instructions ")
    echo = {
        "type": "session.updated",
        "event_id": "event_first_update",
        "session": {"instructions": "First instructions"},
    }
    decoded = adapter.decode_server_event(json.dumps(echo))
    assert decoded.context_request_id == first["event_id"]
    second = adapter.update_instructions("Second instructions")
    duplicate = adapter.decode_server_event(json.dumps(echo))
    assert duplicate.context_request_id is None
    echo["event_id"] = "event_second_update"
    echo["session"]["instructions"] = " Second instructions "
    decoded = adapter.decode_server_event(json.dumps(echo))
    assert decoded.context_request_id == second["event_id"]
