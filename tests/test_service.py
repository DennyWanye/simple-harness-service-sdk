from __future__ import annotations

from typing import Any

import pytest
from simple_harness import CommandKind, CommandOutputState, CommandRetryState, RunId
from simple_harness import CommandReceipt as HarnessReceipt
from simple_harness import CommandSnapshot as HarnessSnapshot

from simple_harness_service import (
    AuthenticatedContext,
    CancelRequest,
    ContinueRequest,
    GetRequest,
    HarnessService,
    HealthSnapshot,
    IdentityProjector,
    Principal,
    StartRequest,
)
from simple_harness_service.service import HarnessAdapter


class FakeRunClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def submit_start(self, intent: Any) -> HarnessReceipt:
        self.calls.append(("submit_start", intent))
        return receipt(intent, CommandKind.START)

    async def submit_continue(self, intent: Any) -> HarnessReceipt:
        self.calls.append(("submit_continue", intent))
        return receipt(intent, CommandKind.CONTINUE)

    async def submit_cancel(self, intent: Any) -> HarnessReceipt:
        self.calls.append(("submit_cancel", intent))
        return receipt(intent, CommandKind.CANCEL)

    async def get_command(self, command_id: str) -> HarnessSnapshot:
        self.calls.append(("get_command", command_id))
        value = HarnessReceipt(
            command_id, RunId("run"), CommandKind.START, 0, "accepted", 1, "ns", "kid", "a" * 64
        )
        return HarnessSnapshot(value, CommandRetryState.READY, CommandOutputState.PENDING)


def receipt(intent: Any, kind: CommandKind) -> HarnessReceipt:
    return HarnessReceipt(
        intent.command_id,
        intent.run_id,
        kind,
        0,
        "accepted",
        1,
        intent.namespace,
        intent.projection_key_id,
        intent.intent_hash,
    )


@pytest.fixture
def context() -> AuthenticatedContext:
    return AuthenticatedContext(Principal("deploy", "home", "alice"), "binding", "nonce")


@pytest.mark.asyncio
async def test_five_methods_map_only_to_public_harness_commands(
    context: AuthenticatedContext,
) -> None:
    run_client = FakeRunClient()

    async def health() -> HealthSnapshot:
        return HealthSnapshot(True)

    service = HarnessService(
        HarnessAdapter(run_client, health=health),  # type: ignore[arg-type]
        IdentityProjector(b"p" * 32, namespace="consumer.example"),
    )
    assert (await service.health()).serving
    start = StartRequest("session", "run", "start-command", "hello")
    await service.start(start, context)
    await service.continue_(
        ContinueRequest("session", "run", "continue-command", "continuation", "again"),
        context,
    )
    await service.get(GetRequest("start-command"), context)
    await service.cancel(CancelRequest("run", "cancel-command"), context)
    assert [name for name, _ in run_client.calls] == [
        "submit_start",
        "submit_continue",
        "get_command",
        "submit_cancel",
    ]
    start_intent = run_client.calls[0][1]
    assert start_intent.conversation.identity.actor_id == "alice"
    assert start_intent.conversation.identity.session_id.startswith("svc_session_")


@pytest.mark.asyncio
async def test_principal_isolation_changes_every_backend_identity(
    context: AuthenticatedContext,
) -> None:
    run_client = FakeRunClient()

    async def health() -> HealthSnapshot:
        return HealthSnapshot(True)

    service = HarnessService(
        HarnessAdapter(run_client, health=health),  # type: ignore[arg-type]
        IdentityProjector(b"p" * 32, namespace="consumer.example"),
    )
    request = StartRequest("session", "run", "command", "hello")
    await service.start(request, context)
    await service.start(
        request,
        AuthenticatedContext(Principal("deploy", "home", "bob"), "binding", "nonce"),
    )
    first, second = (call[1] for call in run_client.calls)
    assert first.command_id != second.command_id
    assert first.run_id != second.run_id
    assert first.conversation.identity.session_id != second.conversation.identity.session_id
