"""Claude-style terminal presentation for the reusable chat controller."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import TextIO

from prompt_toolkit import PromptSession
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import Input, create_input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.output import ColorDepth, Output, create_output
from prompt_toolkit.shortcuts import print_formatted_text

from .chat_controller import (
    Accepted,
    Cancel,
    Cancelled,
    CancelPending,
    ChatController,
    ChatEvent,
    Completed,
    ControllerState,
    Failed,
    Notice,
    Pending,
    ProtocolError,
    Quit,
    QuitRequested,
    SessionChanged,
    Timeout,
    TranscriptCleared,
)
from .contracts import ServiceError
from .text_rendering import render_markdown_fragments, sanitize_untrusted_text


@dataclass(frozen=True, slots=True)
class ChatUiConfig:
    brand: str = "Simple Harness"
    model_label: str = "configured model"
    default_session: str = "main"
    version_label: str | None = None
    help_footer: str | None = None
    screen_reader: bool = False


class TerminalMode(StrEnum):
    INTERACTIVE = "interactive"
    FLAT = "flat"


def detect_terminal_mode(
    stdin: TextIO,
    stdout: TextIO,
    *,
    config: ChatUiConfig,
    environ: dict[str, str] | None = None,
    columns: int | None = None,
) -> TerminalMode:
    env = os.environ if environ is None else environ
    try:
        tty = stdin.isatty() and stdout.isatty()
        stdin.fileno()
        stdout.fileno()
    except (AttributeError, OSError):
        tty = False
    width = shutil.get_terminal_size((80, 24)).columns if columns is None else columns
    if (
        not tty
        or env.get("TERM", "").lower() == "dumb"
        or "NO_COLOR" in env
        or config.screen_reader
        or width < 60
    ):
        return TerminalMode.FLAT
    return TerminalMode.INTERACTIVE


class SlashCommandCompleter(Completer):
    def __init__(self, controller: ChatController) -> None:
        self._controller = controller

    def get_completions(
        self, document: object, complete_event: object
    ) -> Iterable[Completion]:
        del complete_event
        text = getattr(document, "text_before_cursor", "")
        if not isinstance(text, str) or not text.startswith("/") or " " in text:
            return
        for descriptor in self._controller.commands:
            candidate = f"/{descriptor.name}"
            if candidate.startswith(text):
                yield Completion(
                    candidate,
                    start_position=-len(text),
                    display_meta=descriptor.description,
                )


def _key_bindings() -> KeyBindings:
    bindings = KeyBindings()

    @bindings.add("enter")
    def accept_or_newline(event: object) -> None:
        buffer = event.current_buffer  # type: ignore[attr-defined]
        before = buffer.document.text_before_cursor
        if before.endswith("\\"):
            buffer.delete_before_cursor(1)
            buffer.insert_text("\n")
        else:
            buffer.validate_and_handle()

    return bindings


class TerminalChatUI:
    def __init__(
        self,
        controller: ChatController,
        *,
        config: ChatUiConfig,
        stdin: TextIO,
        stdout: TextIO,
        stderr: TextIO,
    ) -> None:
        self.controller = controller
        self.config = config
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.mode = detect_terminal_mode(stdin, stdout, config=config)
        self._quit = False
        self._exit_code = 0
        self._last_idle_interrupt = 0.0
        self._poll_delay = 0.1

    async def run(self) -> int:
        try:
            if self.mode is TerminalMode.FLAT:
                return await self._run_flat()
            return await self._run_interactive()
        except (KeyboardInterrupt, EOFError, ServiceError):
            raise
        except Exception:
            return await self._recover_renderer_failure()

    async def _run_interactive(self) -> int:
        history = InMemoryHistory()
        restart = True
        while restart and not self._quit:
            restart = await self._run_interactive_session(history)
        return self._exit_code

    async def _run_interactive_session(self, history: InMemoryHistory) -> bool:
        output: Output | None = None
        input_stream: Input | None = None
        prompt_task: asyncio.Task[str] | None = None
        observe_task: asyncio.Task[tuple[ChatEvent, ...]] | None = None
        restart = False
        try:
            output = create_output(stdout=self.stdout)
            input_stream = create_input(stdin=self.stdin)
            session: PromptSession[str] = PromptSession(
                input=input_stream,
                output=output,
                history=history,
                completer=SlashCommandCompleter(self.controller),
                complete_while_typing=True,
                key_bindings=_key_bindings(),
                multiline=False,
                color_depth=ColorDepth.DEPTH_8_BIT,
            )
            self._print_welcome(output)
            while not self._quit:
                if prompt_task is None:
                    prompt_task = asyncio.create_task(
                        session.prompt_async(
                            FormattedText([("class:prompt", "\u276f ")]),
                            bottom_toolbar=self._toolbar,
                        )
                    )
                if self.controller.state is not ControllerState.IDLE and observe_task is None:
                    observe_task = asyncio.create_task(
                        self.controller.observe_until(deadline_seconds=315.0)
                    )
                text, events = await _wait_for_activity(prompt_task, observe_task)
                if events is not None:
                    coordinate = not prompt_task.done()
                    await self._emit_events(events, output, coordinate=coordinate)
                    observe_task = None
                if text is not None:
                    prompt_task = None
                    await self._handle_ready_text(text, output)
        except KeyboardInterrupt:
            if output is None:
                raise
            if self.controller.state is not ControllerState.IDLE:
                await self._emit_events(await self.controller.dispatch(Cancel()), output)
            elif time.monotonic() - self._last_idle_interrupt <= 2.0:
                self._quit = True
            else:
                self._last_idle_interrupt = time.monotonic()
                self._print_notice("Press Ctrl-C again to exit", output)
            if not self._quit:
                restart = True
        except EOFError:
            if output is None:
                raise
            if self.controller.state is not ControllerState.IDLE:
                await self._emit_events(await self.controller.dispatch(Quit()), output)
                if not self._quit:
                    restart = True
            else:
                self._quit = True
        finally:
            tasks = [task for task in (prompt_task, observe_task) if task is not None]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if output is not None:
                output.reset_attributes()
                output.show_cursor()
                output.flush()
            if input_stream is not None:
                input_stream.close()
        return restart

    async def _recover_renderer_failure(self) -> int:
        identity = self.controller.active_identity
        suffix = "" if identity is None else f" run_id={identity[0]} command_id={identity[1]}"
        self._write_recovery(f"terminal-ui-render-error{suffix}")
        if identity is None:
            return 3
        # At most one CancelPending event can be emitted because the controller
        # clears its five-second cancel deadline before returning that event.
        for _phase in range(2):
            events = await self.controller.observe_until(deadline_seconds=315.0)
            terminal = self._emit_recovery_events(events)
            if terminal:
                return self._exit_code
        self._write_recovery("protocol_error")
        return 3

    async def _run_flat(self) -> int:
        lines: asyncio.Queue[str] = asyncio.Queue()
        buffered: deque[str] = deque()
        reader = asyncio.create_task(self._pump_flat_lines(lines))
        try:
            while not self._quit:
                if self.controller.state is ControllerState.IDLE:
                    line = buffered.popleft() if buffered else await lines.get()
                    if line == "":
                        break
                    await self._handle_flat_text(line.rstrip("\r\n"))
                    continue
                try:
                    queued_line = lines.get_nowait()
                except asyncio.QueueEmpty:
                    queued_line = None
                if queued_line is not None:
                    message = queued_line.rstrip("\r\n")
                    if message.startswith("/") or queued_line == "":
                        if queued_line == "":
                            self._emit_flat_events(await self.controller.dispatch(Quit()))
                        else:
                            await self._handle_flat_text(message)
                    else:
                        buffered.append(queued_line)
                    continue
                events = await self.controller.observe_once()
                self._emit_flat_events(events)
                if not events or not isinstance(events[0], Pending):
                    self._poll_delay = 0.1
                    continue
                try:
                    line = await asyncio.wait_for(lines.get(), timeout=self._poll_delay)
                except TimeoutError:
                    self._poll_delay = min(1.0, self._poll_delay * 2)
                    continue
                if line == "":
                    self._emit_flat_events(await self.controller.dispatch(Quit()))
                    continue
                await self._handle_flat_text(line.rstrip("\r\n"))
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        return self._exit_code

    async def _pump_flat_lines(self, queue: asyncio.Queue[str]) -> None:
        eof = False
        while not eof:
            line = await self._read_flat_line()
            await queue.put(line)
            eof = line == ""

    async def _read_flat_line(self) -> str:
        try:
            descriptor = self.stdin.fileno()
        except (AttributeError, OSError):
            return await asyncio.to_thread(self.stdin.readline)
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[str] = loop.create_future()

        def read_ready() -> None:
            if ready.done():
                return
            try:
                ready.set_result(self.stdin.readline())
            except BaseException as error:
                ready.set_exception(error)

        loop.add_reader(descriptor, read_ready)
        try:
            return await ready
        finally:
            loop.remove_reader(descriptor)

    async def _handle_text(self, text: str, output: Output) -> None:
        await self._emit_events(await self.controller.dispatch_text(text), output)

    async def _handle_ready_text(self, text: str, output: Output) -> None:
        if not self._quit:
            await self._handle_text(text, output)

    async def _handle_flat_text(self, text: str) -> None:
        self._emit_flat_events(await self.controller.dispatch_text(text))

    async def _emit_events(
        self,
        events: tuple[ChatEvent, ...],
        output: Output,
        *,
        coordinate: bool = False,
    ) -> None:
        if coordinate:
            await run_in_terminal(lambda: self._emit_events_now(events, output))
        else:
            self._emit_events_now(events, output)

    def _emit_events_now(self, events: tuple[ChatEvent, ...], output: Output) -> None:
        for event in events:
            if isinstance(event, Accepted | Pending):
                continue
            if isinstance(event, Completed):
                fragments = [("class:assistant", "● ")]
                fragments.extend(render_markdown_fragments(event.output_text))
                print_formatted_text(FormattedText(fragments), output=output)
            elif isinstance(event, TranscriptCleared):
                output.erase_screen()
                output.cursor_goto(0, 0)
                output.flush()
                self._print_welcome(output)
            elif isinstance(event, Notice | SessionChanged):
                text = event.text if isinstance(event, Notice) else f"Session: {event.session}"
                self._print_notice(text, output)
            elif isinstance(event, Cancelled):
                self._print_notice("Cancelled", output)
            elif isinstance(event, CancelPending):
                self._print_notice(
                    f"Cancel pending run={event.run_id} command={event.cancel_command_id}",
                    output,
                )
            elif isinstance(event, Failed):
                detail = "" if event.error_code is None else f": {event.error_code}"
                self._print_notice(f"Failed{detail}", output)
                self._exit_code = 5
                self._quit = True
            elif isinstance(event, ProtocolError):
                self._print_notice(f"Protocol error: {event.detail}", output)
                self._exit_code = 3
                self._quit = True
            elif isinstance(event, Timeout):
                self._print_notice(f"Timeout run={event.run_id} command={event.command_id}", output)
                self._exit_code = 4
                self._quit = True
            elif isinstance(event, QuitRequested):
                if event.after_cancel:
                    self._exit_code = 6
                self._quit = True

    def _emit_flat_events(self, events: tuple[ChatEvent, ...]) -> None:
        for event in events:
            if isinstance(event, Accepted):
                self._write_flat("Working…")
            elif isinstance(event, Pending):
                continue
            elif isinstance(event, Completed):
                self._write_flat(sanitize_untrusted_text(event.output_text))
            elif isinstance(event, Notice):
                self._write_flat(sanitize_untrusted_text(event.text))
            elif isinstance(event, SessionChanged):
                self._write_flat(f"Session: {sanitize_untrusted_text(event.session)}")
            elif isinstance(event, TranscriptCleared):
                self._write_flat("Display cleared")
            elif isinstance(event, Cancelled):
                self._write_flat("Cancelled")
            elif isinstance(event, CancelPending):
                self._write_flat(
                    f"Cancel pending run={event.run_id} command={event.cancel_command_id}"
                )
            elif isinstance(event, Failed):
                self._write_flat("Failed")
                self._exit_code = 5
                self._quit = True
            elif isinstance(event, ProtocolError):
                self._write_flat("Protocol error")
                self._exit_code = 3
                self._quit = True
            elif isinstance(event, Timeout):
                self._write_flat(f"Timeout run={event.run_id} command={event.command_id}")
                self._exit_code = 4
                self._quit = True
            elif isinstance(event, QuitRequested):
                if event.after_cancel:
                    self._exit_code = 6
                self._quit = True

    def _print_welcome(self, output: Output) -> None:
        brand = sanitize_untrusted_text(self.config.brand)
        model = sanitize_untrusted_text(self.config.model_label)
        version = (
            ""
            if self.config.version_label is None
            else f" v{sanitize_untrusted_text(self.config.version_label)}"
        )
        fragments = FormattedText(
            [
                ("class:brand", f"╭─── {brand}{version} "),
                ("class:muted", "─" * 20 + "╮\n"),
                ("", f"│  {model} · session {sanitize_untrusted_text(self.controller.session)}\n"),
                ("class:muted", "╰" + "─" * 39 + "╯"),
            ]
        )
        print_formatted_text(fragments, output=output)

    def _toolbar(self) -> FormattedText:
        state = self.controller.state.value
        return FormattedText([("class:toolbar", f" {state} · ? for shortcuts · / for commands ")])

    def _print_notice(self, text: str, output: Output) -> None:
        print_formatted_text(
            FormattedText([("class:notice", sanitize_untrusted_text(text))]), output=output
        )

    def _write_flat(self, text: str) -> None:
        self.stdout.write(text + "\n")
        self.stdout.flush()

    def _emit_recovery_events(self, events: tuple[ChatEvent, ...]) -> bool:
        for event in events:
            if isinstance(event, Completed):
                self._write_recovery(sanitize_untrusted_text(event.output_text))
                return True
            if isinstance(event, Cancelled):
                self._write_recovery("cancelled")
                self._exit_code = 6
                return True
            if isinstance(event, CancelPending):
                self._write_recovery(
                    f"cancel pending run_id={event.run_id} "
                    f"command_id={event.cancel_command_id}"
                )
                continue
            if isinstance(event, Failed):
                self._write_recovery("failed")
                self._exit_code = 5
                return True
            if isinstance(event, ProtocolError):
                self._write_recovery("protocol_error")
                self._exit_code = 3
                return True
            if isinstance(event, Timeout):
                self._write_recovery(
                    f"timeout run_id={event.run_id} command_id={event.command_id}"
                )
                self._exit_code = 4
                return True
        return False

    def _write_recovery(self, text: str) -> None:
        with contextlib.suppress(Exception):
            self.stderr.write(sanitize_untrusted_text(text) + "\n")
            self.stderr.flush()


async def _wait_for_activity(
    prompt_task: asyncio.Task[str],
    observe_task: asyncio.Task[tuple[ChatEvent, ...]] | None,
) -> tuple[str | None, tuple[ChatEvent, ...] | None]:
    waiting: set[asyncio.Task[object]] = {prompt_task}
    if observe_task is not None:
        waiting.add(observe_task)
    done, _pending = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
    # Process durable state first, but never discard simultaneously completed input.
    events = observe_task.result() if observe_task is not None and observe_task in done else None
    text = prompt_task.result() if prompt_task in done else None
    return text, events
