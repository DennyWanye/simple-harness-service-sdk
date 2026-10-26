from __future__ import annotations

import asyncio
import json

import pytest
from test_realtime_context_ack import _running
from test_realtime_identity_table import confirm, recovery

from simple_harness_service.realtime.contracts import TranscriptCompleted


@pytest.mark.asyncio
@pytest.mark.parametrize("restore", [False, True], ids=["reconnect", "restored-process"])
async def test_rebind_history_ack_sidecar_and_first_new_transcript_without_audio_replay(
    restore: bool,
) -> None:
    state = recovery()
    async with _running(recovery_state=state) as (connection, session):
        await confirm(connection, session)
        await session.send_audio(b"\0\0")
        old_generation = session.generation
        await connection.incoming.put(
            json.dumps(
                {
                    "event_id": "event_previous_sidecar",
                    "type": "response.audio.done",
                    "response_id": "",
                    "item_id": "item_server_context",
                }
            )
        )
        from test_realtime_control_failures import control

        await connection.incoming.put(json.dumps(control()))
        assert (await asyncio.wait_for(anext(session.events()), 1)).kind == "SessionFailed"
    if restore:
        state = type(state).from_snapshot(json.loads(json.dumps(state.snapshot())))
    async with _running(recovery_state=state) as (connection, session):
        assert session.generation > old_generation
        assert session.identity_table.lookup("item_server_context").confirmed
        assert session.identity_table.lookup("item_server_context").source == "context_ack"
        await connection.incoming.put(
            json.dumps(
                {
                    "type": "response.audio.done",
                    "event_id": "event_old_user_sidecar",
                    "response_id": "",
                    "item_id": "item_server_context",
                }
            )
        )
        await confirm(connection, session, "Restored history")
        await connection.incoming.put(
            json.dumps(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "event_id": "event_next_user",
                    "item_id": "fresh_user",
                    "transcript": "Continue",
                }
            )
        )
        assert await asyncio.wait_for(anext(session.events()), 1) == TranscriptCompleted(
            "fresh_user", "Continue"
        )
        assert not any(
            json.loads(s)["type"] in {"input_audio_buffer.append", "response.create"}
            for s in connection.sent
        )
        assert any(
            e.operation_kind == "recovery.context_acknowledged"
            for e in session.diagnostics_snapshot().events
        )


@pytest.mark.asyncio
async def test_confirmed_idempotency_key_does_not_repeat_injection_after_rebind() -> None:
    state = recovery()
    async with _running(recovery_state=state) as (connection, session):
        await confirm(connection, session, idempotency_key="result_1")
    async with _running(recovery_state=state) as (connection, session):
        await session.append_message(
            "Earlier context", trigger_response=False, idempotency_key="result_1"
        )
        assert not any(json.loads(s)["type"] == "conversation.item.create" for s in connection.sent)


@pytest.mark.asyncio
async def test_announcement_key_survives_snapshot_without_repeating_followup() -> None:
    state = recovery()
    async with _running(recovery_state=state) as (connection, session):
        await confirm(connection, session, trigger_response=True, idempotency_key="result_1")
        assert sum(json.loads(s)["type"] == "response.create" for s in connection.sent) == 1
    state = type(state).from_snapshot(json.loads(json.dumps(state.snapshot())))
    async with _running(recovery_state=state) as (connection, session):
        await session.append_message(
            "Earlier context", trigger_response=True, idempotency_key="result_1"
        )
        assert not any(
            json.loads(s)["type"]
            in {"conversation.item.create", "response.create", "input_audio_buffer.append"}
            for s in connection.sent
        )


@pytest.mark.asyncio
async def test_actual_dispatch_is_not_repeated_after_rebind() -> None:
    from test_realtime_user_side_response import _event

    class Dispatcher:
        count = 0

        async def dispatch(self, call):
            self.count += 1
            return "done"

    dispatcher = Dispatcher()
    state = recovery()
    for attempt in range(2):
        async with _running(recovery_state=state, tool_dispatcher=dispatcher) as (
            connection,
            session,
        ):
            values = [
                {
                    "event_id": "event_response",
                    "type": "response.created",
                    "response": {"id": "response_1"},
                },
                _event(
                    "response.output_item.added",
                    "call_item",
                    response_id="response_1",
                    item={"id": "call_item", "type": "function_call"},
                ),
                _event(
                    "response.function_call_arguments.done",
                    "call_item",
                    response_id="response_1",
                    call_id="call_1",
                    name="get_weather",
                    arguments='{"city":"Taipei"}',
                ),
                {
                    "event_id": "event_next_user",
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": f"next_user_{attempt}",
                    "transcript": "Continue",
                },
            ]
            for value in values:
                await connection.incoming.put(json.dumps(value))
            stream = session.events()
            while (await asyncio.wait_for(anext(stream), 1)).kind != "TranscriptCompleted":
                pass
            for _ in range(20):
                await asyncio.sleep(0)
            assert dispatcher.count == 1
            if attempt:
                assert not any(
                    json.loads(s)["type"] in {"conversation.item.create", "response.create"}
                    for s in connection.sent
                )


@pytest.mark.asyncio
async def test_fresh_client_rebind_and_other_session_isolation() -> None:
    from test_realtime_session import (
        CONTROL,
        FakeConnection,
        FakeMinter,
        FakeTransport,
        _credential,
        _profile,
    )
    from test_realtime_stage2 import _ack

    from simple_harness_service.realtime.adapters.qwen_omni import QwenOmniAdapter
    from simple_harness_service.realtime.client import RealtimeClient
    from simple_harness_service.realtime.contracts import RealtimeOpenRequest

    state = recovery()
    for external in ["same_session", "same_session", "other_session"]:
        connection = FakeConnection()
        await connection.incoming.put((CONTROL / "server-session-created.json").read_text())
        client = RealtimeClient(
            _profile(),
            FakeMinter(_credential()),
            FakeTransport(connection),
            QwenOmniAdapter(),
            recovery_state=state,
            close_timeout=0.01,
        )
        session = await client.open(RealtimeOpenRequest(external, "instructions"))
        await connection.incoming.put(json.dumps(_ack()))
        assert (await asyncio.wait_for(anext(session.events()), 1)).kind == "SessionReady"
        try:
            if external == "other_session":
                assert session.identity_table.lookup("item_server_context") is None
            elif session.generation == 1:
                await confirm(connection, session)
            else:
                assert session.identity_table.lookup("item_server_context").confirmed
                await connection.incoming.put(
                    json.dumps(
                        {
                            "event_id": "event_sidecar",
                            "type": "response.audio.done",
                            "item_id": "item_server_context",
                            "response_id": "",
                        }
                    )
                )
                await connection.incoming.put(
                    json.dumps(
                        {
                            "event_id": "event_next",
                            "type": "conversation.item.input_audio_transcription.completed",
                            "item_id": "new_user",
                            "transcript": "Continue",
                        }
                    )
                )
                assert (
                    await asyncio.wait_for(anext(session.events()), 1)
                ).kind == "TranscriptCompleted"
        finally:
            await session.close()


@pytest.mark.asyncio
async def test_pending_message_key_refuses_automatic_replay_and_changed_payload() -> None:
    from simple_harness_service.realtime.contracts import RealtimeError

    state = recovery()
    async with _running(recovery_state=state) as (connection, session):
        with pytest.raises(RealtimeError):
            await session.append_message(
                "Ambiguous announcement", trigger_response=True, idempotency_key="pending"
            )
    state = type(state).from_snapshot(json.loads(json.dumps(state.snapshot())))
    async with _running(recovery_state=state) as (connection, session):
        for text in ["Ambiguous announcement", "Changed announcement"]:
            with pytest.raises(RealtimeError):
                await session.append_message(text, trigger_response=True, idempotency_key="pending")
        assert not any(json.loads(s)["type"] == "conversation.item.create" for s in connection.sent)


@pytest.mark.asyncio
async def test_recovery_diagnostic_identifies_first_new_user_transcript() -> None:
    state = recovery()
    async with _running(recovery_state=state) as (connection, session):
        await confirm(connection, session)
    async with _running(recovery_state=state) as (connection, session):
        await connection.incoming.put(
            json.dumps(
                {
                    "event_id": "event_next",
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": "new_user",
                    "transcript": "Continue",
                }
            )
        )
        assert (await asyncio.wait_for(anext(session.events()), 1)).kind == "TranscriptCompleted"
        assert (
            sum(
                e.operation_kind == "recovery.first_user_transcript"
                for e in session.diagnostics_snapshot().events
            )
            == 1
        )


@pytest.mark.parametrize(
    "mutation", ["schema", "unknown_session_field", "boolean_generation", "raw_audio_field"]
)
def test_recovery_snapshot_rejects_invalid_or_content_bearing_shapes(mutation: str) -> None:
    state = recovery()
    state.begin_session("session", 1)
    snapshot = state.snapshot()
    if mutation == "schema":
        snapshot["schema_version"] = True
    elif mutation == "unknown_session_field":
        snapshot["sessions"]["session"]["unknown"] = "not permitted"
    elif mutation == "boolean_generation":
        snapshot["sessions"]["session"]["generation"] = True
    else:
        snapshot["sessions"]["session"]["audio"] = "private pcm"
    with pytest.raises(ValueError):
        type(state).from_snapshot(snapshot)


@pytest.mark.asyncio
async def test_new_interpreter_restores_identity_and_dispatch_dedup() -> None:
    import subprocess
    import sys

    state = recovery()
    async with _running(recovery_state=state) as (connection, session):
        await confirm(connection, session)
        state.tool_calls("session").add("call_1")
    snapshot = json.dumps(state.snapshot())
    assert "Earlier context" not in snapshot
    code = """import json,sys
from simple_harness_service import RealtimeRecoveryState
state = RealtimeRecoveryState.from_snapshot(json.loads(sys.stdin.read()))
assert state.identity_table('session').lookup('item_server_context').source == 'context_ack'
assert 'call_1' in state.tool_calls('session')
assert state.begin_session('session', 1) == 2
print('restored')
"""
    result = await asyncio.to_thread(
        subprocess.run, [sys.executable, "-c", code], input=snapshot, text=True, capture_output=True
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "restored"


@pytest.mark.asyncio
async def test_rebound_completed_input_is_not_published_twice() -> None:
    state = recovery()
    for turn in range(2):
        async with _running(recovery_state=state) as (connection, session):
            await connection.incoming.put(
                json.dumps(
                    {
                        "event_id": "event_previous",
                        "type": "conversation.item.input_audio_transcription.completed",
                        "item_id": "prior_user",
                        "transcript": "Prior user request",
                    }
                )
            )
            if turn:
                await connection.incoming.put(
                    json.dumps(
                        {
                            "event_id": "event_fresh",
                            "type": "conversation.item.input_audio_transcription.completed",
                            "item_id": "fresh_user",
                            "transcript": "Continue",
                        }
                    )
                )
            event = await asyncio.wait_for(anext(session.events()), 1)
            assert event.turn_id == ("fresh_user" if turn else "prior_user")
            assert session.duplicate_event_count == turn


@pytest.mark.asyncio
async def test_rebound_instructions_ack_then_history_and_next_turn() -> None:
    state = recovery()
    async with _running(recovery_state=state) as (connection, session):
        await confirm(connection, session)
    async with _running(recovery_state=state) as (connection, session):
        operation = asyncio.create_task(
            session.update_instructions("Recovered instructions", trigger_response=False)
        )

        async def wait_update():
            while not any(
                json.loads(s).get("session") == {"instructions": "Recovered instructions"}
                for s in connection.sent
            ):
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_update(), 1)
        await connection.incoming.put(
            json.dumps(
                {
                    "event_id": "event_instructions",
                    "type": "session.updated",
                    "session": {"instructions": " Recovered instructions\n"},
                }
            )
        )
        await asyncio.wait_for(operation, 1)
        await confirm(connection, session, "Recovered history")
        await connection.incoming.put(
            json.dumps(
                {
                    "event_id": "event_next",
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": "fresh_user",
                    "transcript": "Continue",
                }
            )
        )
        assert (await asyncio.wait_for(anext(session.events()), 1)).kind == "TranscriptCompleted"
        assert not any(
            json.loads(s)["type"] in {"response.create", "input_audio_buffer.append"}
            for s in connection.sent
        )
