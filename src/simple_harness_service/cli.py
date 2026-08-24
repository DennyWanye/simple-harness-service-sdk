"""Reusable CLI engine for chat/ask/status/cancel/session commands."""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
import time
from collections import deque
from collections.abc import Callable, Sequence
from enum import IntEnum
from pathlib import Path
from typing import Protocol, TextIO

from .chat_controller import ChatController
from .contracts import (
    CancelRequest,
    CommandOutcome,
    CommandSnapshot,
    ContinueRequest,
    ServiceError,
    StartRequest,
)
from .terminal_ui import ChatUiConfig, TerminalChatUI
from .transports.unix import UnixServiceClient

DEFAULT_SESSION = "main"
OBSERVE_DEADLINE_SECONDS = 315.0
CANCEL_RECONCILE_SECONDS = 5.0
POLL_INITIAL_SECONDS = 0.1
POLL_MAX_SECONDS = 1.0


class CliClient(Protocol):
    async def start(self, request: StartRequest) -> object: ...
    async def continue_(self, request: ContinueRequest) -> object: ...
    async def get(self, command_id: str) -> CommandSnapshot: ...
    async def cancel(self, request: CancelRequest) -> object: ...


class ExitCode(IntEnum):
    OK = 0
    USAGE = 2
    PROTOCOL = 3
    TIMEOUT = 4
    FAILED = 5
    CANCELLED = 6


class CliEngine:
    def __init__(
        self,
        client_factory: Callable[[Path], CliClient] = UnixServiceClient,
        *,
        stdin: TextIO = sys.stdin,
        stdout: TextIO = sys.stdout,
        stderr: TextIO = sys.stderr,
        now: Callable[[], float] = time.monotonic,
        chat_ui_config: ChatUiConfig | None = None,
    ) -> None:
        self._client_factory = client_factory
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self._now = now
        self._chat_ui_config = chat_ui_config or ChatUiConfig()

    async def run(self, argv: Sequence[str]) -> int:
        parser = _parser()
        try:
            args = parser.parse_args(list(argv))
        except SystemExit as error:
            return error.code if isinstance(error.code, int) else ExitCode.USAGE
        client = self._client_factory(args.socket)
        try:
            if args.command == "ask":
                message = self.stdin.read()
                if not message.strip():
                    return ExitCode.USAGE
                return await self._ask(client, message, args.session)
            if args.command == "status":
                snapshot = await client.get(args.command_id)
                print(json.dumps(_snapshot_json(snapshot), sort_keys=True), file=self.stdout)
                return self._emit_outcome(snapshot, emit_output=False)
            if args.command == "cancel":
                return await self._cancel_and_reconcile(
                    client,
                    args.run_id,
                    f"cancel-{secrets.token_hex(16)}",
                )
            if args.command == "chat":
                return await self._chat(client, args.session)
            return ExitCode.USAGE
        except KeyboardInterrupt:
            return ExitCode.CANCELLED
        except ServiceError as error:
            print(error.code.value, file=self.stderr)
            return ExitCode.TIMEOUT if error.code.value == "timeout" else ExitCode.PROTOCOL

    async def _ask(self, client: CliClient, message: str, session: str) -> int:
        run_id = f"run-{secrets.token_hex(16)}"
        command_id = f"command-{secrets.token_hex(16)}"
        await client.start(StartRequest(session, run_id, command_id, message))
        print(f"accepted run_id={run_id} command_id={command_id}", file=self.stderr)
        return await self._observe(client, run_id, command_id)

    async def _observe(
        self,
        client: CliClient,
        run_id: str,
        command_id: str,
        *,
        deadline_seconds: float = OBSERVE_DEADLINE_SECONDS,
        cancel_on_interrupt: bool = True,
    ) -> int:
        try:
            deadline = self._now() + deadline_seconds
            delay = POLL_INITIAL_SECONDS
            while self._now() < deadline:
                snapshot = await client.get(command_id)
                if snapshot.outcome is not CommandOutcome.PENDING:
                    return self._emit_outcome(snapshot)
                await asyncio.sleep(delay)
                delay = min(POLL_MAX_SECONDS, delay * 2)
            print(f"timeout run_id={run_id} command_id={command_id}", file=self.stderr)
            return ExitCode.TIMEOUT
        except asyncio.CancelledError:
            if cancel_on_interrupt:
                await asyncio.shield(
                    self._cancel_and_reconcile(
                        client,
                        run_id,
                        f"cancel-{secrets.token_hex(16)}",
                    )
                )
            raise

    def _emit_outcome(self, snapshot: CommandSnapshot, *, emit_output: bool = True) -> int:
        if snapshot.outcome is CommandOutcome.COMPLETED:
            if emit_output and snapshot.output_text is not None:
                print(snapshot.output_text, file=self.stdout)
            return ExitCode.OK
        if snapshot.outcome is CommandOutcome.FAILED:
            print("failed", file=self.stderr)
            return ExitCode.FAILED
        if snapshot.outcome is CommandOutcome.CANCELLED:
            print("cancelled", file=self.stderr)
            return ExitCode.CANCELLED
        if snapshot.outcome is CommandOutcome.PROTOCOL_ERROR:
            print("protocol_error", file=self.stderr)
            return ExitCode.PROTOCOL
        raise RuntimeError("pending outcome cannot be emitted")

    async def _cancel_and_reconcile(
        self,
        client: CliClient,
        run_id: str,
        cancel_command_id: str,
    ) -> int:
        await client.cancel(CancelRequest(run_id, cancel_command_id))
        print(
            f"accepted run_id={run_id} command_id={cancel_command_id}",
            file=self.stderr,
        )
        result = await self._observe(
            client,
            run_id,
            cancel_command_id,
            deadline_seconds=CANCEL_RECONCILE_SECONDS,
            cancel_on_interrupt=False,
        )
        if result is ExitCode.TIMEOUT:
            print(
                f"cancel pending run_id={run_id} command_id={cancel_command_id}",
                file=self.stderr,
            )
        return result

    async def _chat(self, client: CliClient, session: str) -> int:
        """Compatibility wrapper for callers that exercised the former private chat hook."""

        config = ChatUiConfig(
            brand=self._chat_ui_config.brand,
            model_label=self._chat_ui_config.model_label,
            default_session=session,
            version_label=self._chat_ui_config.version_label,
            help_footer=self._chat_ui_config.help_footer,
            screen_reader=self._chat_ui_config.screen_reader,
        )
        return await TerminalChatUI(
            ChatController(client, session=session, now=self._now),
            config=config,
            stdin=self.stdin,
            stdout=self.stdout,
            stderr=self.stderr,
        ).run()

    async def _observe_active_chat(
        self,
        client: CliClient,
        lines: asyncio.Queue[str],
        buffered: deque[str],
        run_id: str,
        command_id: str,
    ) -> tuple[int, bool]:
        deadline = self._now() + OBSERVE_DEADLINE_SECONDS
        delay = POLL_INITIAL_SECONDS
        while self._now() < deadline:
            try:
                line = await asyncio.wait_for(lines.get(), timeout=delay)
            except TimeoutError:
                snapshot = await client.get(command_id)
                if snapshot.outcome is not CommandOutcome.PENDING:
                    return self._emit_outcome(snapshot), True
                delay = min(POLL_MAX_SECONDS, delay * 2)
                continue
            message = line.rstrip("\n")
            if message in {"/cancel", "/new", "/quit"}:
                result = await self._cancel_and_reconcile(
                    client,
                    run_id,
                    f"cancel-{secrets.token_hex(16)}",
                )
                if message == "/quit":
                    code = (
                        ExitCode.CANCELLED
                        if result in {ExitCode.OK, ExitCode.CANCELLED}
                        else result
                    )
                    return code, False
                if result not in {ExitCode.OK, ExitCode.CANCELLED}:
                    return result, False
                return ExitCode.OK, True
            if message == "/help":
                print("/help /session NAME /new /cancel /quit", file=self.stdout)
                continue
            buffered.append(line)
        print(f"timeout run_id={run_id} command_id={command_id}", file=self.stderr)
        return ExitCode.TIMEOUT, False


def _snapshot_json(value: CommandSnapshot) -> dict[str, object]:
    return {
        "command_id": value.receipt.command_id,
        "run_id": value.receipt.run_id,
        "state": value.receipt.state.value,
        "kind": value.receipt.kind.value,
        "output_state": value.output_state.value,
        "output_text": value.output_text,
        "error_code": value.error_code,
        "run_state": None if value.run_state is None else value.run_state.value,
        "outcome": value.outcome.value,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="simple-harness-service")
    parser.add_argument("--socket", type=Path, required=True)
    subcommands = parser.add_subparsers(dest="command", required=True)
    ask = subcommands.add_parser("ask")
    ask.add_argument("--stdin", action="store_true", required=True)
    ask.add_argument("--session", default=DEFAULT_SESSION)
    chat = subcommands.add_parser("chat")
    chat.add_argument("--session", default=DEFAULT_SESSION)
    status = subcommands.add_parser("status")
    status.add_argument("command_id")
    cancel = subcommands.add_parser("cancel")
    cancel.add_argument("run_id")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    chat_ui_config: ChatUiConfig | None = None,
) -> int:
    try:
        return asyncio.run(
            CliEngine(chat_ui_config=chat_ui_config).run(sys.argv[1:] if argv is None else argv)
        )
    except KeyboardInterrupt:
        return ExitCode.CANCELLED


if __name__ == "__main__":
    raise SystemExit(main())
