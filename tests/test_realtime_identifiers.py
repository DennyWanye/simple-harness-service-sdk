from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from typing import Any

import pytest
from test_realtime_context_ack import _responses, _running, _sent_message
from test_realtime_stage2 import _ack
from test_realtime_user_side_response import ITEM_EVENTS, _event, _trust
from test_realtime_user_side_transcript import _confirmed_context, _decode

from simple_harness_service.realtime.adapters.openai import OpenAIRealtimeAdapter
from simple_harness_service.realtime.adapters.qwen_omni import QwenOmniAdapter
from simple_harness_service.realtime.contracts import (
    RealtimeError,
    RealtimeErrorCode,
    RealtimeOpenRequest,
    TranscriptCompleted,
    TranscriptDelta,
)
from simple_harness_service.realtime.observability import RealtimeDiagnostics

MISSING = object()
ABSENT_IDS = [MISSING, None, False, 7, [], {}, "", " \t\n"]
ABSENT_NAMES = ["missing", "null", "boolean", "number", "array", "object", "empty", "whitespace"]


def _field(value: dict[str, Any], key: str, identity: Any) -> None:
    if identity is MISSING:
        value.pop(key, None)
    else:
        value[key] = identity


@pytest.mark.parametrize("kind", ITEM_EVENTS)
@pytest.mark.parametrize("source", ["context", "speech_started", "committed"])
@pytest.mark.parametrize("identity", ABSENT_IDS, ids=ABSENT_NAMES)
def test_all_absent_response_identifiers_use_user_contract(
    kind: str, source: str, identity: Any
) -> None:
    adapter = QwenOmniAdapter()
    _trust(adapter, source)
    value = _event(kind, delta="", transcript="")
    _field(value, "response_id", identity)
    decoded = _decode(adapter, value)
    assert not decoded.events
    assert decoded.user_side_response_event_ignored == kind
    assert decoded.context_ack_id is None and decoded.introduced_response_id is None
    assert decoded.completed_item is None and decoded.completed_content is None
    assert decoded.tool_call_identity is None


@pytest.mark.parametrize(
    "kind", ["response.audio_transcript.delta", "response.audio_transcript.done"]
)
@pytest.mark.parametrize("identity", ABSENT_IDS, ids=ABSENT_NAMES)
def test_nonempty_user_transcript_with_absent_response_identifier(kind: str, identity: Any) -> None:
    adapter = QwenOmniAdapter()
    _trust(adapter, "context")
    key = "delta" if kind.endswith("delta") else "transcript"
    value = _event(kind, **{key: "A user transcript"})
    _field(value, "response_id", identity)
    decoded = _decode(adapter, value)
    expected = TranscriptDelta if kind.endswith("delta") else TranscriptCompleted
    assert decoded.events == (expected("item_user", "A user transcript"),)


@pytest.mark.parametrize("identity", ABSENT_IDS, ids=ABSENT_NAMES)
def test_user_response_without_item_identity_is_still_rejected(identity: Any) -> None:
    adapter = QwenOmniAdapter()
    _trust(adapter, "context")
    value = _event("response.audio.done", response_id="")
    _field(value, "item_id", identity)
    with pytest.raises(RealtimeError) as error:
        _decode(adapter, value)
    assert error.value.code is RealtimeErrorCode.PROTOCOL_ERROR


@pytest.mark.parametrize("identity", ABSENT_IDS, ids=ABSENT_NAMES)
@pytest.mark.parametrize("source", ["unknown", "assistant", "previous_session"])
def test_absent_response_identifier_cannot_borrow_active_response(
    identity: Any, source: str
) -> None:
    adapter = QwenOmniAdapter()
    item_id = "item_unknown"
    if source == "previous_session":
        item_id = _confirmed_context(adapter)
        adapter.session_update(RealtimeOpenRequest("next_session", "instructions"))
    _decode(
        adapter,
        {
            "event_id": "event_response",
            "type": "response.created",
            "response": {"id": "response_active"},
        },
    )
    if source == "assistant":
        item_id = "item_assistant"
        _decode(
            adapter,
            _event(
                "response.output_item.added",
                item_id,
                response_id="response_active",
                item={"id": item_id, "type": "message"},
            ),
        )
    value = _event("response.audio.done", item_id)
    _field(value, "response_id", identity)
    with pytest.raises(RealtimeError):
        _decode(adapter, value)


@pytest.mark.parametrize("adapter_class", [QwenOmniAdapter, OpenAIRealtimeAdapter])
@pytest.mark.parametrize("field", ["response_id", "item_id", "call_id", "response.id", "item.id"])
@pytest.mark.parametrize("identity", ABSENT_IDS, ids=ABSENT_NAMES)
def test_assistant_paths_require_real_identifiers(
    adapter_class: Any, field: str, identity: Any
) -> None:
    adapter = adapter_class()
    if field in {"response_id", "item_id"}:
        value = {
            "response_id": "response_1",
            "item_id": "item_1",
            "output_index": 0,
            "content_index": 0,
        }
        _field(value, field, identity)
        with pytest.raises(RealtimeError):
            adapter._content_identity(value)
        return
    if field == "response.id":
        value = {
            "event_id": "event_response",
            "type": "response.created",
            "response": {"id": "response_1"},
        }
        _field(value["response"], "id", identity)
    elif field == "item.id":
        value = _event(
            "response.output_item.added",
            response_id="response_1",
            item={"id": "item_1", "type": "message"},
        )
        _field(value["item"], "id", identity)
    else:
        value = _event(
            "response.function_call_arguments.done",
            response_id="response_1",
            call_id="call_1",
            name="get_weather",
            arguments="{}",
        )
        _field(value, "call_id", identity)
    with pytest.raises(RealtimeError) as error:
        _decode(adapter, value)
    assert error.value.code is RealtimeErrorCode.PROTOCOL_ERROR


@pytest.mark.parametrize("field", ["id", "call_id"])
@pytest.mark.parametrize("identity", ABSENT_IDS, ids=ABSENT_NAMES)
def test_tool_ack_identifiers_are_required(field: str, identity: Any) -> None:
    item = {
        "id": "tool_result",
        "type": "function_call_output",
        "status": "completed",
        "call_id": "call_1",
        "output": "done",
    }
    _field(item, field, identity)
    with pytest.raises(RealtimeError):
        _decode(
            QwenOmniAdapter(),
            {"event_id": "event_tool_ack", "type": "conversation.item.created", "item": item},
        )


@pytest.mark.parametrize("identity", ABSENT_IDS, ids=ABSENT_NAMES)
def test_session_identity_is_required(identity: Any) -> None:
    value = _ack()
    _field(value["session"], "id", identity)
    with pytest.raises(RealtimeError):
        _decode(QwenOmniAdapter(), value)


@pytest.mark.parametrize("identity", ABSENT_IDS, ids=ABSENT_NAMES)
@pytest.mark.parametrize("empty_field", ["item_id", "item.id"])
def test_output_item_uses_only_real_aliases(identity: Any, empty_field: str) -> None:
    adapter = QwenOmniAdapter()
    _trust(adapter, "context")
    value = _event("response.output_item.done", response_id="", item={"id": "item_user"})
    if empty_field == "item_id":
        _field(value, "item_id", identity)
    else:
        _field(value["item"], "id", identity)
    assert _decode(adapter, value).user_side_response_event_ignored == value["type"]


def test_nonblank_identifiers_and_text_are_preserved_verbatim() -> None:
    adapter = QwenOmniAdapter()
    decoded = _decode(
        adapter,
        {
            "event_id": "event_response",
            "type": "response.created",
            "response": {"id": " response_1 "},
        },
    )
    assert decoded.introduced_response_id == " response_1 "
    _trust(adapter, "context")
    value = _event("response.audio_transcript.done", transcript=" \t", response_id=None)
    assert _decode(adapter, value).events == (TranscriptCompleted("item_user", " \t"),)


async def _confirm_history(connection: Any, session: Any) -> None:
    operation = asyncio.create_task(
        session.append_message("Earlier user context", trigger_response=False)
    )
    request = await _sent_message(connection)
    item = deepcopy(request["item"])
    item.update(id="item_user", status="completed")
    await connection.incoming.put(
        json.dumps(
            {"event_id": "event_context_ack", "type": "conversation.item.created", "item": item}
        )
    )
    await asyncio.wait_for(operation, 1)


@pytest.mark.asyncio
async def test_host_empty_scope_history_sequence_keeps_session_alive() -> None:
    diagnostics = RealtimeDiagnostics()
    async with _running(diagnostics=diagnostics) as (connection, session):
        await _confirm_history(connection, session)
        for value in [
            _event("response.audio_transcript.done", transcript="", response_id=""),
            _event("response.audio.done", response_id=None),
            {
                "event_id": "event_next_user",
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "new_audio_item",
                "transcript": "Continue",
            },
        ]:
            await connection.incoming.put(json.dumps(value))
        assert await asyncio.wait_for(anext(session.events()), 1) == TranscriptCompleted(
            "new_audio_item", "Continue"
        )
        assert not session._terminal_published.is_set() and not connection.closed
        assert not _responses(connection) and session.context_ack_id == "item_user"
        ignored = [
            e
            for e in diagnostics.snapshot().events
            if e.operation_kind == "user_side_response_event_ignored"
        ]
        assert [e.provider_event_type for e in ignored] == [
            "response.audio_transcript.done",
            "response.audio.done",
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("finished", [False, True], ids=["active", "finished"])
@pytest.mark.parametrize("scope", ["", None], ids=["empty", "null"])
async def test_review7_user_sidecars_do_not_complete_interleaved_assistant(
    finished: bool, scope: Any
) -> None:
    async with _running() as (connection, session):
        await _confirm_history(connection, session)
        generation = session.generation
        values = [
            {
                "event_id": "event_response",
                "type": "response.created",
                "response": {"id": "response_active"},
            },
            _event(
                "response.output_item.added",
                "item_assistant",
                response_id="response_active",
                item={"id": "item_assistant", "type": "message"},
            ),
        ]
        if finished:
            values.append(
                {
                    "event_id": "event_finished",
                    "type": "response.done",
                    "response": {"id": "response_active", "status": "cancelled"},
                }
            )
        values += [
            _event("response.audio_transcript.done", transcript="", response_id=scope),
            _event("response.audio.done", response_id=scope),
            {
                "event_id": "event_next_user",
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "new_audio_item",
                "transcript": "Continue",
            },
        ]
        for value in values:
            await connection.incoming.put(json.dumps(value))
        stream = session.events()
        events = [await asyncio.wait_for(anext(stream), 1) for _ in range(3 if finished else 2)]
        assert [e.kind for e in events] == (
            ["ResponseStarted", "ResponseFinished", "TranscriptCompleted"]
            if finished
            else ["ResponseStarted", "TranscriptCompleted"]
        )
        assert events[-1] == TranscriptCompleted("new_audio_item", "Continue")
        assert not session._completed_items and not session._completed_contents
        assert not session._terminal_published.is_set() and not connection.closed
        assert not _responses(connection) and session.generation == generation
