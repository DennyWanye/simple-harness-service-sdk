from __future__ import annotations

import io
from typing import Any

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from simple_harness_service.chat_controller import ChatController
from simple_harness_service.terminal_ui import (
    ChatUiConfig,
    SlashCommandCompleter,
    TerminalChatUI,
    TerminalMode,
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
