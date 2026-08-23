from __future__ import annotations

import asyncio
import io
import os
import pty
import select
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from simple_harness_service import CommandReceipt, CommandSnapshot, OutputState
from simple_harness_service.cli import CliEngine, ExitCode


class FakeClient:
    def __init__(self) -> None:
        self.started: list[Any] = []
        self.continued: list[Any] = []
        self.cancelled: list[Any] = []

    async def start(self, request: Any) -> CommandReceipt:
        self.started.append(request)
        return _receipt()

    async def continue_(self, request: Any) -> CommandReceipt:
        self.continued.append(request)
        return _receipt()

    async def get(self, command_id: str) -> CommandSnapshot:
        return CommandSnapshot(_receipt(), OutputState.PRESENT, "answer")

    async def cancel(self, request: Any) -> CommandReceipt:
        self.cancelled.append(request)
        return _receipt()


class PendingClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.observed = asyncio.Event()

    async def get(self, command_id: str) -> CommandSnapshot:
        self.observed.set()
        return CommandSnapshot(_receipt(), OutputState.PENDING)


def _receipt() -> CommandReceipt:
    return CommandReceipt("backend-command", "backend-run", 0, "accepted", 1)


@pytest.mark.asyncio
async def test_ask_reads_message_only_from_stdin() -> None:
    client = FakeClient()
    stdout = io.StringIO()
    engine = CliEngine(
        lambda _: client,  # type: ignore[arg-type,return-value]
        stdin=io.StringIO("private message"),
        stdout=stdout,
        stderr=io.StringIO(),
    )
    result = await engine.run(["--socket", "/tmp/test.sock", "ask", "--stdin"])
    assert result == ExitCode.OK
    assert client.started[0].message == "private message"
    assert stdout.getvalue() == "answer\n"


@pytest.mark.asyncio
async def test_chat_session_new_cancel_and_quit() -> None:
    client = FakeClient()
    engine = CliEngine(
        lambda _: client,  # type: ignore[arg-type,return-value]
        stdin=io.StringIO("/session\nhello\nagain\n/new\nnew run\n/cancel\n/quit\n"),
        stdout=(stdout := io.StringIO()),
        stderr=io.StringIO(),
    )
    result = await engine.run(["--socket", str(Path("/tmp/test.sock")), "chat"])
    assert result == ExitCode.OK
    assert len(client.started) == 2
    assert len(client.continued) == 1
    assert len(client.cancelled) == 1
    assert stdout.getvalue().startswith("main\n")


@pytest.mark.asyncio
async def test_observation_cancellation_sends_durable_cancel() -> None:
    client = PendingClient()
    engine = CliEngine(lambda _: client)  # type: ignore[arg-type,return-value]
    task = asyncio.create_task(engine._observe(client, "external-run", "external-command"))
    await client.observed.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.cancelled[0].external_run_id == "external-run"


@pytest.mark.asyncio
async def test_status_and_cancel_commands() -> None:
    client = FakeClient()
    stdout = io.StringIO()
    engine = CliEngine(
        lambda _: client,  # type: ignore[arg-type,return-value]
        stdin=io.StringIO(),
        stdout=stdout,
        stderr=io.StringIO(),
    )
    assert (
        await engine.run(["--socket", "/tmp/test.sock", "status", "external-command"])
        == ExitCode.OK
    )
    assert '"output_state": "present"' in stdout.getvalue()
    assert (
        await engine.run(["--socket", "/tmp/test.sock", "cancel", "external-run"])
        == ExitCode.OK
    )
    assert client.cancelled[-1].external_run_id == "external-run"


@pytest.mark.skipif(sys.platform == "win32", reason="PTY is POSIX-only")
def test_chat_help_and_quit_over_real_pty() -> None:
    master, slave = pty.openpty()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "simple_harness_service.cli",
            "--socket",
            "/tmp/not-used.sock",
            "chat",
        ],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
    )
    os.close(slave)
    try:
        os.write(master, b"/help\n/quit\n")
        output = bytearray()
        deadline = time.monotonic() + 5
        expected = b"/help /session NAME /new /cancel /quit"
        while time.monotonic() < deadline and expected not in output:
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    output.extend(os.read(master, 4096))
                except OSError:
                    break
        assert process.wait(timeout=5) == 0
        assert expected in output
    finally:
        os.close(master)
        if process.poll() is None:
            process.kill()
