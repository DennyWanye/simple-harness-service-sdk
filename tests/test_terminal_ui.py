from __future__ import annotations

import asyncio
import io
from typing import Any

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from simple_harness_service.chat_controller import (
    ChatController,
    Failed,
    Pending,
    TranscriptCleared,
)
from simple_harness_service.terminal_ui import (
    ChatUiConfig,
    SlashCommandCompleter,
    TerminalChatUI,
    TerminalMode,
    _wait_for_activity,
    detect_terminal_mode,
)


class FakeClient:
    async def start(self, request: Any) -> object:
        raise AssertionError("local commands must not start a run")

    async def get(self, command_id: str) -> Any:
        raise AssertionError("local commands must not read a run")

    async def cancel(self, request: Any) -> object:
        raise AssertionError("local commands must not cancel a run")


class TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return 1


def test_capability_detector_is_fail_closed() -> None:
    config = ChatUiConfig()
    assert (
        detect_terminal_mode(io.StringIO(), io.StringIO(), config=config, environ={})
        is TerminalMode.FLAT
    )
    tty = TtyStringIO()
    assert (
        detect_terminal_mode(tty, tty, config=config, environ={}, columns=80)
        is TerminalMode.INTERACTIVE
    )
    for environment in ({"NO_COLOR": "1"}, {"TERM": "dumb"}):
        assert (
            detect_terminal_mode(tty, tty, config=config, environ=environment, columns=80)
            is TerminalMode.FLAT
        )
    assert (
        detect_terminal_mode(tty, tty, config=config, environ={}, columns=59)
        is TerminalMode.FLAT
    )
    assert (
        detect_terminal_mode(
            tty, tty, config=ChatUiConfig(screen_reader=True), environ={}, columns=80
        )
        is TerminalMode.FLAT
    )


def test_slash_completion_exposes_closed_registry() -> None:
    completer = SlashCommandCompleter(ChatController(FakeClient()))
    matches = list(completer.get_completions(Document("/st"), CompleteEvent()))
    assert [item.text for item in matches] == ["/status"]
    assert list(completer.get_completions(Document("hello"), CompleteEvent())) == []


@pytest.mark.asyncio
async def test_flat_mode_has_no_ansi_and_keeps_local_commands() -> None:
    stdin = io.StringIO("/help\n/status\n/quit\n")
    stdout = io.StringIO()
    controller = ChatController(FakeClient(), session="main")
    ui = TerminalChatUI(
        controller,
        config=ChatUiConfig(brand="AIPhone\x1b]2;bad", model_label="model\u202e"),
        stdin=stdin,
        stdout=stdout,
        stderr=io.StringIO(),
    )

    assert await ui.run() == 0
    output = stdout.getvalue()
    assert "/help /session NAME /new /cancel /quit /status /clear" in output
    assert "session=main state=idle" in output
    assert "\x1b" not in output
    assert "\u202e" not in output


@pytest.mark.asyncio
async def test_pending_prompt_survives_observation_and_simultaneous_results() -> None:
    prompt_gate = asyncio.Event()

    async def prompt() -> str:
        await prompt_gate.wait()
        return "/status"

    async def observed() -> tuple[Pending, ...]:
        return (Pending("run-1", "command-1"),)

    prompt_task = asyncio.create_task(prompt())
    observe_task = asyncio.create_task(observed())
    _text, events = await _wait_for_activity(prompt_task, observe_task)
    assert events == (Pending("run-1", "command-1"),)
    assert not prompt_task.done()
    assert not prompt_task.cancelled()

    prompt_gate.set()
    await asyncio.sleep(0)
    observe_task = asyncio.create_task(observed())
    await asyncio.sleep(0)
    text, events = await _wait_for_activity(prompt_task, observe_task)
    assert text == "/status"
    assert events == (Pending("run-1", "command-1"),)


@pytest.mark.asyncio
async def test_clear_uses_injected_output_only(monkeypatch: pytest.MonkeyPatch) -> None:
    class RecordingOutput:
        def __init__(self) -> None:
            self.calls: list[object] = []

        def erase_screen(self) -> None:
            self.calls.append("erase")

        def cursor_goto(self, row: int, column: int) -> None:
            self.calls.append((row, column))

        def flush(self) -> None:
            self.calls.append("flush")

    ui = TerminalChatUI(
        ChatController(FakeClient()),
        config=ChatUiConfig(),
        stdin=io.StringIO(),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    monkeypatch.setattr(ui, "_print_welcome", lambda output: None)
    output = RecordingOutput()

    await ui._emit_events((TranscriptCleared(),), output)  # type: ignore[arg-type]

    assert output.calls == ["erase", (0, 0), "flush"]


@pytest.mark.asyncio
async def test_renderer_failure_reconciles_in_flat_mode_without_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CompletingClient(FakeClient):
        def __init__(self) -> None:
            self.started: list[Any] = []
            self.cancelled: list[Any] = []

        async def start(self, request: Any) -> object:
            self.started.append(request)
            return object()

        async def get(self, command_id: str) -> Any:
            from simple_harness_service import (
                CommandKind,
                CommandOutcome,
                CommandReceipt,
                CommandSnapshot,
                CommandState,
                OutputState,
                RunState,
            )

            return CommandSnapshot(
                CommandReceipt(
                    command_id,
                    "backend-run",
                    1,
                    CommandState.APPLIED,
                    1,
                    CommandKind.START,
                ),
                OutputState.PRESENT,
                "answer",
                run_state=RunState.COMPLETED,
                outcome=CommandOutcome.COMPLETED,
            )

        async def cancel(self, request: Any) -> object:
            self.cancelled.append(request)
            return object()

    client = CompletingClient()
    controller = ChatController(client, id_factory=lambda kind: f"{kind}-1")
    await controller.dispatch_text("hello")
    stdout = io.StringIO()
    stderr = io.StringIO()
    ui = TerminalChatUI(
        controller,
        config=ChatUiConfig(),
        stdin=io.StringIO(),
        stdout=stdout,
        stderr=stderr,
    )
    ui.mode = TerminalMode.INTERACTIVE

    async def fail_renderer() -> int:
        raise RuntimeError("render failed")

    monkeypatch.setattr(ui, "_run_interactive", fail_renderer)

    assert await ui.run() == 0
    assert stdout.getvalue() == ""
    assert "answer" in stderr.getvalue()
    assert "terminal-ui-render-error run_id=run-1 command_id=command-1" in stderr.getvalue()
    assert client.cancelled == []


@pytest.mark.asyncio
async def test_exit_event_suppresses_simultaneous_prompt_submit(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ui = TerminalChatUI(
        ChatController(FakeClient()),
        config=ChatUiConfig(),
        stdin=io.StringIO(),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    output = object()
    monkeypatch.setattr(ui, "_print_notice", lambda text, selected: None)
    await ui._emit_events((Failed(),), output)  # type: ignore[arg-type]
    await ui._handle_ready_text("must-not-start", output)  # type: ignore[arg-type]
    assert ui._quit is True


@pytest.mark.asyncio
async def test_interactive_restart_reuses_history_without_recursion(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ui = TerminalChatUI(
        ChatController(FakeClient()),
        config=ChatUiConfig(),
        stdin=io.StringIO(),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    histories: list[int] = []

    async def session(history) -> bool:  # type: ignore[no-untyped-def]
        histories.append(id(history))
        return len(histories) == 1

    monkeypatch.setattr(ui, "_run_interactive_session", session)
    assert await ui._run_interactive() == 0
    assert len(histories) == 2
    assert len(set(histories)) == 1


@pytest.mark.asyncio
async def test_interactive_session_closes_input_and_restores_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from simple_harness_service import terminal_ui as ui_module

    class RecordingInput:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class RecordingOutput:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def reset_attributes(self) -> None:
            self.calls.append("reset")

        def show_cursor(self) -> None:
            self.calls.append("cursor")

        def flush(self) -> None:
            self.calls.append("flush")

    class FailingSession:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def prompt_async(self, *args: Any, **kwargs: Any) -> str:
            raise EOFError

    selected_input = RecordingInput()
    selected_output = RecordingOutput()
    monkeypatch.setattr(ui_module, "create_input", lambda stdin: selected_input)
    monkeypatch.setattr(ui_module, "create_output", lambda stdout: selected_output)
    monkeypatch.setattr(ui_module, "PromptSession", FailingSession)
    ui = TerminalChatUI(
        ChatController(FakeClient()),
        config=ChatUiConfig(),
        stdin=io.StringIO(),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    monkeypatch.setattr(ui, "_print_welcome", lambda output: None)

    assert await ui._run_interactive_session(object()) is False  # type: ignore[arg-type]
    assert selected_input.closed is True
    assert selected_output.calls == ["reset", "cursor", "flush"]


@pytest.mark.asyncio
async def test_active_prompt_output_uses_terminal_coordination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from simple_harness_service import terminal_ui as ui_module

    called: list[str] = []

    async def coordinated(callback) -> None:  # type: ignore[no-untyped-def]
        called.append("coordinated")
        callback()

    ui = TerminalChatUI(
        ChatController(FakeClient()),
        config=ChatUiConfig(),
        stdin=io.StringIO(),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    monkeypatch.setattr(ui_module, "run_in_terminal", coordinated)
    monkeypatch.setattr(ui, "_emit_events_now", lambda events, output: called.append("emit"))

    await ui._emit_events((Pending("run-1", "command-1"),), object(), coordinate=True)  # type: ignore[arg-type]
    assert called == ["coordinated", "emit"]


@pytest.mark.asyncio
async def test_flat_stdout_failure_recovers_only_through_stderr() -> None:
    class BrokenStdout(io.StringIO):
        def write(self, value: str) -> int:
            raise OSError("stdout failed")

    class CompletingClient:
        def __init__(self) -> None:
            self.cancelled: list[Any] = []

        async def start(self, request: Any) -> object:
            self.command_id = request.external_command_id
            return object()

        async def get(self, command_id: str) -> Any:
            from simple_harness_service import (
                CommandKind,
                CommandOutcome,
                CommandReceipt,
                CommandSnapshot,
                CommandState,
                OutputState,
                RunState,
            )

            return CommandSnapshot(
                CommandReceipt(
                    command_id,
                    "backend-run",
                    1,
                    CommandState.APPLIED,
                    1,
                    CommandKind.START,
                ),
                OutputState.PRESENT,
                "answer",
                run_state=RunState.COMPLETED,
                outcome=CommandOutcome.COMPLETED,
            )

        async def cancel(self, request: Any) -> object:
            self.cancelled.append(request)
            return object()

    client = CompletingClient()
    stderr = io.StringIO()
    ui = TerminalChatUI(
        ChatController(client, id_factory=lambda kind: f"{kind}-1"),
        config=ChatUiConfig(screen_reader=True),
        stdin=io.StringIO("hello\n"),
        stdout=BrokenStdout(),
        stderr=stderr,
    )

    assert await ui.run() == 0
    assert "terminal-ui-render-error run_id=run-1 command_id=command-1" in stderr.getvalue()
    assert "answer" in stderr.getvalue()
    assert client.cancelled == []
