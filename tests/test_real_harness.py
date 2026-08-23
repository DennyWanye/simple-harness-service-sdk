from __future__ import annotations

import asyncio
import io
from pathlib import Path
from typing import Any

import pytest
from simple_harness import (
    CommittedTurnReceipt,
    CommittedTurnStatus,
    ConsumerRuntimePorts,
    CurrentMessageContextProvider,
    Message,
    MessageRole,
    build_consumer_runtime,
)
from simple_harness.providers import ProviderRequestRejectedError, ProviderResponse

from simple_harness_service import (
    AuthenticatedContext,
    CancelRequest,
    CommandKind,
    CommandOutcome,
    CommandReceipt,
    CommandSnapshot,
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
from simple_harness_service.cli import CliEngine, ExitCode


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


class CountingProvider(AnswerProvider):
    def __init__(self) -> None:
        self.calls = 0

    async def invoke(self, request: Any, *, cancel: Any) -> ProviderResponse:
        self.calls += 1
        return await super().invoke(request, cancel=cancel)


class BlockingContext:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.delegate = CurrentMessageContextProvider()

    async def prepare_once(self, request: Any) -> Any:
        self.entered.set()
        await self.release.wait()
        return await self.delegate.prepare_once(request)


class CancelAwareProvider(AnswerProvider):
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def invoke(self, request: Any, *, cancel: Any) -> ProviderResponse:
        self.entered.set()
        await self.release.wait()
        return await super().invoke(request, cancel=cancel)


class DirectServiceClient:
    def __init__(self, service: HarnessService, context: AuthenticatedContext) -> None:
        self.service = service
        self.context = context

    async def start(self, request: StartRequest) -> CommandReceipt:
        return await self.service.start(request, self.context)

    async def continue_(self, request: ContinueRequest) -> CommandReceipt:
        return await self.service.continue_(request, self.context)

    async def get(self, command_id: str) -> CommandSnapshot:
        return await self.service.get(GetRequest(command_id), self.context)

    async def cancel(self, request: CancelRequest) -> CommandReceipt:
        return await self.service.cancel(request, self.context)


class NoopMemory:
    async def recall_for_turn(self, request: Any) -> Any:
        raise TimeoutError

    async def release_recall(self, request: Any) -> None:
        return None

    async def record_committed_turn(self, request: Any) -> CommittedTurnReceipt:
        return CommittedTurnReceipt(
            request.turn_id,
            request.payload_hash,
            CommittedTurnStatus.APPLIED,
            "noop-memory-receipt",
        )


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


@pytest.mark.asyncio
async def test_real_harness_pre_dispatch_cancel_closes_without_provider_call(
    tmp_path: Path,
) -> None:
    context_provider = BlockingContext()
    provider = CountingProvider()
    runtime = await build_consumer_runtime(
        ConsumerRuntimePorts(
            provider=provider,
            tool_executor=NoopToolExecutor(),
            authorization=AllowAuthorization(),
            database_path=str(tmp_path / "pre-dispatch-cancel.db"),
            owner_id="service-pre-dispatch-cancel",
            context_provider=context_provider,
            memory=NoopMemory(),
        )
    )

    async def health() -> HealthSnapshot:
        return HealthSnapshot(True)

    service = HarnessService(
        HarnessAdapter(runtime.client, health=health),
        IdentityProjector(b"p" * 32, namespace="consumer.pre-dispatch-cancel"),
    )
    context = AuthenticatedContext(
        Principal("deploy", "home", "alice"), "binding", "nonce"
    )
    client = DirectServiceClient(service, context)
    await runtime.start()
    try:
        start = await client.start(StartRequest("session", "run", "start", "hello"))
        assert start.kind is CommandKind.START
        engine = CliEngine(
            lambda _: client, stdout=io.StringIO(), stderr=io.StringIO()
        )
        assert (
            await engine._cancel_and_reconcile(client, "run", "cancel")
            is ExitCode.CANCELLED
        )
        cancel_snapshot = await client.get("cancel")
        start_snapshot = await client.get("start")
        assert cancel_snapshot.receipt.kind is CommandKind.CANCEL
        assert cancel_snapshot.outcome is CommandOutcome.CANCELLED
        assert cancel_snapshot.run_state is None
        assert start_snapshot.receipt.kind is CommandKind.START
        assert start_snapshot.outcome is CommandOutcome.CANCELLED
        assert not context_provider.entered.is_set()
        assert provider.calls == 0
    finally:
        context_provider.release.set()
        await runtime.close()


@pytest.mark.asyncio
async def test_real_harness_active_cancel_and_cli_reconcile_close_both_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = CancelAwareProvider()
    runtime = await build_consumer_runtime(
        ConsumerRuntimePorts(
            provider=provider,
            tool_executor=NoopToolExecutor(),
            authorization=AllowAuthorization(),
            database_path=str(tmp_path / "active-cancel.db"),
            owner_id="service-active-cancel",
        )
    )

    async def health() -> HealthSnapshot:
        return HealthSnapshot(True)

    service = HarnessService(
        HarnessAdapter(runtime.client, health=health),
        IdentityProjector(b"p" * 32, namespace="consumer.active-cancel"),
    )
    context = AuthenticatedContext(
        Principal("deploy", "home", "alice"), "binding", "nonce"
    )
    client = DirectServiceClient(service, context)
    await runtime.start()
    try:
        await client.start(StartRequest("session", "run", "start", "hello"))
        await asyncio.wait_for(provider.entered.wait(), 2)
        stderr = io.StringIO()
        engine = CliEngine(
            lambda _: client,
            stdout=io.StringIO(),
            stderr=stderr,
        )
        from simple_harness_service import cli as cli_module

        monkeypatch.setattr(cli_module, "CANCEL_RECONCILE_SECONDS", 0.2)
        result = await engine._cancel_and_reconcile(client, "run", "cancel")
        assert result is ExitCode.TIMEOUT
        start_snapshot = await client.get("start")
        cancel_snapshot = await client.get("cancel")
        assert start_snapshot.receipt.kind is CommandKind.START
        assert start_snapshot.outcome is CommandOutcome.PENDING
        assert cancel_snapshot.receipt.kind is CommandKind.CANCEL
        assert cancel_snapshot.outcome is CommandOutcome.PENDING
        assert start_snapshot.receipt.run_id == cancel_snapshot.receipt.run_id
        assert "cancel pending run_id=run command_id=cancel" in stderr.getvalue()
        provider.release.set()
        for _ in range(300):
            start_snapshot = await client.get("start")
            cancel_snapshot = await client.get("cancel")
            if start_snapshot.outcome is not CommandOutcome.PENDING:
                break
            await asyncio.sleep(0.01)
        # The formal consumer composition intentionally remains recoverable after
        # an active physical call settles unknown: it exposes no public provider
        # reconciliation injection and must not forge a terminal cancellation.
        assert start_snapshot.outcome is CommandOutcome.PENDING
        assert cancel_snapshot.outcome is CommandOutcome.PENDING
        assert start_snapshot.run_state is not None
        assert start_snapshot.run_state.value == "waiting"
    finally:
        provider.release.set()
        await runtime.close()
