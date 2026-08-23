"""Reusable five-method semantic conformance kit."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from ..auth import AuthenticatedContext
from ..client import ConversationClient, ServicePort
from ..contracts import CancelRequest, ContinueRequest, GetRequest, StartRequest


async def run_service_conformance(
    factory: Callable[[], Awaitable[tuple[ServicePort, AuthenticatedContext]]],
) -> dict[str, bool]:
    service, context = await factory()
    client = ConversationClient(service, context)
    health = await client.health()
    start = await client.start(StartRequest("session", "run", "start", "hello"))
    continuation = await client.continue_(
        ContinueRequest("session", "run", "continue", "continuation", "again")
    )
    snapshot = await service.get(GetRequest("start"), context)
    cancel = await client.cancel(CancelRequest("run", "cancel"))
    return {
        "health": health.serving,
        "start": start.command_id != "",
        "continue": continuation.command_id != "",
        "get": snapshot.receipt.command_id != "",
        "cancel": cancel.command_id != "",
    }

