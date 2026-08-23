from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

from simple_harness_service import (
    CommandReceipt,
    CommandSnapshot,
    ContextAuthority,
    HarnessService,
    HealthSnapshot,
    IdentityProjector,
    OutputState,
    Principal,
)
from simple_harness_service.codec import encode_frame, read_frame
from simple_harness_service.transports import UnixServiceClient, UnixServiceServer


class FakeAdapter:
    async def health(self) -> HealthSnapshot:
        return HealthSnapshot(True)

    async def submit_start(self, intent: Any) -> CommandReceipt:
        return CommandReceipt(intent.command_id, intent.run_id.value, 0, "accepted", 1)

    async def submit_continue(self, intent: Any) -> CommandReceipt:
        return CommandReceipt(intent.command_id, intent.run_id.value, 1, "accepted", 1)

    async def submit_cancel(self, intent: Any) -> CommandReceipt:
        return CommandReceipt(intent.command_id, intent.run_id.value, 2, "accepted", 1)

    async def get_command(self, command_id: str) -> CommandSnapshot:
        receipt = CommandReceipt(command_id, "backend-run", 0, "applied", 2)
        return CommandSnapshot(receipt, OutputState.PRESENT, "answer")


@pytest.fixture
def socket_dir() -> Path:
    with tempfile.TemporaryDirectory(prefix="shs-", dir="/tmp") as raw:
        path = Path(raw)
        os.chmod(path, 0o700)
        yield path


@pytest.mark.asyncio
async def test_owner_only_unix_transport_exercises_five_methods(socket_dir: Path) -> None:
    path = socket_dir / "svc.sock"
    principal = Principal("deploy", "home", "alice")
    service = HarnessService(
        FakeAdapter(),  # type: ignore[arg-type]
        IdentityProjector(b"p" * 32, namespace="consumer.example"),
    )
    server = UnixServiceServer(
        path,
        service,
        ContextAuthority(b"a" * 32, key_id="context-v1"),
        principal_for_uid=lambda _: principal,
    )
    async with server:
        assert path.stat().st_mode & 0o777 == 0o600
        client = UnixServiceClient(path)
        assert (await client.health()).serving
        from simple_harness_service import CancelRequest, ContinueRequest, StartRequest

        await client.start(StartRequest("session", "run", "start", "hello"))
        await client.continue_(
            ContinueRequest("session", "run", "continue", "continuation", "again")
        )
        assert (await client.get("start")).output_text == "answer"
        await client.cancel(CancelRequest("run", "cancel"))
    assert not path.exists()


@pytest.mark.asyncio
async def test_wrong_peer_uid_is_rejected(socket_dir: Path) -> None:
    path = socket_dir / "svc.sock"
    principal = Principal("deploy", "home", "alice")
    service = HarnessService(
        FakeAdapter(),  # type: ignore[arg-type]
        IdentityProjector(b"p" * 32, namespace="consumer.example"),
    )
    server = UnixServiceServer(
        path,
        service,
        ContextAuthority(b"a" * 32, key_id="context-v1"),
        principal_for_uid=lambda _: principal,
        peer_uid_resolver=lambda _: os.getuid() + 1,
    )
    async with server:
        from simple_harness_service import ServiceError

        with pytest.raises(ServiceError):
            await UnixServiceClient(path).health()


@pytest.mark.asyncio
async def test_directory_mode_must_be_owner_only(socket_dir: Path) -> None:
    os.chmod(socket_dir, 0o755)
    path = socket_dir / "svc.sock"
    service = HarnessService(
        FakeAdapter(),  # type: ignore[arg-type]
        IdentityProjector(b"p" * 32, namespace="consumer.example"),
    )
    server = UnixServiceServer(
        path,
        service,
        ContextAuthority(b"a" * 32, key_id="context-v1"),
        principal_for_uid=lambda _: Principal("deploy", "home", "alice"),
    )
    with pytest.raises(PermissionError):
        await server.start()


@pytest.mark.asyncio
async def test_capability_replay_on_another_connection_is_rejected(socket_dir: Path) -> None:
    path = socket_dir / "svc.sock"
    principal = Principal("deploy", "home", "alice")
    service = HarnessService(
        FakeAdapter(),  # type: ignore[arg-type]
        IdentityProjector(b"p" * 32, namespace="consumer.example"),
    )
    server = UnixServiceServer(
        path,
        service,
        ContextAuthority(b"a" * 32, key_id="context-v1"),
        principal_for_uid=lambda _: principal,
    )
    async with server:
        reader_one, writer_one = await asyncio.open_unix_connection(path)
        hello_one = await read_frame(reader_one)
        writer_one.close()
        await writer_one.wait_closed()

        reader_two, writer_two = await asyncio.open_unix_connection(path)
        await read_frame(reader_two)
        writer_two.write(
            encode_frame(
                {
                    "request_id": "request",
                    "op": "health",
                    "payload": {},
                    "capability": hello_one["capability"],
                }
            )
        )
        await writer_two.drain()
        response = await read_frame(reader_two)
        assert response == {"ok": False, "error": "forbidden"}
        writer_two.close()
        await writer_two.wait_closed()
