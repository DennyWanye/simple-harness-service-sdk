"""Typed, renderer-independent orchestration for interactive chat."""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .contracts import (
    CancelRequest,
    CommandOutcome,
    CommandSnapshot,
    OutputState,
    RunState,
    StartRequest,
)


class ChatClient(Protocol):
    async def start(self, request: StartRequest) -> object: ...
    async def get(self, command_id: str) -> CommandSnapshot: ...
    async def cancel(self, request: CancelRequest) -> object: ...


class ControllerState(StrEnum):
    IDLE = "idle"
    PENDING = "pending"
    CANCEL_IN_FLIGHT = "cancel_in_flight"


@dataclass(frozen=True, slots=True)
class Submit:
    text: str


@dataclass(frozen=True, slots=True)
class Cancel:
    pass


@dataclass(frozen=True, slots=True)
class Quit:
    pass


@dataclass(frozen=True, slots=True)
class Clear:
    pass


@dataclass(frozen=True, slots=True)
class Help:
    pass


@dataclass(frozen=True, slots=True)
class Status:
    pass


@dataclass(frozen=True, slots=True)
class SetSession:
    name: str


@dataclass(frozen=True, slots=True)
class NewSession:
    pass


ChatAction = Submit | Cancel | Quit | Clear | Help | Status | SetSession | NewSession


@dataclass(frozen=True, slots=True)
class CommandDescriptor:
    name: str
    usage: str
    description: str
    allowed_while_pending: bool


COMMANDS = (
    CommandDescriptor("help", "/help", "Show commands and shortcuts", True),
    CommandDescriptor("session", "/session NAME", "Show or change session", False),
    CommandDescriptor("new", "/new", "Start a new display context", False),
    CommandDescriptor("cancel", "/cancel", "Cancel the active run", True),
    CommandDescriptor("quit", "/quit", "Cancel the active run and exit", True),
    CommandDescriptor("status", "/status", "Show local chat status", True),
    CommandDescriptor("clear", "/clear", "Clear the display transcript", True),
)


@dataclass(frozen=True, slots=True)
class Accepted:
    run_id: str
    command_id: str


@dataclass(frozen=True, slots=True)
class Pending:
    run_id: str
    command_id: str
    cancel_command_id: str | None = None


@dataclass(frozen=True, slots=True)
class Completed:
    output_text: str


@dataclass(frozen=True, slots=True)
class Failed:
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class Cancelled:
    pass


@dataclass(frozen=True, slots=True)
class Timeout:
    run_id: str
    command_id: str


@dataclass(frozen=True, slots=True)
class ProtocolError:
    detail: str


@dataclass(frozen=True, slots=True)
class Notice:
    text: str


@dataclass(frozen=True, slots=True)
class TranscriptCleared:
    pass


@dataclass(frozen=True, slots=True)
class SessionChanged:
    session: str


@dataclass(frozen=True, slots=True)
class QuitRequested:
    after_cancel: bool


ChatEvent = (
    Accepted
    | Pending
    | Completed
    | Failed
    | Cancelled
    | Timeout
    | ProtocolError
    | Notice
    | TranscriptCleared
    | SessionChanged
    | QuitRequested
)


class ChatController:
    """Own the single active durable identity and project it into UI events."""

    def __init__(
        self,
        client: ChatClient,
        *,
        session: str = "main",
        id_factory: Callable[[str], str] | None = None,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not session.strip():
            raise ValueError("session is required")
        self._client = client
        self._session = session
        self._id_factory = id_factory or _random_id
        self._now = now
        self._sleep = sleep
        self._run_id: str | None = None
        self._start_command_id: str | None = None
        self._cancel_command_id: str | None = None
        self._cancel_task: asyncio.Task[object] | None = None
        self._start_snapshot: CommandSnapshot | None = None
        self._cancel_snapshot: CommandSnapshot | None = None
        self._quit_after_settle = False
        self._last_outcome = "idle"
        self._lock = asyncio.Lock()

    @property
    def state(self) -> ControllerState:
        if self._run_id is None:
            return ControllerState.IDLE
        if self._cancel_task is not None:
            return ControllerState.CANCEL_IN_FLIGHT
        return ControllerState.PENDING

    @property
    def session(self) -> str:
        return self._session

    @property
    def commands(self) -> tuple[CommandDescriptor, ...]:
        return COMMANDS

    async def dispatch(self, action: ChatAction) -> tuple[ChatEvent, ...]:
        if isinstance(action, Submit):
            return await self._submit(action.text)
        if isinstance(action, Cancel):
            return await self._cancel()
        if isinstance(action, Quit):
            if self.state is ControllerState.IDLE:
                return (QuitRequested(after_cancel=False),)
            self._quit_after_settle = True
            return await self._cancel()
        if isinstance(action, Clear):
            return (TranscriptCleared(),)
        if isinstance(action, Help):
            return (Notice(" ".join(item.usage for item in COMMANDS)),)
        if isinstance(action, Status):
            return (Notice(self._status_text()),)
        if isinstance(action, SetSession):
            return self._set_session(action.name)
        if isinstance(action, NewSession):
            if self.state is not ControllerState.IDLE:
                return (Notice("Cancel the active run before /new"),)
            self._last_outcome = "idle"
            return (TranscriptCleared(), Notice("New display context"))
        raise TypeError(f"unsupported chat action: {type(action).__name__}")

    async def dispatch_text(self, text: str) -> tuple[ChatEvent, ...]:
        if not text.strip():
            return ()
        if not text.startswith("/"):
            return await self.dispatch(Submit(text))
        command, _, argument = text.partition(" ")
        if command == "/help" and not argument:
            return await self.dispatch(Help())
        if command == "/status" and not argument:
            return await self.dispatch(Status())
        if command == "/clear" and not argument:
            return await self.dispatch(Clear())
        if command == "/cancel" and not argument:
            return await self.dispatch(Cancel())
        if command == "/quit" and not argument:
            return await self.dispatch(Quit())
        if command == "/new" and not argument:
            return await self.dispatch(NewSession())
        if command == "/session":
            if not argument:
                return (Notice(self._session),)
            return await self.dispatch(SetSession(argument.strip()))
        return (Notice(f"Unknown command: {command}"),)

    async def observe_once(self) -> tuple[ChatEvent, ...]:
        run_id, start_id = self._active_identity()
        start_snapshot, cancel_snapshot = await self._read_snapshots(start_id)
        self._start_snapshot = start_snapshot
        if cancel_snapshot is not None:
            self._cancel_snapshot = cancel_snapshot
        terminal = self._reconcile(start_snapshot, self._cancel_snapshot)
        if terminal is not None:
            quit_after_settle = self._quit_after_settle
            self._settle(terminal)
            if quit_after_settle:
                return (terminal, QuitRequested(after_cancel=True))
            return (terminal,)
        return (Pending(run_id, start_id, self._cancel_command_id),)

    async def observe_until(
        self,
        *,
        deadline_seconds: float,
        initial_delay: float = 0.1,
        maximum_delay: float = 1.0,
    ) -> tuple[ChatEvent, ...]:
        run_id, command_id = self._active_identity()
        deadline = self._now() + deadline_seconds
        delay = initial_delay
        while self._now() < deadline:
            events = await self.observe_once()
            if not isinstance(events[0], Pending):
                return events
            await self._sleep(delay)
            delay = min(maximum_delay, delay * 2)
        return (Timeout(run_id, command_id),)

    async def _submit(self, text: str) -> tuple[ChatEvent, ...]:
        if not text.strip():
            return ()
        async with self._lock:
            if self._run_id is not None:
                return (Notice("A run is already active"),)
            run_id = self._id_factory("run")
            command_id = self._id_factory("command")
            await self._client.start(StartRequest(self._session, run_id, command_id, text))
            self._run_id = run_id
            self._start_command_id = command_id
            self._last_outcome = "pending"
        return (Accepted(run_id, command_id), Pending(run_id, command_id))

    async def _cancel(self) -> tuple[ChatEvent, ...]:
        async with self._lock:
            if self._run_id is None or self._start_command_id is None:
                return (Notice("No active run"),)
            if self._cancel_task is None:
                self._cancel_command_id = self._id_factory("cancel")
                request = CancelRequest(self._run_id, self._cancel_command_id)
                self._cancel_task = asyncio.create_task(self._client.cancel(request))
            task = self._cancel_task
            run_id = self._run_id
            start_id = self._start_command_id
            cancel_id = self._cancel_command_id
        await asyncio.shield(task)
        return (Pending(run_id, start_id, cancel_id),)

    async def _read_snapshots(
        self, start_id: str
    ) -> tuple[CommandSnapshot, CommandSnapshot | None]:
        cancel_id = self._cancel_command_id
        if cancel_id is None:
            return await self._client.get(start_id), None
        start, cancel = await asyncio.gather(
            self._client.get(start_id), self._client.get(cancel_id)
        )
        return start, cancel

    def _reconcile(
        self, start: CommandSnapshot, cancel: CommandSnapshot | None
    ) -> ChatEvent | None:
        snapshots = (start,) if cancel is None else (start, cancel)
        terminal_states = {
            item.run_state
            for item in snapshots
            if item.run_state in {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}
        }
        if len(terminal_states) > 1:
            return ProtocolError("conflicting terminal run states")
        if terminal_states:
            state = terminal_states.pop()
            if state is RunState.COMPLETED:
                if (
                    start.outcome is not CommandOutcome.COMPLETED
                    or start.output_state is not OutputState.PRESENT
                    or start.output_text is None
                ):
                    return ProtocolError("completed run lacks start output")
                return Completed(start.output_text)
            if state is RunState.FAILED:
                error = start.error_code or (cancel.error_code if cancel else None)
                return Failed(error)
            return Cancelled()
        if any(item.outcome is CommandOutcome.PROTOCOL_ERROR for item in snapshots):
            return ProtocolError("backend protocol error")
        if cancel is not None and all(
            item.outcome is CommandOutcome.CANCELLED and item.run_state is None
            for item in snapshots
        ):
            return Cancelled()
        if any(
            item.outcome
            in {
                CommandOutcome.COMPLETED,
                CommandOutcome.FAILED,
                CommandOutcome.CANCELLED,
            }
            for item in snapshots
        ):
            return ProtocolError("closed command lacks authoritative run state")
        return None

    def _settle(self, event: ChatEvent) -> None:
        self._last_outcome = _event_name(event)
        self._run_id = None
        self._start_command_id = None
        self._cancel_command_id = None
        self._cancel_task = None
        self._start_snapshot = None
        self._cancel_snapshot = None
        self._quit_after_settle = False

    def _set_session(self, name: str) -> tuple[ChatEvent, ...]:
        if self.state is not ControllerState.IDLE:
            return (Notice("Cancel the active run before changing session"),)
        if not name.strip():
            return (Notice("Session is required"),)
        self._session = name.strip()
        self._last_outcome = "idle"
        return (SessionChanged(self._session),)

    def _active_identity(self) -> tuple[str, str]:
        if self._run_id is None or self._start_command_id is None:
            raise RuntimeError("no active run")
        return self._run_id, self._start_command_id

    def _status_text(self) -> str:
        if self._run_id is None:
            return f"session={self._session} state=idle outcome={self._last_outcome}"
        cancel = "" if self._cancel_command_id is None else f" cancel={self._cancel_command_id}"
        return (
            f"session={self._session} state={self.state.value} "
            f"run={self._run_id} command={self._start_command_id}{cancel}"
        )


def _random_id(kind: str) -> str:
    return f"{kind}-{secrets.token_hex(16)}"


def _event_name(event: ChatEvent) -> str:
    if isinstance(event, Completed):
        return "completed"
    if isinstance(event, Failed):
        return "failed"
    if isinstance(event, Cancelled):
        return "cancelled"
    if isinstance(event, ProtocolError):
        return "protocol_error"
    return "settled"
