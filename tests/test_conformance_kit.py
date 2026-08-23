from __future__ import annotations

from typing import Any

import pytest

from simple_harness_service import (
    AuthenticatedContext,
    CommandKind,
    CommandReceipt,
    CommandSnapshot,
    CommandState,
    ContextAuthority,
    HealthSnapshot,
    OutputState,
    Principal,
)
from simple_harness_service.testing import TokenSubjectAdapter, run_service_conformance


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
    kind = {
        "start": CommandKind.START,
        "continue": CommandKind.CONTINUE,
        "cancel": CommandKind.CANCEL,
    }.get(command_id, CommandKind.START)
    return CommandReceipt(command_id, "run", 0, CommandState.ACCEPTED, 1, kind)


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


@pytest.mark.asyncio
async def test_agentos_two_subjects_run_the_same_five_method_conformance() -> None:
    adapter = TokenSubjectAdapter(
        ContextAuthority(b"a" * 32),
        {
            "issuer|alice": Principal("deploy", "home", "alice"),
            "issuer|bob": Principal("deploy", "home", "bob"),
        },
    )

    async def run_subject(subject: str, binding: str) -> dict[str, bool]:
        async def factory() -> tuple[ConformingService, AuthenticatedContext]:
            return ConformingService(), adapter.authenticate(
                subject, observed_tls_binding=binding, now=10
            )

        return await run_service_conformance(factory)

    alice = await run_subject("issuer|alice", "binding-alice")
    bob = await run_subject("issuer|bob", "binding-bob")
    assert alice == bob == {
        "health": True,
        "start": True,
        "continue": True,
        "get": True,
        "cancel": True,
    }
