from __future__ import annotations

import asyncio
import json

import pytest
from test_realtime_context_ack import _running
from test_realtime_identity_table import confirm

from simple_harness_service.realtime.contracts import SessionFailed


def control(**fields: object) -> dict:
    return {
        "type": "tokenseller.error",
        "event_id": "ctl_error",
        "code": "protocol_error",
        "retryable": False,
        "fatal": True,
        "message_class": "upstream_protocol_error",
        **fields,
    }


@pytest.mark.asyncio
async def test_live6_order_valid_terminal_preserves_relay_reason_and_client_event() -> None:
    async with _running() as (connection, session):
        await confirm(connection, session)
        sent = json.loads(
            next(s for s in connection.sent if json.loads(s)["type"] == "conversation.item.create")
        )
        await connection.incoming.put(
            json.dumps(
                {
                    "type": "response.audio.done",
                    "event_id": "event_sidecar",
                    "item_id": "item_server_context",
                    "response_id": "",
                }
            )
        )
        await connection.incoming.put(json.dumps(control(related_event_id=sent["event_id"])))
        event = await asyncio.wait_for(anext(session.events()), 1)
        assert isinstance(event, SessionFailed)
        assert event.control_code == "protocol_error"
        assert event.message_class == "upstream_protocol_error"
        assert event.related_event_id == sent["event_id"]
        assert event.client_event_type == "conversation.item.create"
        assert event.failure.stage == "control.terminal"
        assert event.failure.event_type == "tokenseller.error"
        assert event.failure.retryable is False and event.failure.fatal is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("code", "arbitrary"),
        ("retryable", 1),
        ("fatal", False),
        ("message_class", "unknown"),
        ("related_event_id", ""),
        ("retry_after_ms", -1),
    ],
)
async def test_malformed_control_has_field_validation_failure(field: str, value: object) -> None:
    async with _running() as (connection, session):
        await connection.incoming.put(json.dumps(control(**{field: value})))
        event = await asyncio.wait_for(anext(session.events()), 1)
        assert event.failure.stage == "control.decode"
        assert event.failure.validation_field == field
        assert event.message_class is None


def test_control_enums_are_parsed_from_frozen_manifest_without_losing_reason() -> None:
    from pathlib import Path

    from simple_harness_service.realtime.relay_control import RelayControlCodec

    manifest = json.loads(
        (
            Path(__file__).parents[1]
            / "ARCHITECTURE/protocols/tokenseller-realtime-control-2026-08-28.3/manifest.json"
        ).read_text()
    )
    codec = RelayControlCodec()
    for code in manifest["enums"]["error_code"]:
        for message_class in manifest["enums"]["message_class"]:
            event = codec.decode_runtime_event(
                json.dumps(control(code=code, message_class=message_class))
            )
            assert event.control_code == code and event.message_class == message_class


@pytest.mark.asyncio
async def test_closed_notification_exposes_related_client_event() -> None:
    async with _running() as (connection, session):
        await confirm(connection, session)
        sent = json.loads(
            next(s for s in connection.sent if json.loads(s)["type"] == "conversation.item.create")
        )
        await connection.incoming.put(
            json.dumps(
                {
                    "type": "tokenseller.session.closed",
                    "event_id": "ctl_closed",
                    "related_event_id": sent["event_id"],
                    "initiator": "relay",
                    "disposition": "fatal",
                    "active_response": "none",
                    "settlement": "released",
                }
            )
        )
        event = await asyncio.wait_for(anext(session.events()), 1)
        assert event.kind == "SessionClosed"
        assert event.client_event_type == "conversation.item.create"
        assert event.failure.stage == "control.closed"


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["code", "retryable", "fatal", "message_class"])
async def test_missing_control_field_is_identified(field: str) -> None:
    value = control()
    value.pop(field)
    async with _running() as (connection, session):
        await connection.incoming.put(json.dumps(value))
        event = await asyncio.wait_for(anext(session.events()), 1)
        assert event.failure.validation_field == field
        assert event.failure.validation_reason == "missing_field"


@pytest.mark.asyncio
async def test_invalid_control_still_links_a_verified_client_reference() -> None:
    async with _running() as (connection, session):
        await confirm(connection, session)
        sent = json.loads(
            next(s for s in connection.sent if json.loads(s)["type"] == "conversation.item.create")
        )
        await connection.incoming.put(
            json.dumps(control(retryable="invalid", related_event_id=sent["event_id"]))
        )
        event = await asyncio.wait_for(anext(session.events()), 1)
        assert event.failure.stage == "control.decode"
        assert event.failure.validation_field == "retryable"
        assert event.failure.related_event_id == sent["event_id"]
        assert event.failure.client_event_type == "conversation.item.create"
