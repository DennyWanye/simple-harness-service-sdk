from __future__ import annotations

import asyncio
import base64
import inspect
import json
from collections import Counter
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from test_realtime_session import FakeConnection, _credential

from simple_harness_service.realtime.adapters.qwen_omni import QwenOmniAdapter
from simple_harness_service.realtime.contracts import (
    OutputAudio,
    OutputAudioStarted,
    RealtimeErrorCode,
    RealtimeOpenRequest,
    ResponseFinished,
    ResponseStarted,
    ResponseStatus,
    SessionFailed,
    ToolCallRequested,
    TranscriptCompleted,
)
from simple_harness_service.realtime.relay_control import RelayControlCodec
from simple_harness_service.realtime.session import ManagedRealtimeSession

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/qwen-response-supersede-20261030.json").read_text()
)
IDS = FIXTURE["ids"]
PENDING = pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="Pending negotiated Qwen response_supersede compatibility; proposal only",
)


def _frame(record: dict[str, Any]) -> dict[str, Any]:
    frame = deepcopy(record["frame"])
    if record["origin"] == "synthetic_audio":
        frame["delta"] = base64.b64encode(bytes(record["audio_decoded_bytes"])).decode("ascii")
        assert len(frame["delta"]) == record["audio_base64_length"]
    return frame


def _scope(frame: dict[str, Any]) -> str | None:
    return frame.get("response_id", frame.get("response", {}).get("id"))


class _Dispatcher:
    def __init__(self) -> None:
        self.calls: list[ToolCallRequested] = []

    async def dispatch(self, call: ToolCallRequested) -> str:
        self.calls.append(call)
        assert json.loads(call.arguments_json) == {"city": "北京"}
        return json.dumps(FIXTURE["tool_results"][call.name], ensure_ascii=False)


class _AcknowledgingConnection(FakeConnection):
    session: ManagedRealtimeSession

    async def send_text(self, payload: str) -> None:
        await super().send_text(payload)
        value = json.loads(payload)
        if value["type"] != "conversation.item.create":
            return
        item = value["item"]
        if item["type"] != "function_call_output":
            return
        # ACKs are synthetic transport feedback, not original provider trace entries.
        ack = {
            "type": "conversation.item.created",
            "event_id": f"event_synthetic_ack_{len(self.sent)}",
            "item": {
                **item,
                "id": f"item_synthetic_result_{len(self.sent)}",
                "status": "completed",
            },
        }
        assert await self.session._apply(self.session._adapter.decode_server_event(json.dumps(ack)))


@asynccontextmanager
async def _running() -> Any:
    connection = _AcknowledgingConnection()
    dispatcher = _Dispatcher()
    session = ManagedRealtimeSession(
        connection=connection,
        adapter=QwenOmniAdapter(),
        control=RelayControlCodec(),
        credential=_credential(),
        request=RealtimeOpenRequest("session", "Synthetic QA", tools=deepcopy(FIXTURE["tools"])),
        correlation="corr_0123456789ABCDEFGHJKMNPQRS",
        generation=1,
        tool_dispatcher=dispatcher,
        close_timeout=0.01,
        tool_ack_timeout=0.1,
    )
    connection.session = session
    session._ready = True
    try:
        yield connection, session, dispatcher
    finally:
        await session.close()


async def _apply(session: ManagedRealtimeSession, frame: dict[str, Any]) -> bool:
    accepted = await session._apply(session._adapter.decode_server_event(json.dumps(frame)))
    if session._tool_tasks:
        await asyncio.wait_for(asyncio.gather(*tuple(session._tool_tasks)), 1)
    return accepted


async def _replay(session: ManagedRealtimeSession) -> dict[str, Any] | None:
    for record in FIXTURE["frames"]:
        if not await _apply(session, _frame(record)):
            return record
    return None


def _drain(session: ManagedRealtimeSession) -> list[Any]:
    events = []
    while not session._events.empty():
        events.append(session._events.get_nowait())
    return events


def test_fixture_preserves_observed_identity_pattern_and_audio_totals() -> None:
    frames = [_frame(record) for record in FIXTURE["frames"]]
    assert len(frames) == 62
    assert FIXTURE["observation_after_final_ms"] == 65_000
    assert [_scope(f) for f in frames if f["type"] == "response.created"] == [
        IDS["weather"],
        IDS["prefix"],
        IDS["successor"],
        IDS["final"],
    ]
    prefix = [f for f in frames if _scope(f) == IDS["prefix"]]
    successor = [f for f in frames if _scope(f) == IDS["successor"]]
    assert Counter(f["type"] for f in prefix) == {
        "response.created": 1,
        "response.output_item.added": 1,
        "response.content_part.added": 1,
        "response.audio_transcript.delta": 3,
    }
    assert prefix[1]["item"]["id"] == successor[1]["item"]["id"] == IDS["shared_item"]
    assert prefix[1]["item"]["type"] == "message"
    assert successor[1]["item"]["type"] == "function_call"
    assert not any(f["type"] == "response.content_part.added" for f in successor)
    for name, count, total in [("successor", 12, 172_800), ("final", 11, 168_960)]:
        audio = [
            f for f in frames if _scope(f) == IDS[name] and f["type"] == "response.audio.delta"
        ]
        assert len(audio) == count
        assert sum(len(base64.b64decode(f["delta"])) for f in audio) == total
    sdk = FIXTURE["sdk_serialized_trace"]
    assert [
        (e["trace_index"], e["response_id"]) for e in sdk if e["type"] == "response.created"
    ] == [(127, "resp_SL3aE6BWRRtkIB6qKKsMJ"), (140, "resp_KjdRWL4HW7PWfS3HXvuf2")]
    assert sdk[-1]["trace_index"] == 148 and sdk[-1]["type"] == "tokenseller.error"
    assert not any(e["response_id"] == "resp_JoNI8BteDEoQ2OxUuqEzA" for e in sdk)


@pytest.mark.asyncio
async def test_current_sdk_first_failure_is_successor_created(monkeypatch: Any) -> None:
    async with _running() as (connection, session, dispatcher):
        failure_lines = []
        original = session._fail

        async def record_failure(code: RealtimeErrorCode, *, retryable: bool) -> None:
            caller = inspect.currentframe()
            assert caller is not None and caller.f_back is not None
            failure_lines.append(caller.f_back.f_lineno)
            await original(code, retryable=retryable)

        monkeypatch.setattr(session, "_fail", record_failure)
        failed = await _replay(session)
        assert failed is not None
        assert (failed["sequence"], failed["trace_index"]) == (18, 28)
        assert failed["frame"]["type"] == "response.created"
        assert _scope(failed["frame"]) == IDS["successor"]
        # Pin the precise baseline check, not just any PROTOCOL_ERROR.
        assert failure_lines == [977]
        assert session._responses - session._terminal_responses == {IDS["prefix"]}
        assert IDS["prefix"] not in session._cancelled_responses
        failures = [e for e in _drain(session) if isinstance(e, SessionFailed)]
        assert len(failures) == 1 and failures[0].code is RealtimeErrorCode.PROTOCOL_ERROR
        assert [call.call_id for call in dispatcher.calls] == [IDS["weather_call"]]
        assert connection.closed == 1


@pytest.mark.asyncio
async def test_admission_alone_still_fails_at_unintroduced_audio_content(monkeypatch: Any) -> None:
    async with _running() as (_, session, _):
        failure_lines = []
        original = session._fail

        async def record_failure(code: RealtimeErrorCode, *, retryable: bool) -> None:
            caller = inspect.currentframe()
            assert caller is not None and caller.f_back is not None
            failure_lines.append(caller.f_back.f_lineno)
            await original(code, retryable=retryable)

        monkeypatch.setattr(session, "_fail", record_failure)
        for record in FIXTURE["frames"]:
            if record["sequence"] == 18:
                # Counterfactual isolation only: this is NOT a compatibility fix.
                session._cancelled_responses.add(IDS["prefix"])
            if not await _apply(session, _frame(record)):
                assert record["sequence"] == 23
                assert record["frame"]["type"] == "response.audio.delta"
                break
        else:
            pytest.fail("Expected the missing-content identity check")
        assert failure_lines == [1121]
        assert (IDS["prefix"], IDS["shared_item"]) in session._items
        assert (IDS["successor"], IDS["shared_item"]) in session._items
        assert (IDS["prefix"], IDS["shared_item"], 0, 0) in session._contents
        assert (IDS["successor"], IDS["shared_item"], 0, 0) not in session._contents
        assert session._terminal_error_code is RealtimeErrorCode.PROTOCOL_ERROR


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["unknown_response", "unrelated_overlap", "third_live_response"])
async def test_unmatched_identity_patterns_remain_protocol_errors(case: str) -> None:
    async with _running() as (_, session, _):
        frames = [_frame(r) for r in FIXTURE["frames"] if r["sequence"] >= 12]
        for frame in frames[:6]:
            assert await _apply(session, frame)
        if case == "unknown_response":
            bad = {**frames[4], "response_id": "resp_unknown"}
        elif case == "unrelated_overlap":
            bad = frames[6]
        else:
            # Preserve the existing cancellation overlap allowance while checking its cap.
            session._cancelled_responses.add(IDS["prefix"])
            assert await _apply(session, frames[6])
            session._cancelled_responses.add(IDS["successor"])
            bad = {**frames[6], "response": {"id": "resp_third", "status": "in_progress"}}
        assert not await _apply(session, bad)
        assert session._terminal_error_code is RealtimeErrorCode.PROTOCOL_ERROR


@PENDING
@pytest.mark.asyncio
async def test_expected_successor_is_admitted_as_replacement() -> None:
    async with _running() as (_, session, _):
        failed = await _replay(session)
        sequence = failed["sequence"] if failed else None
        assert failed is None, f"Compatibility missing at fixture frame {sequence}"


@PENDING
@pytest.mark.asyncio
async def test_expected_prefix_is_superseded_without_fabricated_terminal_or_usage() -> None:
    async with _running() as (_, session, _):
        await _replay(session)
        # Proposed content-free API; the implementation must also wire negotiated opt-in.
        snapshot = getattr(session, "response_identity_snapshot", None)
        assert callable(snapshot), "Missing proposed response identity diagnostics API"
        rows = {row["provider_response_id"]: row for row in snapshot()}
        prefix = rows[IDS["prefix"]]
        successor = rows[IDS["successor"]]
        assert prefix["local_state"] == "superseded"
        assert prefix["provider_status"] == "in_progress"
        assert prefix["provider_terminal_status"] is None and prefix["usage"] is None
        assert prefix["superseded_by"] == IDS["successor"]
        assert prefix["logical_response_id"] == successor["logical_response_id"] == IDS["prefix"]
        assert successor["provider_terminal_status"] == "completed"


@PENDING
@pytest.mark.asyncio
async def test_expected_audio_inherits_logical_content_and_preserves_bytes() -> None:
    async with _running() as (_, session, _):
        await _replay(session)
        events = _drain(session)
        audio = [e for e in events if isinstance(e, OutputAudio)]
        assert sum(len(e.data) for e in audio if e.response_id == IDS["prefix"]) == 172_800
        assert sum(len(e.data) for e in audio if e.response_id == IDS["final"]) == 168_960
        assert all(
            (e.item_id, e.output_index, e.content_index) == (IDS["shared_item"], 0, 0)
            for e in audio
            if e.response_id == IDS["prefix"]
        )
        assert {e.response_id for e in audio} == {IDS["prefix"], IDS["final"]}
        assert Counter(e.response_id for e in events if isinstance(e, OutputAudioStarted)) == {
            IDS["prefix"]: 1,
            IDS["final"]: 1,
        }


@PENDING
@pytest.mark.asyncio
async def test_expected_each_call_executes_and_submits_one_result_and_one_followup() -> None:
    async with _running() as (connection, session, dispatcher):
        await _replay(session)
        expected = Counter([IDS["weather_call"], IDS["time_call"]])
        assert Counter(call.call_id for call in dispatcher.calls) == expected
        sent = [json.loads(payload) for payload in connection.sent]
        results = [e["item"] for e in sent if e["type"] == "conversation.item.create"]
        assert Counter(item["call_id"] for item in results) == expected
        assert len([e for e in sent if e["type"] == "response.create"]) == 2
        assert [json.loads(item["output"]) for item in results] == list(
            FIXTURE["tool_results"].values()
        )


@PENDING
@pytest.mark.asyncio
async def test_expected_public_lifecycle_is_contiguous_and_asr_final_is_user_only() -> None:
    async with _running() as (_, session, _):
        await _replay(session)
        events = _drain(session)
        assert not any(isinstance(e, SessionFailed) for e in events)
        expected_ids = [IDS["weather"], IDS["prefix"], IDS["final"]]
        assert [e.response_id for e in events if isinstance(e, ResponseStarted)] == expected_ids
        finished = [e for e in events if isinstance(e, ResponseFinished)]
        assert [e.response_id for e in finished] == expected_ids
        assert all(e.status is ResponseStatus.COMPLETED and not e.local for e in finished)
        assert [e.response_id for e in events if isinstance(e, ToolCallRequested)] == expected_ids[
            :2
        ]
        active = None
        for event in events:
            if isinstance(event, ResponseStarted):
                assert active is None
                active = event.response_id
            elif isinstance(event, (OutputAudio, ToolCallRequested)):
                assert event.response_id == active
            elif isinstance(event, ResponseFinished):
                assert event.response_id == active
                active = None
        assert active is None
        finals = [e for e in events if isinstance(e, TranscriptCompleted)]
        assert len(finals) == 1
        assert finals[0].turn_id == FIXTURE["frames"][0]["frame"]["item_id"]
