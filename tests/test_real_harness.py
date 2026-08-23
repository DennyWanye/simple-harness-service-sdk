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
from simple_harness.providers import ProviderResponse

from simple_harness_service import (
    AuthenticatedContext,
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
    finally:
        await runtime.close()
