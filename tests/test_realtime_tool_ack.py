from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import pytest
from test_realtime_session import FakeConnection
from test_realtime_stage2 import _ack, _session

from simple_harness_service.realtime.adapters.qwen_omni import QwenOmniAdapter
from simple_harness_service.realtime.contracts import (
    RealtimeError,
    RealtimeErrorCode,
    RealtimeOpenRequest,
)


def _tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "name": "delegate_task",
            "description": "Delegate work",
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string"},
                    "success_criteria": {"type": "array"},
                    "reason": {"type": "string"},
                },
                "required": ["goal", "success_criteria"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "task_status",
            "parameters": {
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
                "additionalProperties": False,
            },
        },
    ]


def _normalized_tools() -> list[dict[str, Any]]:
    # Preserve only the structure observed in the phase-0 relay acknowledgement.
    result = []
    for tool in reversed(_tools()):
        function = deepcopy(tool)
        function.pop("type")
        function["description"] = "  "
        function["parameters"].pop("additionalProperties")
        function["parameters"]["required"].reverse()
        function["parameters"]["properties"] = dict(
            reversed(list(function["parameters"]["properties"].items()))
        )
        for prop in function["parameters"]["properties"].values():
            prop["default"] = None
        result.append({"type": "function", "function": function})
    return result


@pytest.mark.parametrize("case", ["exact", "normalized", "missing", "extra"])
def test_qwen_semantic_tool_ack(case: str) -> None:
    adapter = QwenOmniAdapter()
    adapter.session_update(RealtimeOpenRequest("session", "instructions", tools=_tools()))
    echo = _normalized_tools() if case == "normalized" else _tools()
    if case == "missing":
        echo.pop()
    if case == "extra":
        echo.append(
            {
                "type": "function",
                "name": "unknown",
                "parameters": {"type": "object", "properties": {}, "required": []},
            }
        )
    ack = _ack()
    ack["session"]["tools"] = echo
    if case in {"missing", "extra"}:
        with pytest.raises(RealtimeError) as caught:
            adapter.decode_server_event(json.dumps(ack))
        assert caught.value.code is RealtimeErrorCode.UNSUPPORTED
    else:
        decoded = adapter.decode_server_event(json.dumps(ack))
        assert decoded.tools_acknowledged
        assert decoded.tool_catalog_normalized is (case == "normalized")


@pytest.mark.parametrize("mutation", ["required", "properties", "duplicate", "not_function"])
def test_qwen_semantic_tool_mismatch_is_unsupported(mutation: str) -> None:
    adapter = QwenOmniAdapter()
    adapter.session_update(RealtimeOpenRequest("session", "instructions", tools=_tools()))
    echo = _tools()
    if mutation == "required":
        echo[0]["parameters"]["required"] = ["goal"]
    elif mutation == "properties":
        echo[0]["parameters"]["properties"].pop("reason")
    elif mutation == "duplicate":
        echo.append(deepcopy(echo[0]))
    else:
        echo[0]["type"] = "message"
    ack = _ack()
    ack["session"]["tools"] = echo
    with pytest.raises(RealtimeError) as caught:
        adapter.decode_server_event(json.dumps(ack))
    assert caught.value.code is RealtimeErrorCode.UNSUPPORTED


@pytest.mark.asyncio
async def test_normalized_ack_records_content_free_diagnostic() -> None:
    session = _session(FakeConnection())
    session._request = RealtimeOpenRequest("session", "instructions", tools=_tools())
    await session.start()
    ack = _ack()
    ack["session"]["tools"] = _normalized_tools()
    decoded = session._adapter.decode_server_event(json.dumps(ack))
    assert await session._apply(decoded)
    events = session.diagnostics_snapshot().events
    assert any(event.operation_kind == "session.tools.normalized" for event in events)
    assert "delegate_task" not in str(events)
    assert "success_criteria" not in str(events)
    await session.close()
