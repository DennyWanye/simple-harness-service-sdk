from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from typing import Any

import pytest
from test_realtime_context_ack import _responses, _running, _sent_message
from test_realtime_user_side_transcript import _confirmed_context, _decode

from simple_harness_service.realtime.adapters.qwen_omni import QwenOmniAdapter
from simple_harness_service.realtime.adapters.qwen_wire import QWEN_SERVER_EVENTS, QwenWireCodec
from simple_harness_service.realtime.contracts import (
    OutputAudio,
    OutputAudioCompleted,
    OutputAudioStarted,
    OutputText,
    RealtimeError,
    RealtimeErrorCode,
    RealtimeOpenRequest,
    ToolCallRequested,
    TranscriptCompleted,
    TranscriptDelta,
)
from simple_harness_service.realtime.observability import (
    RealtimeDiagnostics,
    RealtimeDiagnosticStage,
)

ITEM_EVENTS = sorted(
    kind
    for kind in QWEN_SERVER_EVENTS
    if kind.startswith("response.") and kind not in {"response.created", "response.done"}
)


def _event(kind: str, identity: str = "item_user", **fields: Any) -> dict[str, Any]:
    return {
        "event_id": "event_" + kind.replace(".", "_") + "_" + identity,
        "type": kind,
        "item_id": identity,
        "output_index": 0,
        "content_index": 0,
        **fields,
    }


def _trust(adapter: QwenOmniAdapter, source: str) -> None:
    if source == "context":
        _confirmed_context(adapter, identity="item_user")
    else:
        decoded = _decode(
            adapter,
            {
                "event_id": "event_user_source",
                "type": "input_audio_buffer." + source,
                "item_id": "item_user",
            },
        )
        assert not decoded.introduced_response_id


@pytest.mark.parametrize("kind", ITEM_EVENTS)
@pytest.mark.parametrize("source", ["context", "speech_started", "committed"])
def test_known_user_response_events_share_one_contract(kind: str, source: str) -> None:
    adapter = QwenOmniAdapter()
    _trust(adapter, source)
    fields = (
        {"delta" if kind.endswith("delta") else "transcript": ""}
        if "audio_transcript" in kind
        else {}
    )
    decoded = _decode(adapter, _event(kind, **fields))
    assert decoded.events == ()
    assert decoded.user_side_response_event_ignored == kind
    assert decoded.context_ack_id is None and decoded.context_request_id is None
    assert decoded.introduced_response_id is None and decoded.introduced_item is None
    assert decoded.introduced_content is None and decoded.completed_content is None
    assert decoded.completed_item is None and decoded.tool_call_identity is None


@pytest.mark.parametrize("kind", ITEM_EVENTS)
@pytest.mark.parametrize("source", ["unknown", "pending", "system", "assistant"])
def test_unscoped_nonuser_events_are_still_protocol_errors(kind: str, source: str) -> None:
    adapter = QwenOmniAdapter()
    identity = "item_untrusted"
    if source == "pending":
        identity = str(adapter.append_message("Pending context")["item"]["id"])
    elif source == "system":
        _confirmed_context(adapter, role="system", identity=identity)
    elif source == "assistant":
        _decode(
            adapter,
            _event(
                "response.output_item.added",
                identity,
                response_id="response_1",
                item={"id": identity, "type": "message", "role": "assistant"},
            ),
        )
    with pytest.raises(RealtimeError) as error:
        _decode(adapter, _event(kind, identity, delta="AAA=", transcript=""))
    assert error.value.code is RealtimeErrorCode.PROTOCOL_ERROR


@pytest.mark.parametrize("kind", ITEM_EVENTS)
@pytest.mark.parametrize("response_id", [None, ""])
def test_explicit_absent_response_scope_uses_user_side(kind: str, response_id: Any) -> None:
    adapter = QwenOmniAdapter()
    _trust(adapter, "context")
    decoded = _decode(adapter, _event(kind, response_id=response_id, delta="", transcript=""))
    assert not decoded.events
    assert decoded.user_side_response_event_ignored == kind
    assert decoded.context_ack_id is None and decoded.introduced_response_id is None


@pytest.mark.parametrize(
    "kind", ["response.audio_transcript.delta", "response.audio_transcript.done"]
)
def test_committed_nonempty_transcript_is_a_user_event(kind: str) -> None:
    adapter = QwenOmniAdapter()
    _trust(adapter, "committed")
    key = "delta" if kind.endswith("delta") else "transcript"
    decoded = _decode(adapter, _event(kind, **{key: "A real user request"}))
    expected = TranscriptDelta if kind.endswith("delta") else TranscriptCompleted
    assert decoded.events == (expected("item_user", "A real user request"),)
    assert decoded.user_side_response_event_ignored is None


@pytest.mark.parametrize("kind", ["response.output_item.added", "response.output_item.done"])
def test_nested_output_item_identity_is_supported_and_conflicts_rejected(kind: str) -> None:
    adapter = QwenOmniAdapter()
    _trust(adapter, "context")
    value = _event(kind, item={"id": "item_user", "type": "message", "role": "user"})
    value.pop("item_id")
    assert _decode(adapter, value).user_side_response_event_ignored == kind
    value["item_id"] = "item_unknown"
    with pytest.raises(RealtimeError):
        _decode(adapter, value)
    value["item_id"] = "item_user"
    value["item"]["id"] = "item_assistant"
    with pytest.raises(RealtimeError):
        _decode(adapter, value)


@pytest.mark.parametrize("source", ["context", "speech_started", "committed"])
def test_user_item_provenance_is_reset_for_new_session(source: str) -> None:
    adapter = QwenOmniAdapter()
    _trust(adapter, source)
    adapter.session_update(RealtimeOpenRequest("next_session", "instructions"))
    with pytest.raises(RealtimeError):
        _decode(adapter, _event("response.audio.done"))


@pytest.mark.parametrize("kind", ["response.created", "response.done"])
def test_lifecycle_events_still_require_nested_response_identity(kind: str) -> None:
    adapter = QwenOmniAdapter()
    _trust(adapter, "context")
    with pytest.raises(RealtimeError):
        _decode(adapter, _event(kind))


def test_default_wire_codec_keeps_audio_validation() -> None:
    with pytest.raises(RealtimeError):
        QwenWireCodec().decode_server_event(
            json.dumps(_event("response.audio.delta", delta="invalid"))
        )


@pytest.mark.parametrize("kind", ITEM_EVENTS)
def test_scoped_assistant_item_events_are_preserved(kind: str) -> None:
    adapter = QwenOmniAdapter()
    _trust(adapter, "context")
    fields: dict[str, Any] = {"response_id": "response_1"}
    if kind == "response.audio.delta":
        fields["delta"] = "AAA="
    elif kind in {"response.text.delta", "response.audio_transcript.delta"}:
        fields["delta"] = "A real answer"
    elif kind == "response.text.done":
        fields["text"] = "A real answer"
    elif kind == "response.audio_transcript.done":
        fields["transcript"] = "A real answer"
    elif "content_part" in kind:
        fields["part"] = {"type": "audio"}
    elif "output_item" in kind:
        fields["item"] = {"id": "item_user", "type": "message", "role": "assistant"}
    else:
        fields.update(
            call_id="call_1", name="get_weather", arguments='{"city":"Taipei"}', delta="{"
        )
    decoded = _decode(adapter, _event(kind, **fields))
    assert decoded.user_side_response_event_ignored is None
    identity = ("response_1", "item_user", 0, 0)
    if kind == "response.audio.delta":
        assert decoded.events == (OutputAudio(*identity, b"\0\0"),)
    elif kind == "response.audio.done":
        assert decoded.events == (OutputAudioCompleted(*identity),)
    elif "audio_transcript" in kind or kind.startswith("response.text."):
        assert decoded.events == (OutputText(*identity, "A real answer", kind.endswith("delta")),)
    elif kind == "response.content_part.added":
        assert decoded.events == (OutputAudioStarted(*identity),)
        assert decoded.introduced_content == identity
    elif kind == "response.content_part.done":
        assert decoded.completed_content == identity
    elif kind == "response.output_item.added":
        assert decoded.introduced_item == identity[:2]
    elif kind == "response.output_item.done":
        assert decoded.completed_item == identity[:2]
    elif kind.endswith("arguments.done"):
        assert isinstance(decoded.events[0], ToolCallRequested)
    else:
        assert not decoded.events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "variant", ["response.audio.done", "response.content_part.done", "response.output_item.done"]
)
async def test_context_transcript_completion_then_next_user_turn_stays_live(variant: str) -> None:
    diagnostics = RealtimeDiagnostics()
    async with _running(diagnostics=diagnostics) as (connection, session):
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
        generation = session.generation
        transcript = _event("response.audio_transcript.done", transcript="")
        completion = _event(variant)
        if variant == "response.audio.done":
            assert set(completion) == {
                "content_index",
                "event_id",
                "item_id",
                "output_index",
                "type",
            }
        for value in [
            transcript,
            completion,
            completion,
            {
                "event_id": "event_fresh_user",
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": "new_audio_item",
                "transcript": "Continue",
            },
        ]:
            await connection.incoming.put(json.dumps(value))
        stream = session.events()
        assert await asyncio.wait_for(anext(stream), 1) == TranscriptCompleted(
            "new_audio_item", "Continue"
        )
        assert session.generation == generation and not connection.closed
        assert session.context_ack_id == "item_user" and not _responses(connection)
        assert not session._terminal_published.is_set()
        assert session.duplicate_event_count == 1
        ignored = [
            e
            for e in diagnostics.snapshot().events
            if e.operation_kind == "user_side_response_event_ignored"
        ]
        assert [e.provider_event_type for e in ignored] == [transcript["type"], variant]
        assert not any(
            e.stage is RealtimeDiagnosticStage.PROVIDER_EVENT_DECODE_FAILED
            for e in diagnostics.snapshot().events
        )
        assert "Earlier user context" not in repr(diagnostics.snapshot())
        assert "item_user" not in repr(diagnostics.snapshot())


@pytest.mark.parametrize(
    "kind", ["response.text.delta", "response.text.done", "response.audio.delta"]
)
def test_known_user_payload_is_not_decoded_as_assistant_content(kind: str) -> None:
    adapter = QwenOmniAdapter()
    _trust(adapter, "context")
    decoded = _decode(
        adapter, _event(kind, delta="not base64 or assistant text", text="A user echo")
    )
    assert not decoded.events
    assert decoded.user_side_response_event_ignored == kind


@pytest.mark.parametrize("identity", [None, "", [], {}])
def test_committed_must_supply_a_valid_user_identity(identity: Any) -> None:
    with pytest.raises(RealtimeError):
        _decode(
            QwenOmniAdapter(),
            {
                "event_id": "event_commit",
                "type": "input_audio_buffer.committed",
                "item_id": identity,
            },
        )


@pytest.mark.asyncio
async def test_user_completions_do_not_complete_interleaved_assistant_output() -> None:
    async with _running() as (connection, session):
        values = [
            {
                "event_id": "event_committed",
                "type": "input_audio_buffer.committed",
                "item_id": "item_user",
            },
            {
                "event_id": "event_response",
                "type": "response.created",
                "response": {"id": "response_1"},
            },
            _event(
                "response.output_item.added",
                "item_assistant",
                response_id="response_1",
                item={"id": "item_assistant", "type": "message", "role": "assistant"},
            ),
            _event(
                "response.content_part.added",
                "item_assistant",
                response_id="response_1",
                part={"type": "audio"},
            ),
            _event("response.audio_transcript.done", transcript=""),
            _event("response.audio.done"),
            _event("response.content_part.done"),
            _event("response.output_item.done"),
            _event(
                "response.audio.delta", "item_assistant", response_id="response_1", delta="AAA="
            ),
            _event("response.audio.done", "item_assistant", response_id="response_1"),
        ]
        for value in values:
            await connection.incoming.put(json.dumps(value))
        stream = session.events()
        received = [await asyncio.wait_for(anext(stream), 1) for _ in range(4)]
        identity = ("response_1", "item_assistant", 0, 0)
        assert [e.kind for e in received] == [
            "ResponseStarted",
            "OutputAudioStarted",
            "OutputAudio",
            "OutputAudioCompleted",
        ]
        assert received[2] == OutputAudio(*identity, b"\0\0")
        assert received[3] == OutputAudioCompleted(*identity)
        assert identity not in session._completed_contents
        assert identity[:2] not in session._completed_items
        assert not session._terminal_published.is_set() and not _responses(connection)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "variant", ["response.audio.done", "response.content_part.done", "response.output_item.done"]
)
async def test_old_generation_user_completion_is_not_applied(variant: str) -> None:
    current = True
    diagnostics = RealtimeDiagnostics()
    async with _running(is_current_generation=lambda: current, diagnostics=diagnostics) as (
        connection,
        session,
    ):
        await connection.incoming.put(
            json.dumps(
                {
                    "event_id": "event_speech",
                    "type": "input_audio_buffer.speech_started",
                    "item_id": "item_user",
                }
            )
        )
        assert (await asyncio.wait_for(anext(session.events()), 1)).kind == "SpeechStarted"
        current = False
        await connection.incoming.put(json.dumps(_event(variant)))
        for _ in range(30):
            await asyncio.sleep(0)
        assert session.late_event_count == 1 and session._events.empty()
        assert not session._terminal_published.is_set() and not _responses(connection)
        assert not any(
            e.operation_kind == "user_side_response_event_ignored"
            for e in diagnostics.snapshot().events
        )
