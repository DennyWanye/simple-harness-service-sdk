"""Authenticated loopback WebSocket host for product-local Realtime."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import secrets
from collections.abc import Awaitable, Callable, Iterable
from typing import Protocol

from .websocket import WebSocketConnection

LOCAL_REALTIME_PATH = "/ws/realtime-voice"
LOCAL_REALTIME_VERSION = "2026-08-27.1"
DEFAULT_MAX_FRAME_BYTES = 1_048_576
DEFAULT_WRITE_TIMEOUT_SECONDS = 2.0
DEFAULT_CLOSE_TIMEOUT_SECONDS = 5.0


class AsyncSession(Protocol):
    async def close(self) -> None: ...


class LocalAdmissionError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class LoopbackWebSocketRealtimeChannel:
    """Authenticated bounded channel with session-first teardown."""

    def __init__(
        self,
        socket: WebSocketConnection,
        *,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
        write_timeout_seconds: float = DEFAULT_WRITE_TIMEOUT_SECONDS,
        close_timeout_seconds: float = DEFAULT_CLOSE_TIMEOUT_SECONDS,
    ) -> None:
        if (
            max_frame_bytes <= 0
            or write_timeout_seconds <= 0
            or close_timeout_seconds <= 0
        ):
            raise ValueError("channel bounds must be positive")
        self._socket = socket
        self._max_frame_bytes = max_frame_bytes
        self._write_timeout_seconds = write_timeout_seconds
        self._close_timeout_seconds = close_timeout_seconds
        self._session: AsyncSession | None = None
        self._send_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._closed = False
        self._close_owner: asyncio.Task[object] | None = None
        self._close_done = asyncio.Event()

    @property
    def closed(self) -> bool:
        return self._closed

    def bind_session(self, session: AsyncSession) -> None:
        if self._closed:
            raise RuntimeError("channel is closed")
        if self._session is not None:
            raise RuntimeError("a session is already bound")
        self._session = session

    async def receive(self) -> str | bytes:
        if self._closed:
            raise RuntimeError("channel is closed")
        message = await self._socket.recv()
        if not isinstance(message, (str, bytes)):
            await self.close(code=1003)
            raise LocalAdmissionError("unsupported_frame")
        if _message_size(message) > self._max_frame_bytes:
            await self.close(code=1009)
            raise LocalAdmissionError("frame_too_large")
        return message

    async def send(self, message: str | bytes) -> None:
        if self._closed:
            raise RuntimeError("channel is closed")
        if _message_size(message) > self._max_frame_bytes:
            raise ValueError("frame exceeds configured bound")
        async with self._send_lock:
            try:
                await asyncio.wait_for(
                    self._socket.send(message), timeout=self._write_timeout_seconds
                )
            except TimeoutError:
                raise TimeoutError from None

    async def close(self, *, code: int = 1000) -> None:
        current = asyncio.current_task()
        owner = False
        async with self._close_lock:
            if self._closed:
                if self._close_owner is current:
                    return
                done = self._close_done
            else:
                self._closed = True
                self._close_owner = current
                session = self._session
                self._session = None
                owner = True
                done = self._close_done
        if not owner:
            await done.wait()
            return
        try:
            if session is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        session.close(), timeout=self._close_timeout_seconds
                    )
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    self._socket.close(code=code, reason=""),
                    timeout=self._close_timeout_seconds,
                )
        finally:
            self._close_done.set()


LoopbackHandler = Callable[[LoopbackWebSocketRealtimeChannel], Awaitable[None]]


class LoopbackWebSocketRealtimeHost:
    """Admit only the exact local route after loopback, Origin and secret checks."""

    def __init__(
        self,
        shared_secret: str,
        allowed_origins: Iterable[str],
        *,
        first_frame_timeout_seconds: float = 5.0,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
        max_connections: int = 4,
        write_timeout_seconds: float = DEFAULT_WRITE_TIMEOUT_SECONDS,
        close_timeout_seconds: float = DEFAULT_CLOSE_TIMEOUT_SECONDS,
    ) -> None:
        origins = frozenset(allowed_origins)
        if not shared_secret:
            raise ValueError("shared_secret must not be empty")
        if not origins:
            raise ValueError("at least one Origin must be allowed")
        if (
            first_frame_timeout_seconds <= 0
            or max_frame_bytes <= 0
            or max_connections <= 0
            or write_timeout_seconds <= 0
            or close_timeout_seconds <= 0
        ):
            raise ValueError("host bounds must be positive")
        self._shared_secret = shared_secret
        self._allowed_origins = origins
        self._first_frame_timeout_seconds = first_frame_timeout_seconds
        self._max_frame_bytes = max_frame_bytes
        self._max_connections = max_connections
        self._write_timeout_seconds = write_timeout_seconds
        self._close_timeout_seconds = close_timeout_seconds
        self._channels: set[LoopbackWebSocketRealtimeChannel] = set()
        self._pending: dict[asyncio.Task[object], WebSocketConnection] = {}
        self._handler_tasks: set[asyncio.Task[object]] = set()
        self._gate_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._closed = False
        self._close_owner: asyncio.Task[object] | None = None
        self._close_done = asyncio.Event()

    def __repr__(self) -> str:
        return (
            "LoopbackWebSocketRealtimeHost("
            f"path={LOCAL_REALTIME_PATH!r}, allowed_origins={len(self._allowed_origins)!r})"
        )

    @property
    def active_channel_count(self) -> int:
        return len(self._channels)

    async def accept(
        self,
        socket: WebSocketConnection,
        *,
        path: str,
        origin: str | None,
        peer_host: str,
    ) -> LoopbackWebSocketRealtimeChannel:
        task = asyncio.current_task()
        if task is None:
            await _reject(socket, 1011, self._close_timeout_seconds)
            raise LocalAdmissionError("host_closed")
        async with self._gate_lock:
            if self._closed:
                rejection = "host_closed"
            elif len(self._channels) + len(self._pending) >= self._max_connections:
                rejection = "capacity_busy"
            else:
                rejection = None
                self._pending[task] = socket
        if rejection is not None:
            await _reject(socket, 1001, self._close_timeout_seconds)
            raise LocalAdmissionError(rejection)
        try:
            try:
                if path != LOCAL_REALTIME_PATH:
                    raise LocalAdmissionError("wrong_path")
                if not _is_loopback(peer_host):
                    raise LocalAdmissionError("non_loopback_peer")
                if origin is None or origin not in self._allowed_origins:
                    raise LocalAdmissionError("wrong_origin")
                first = await asyncio.wait_for(
                    socket.recv(), timeout=self._first_frame_timeout_seconds
                )
                auth = _auth_message(first, self._max_frame_bytes)
            except TimeoutError:
                await _reject(socket, 1008, self._close_timeout_seconds)
                raise LocalAdmissionError("auth_timeout") from None
            except LocalAdmissionError as error:
                await _reject(
                    socket,
                    1009 if error.code == "frame_too_large" else 1008,
                    self._close_timeout_seconds,
                )
                raise
            if not secrets.compare_digest(auth, self._shared_secret):
                await _reject(socket, 1008, self._close_timeout_seconds)
                raise LocalAdmissionError("auth_failed")
            async with self._gate_lock:
                if self._closed:
                    rejection = "host_closed"
                elif len(self._channels) >= self._max_connections:
                    rejection = "capacity_busy"
                else:
                    rejection = None
                    channel = LoopbackWebSocketRealtimeChannel(
                        socket,
                        max_frame_bytes=self._max_frame_bytes,
                        write_timeout_seconds=self._write_timeout_seconds,
                        close_timeout_seconds=self._close_timeout_seconds,
                    )
                    self._channels.add(channel)
            if rejection is not None:
                await _reject(socket, 1001, self._close_timeout_seconds)
                raise LocalAdmissionError(rejection)
            return channel
        finally:
            async with self._gate_lock:
                self._pending.pop(task, None)

    async def run(
        self,
        socket: WebSocketConnection,
        *,
        path: str,
        origin: str | None,
        peer_host: str,
        handler: LoopbackHandler,
    ) -> None:
        channel = await self.accept(
            socket, path=path, origin=origin, peer_host=peer_host
        )
        task = asyncio.current_task()
        if task is None:
            await channel.close(code=1011)
            return
        async with self._gate_lock:
            if self._closed or channel.closed:
                runnable = False
            else:
                runnable = True
                self._handler_tasks.add(task)
        if not runnable:
            await channel.close(code=1001)
            raise LocalAdmissionError("host_closed")
        try:
            await handler(channel)
        finally:
            await channel.close()
            async with self._gate_lock:
                self._channels.discard(channel)
                self._handler_tasks.discard(task)

    async def close(self) -> None:
        current = asyncio.current_task()
        owner = False
        async with self._close_lock:
            if self._closed:
                if self._close_owner is current:
                    return
                done = self._close_done
            else:
                self._closed = True
                self._close_owner = current
                owner = True
                done = self._close_done
        if not owner:
            await done.wait()
            return
        try:
            async with self._gate_lock:
                channels = tuple(self._channels)
                pending = tuple(self._pending.items())
                handlers = tuple(self._handler_tasks)
                self._pending.clear()
            pending_tasks = tuple(
                task for task, _socket in pending if task is not current and not task.done()
            )
            for task in pending_tasks:
                task.cancel()
            if pending:
                await asyncio.gather(
                    *(
                        _reject(socket, 1001, self._close_timeout_seconds)
                        for _task, socket in pending
                    )
                )
            if channels:
                await asyncio.gather(*(channel.close() for channel in channels))
            active_handlers = tuple(
                task for task in handlers if task is not current and not task.done()
            )
            for task in active_handlers:
                task.cancel()
            waiters = pending_tasks + active_handlers
            if waiters:
                await asyncio.gather(*waiters, return_exceptions=True)
            async with self._gate_lock:
                self._channels.clear()
                self._handler_tasks.clear()
        finally:
            self._close_done.set()


def _auth_message(message: object, max_frame_bytes: int) -> str:
    if not isinstance(message, str):
        raise LocalAdmissionError("auth_must_be_text")
    if len(message.encode("utf-8")) > max_frame_bytes:
        raise LocalAdmissionError("frame_too_large")
    try:
        value = json.loads(message, object_pairs_hook=_unique_object)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise LocalAdmissionError("invalid_auth") from None
    if not isinstance(value, dict) or set(value) != {"type", "version", "secret"}:
        raise LocalAdmissionError("invalid_auth")
    if value["type"] != "local.auth" or value["version"] != LOCAL_REALTIME_VERSION:
        raise LocalAdmissionError("invalid_auth")
    secret = value["secret"]
    if not isinstance(secret, str):
        raise LocalAdmissionError("invalid_auth")
    return secret


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _message_size(message: str | bytes) -> int:
    return len(message) if isinstance(message, bytes) else len(message.encode("utf-8"))


async def _reject(
    socket: WebSocketConnection, code: int, timeout_seconds: float
) -> None:
    with contextlib.suppress(Exception):
        await asyncio.wait_for(
            socket.close(code=code, reason=""),
            timeout=timeout_seconds,
        )
