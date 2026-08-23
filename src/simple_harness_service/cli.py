"""Reusable CLI engine for chat/ask/status/cancel/session commands."""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
import time
from collections.abc import Callable, Sequence
from enum import IntEnum
from pathlib import Path
from typing import TextIO

from .contracts import (
    CancelRequest,
    CommandSnapshot,
    ContinueRequest,
    OutputState,
    ServiceError,
    StartRequest,
)
from .transports.unix import UnixServiceClient

DEFAULT_SESSION = "main"
OBSERVE_DEADLINE_SECONDS = 315.0
POLL_SECONDS = 0.2


class ExitCode(IntEnum):
    OK = 0
    USAGE = 2
    UNAVAILABLE = 3
    REJECTED = 4
    TIMEOUT = 5
    INTERRUPTED = 130


class CliEngine:
    def __init__(
        self,
        client_factory: Callable[[Path], UnixServiceClient] = UnixServiceClient,
        *,
        stdin: TextIO = sys.stdin,
        stdout: TextIO = sys.stdout,
        stderr: TextIO = sys.stderr,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client_factory = client_factory
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self._now = now

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
                return ExitCode.OK
            if args.command == "cancel":
                receipt = await client.cancel(
                    CancelRequest(args.run_id, f"cancel-{secrets.token_hex(16)}")
                )
                print(receipt.command_id, file=self.stdout)
                return ExitCode.OK
            if args.command == "chat":
                return await self._chat(client, args.session)
            return ExitCode.USAGE
        except KeyboardInterrupt:
            return ExitCode.INTERRUPTED
        except ServiceError as error:
            print(error.code.value, file=self.stderr)
            return ExitCode.TIMEOUT if error.code.value == "timeout" else ExitCode.UNAVAILABLE

    async def _ask(self, client: UnixServiceClient, message: str, session: str) -> int:
        run_id = f"run-{secrets.token_hex(16)}"
        command_id = f"command-{secrets.token_hex(16)}"
        await client.start(StartRequest(session, run_id, command_id, message))
        print(f"accepted run_id={run_id} command_id={command_id}", file=self.stderr)
        return await self._observe(client, run_id, command_id)

    async def _observe(
        self, client: UnixServiceClient, run_id: str, command_id: str
    ) -> int:
        try:
            deadline = self._now() + OBSERVE_DEADLINE_SECONDS
            while self._now() < deadline:
                snapshot = await client.get(command_id)
                if snapshot.output_state is OutputState.PRESENT:
                    print(snapshot.output_text, file=self.stdout)
                    return ExitCode.OK
                if snapshot.output_state in {OutputState.ABSENT, OutputState.UNKNOWN}:
                    print(snapshot.output_state.value, file=self.stderr)
                    return ExitCode.REJECTED
                await asyncio.sleep(POLL_SECONDS)
            print(f"timeout run_id={run_id} command_id={command_id}", file=self.stderr)
            return ExitCode.TIMEOUT
        except asyncio.CancelledError:
            cancel = CancelRequest(run_id, f"cancel-{secrets.token_hex(16)}")
            await asyncio.shield(client.cancel(cancel))
            raise

    async def _chat(self, client: UnixServiceClient, session: str) -> int:
        run_id: str | None = None
        last_command: str | None = None
        current_session = session
        while True:
            line = self.stdin.readline()
            if line == "":
                return ExitCode.OK
            message = line.rstrip("\n")
            if not message:
                continue
            if message == "/quit":
                return ExitCode.OK
            if message == "/help":
                print("/help /session NAME /new /cancel /quit", file=self.stdout)
                continue
            if message.startswith("/session "):
                current_session = message.removeprefix("/session ").strip()
                if not current_session:
                    print("session is required", file=self.stderr)
                    continue
                run_id = None
                last_command = None
                continue
            if message == "/session":
                print(current_session, file=self.stdout)
                continue
            if message == "/new":
                run_id = None
                last_command = None
                continue
            if message == "/cancel":
                if run_id is not None:
                    await client.cancel(
                        CancelRequest(run_id, f"cancel-{secrets.token_hex(16)}")
                    )
                continue
            command_id = f"command-{secrets.token_hex(16)}"
            if run_id is None:
                run_id = f"run-{secrets.token_hex(16)}"
                await client.start(
                    StartRequest(current_session, run_id, command_id, message)
                )
            else:
                await client.continue_(
                    ContinueRequest(
                        current_session,
                        run_id,
                        command_id,
                        f"continuation-{secrets.token_hex(16)}",
                        message,
                    )
                )
            print(f"accepted run_id={run_id} command_id={command_id}", file=self.stderr)
            last_command = command_id
            result = await self._observe(client, run_id, last_command)
            if result != ExitCode.OK:
                return result


def _snapshot_json(value: CommandSnapshot) -> dict[str, object]:
    return {
        "command_id": value.receipt.command_id,
        "run_id": value.receipt.run_id,
        "state": value.receipt.state.value,
        "output_state": value.output_state.value,
        "output_text": value.output_text,
        "error_code": value.error_code,
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


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return asyncio.run(CliEngine().run(sys.argv[1:] if argv is None else argv))
    except KeyboardInterrupt:
        return ExitCode.INTERRUPTED


if __name__ == "__main__":
    raise SystemExit(main())
