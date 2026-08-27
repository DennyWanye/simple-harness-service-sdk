from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from simple_harness_service.realtime.adapters.openai import OpenAIRealtimeAdapter
from simple_harness_service.realtime.adapters.openai_wire import OpenAIWireCodec
from simple_harness_service.realtime.adapters.qwen_omni import QwenOmniAdapter
from simple_harness_service.realtime.adapters.qwen_wire import QwenWireCodec
from simple_harness_service.realtime.contracts import (
    OutputAudio,
    RealtimeError,
    RealtimeErrorCode,
    RealtimeOpenRequest,
    ResponseFinished,
    ResponseStatus,
    SessionFailed,
)

ROOT = Path(__file__).parents[1]
PACKS = ROOT / "ARCHITECTURE/protocols"


@pytest.mark.parametrize(
    ("source_glob", "forbidden_identifier"),
    [("openai*.py", "qwen"), ("qwen*.py", "openai")],
)
def test_provider_adapters_have_no_cross_provider_source_dependency(
    source_glob: str,
    forbidden_identifier: str,
) -> None:
    adapter_root = ROOT / "src/simple_harness_service/realtime/adapters"
    for source_path in sorted(adapter_root.glob(source_glob)):
        source = source_path.read_text(encoding="utf-8")
        assert (
            re.search(
                rf"\b{re.escape(forbidden_identifier)}\w*\b",
                source,
                flags=re.IGNORECASE,
            )
            is None
        ), source_path


def _fixture(provider: str, name: str) -> str:
    version = "2026-08-28.1" if provider == "qwen" else "2026-08-27.1"
    folder = f"{provider}-native-{version}"
    return (PACKS / folder / name).read_text()


@pytest.mark.parametrize(
    ("adapter", "provider", "audio_event"),
    [
        (QwenOmniAdapter(), "qwen", "response.audio.delta"),
        (OpenAIRealtimeAdapter(), "openai", "response.output_audio.delta"),
    ],
)
def test_both_adapters_decode_same_consumer_audio_and_terminal_contract(
    adapter: Any,
    provider: str,
    audio_event: str,
) -> None:
    lifecycle = json.loads(_fixture(provider, "server-lifecycle-sequence.json"))
    updated = adapter.decode_server_event(
        json.dumps(lifecycle["scenarios"][0]["events"][0])
    )
    if provider == "openai":
        session = adapter.decode_server_event(
            _fixture(provider, "server-session-created.json")
        )
        assert session.session_acknowledged
        assert not session.provider_ready
    assert updated.session_acknowledged
    assert updated.provider_ready

    audio = adapter.decode_server_event(_fixture(provider, "server-audio-delta.json"))
    assert json.loads(_fixture(provider, "server-audio-delta.json"))["type"] == audio_event
    assert isinstance(audio.events[0], OutputAudio)
    assert audio.events[0].data == b"\x00\x00\x00\x00"

    terminal = adapter.decode_server_event(_fixture(provider, "server-response-done.json"))
    assert isinstance(terminal.events[0], ResponseFinished)
    assert terminal.events[0].status.value == "completed"
    assert terminal.events[0].usage is not None


def test_qwen_session_update_is_nested_native_shape_and_cancel_has_no_response_id() -> None:
    adapter = QwenOmniAdapter()
    update = adapter.session_update(RealtimeOpenRequest("session", "instructions"))
    session = update["session"]
    assert isinstance(session, dict)
    assert session["audio"]["input"]["format"] == {
        "type": "pcm",
        "sample_rate": 16_000,
    }
    assert "response_id" not in adapter.cancel_response("response-1")
    with pytest.raises(RealtimeError) as caught:
        adapter.truncate_output("item", 0, 100)
    assert caught.value.code is RealtimeErrorCode.UNSUPPORTED


def test_openai_offline_seam_supports_truncate_but_refuses_live_enablement() -> None:
    adapter = OpenAIRealtimeAdapter()
    truncate = adapter.truncate_output("item", 0, 750)
    assert truncate["type"] == "conversation.item.truncate"
    with pytest.raises(RealtimeError) as caught:
        OpenAIRealtimeAdapter(enable_live=True)
    assert caught.value.code is RealtimeErrorCode.UNSUPPORTED


@pytest.mark.parametrize(
    ("provider", "adapter"),
    [("qwen", QwenOmniAdapter()), ("openai", OpenAIRealtimeAdapter())],
)
@pytest.mark.parametrize("mutation", ["missing_session", "model", "voice", "format"])
def test_session_ack_requires_frozen_negotiated_configuration(
    provider: str,
    adapter: Any,
    mutation: str,
) -> None:
    if provider == "qwen":
        lifecycle = json.loads(_fixture(provider, "server-lifecycle-sequence.json"))
        value = lifecycle["scenarios"][0]["events"][0]
    else:
        value = json.loads(_fixture(provider, "server-session-created.json"))
    if mutation == "missing_session":
        value.pop("session")
    elif mutation == "model":
        value["session"]["model"] = "future-model"
    elif mutation == "voice":
        if provider == "qwen":
            value["session"]["voice"] = "future-voice"
        else:
            value["session"]["audio"]["output"]["voice"] = "future-voice"
    elif provider == "qwen":
        value["session"]["audio"]["input"]["format"]["sample_rate"] = 24_000
    else:
        value["session"]["audio"]["input"]["format"]["rate"] = 16_000

    with pytest.raises(RealtimeError) as caught:
        adapter.decode_server_event(json.dumps(value))

    assert caught.value.code is RealtimeErrorCode.PROTOCOL_ERROR


@pytest.mark.parametrize(
    ("adapter", "provider", "expected"),
    [
        (QwenOmniAdapter(), "qwen", RealtimeErrorCode.RATE_LIMITED),
        (OpenAIRealtimeAdapter(), "openai", RealtimeErrorCode.RATE_LIMITED),
    ],
)
def test_provider_error_mapping_is_closed(
    adapter: Any,
    provider: str,
    expected: RealtimeErrorCode,
) -> None:
    mapping = json.loads(_fixture(provider, "error-mapping.json"))
    payload = json.dumps(mapping["vectors"][1]["input"])
    decoded = adapter.decode_server_event(payload)
    assert decoded.events == (SessionFailed(expected, True),)

    unknown = json.dumps(mapping["vectors"][-1]["input"])
    decoded_unknown = adapter.decode_server_event(unknown)
    assert decoded_unknown.events == (
        SessionFailed(RealtimeErrorCode.PROTOCOL_ERROR, False),
    )


def test_wire_codecs_fail_closed_on_unknown_or_invalid_audio() -> None:
    adapter = QwenOmniAdapter()
    with pytest.raises(RealtimeError):
        adapter.decode_server_event('{"event_id":"event_x","type":"future.event"}')
    with pytest.raises(RealtimeError):
        adapter.decode_server_event(
            '{"event_id":"event_x","type":"response.audio.delta",'
            '"response_id":"r","item_id":"i","output_index":0,'
            '"content_index":0,"delta":"***"}'
        )


@pytest.mark.parametrize(
    ("provider", "adapter"),
    [("qwen", QwenOmniAdapter()), ("openai", OpenAIRealtimeAdapter())],
)
def test_every_frozen_server_lifecycle_event_is_decodable(
    provider: str, adapter: Any
) -> None:
    lifecycle = json.loads(_fixture(provider, "server-lifecycle-sequence.json"))
    for scenario in lifecycle["scenarios"]:
        for event in scenario["events"]:
            decoded = adapter.decode_server_event(json.dumps(event))
            assert decoded.event_id == event["event_id"]


@pytest.mark.parametrize(
    ("provider", "wire"),
    [("qwen", QwenWireCodec()), ("openai", OpenAIWireCodec())],
)
def test_every_frozen_client_event_vector_is_encodable(provider: str, wire: Any) -> None:
    direct = [
        "client-session-update.json",
        "client-audio-append.json",
        "client-response-cancel.json",
        "client-tool-result.json",
    ]
    if provider == "openai":
        direct.append("client-truncate.json")
    for name in direct:
        value = json.loads(_fixture(provider, name))
        assert json.loads(wire.encode_client_event(value)) == value
    lifecycle = json.loads(_fixture(provider, "client-lifecycle-sequence.json"))
    for event in lifecycle["events"]:
        assert json.loads(wire.encode_client_event(event)) == event


@pytest.mark.parametrize(
    ("provider", "adapter", "failure_code"),
    [
        ("qwen", QwenOmniAdapter(), RealtimeErrorCode.PROTOCOL_ERROR),
        ("openai", OpenAIRealtimeAdapter(), RealtimeErrorCode.UNAVAILABLE),
    ],
)
def test_frozen_terminal_matrix_status_and_failure_semantics(
    provider: str,
    adapter: Any,
    failure_code: RealtimeErrorCode,
) -> None:
    matrix = json.loads(_fixture(provider, "server-response-terminal-matrix.json"))
    completed = next(
        case for case in matrix["cases"] if case["name"] == "completed_with_valid_usage"
    )
    decoded = adapter.decode_server_event(json.dumps(completed["wire_events"][0]))
    assert decoded.events[0].status is ResponseStatus.COMPLETED

    failed = next(
        case for case in matrix["cases"] if case["name"] == "provider_failed_without_usage"
    )
    failed_decoded = adapter.decode_server_event(json.dumps(failed["wire_events"][0]))
    assert failed_decoded.events == (
        ResponseFinished("resp_terminal_1", ResponseStatus.FAILED),
        SessionFailed(failure_code, failure_code is RealtimeErrorCode.UNAVAILABLE),
    )

    missing = next(case for case in matrix["cases"] if case["name"] == "completed_missing_usage")
    with pytest.raises(RealtimeError) as caught:
        adapter.decode_server_event(json.dumps(missing["wire_events"][0]))
    assert caught.value.code is RealtimeErrorCode.PROTOCOL_ERROR
