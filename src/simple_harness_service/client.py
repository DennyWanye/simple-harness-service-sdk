"""Reusable product-neutral conversation client."""

from __future__ import annotations

from typing import Protocol

from .auth import AuthenticatedContext
from .contracts import (
    CancelRequest,
    CommandReceipt,
    CommandSnapshot,
    ContinueRequest,
    GetRequest,
    HealthSnapshot,
    StartRequest,
)


class ServicePort(Protocol):
    async def health(self) -> HealthSnapshot: ...
    async def start(
        self, request: StartRequest, context: AuthenticatedContext
    ) -> CommandReceipt: ...
    async def continue_(
        self, request: ContinueRequest, context: AuthenticatedContext
    ) -> CommandReceipt: ...
    async def get(self, request: GetRequest, context: AuthenticatedContext) -> CommandSnapshot: ...
    async def cancel(
        self, request: CancelRequest, context: AuthenticatedContext
    ) -> CommandReceipt: ...


class ConversationClient:
    def __init__(self, service: ServicePort, context: AuthenticatedContext) -> None:
        self._service = service
        self._context = context

    async def health(self) -> HealthSnapshot:
        return await self._service.health()

    async def start(self, request: StartRequest) -> CommandReceipt:
        return await self._service.start(request, self._context)

    async def continue_(self, request: ContinueRequest) -> CommandReceipt:
        return await self._service.continue_(request, self._context)

    async def get(self, external_command_id: str) -> CommandSnapshot:
        return await self._service.get(GetRequest(external_command_id), self._context)

    async def cancel(self, request: CancelRequest) -> CommandReceipt:
        return await self._service.cancel(request, self._context)
