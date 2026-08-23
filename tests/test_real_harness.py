from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from simple_harness import (
    ConsumerRuntimePorts,
    Message,
    MessageRole,
    build_consumer_runtime,
)
from simple_harness.providers import ProviderRequestRejectedError, ProviderResponse

from simple_harness_service import (
    AuthenticatedContext,
    CancelRequest,
    CommandOutcome,
    ContinueRequest,
    GetRequest,
    HarnessAdapter,
    HarnessService,
    HealthSnapshot,
    IdentityProjector,
    OutputState,
    Principal,
    StartRequest,
)


class AnswerProvider:
    async def invoke(self, request: Any, *, cancel: Any) -> ProviderResponse:
        return ProviderResponse(
            request.request_id,
            Message(MessageRole.ASSISTANT, "real harness answer"),
            model="consumer-model",
            finish_reason="stop",
        )


class FailingProvider:
    async def invoke(self, request: Any, *, cancel: Any) -> ProviderResponse:
        raise ProviderRequestRejectedError()


class NoopToolExecutor:
    async def execute(self, call: Any, context: dict[str, Any]) -> Any:
        raise AssertionError("the no-tool conformance run must not dispatch a tool")


class AllowAuthorization:
    async def request_authorization(self, request: Any) -> Any:
        return type("Authorization", (), {"decision": "allow", "reason": None})()


@pytest.mark.asyncio
async def test_service_reaches_terminal_output_through_real_public_harness(
    tmp_path: Path,
) -> None:
    runtime = await build_consumer_runtime(
        ConsumerRuntimePorts(
            provider=AnswerProvider(),
            tool_executor=NoopToolExecutor(),
            authorization=AllowAuthorization(),
            database_path=str(tmp_path / "execution.db"),
            owner_id="service-real-harness-test",
        )
    )

    async def health() -> HealthSnapshot:
        return HealthSnapshot(True)

    service = HarnessService(
        HarnessAdapter(runtime.client, health=health),
        IdentityProjector(b"p" * 32, namespace="consumer.real-harness"),
    )
    context = AuthenticatedContext(
        Principal("deploy", "home", "alice"), "binding", "nonce"
    )
    await runtime.start()
    try:
        await service.start(
            StartRequest("session", "run", "command", "hello real harness"),
            context,
        )
        for _ in range(200):
            snapshot = await service.get(GetRequest("command"), context)
            if snapshot.output_state is not OutputState.PENDING:
                break
            await asyncio.sleep(0.01)
        assert snapshot.output_state is OutputState.PRESENT
        assert snapshot.output_text == "real harness answer"
        assert snapshot.outcome is CommandOutcome.COMPLETED
        # The formal wheel's public continuation command is exercised without
        # importing any implementation module. A terminal run rejects it durably.
        await service.continue_(
            ContinueRequest(
                "session", "run", "continue-command", "continuation", "again"
            ),
            context,
        )
        continuation = await service.get(GetRequest("continue-command"), context)
        assert continuation.receipt.command_id
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_real_public_harness_failed_terminal_and_cancel_method(
    tmp_path: Path,
) -> None:
    context = AuthenticatedContext(
        Principal("deploy", "home", "alice"), "binding", "nonce"
    )

    async def health() -> HealthSnapshot:
        return HealthSnapshot(True)

    failed_runtime = await build_consumer_runtime(
        ConsumerRuntimePorts(
            provider=FailingProvider(),
            tool_executor=NoopToolExecutor(),
            authorization=AllowAuthorization(),
            database_path=str(tmp_path / "failed.db"),
            owner_id="service-real-failed",
        )
    )
    failed_service = HarnessService(
        HarnessAdapter(failed_runtime.client, health=health),
        IdentityProjector(b"p" * 32, namespace="consumer.real-failed"),
    )
    await failed_runtime.start()
    try:
        await failed_service.start(
            StartRequest("session", "run", "command", "fail"), context
        )
        for _ in range(200):
            failed = await failed_service.get(GetRequest("command"), context)
            if failed.outcome is not CommandOutcome.PENDING:
                break
            await asyncio.sleep(0.01)
        assert failed.outcome is CommandOutcome.FAILED
    finally:
        await failed_runtime.close()

    cancelled_runtime = await build_consumer_runtime(
        ConsumerRuntimePorts(
            provider=AnswerProvider(),
            tool_executor=NoopToolExecutor(),
            authorization=AllowAuthorization(),
            database_path=str(tmp_path / "cancelled.db"),
            owner_id="service-real-cancelled",
        )
    )
    cancelled_service = HarnessService(
        HarnessAdapter(cancelled_runtime.client, health=health),
        IdentityProjector(b"p" * 32, namespace="consumer.real-cancelled"),
    )
    await cancelled_runtime.start()
    try:
        await cancelled_service.start(
            StartRequest("session", "run", "command", "block"), context
        )
        for _ in range(200):
            completed = await cancelled_service.get(GetRequest("command"), context)
            if completed.outcome is not CommandOutcome.PENDING:
                break
            await asyncio.sleep(0.01)
        cancel = await cancelled_service.cancel(CancelRequest("run", "cancel"), context)
        assert cancel.command_id
    finally:
        await cancelled_runtime.close()
