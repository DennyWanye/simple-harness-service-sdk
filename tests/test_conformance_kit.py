from __future__ import annotations

from typing import Any

import pytest

from simple_harness_service import (
    AuthenticatedContext,
    CommandReceipt,
    CommandSnapshot,
    CommandState,
    HealthSnapshot,
    OutputState,
    Principal,
)
from simple_harness_service.testing import run_service_conformance


class ConformingService:
    async def health(self) -> HealthSnapshot:
        return HealthSnapshot(True)

    async def start(self, request: Any, context: AuthenticatedContext) -> CommandReceipt:
        return _receipt("start")

    async def continue_(self, request: Any, context: AuthenticatedContext) -> CommandReceipt:
        return _receipt("continue")

    async def get(self, request: Any, context: AuthenticatedContext) -> CommandSnapshot:
        return CommandSnapshot(_receipt("start"), OutputState.PENDING)

    async def cancel(self, request: Any, context: AuthenticatedContext) -> CommandReceipt:
        return _receipt("cancel")


def _receipt(command_id: str) -> CommandReceipt:
    return CommandReceipt(command_id, "run", 0, CommandState.ACCEPTED, 1)


@pytest.mark.asyncio
async def test_public_conformance_kit_exercises_all_five_methods() -> None:
    async def factory() -> tuple[ConformingService, AuthenticatedContext]:
        context = AuthenticatedContext(
            Principal("deploy", "home", "alice"), "binding", "nonce"
        )
        return ConformingService(), context

    assert await run_service_conformance(factory) == {
        "health": True,
        "start": True,
        "continue": True,
        "get": True,
        "cancel": True,
    }
