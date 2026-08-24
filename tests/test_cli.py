from __future__ import annotations

import asyncio
import io
import os
import pty
import select
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any

import pytest

from simple_harness_service import (
    ChatUiConfig,
    CommandKind,
    CommandOutcome,
    CommandReceipt,
    CommandSnapshot,
    CommandState,
    OutputState,
    RunState,
)
from simple_harness_service.cli import CliEngine, ExitCode, main


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
        if any(item.external_command_id == command_id for item in self.cancelled):
            return CommandSnapshot(
                CommandReceipt(
                    command_id,
                    "backend-run",
                    1,
                    CommandState.APPLIED,
                    2,
                    CommandKind.CANCEL,
                ),
                OutputState.ABSENT,
                outcome=CommandOutcome.CANCELLED,
            )
        return CommandSnapshot(
            _receipt(),
            OutputState.PRESENT,
            "answer",
            run_state=RunState.COMPLETED,
            outcome=CommandOutcome.COMPLETED,
        )

    async def cancel(self, request: Any) -> CommandReceipt:
        self.cancelled.append(request)
        return _receipt(CommandKind.CANCEL)


class PendingClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.observed = asyncio.Event()

    async def get(self, command_id: str) -> CommandSnapshot:
        if any(
            item.external_command_id == command_id for item in self.cancelled
        ):
            return CommandSnapshot(
                _receipt(CommandKind.CANCEL),
                OutputState.ABSENT,
                run_state=RunState.CANCELLED,
                outcome=CommandOutcome.CANCELLED,
            )
        self.observed.set()
        return CommandSnapshot(
            _receipt(), OutputState.PENDING, outcome=CommandOutcome.PENDING
        )


class UnsettledCancelClient(PendingClient):
    async def get(self, command_id: str) -> CommandSnapshot:
        self.observed.set()
        return CommandSnapshot(
            _receipt(CommandKind.CANCEL if self.cancelled else CommandKind.START),
            OutputState.PENDING,
            outcome=CommandOutcome.PENDING,
        )


def _receipt(kind: CommandKind = CommandKind.START) -> CommandReceipt:
    return CommandReceipt(
        "backend-command", "backend-run", 0, CommandState.ACCEPTED, 1, kind
    )


@pytest.mark.asyncio
async def test_ask_reads_message_only_from_stdin() -> None:
    client = FakeClient()
    stdout = io.StringIO()
    engine = CliEngine(
        lambda _: client,
        stdin=io.StringIO("private message"),
        stdout=stdout,
        stderr=io.StringIO(),
    )
    result = await engine.run(["--socket", "/tmp/test.sock", "ask", "--stdin"])
    assert result == ExitCode.OK
    assert client.started[0].message == "private message"
    assert stdout.getvalue() == "answer\n"


@pytest.mark.asyncio
async def test_chat_session_new_and_quit() -> None:
    client = FakeClient()
    engine = CliEngine(
        lambda _: client,
        stdin=io.StringIO("/session\nhello\nagain\n/new\nnew run\n/quit\n"),
        stdout=(stdout := io.StringIO()),
        stderr=io.StringIO(),
    )
    result = await engine.run(["--socket", str(Path("/tmp/test.sock")), "chat"])
    assert result in {ExitCode.OK, ExitCode.CANCELLED}
    assert len(client.started) >= 2
    assert client.continued == []
    assert len({request.external_run_id for request in client.started}) == len(
        client.started
    )
    assert {request.external_session_id for request in client.started} == {"main"}
    assert client.cancelled == []
    assert stdout.getvalue().startswith("main\n")


@pytest.mark.asyncio
async def test_completed_chat_turn_releases_run_for_same_session_fresh_start() -> None:
    client = FakeClient()
    engine = CliEngine(lambda _: client, stdout=io.StringIO(), stderr=io.StringIO())
    lines: asyncio.Queue[str] = asyncio.Queue()
    buffered: deque[str] = deque()

    result, reset_run = await engine._observe_active_chat(
        client, lines, buffered, "external-run", "external-command"
    )

    assert result == ExitCode.OK
    assert reset_run is True


@pytest.mark.asyncio
async def test_observation_cancellation_sends_durable_cancel() -> None:
    client = PendingClient()
    engine = CliEngine(lambda _: client)
    task = asyncio.create_task(engine._observe(client, "external-run", "external-command"))
    await client.observed.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.cancelled[0].external_run_id == "external-run"


@pytest.mark.asyncio
async def test_interrupt_reports_durable_cancel_pending_without_forging_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from simple_harness_service import cli as cli_module

    client = UnsettledCancelClient()
    stderr = io.StringIO()
    engine = CliEngine(lambda _: client, stderr=stderr)
    monkeypatch.setattr(cli_module, "CANCEL_RECONCILE_SECONDS", 0.01)
    task = asyncio.create_task(
        engine._observe(client, "external-run", "external-command")
    )
    await client.observed.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "cancel pending run_id=external-run" in stderr.getvalue()
    assert "cancelled" not in stderr.getvalue()


def test_sigint_and_successful_cancel_share_exit_six(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupted(awaitable: Any) -> int:
        awaitable.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(asyncio, "run", interrupted)
    assert main([]) == ExitCode.CANCELLED


@pytest.mark.skipif(sys.platform == "win32", reason="PTY is POSIX-only")
@pytest.mark.asyncio
async def test_active_chat_quit_durably_cancels_and_reconciles_over_pty() -> None:
    master, slave = pty.openpty()
    reader = os.fdopen(slave, "r", buffering=1, closefd=False)
    writer = os.fdopen(os.dup(slave), "w", buffering=1)
    client = PendingClient()
    engine = CliEngine(
        lambda _: client,
        stdin=reader,
        stdout=writer,
        stderr=writer,
        chat_ui_config=ChatUiConfig(screen_reader=True),
    )
    task = asyncio.create_task(engine._chat(client, "main"))
    try:
        os.write(master, b"hello\n")
        await asyncio.wait_for(client.observed.wait(), 2)
        os.write(master, b"/quit\n")
        assert await asyncio.wait_for(task, 2) == ExitCode.CANCELLED
        assert len(client.cancelled) == 1
    finally:
        if not task.done():
            task.cancel()
        reader.close()
        writer.close()
        os.close(slave)
        os.close(master)


@pytest.mark.asyncio
async def test_status_and_cancel_commands() -> None:
    client = FakeClient()
    stdout = io.StringIO()
    engine = CliEngine(
        lambda _: client,
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
        == ExitCode.CANCELLED
    )
    assert client.cancelled[-1].external_run_id == "external-run"


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (CommandOutcome.COMPLETED, ExitCode.OK),
        (CommandOutcome.FAILED, ExitCode.FAILED),
        (CommandOutcome.CANCELLED, ExitCode.CANCELLED),
        (CommandOutcome.PROTOCOL_ERROR, ExitCode.PROTOCOL),
    ],
)
def test_closed_terminal_outcomes_have_stable_exit_codes(
    outcome: CommandOutcome, expected: ExitCode
) -> None:
    output_state = (
        OutputState.PRESENT if outcome is CommandOutcome.COMPLETED else OutputState.ABSENT
    )
    snapshot = CommandSnapshot(
        _receipt(),
        output_state,
        "answer" if output_state is OutputState.PRESENT else None,
        outcome=outcome,
    )
    engine = CliEngine(stdout=io.StringIO(), stderr=io.StringIO())
    assert engine._emit_outcome(snapshot) == expected


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
        os.write(master, b"/help\n")
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
        os.write(master, b"/quit\n")
        assert process.wait(timeout=5) == 0
        assert expected in output
    finally:
        os.close(master)
        if process.poll() is None:
            process.kill()
