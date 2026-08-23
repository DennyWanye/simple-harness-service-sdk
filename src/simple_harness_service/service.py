"""Stateless semantic service mapped only to public Harness APIs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, TypeVar

from simple_harness import (
    AgentIdentity,
    CancelCommandIntent,
    CommandError,
    ContinueCommandIntent,
    ConversationContinuationInput,
    ConversationTurnInput,
    Message,
    MessageRole,
    RequestId,
    RunClient,
    RunId,
    StartCommandIntent,
)
from simple_harness import (
    CommandReceipt as HarnessCommandReceipt,
)

from .auth import AuthenticatedContext
from .contracts import (
    CancelRequest,
    CommandReceipt,
    CommandSnapshot,
    CommandState,
    ContinueRequest,
    GetRequest,
    HealthSnapshot,
    OutputState,
    ServiceError,
    ServiceErrorCode,
    StartRequest,
)
from .identity import IdentityProjector, Principal

T = TypeVar("T")


class HarnessPort(Protocol):
    async def submit_start(self, intent: StartCommandIntent) -> object: ...
    async def submit_continue(self, intent: ContinueCommandIntent) -> object: ...
    async def submit_cancel(self, intent: CancelCommandIntent) -> object: ...
    async def get_command(self, command_id: str) -> object: ...


class HarnessAdapter:
    """The sole translation point to the four public durable command methods."""

    def __init__(
        self,
        client: RunClient,
        *,
        health: Callable[[], Awaitable[HealthSnapshot]],
    ) -> None:
        self._client = client
        self._health = health

    async def health(self) -> HealthSnapshot:
        return await self._health()

    async def submit_start(self, intent: StartCommandIntent) -> CommandReceipt:
        return _receipt(await self._client.submit_start(intent))

    async def submit_continue(self, intent: ContinueCommandIntent) -> CommandReceipt:
        return _receipt(await self._client.submit_continue(intent))

    async def submit_cancel(self, intent: CancelCommandIntent) -> CommandReceipt:
        return _receipt(await self._client.submit_cancel(intent))

    async def get_command(self, command_id: str) -> CommandSnapshot:
        snapshot = await self._client.get_command(command_id)
        output_text = None
        if snapshot.output is not None:
            output_text = snapshot.output.memory_text
            if output_text is None and isinstance(snapshot.output.message.content, str):
                output_text = snapshot.output.message.content
        return CommandSnapshot(
            _receipt(snapshot.receipt),
            OutputState(snapshot.output_state.value),
            output_text,
            None if snapshot.error_code is None else snapshot.error_code.value,
        )


class ServiceAdapterPort(Protocol):
    async def health(self) -> HealthSnapshot: ...
    async def submit_start(self, intent: StartCommandIntent) -> CommandReceipt: ...
    async def submit_continue(self, intent: ContinueCommandIntent) -> CommandReceipt: ...
    async def submit_cancel(self, intent: CancelCommandIntent) -> CommandReceipt: ...
    async def get_command(self, command_id: str) -> CommandSnapshot: ...


class HarnessService:
    """Five stateless methods; all durable lifecycle remains in Harness."""

    def __init__(self, adapter: ServiceAdapterPort, projector: IdentityProjector) -> None:
        self._adapter = adapter
        self._projector = projector

    async def health(self) -> HealthSnapshot:
        return await self._adapter.health()

    async def start(self, request: StartRequest, context: AuthenticatedContext) -> CommandReceipt:
        p = context.principal
        session_id = self._id("session", p, request.external_session_id)
        run_id = self._id("run", p, request.external_run_id)
        command_id = self._id("command", p, request.external_command_id)
        turn_id = self._id("turn", p, request.external_command_id)
        conversation = ConversationTurnInput(
            AgentIdentity(
                p.deployment_id,
                p.household_id,
                p.actor_id,
                session_id,
            ),
            Message(MessageRole.USER, request.message),
            request.message,
        )
        intent = StartCommandIntent(
            self._projector.namespace,
            self._projector.key_id,
            command_id,
            RunId(run_id),
            RequestId(self._id("request", p, request.external_command_id)),
            turn_id,
            conversation,
            request.profile_key,
        )
        return await self._call(self._adapter.submit_start, intent)

    async def continue_(
        self, request: ContinueRequest, context: AuthenticatedContext
    ) -> CommandReceipt:
        p = context.principal
        intent = ContinueCommandIntent(
            self._projector.namespace,
            self._projector.key_id,
            self._id("command", p, request.external_command_id),
            RunId(self._id("run", p, request.external_run_id)),
            self._id("continuation", p, request.external_continuation_id),
            self._id("turn", p, request.external_command_id),
            ConversationContinuationInput(
                Message(MessageRole.USER, request.message), request.message
            ),
        )
        return await self._call(self._adapter.submit_continue, intent)

    async def get(self, request: GetRequest, context: AuthenticatedContext) -> CommandSnapshot:
        return await self._call(
            self._adapter.get_command,
            self._id("command", context.principal, request.external_command_id),
        )

    async def cancel(self, request: CancelRequest, context: AuthenticatedContext) -> CommandReceipt:
        p = context.principal
        intent = CancelCommandIntent(
            self._projector.namespace,
            self._projector.key_id,
            self._id("command", p, request.external_command_id),
            RunId(self._id("run", p, request.external_run_id)),
        )
        return await self._call(self._adapter.submit_cancel, intent)

    def _id(self, kind: str, principal: Principal, external_id: str) -> str:
        return self._projector.project(kind, principal, external_id)

    @staticmethod
    async def _call(call: Callable[..., Awaitable[T]], *args: object) -> T:
        try:
            return await call(*args)
        except CommandError as error:
            code = error.code.value
            mapped = (
                ServiceErrorCode.CONFLICT
                if "conflict" in code
                else ServiceErrorCode.INVALID_REQUEST
            )
            raise ServiceError(mapped, code) from None


def _receipt(value: HarnessCommandReceipt) -> CommandReceipt:
    return CommandReceipt(
        command_id=value.command_id,
        run_id=value.run_id.value,
        accept_seq=value.accept_seq,
        state=CommandState(value.state.value),
        version=value.version,
    )
