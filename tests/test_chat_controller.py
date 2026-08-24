from __future__ import annotations

import asyncio
from typing import Any

import pytest

from simple_harness_service.chat_controller import (
    COMMANDS,
    Accepted,
    Cancel,
    Cancelled,
    ChatController,
    Completed,
    ControllerState,
    Notice,
    Pending,
    ProtocolError,
    QuitRequested,
    SessionChanged,
    Timeout,
    TranscriptCleared,
)
from simple_harness_service.contracts import (
    CommandKind,
    CommandOutcome,
    CommandReceipt,
    CommandSnapshot,
    CommandState,
    OutputState,
    RunState,
)


class FakeClient:
    def __init__(self) -> None:
        self.started: list[Any] = []
        self.cancelled: list[Any] = []
        self.snapshots: dict[str, CommandSnapshot] = {}
        self.cancel_gate: asyncio.Event | None = None
        self.cancel_called = asyncio.Event()

    async def start(self, request: Any) -> object:
        self.started.append(request)
        return object()

    async def cancel(self, request: Any) -> object:
        self.cancelled.append(request)
        self.cancel_called.set()
        if self.cancel_gate is not None:
            await self.cancel_gate.wait()
        return object()

    async def get(self, command_id: str) -> CommandSnapshot:
        return self.snapshots[command_id]


def snapshot(
    command_id: str,
    *,
    kind: CommandKind = CommandKind.START,
    outcome: CommandOutcome = CommandOutcome.PENDING,
    run_state: RunState | None = RunState.RUNNING,
    output: str | None = None,
) -> CommandSnapshot:
    return CommandSnapshot(
        CommandReceipt(command_id, "backend-run", 1, CommandState.APPLIED, 1, kind),
        OutputState.PRESENT if output is not None else OutputState.PENDING,
        output,
        run_state=run_state,
        outcome=outcome,
    )


@pytest.fixture
def ids() -> Any:
    counters: dict[str, int] = {}

    def make(kind: str) -> str:
        counters[kind] = counters.get(kind, 0) + 1
        return f"{kind}-{counters[kind]}"

    return make


@pytest.mark.asyncio
async def test_submit_is_single_start_and_settles_before_next_submit(ids: Any) -> None:
    client = FakeClient()
    controller = ChatController(client, id_factory=ids)

    events = await controller.dispatch_text("hello")
    assert events == (Accepted("run-1", "command-1"), Pending("run-1", "command-1"))
    assert len(client.started) == 1
    assert (await controller.dispatch_text("duplicate"))[0] == Notice("A run is already active")
    assert len(client.started) == 1

    client.snapshots["command-1"] = snapshot(
        "command-1", outcome=CommandOutcome.COMPLETED, run_state=RunState.COMPLETED, output="answer"
    )
    assert await controller.observe_once() == (Completed("answer"),)
    assert controller.state is ControllerState.IDLE
    await controller.dispatch_text("next")
    assert len(client.started) == 2


@pytest.mark.asyncio
async def test_cancel_is_physically_submitted_once_under_concurrency(ids: Any) -> None:
    client = FakeClient()
    client.cancel_gate = asyncio.Event()
    controller = ChatController(client, id_factory=ids)
    await controller.dispatch_text("hello")

    first = asyncio.create_task(controller.dispatch(Cancel()))
    second = asyncio.create_task(controller.dispatch_text("/cancel"))
    await client.cancel_called.wait()
    assert len(client.cancelled) == 1
    client.cancel_gate.set()
    assert await first == (Pending("run-1", "command-1", "cancel-1"),)
    assert await second == (Pending("run-1", "command-1", "cancel-1"),)


@pytest.mark.asyncio
async def test_late_cancel_uses_completed_run_and_start_output(ids: Any) -> None:
    client = FakeClient()
    controller = ChatController(client, id_factory=ids)
    await controller.dispatch_text("hello")
    await controller.dispatch(Cancel())
    client.snapshots["command-1"] = snapshot(
        "command-1", outcome=CommandOutcome.COMPLETED, run_state=RunState.COMPLETED, output="answer"
    )
    client.snapshots["cancel-1"] = snapshot(
        "cancel-1",
        kind=CommandKind.CANCEL,
        outcome=CommandOutcome.COMPLETED,
        run_state=RunState.COMPLETED,
    )

    assert await controller.observe_once() == (Completed("answer"),)


@pytest.mark.asyncio
async def test_predispatch_cancel_requires_both_commands_cancelled(ids: Any) -> None:
    client = FakeClient()
    controller = ChatController(client, id_factory=ids)
    await controller.dispatch_text("hello")
    await controller.dispatch(Cancel())
    client.snapshots["command-1"] = snapshot(
        "command-1", outcome=CommandOutcome.CANCELLED, run_state=None
    )
    client.snapshots["cancel-1"] = snapshot(
        "cancel-1", kind=CommandKind.CANCEL, outcome=CommandOutcome.CANCELLED, run_state=None
    )

    assert await controller.observe_once() == (Cancelled(),)


@pytest.mark.asyncio
async def test_pending_cancel_does_not_forge_cancelled(ids: Any) -> None:
    client = FakeClient()
    controller = ChatController(client, id_factory=ids)
    await controller.dispatch_text("hello")
    await controller.dispatch(Cancel())
    client.snapshots["command-1"] = snapshot("command-1")
    client.snapshots["cancel-1"] = snapshot("cancel-1", kind=CommandKind.CANCEL)

    assert await controller.observe_once() == (Pending("run-1", "command-1", "cancel-1"),)


@pytest.mark.asyncio
async def test_conflicting_terminal_run_states_are_protocol_error(ids: Any) -> None:
    client = FakeClient()
    controller = ChatController(client, id_factory=ids)
    await controller.dispatch_text("hello")
    await controller.dispatch(Cancel())
    client.snapshots["command-1"] = snapshot(
        "command-1", outcome=CommandOutcome.COMPLETED, run_state=RunState.COMPLETED, output="answer"
    )
    client.snapshots["cancel-1"] = snapshot(
        "cancel-1",
        kind=CommandKind.CANCEL,
        outcome=CommandOutcome.CANCELLED,
        run_state=RunState.CANCELLED,
    )

    assert await controller.observe_once() == (ProtocolError("conflicting terminal run states"),)


@pytest.mark.asyncio
async def test_commands_have_closed_registry_and_precise_idle_pending_semantics(ids: Any) -> None:
    client = FakeClient()
    controller = ChatController(client, session="main", id_factory=ids)
    assert {item.name for item in COMMANDS} == {
        "help",
        "session",
        "new",
        "cancel",
        "quit",
        "status",
        "clear",
    }
    assert await controller.dispatch_text("/session demo") == (SessionChanged("demo"),)
    assert await controller.dispatch_text("/session") == (Notice("demo"),)
    assert await controller.dispatch_text("/new") == (
        TranscriptCleared(),
        Notice("New display context"),
    )
    assert await controller.dispatch_text("/clear") == (TranscriptCleared(),)
    assert await controller.dispatch_text("/cancel") == (Notice("No active run"),)
    assert await controller.dispatch_text("/quit") == (QuitRequested(after_cancel=False),)

    await controller.dispatch_text("hello")
    assert await controller.dispatch_text("/session other") == (
        Notice("Cancel the active run before changing session"),
    )
    assert await controller.dispatch_text("/new") == (Notice("Cancel the active run before /new"),)
    status = await controller.dispatch_text("/status")
    assert isinstance(status[0], Notice) and "state=pending" in status[0].text


@pytest.mark.asyncio
async def test_help_and_unknown_commands_are_local(ids: Any) -> None:
    client = FakeClient()
    controller = ChatController(client, id_factory=ids)
    help_events = await controller.dispatch_text("/help")
    assert isinstance(help_events[0], Notice)
    assert "/status" in help_events[0].text
    assert await controller.dispatch_text("/bogus") == (Notice("Unknown command: /bogus"),)
    assert client.started == []
    assert client.cancelled == []


@pytest.mark.asyncio
async def test_question_mark_is_local_help_without_start(ids: Any) -> None:
    client = FakeClient()
    controller = ChatController(client, id_factory=ids)

    events = await controller.dispatch_text("?")

    assert isinstance(events[0], Notice)
    assert "/help" in events[0].text
    assert client.started == []


@pytest.mark.asyncio
async def test_observe_and_cancel_deadlines_preserve_active_identity(ids: Any) -> None:
    clock = [0.0]

    async def advance(delay: float) -> None:
        clock[0] += delay

    client = FakeClient()
    controller = ChatController(
        client,
        id_factory=ids,
        now=lambda: clock[0],
        sleep=advance,
        observe_deadline_seconds=10.0,
        cancel_reconcile_seconds=0.15,
    )
    await controller.dispatch_text("hello")
    await controller.dispatch(Cancel())
    client.snapshots["command-1"] = snapshot("command-1")
    client.snapshots["cancel-1"] = snapshot("cancel-1", kind=CommandKind.CANCEL)

    events = await controller.observe_until(
        deadline_seconds=10.0, initial_delay=0.1, maximum_delay=0.1
    )

    assert events == (Timeout("run-1", "command-1"),)
    assert controller.active_identity == ("run-1", "command-1")
    assert len(client.cancelled) == 1


@pytest.mark.asyncio
async def test_observe_deadline_times_out_without_cancel(ids: Any) -> None:
    clock = [0.0]

    async def advance(delay: float) -> None:
        clock[0] += delay

    client = FakeClient()
    controller = ChatController(
        client,
        id_factory=ids,
        now=lambda: clock[0],
        sleep=advance,
        observe_deadline_seconds=0.15,
    )
    await controller.dispatch_text("hello")
    client.snapshots["command-1"] = snapshot("command-1")

    events = await controller.observe_until(
        deadline_seconds=10.0, initial_delay=0.1, maximum_delay=0.1
    )

    assert events == (Timeout("run-1", "command-1"),)
    assert client.cancelled == []


@pytest.mark.asyncio
async def test_pending_quit_waits_for_durable_reconciliation(ids: Any) -> None:
    client = FakeClient()
    controller = ChatController(client, id_factory=ids)
    await controller.dispatch_text("hello")

    assert await controller.dispatch_text("/quit") == (Pending("run-1", "command-1", "cancel-1"),)
    client.snapshots["command-1"] = snapshot(
        "command-1", outcome=CommandOutcome.CANCELLED, run_state=RunState.CANCELLED
    )
    client.snapshots["cancel-1"] = snapshot(
        "cancel-1",
        kind=CommandKind.CANCEL,
        outcome=CommandOutcome.CANCELLED,
        run_state=RunState.CANCELLED,
    )
    assert await controller.observe_once() == (
        Cancelled(),
        QuitRequested(after_cancel=True),
    )
