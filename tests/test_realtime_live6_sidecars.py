from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_realtime_user_side_transcript import _confirmed_context, _decode

from simple_harness_service.realtime.adapters.qwen_omni import QwenOmniAdapter
from simple_harness_service.realtime.contracts import RealtimeError

ORDER = json.loads((Path(__file__).parent / "fixtures/live6-identity-order.json").read_text())


@pytest.mark.parametrize("scenario", ORDER["scenarios"], ids=lambda s: s["name"])
def test_live6_unresolved_item_stays_rejected_with_reason(scenario: dict) -> None:
    adapter = QwenOmniAdapter()
    for index, step in enumerate(scenario["steps"]):
        if step["type"] in {"input_audio_buffer.speech_started", "input_audio_buffer.committed"}:
            _decode(
                adapter,
                {
                    "event_id": f"event_input_{index}",
                    "type": step["type"],
                    "item_id": step["item_id"],
                },
            )
        elif step["type"] == "conversation.item.created" and step.get("role") == "user":
            if step.get("content_types") == ["input_audio"]:
                _decode(
                    adapter,
                    {
                        "event_id": f"event_audio_{index}",
                        "type": "conversation.item.created",
                        "item": {
                            "id": step["item_id"],
                            "type": "message",
                            "role": "user",
                            "status": "completed",
                            "content": [{"type": "input_audio"}],
                        },
                    },
                )
            else:
                _confirmed_context(adapter, identity=step["item_id"])
        elif step["type"] in {"response.output_item.added", "response.output_item.done"}:
            # The frozen probe omitted nested item IDs. Preserve ordering with
            # explicit placeholders; never assign the unresolved ID to them.
            response_id = step["response_id"]
            _decode(
                adapter,
                {
                    "event_id": f"event_output_{index}",
                    "type": step["type"],
                    "response_id": response_id,
                    "output_index": 0,
                    "item": {
                        "id": f"unrecorded_{response_id}",
                        "type": "message",
                        "role": "assistant",
                    },
                },
            )
    assert adapter.identity_table.lookup(scenario["unresolved_item"]) is None
    with pytest.raises(RealtimeError) as error:
        _decode(
            adapter,
            {
                "event_id": "event_unresolved",
                "type": "response.audio_transcript.done",
                "response_id": "",
                "item_id": scenario["unresolved_item"],
                "output_index": 0,
                "content_index": 0,
                "transcript": "",
            },
        )
    assert error.value.validation_reason == "unresolved_item_missing_response_id"


def test_known_nested_assistant_missing_scope_reports_suspicion() -> None:
    adapter = QwenOmniAdapter()
    _decode(
        adapter,
        {
            "event_id": "event_output",
            "type": "response.output_item.added",
            "response_id": "response_1",
            "output_index": 0,
            "item": {"id": "assistant_1", "type": "message", "role": "assistant"},
        },
    )
    with pytest.raises(RealtimeError) as error:
        _decode(
            adapter,
            {
                "event_id": "event_sidecar",
                "type": "response.audio.done",
                "item_id": "assistant_1",
                "response_id": "",
            },
        )
    assert error.value.validation_reason == "suspected_assistant_missing_response_id"
