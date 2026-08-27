"""Long-lived owner-authenticated AF_UNIX Realtime transport."""

from __future__ import annotations

import asyncio
import contextlib
import os
import stat
import struct
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

from ...transports.unix import _peer_uid, _validate_parent

DEFAULT_MAX_FRAME_BYTES = 1_048_576
DEFAULT_WRITE_TIMEOUT_SECONDS = 2.0
DEFAULT_CLOSE_TIMEOUT_SECONDS = 5.0


class AsyncSession(Protocol):
    async def close(self) -> None: ...


class UnixRealtimeChannel:
    """A bounded framed channel whose attached session always closes first."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        peer_uid: int,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
        write_timeout_seconds: float = DEFAULT_WRITE_TIMEOUT_SECONDS,
        close_timeout_seconds: float = DEFAULT_CLOSE_TIMEOUT_SECONDS,
    ) -> None:
        if max_frame_bytes <= 0 or write_timeout_seconds <= 0 or close_timeout_seconds <= 0:
            raise ValueError("channel bounds must be positive")
        self._reader = reader
        self._writer = writer
        self.peer_uid = peer_uid
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

    async def receive_frame(self) -> bytes:
        if self._closed:
            raise RuntimeError("channel is closed")
        header = await self._reader.readexactly(4)
        length = struct.unpack("!I", header)[0]
        if length > self._max_frame_bytes:
            await self.close()
            raise ValueError("frame exceeds configured bound")
        return await self._reader.readexactly(length)

    async def send_frame(self, payload: bytes) -> None:
        if self._closed:
            raise RuntimeError("channel is closed")
        if len(payload) > self._max_frame_bytes:
            raise ValueError("frame exceeds configured bound")
        async with self._send_lock:
            self._writer.write(struct.pack("!I", len(payload)) + payload)
            await asyncio.wait_for(
                self._writer.drain(), timeout=self._write_timeout_seconds
            )

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
                self._writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await asyncio.wait_for(
                    self._writer.wait_closed(), timeout=self._close_timeout_seconds
                )
        finally:
            self._close_done.set()

    async def receive(self) -> str | bytes:
        payload = await self.receive_frame()
        if payload.startswith(b"SHRT"):
            return payload
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("local text frame is not UTF-8") from error

    async def send(self, payload: str | bytes) -> None:
        encoded = payload.encode("utf-8") if isinstance(payload, str) else payload
        await self.send_frame(encoded)


ChannelHandler = Callable[[UnixRealtimeChannel], Awaitable[None]]


class UnixRealtimeHost:
    """Owner-only AF_UNIX host with bounded active channels."""

    def __init__(
        self,
        path: Path,
        handler: ChannelHandler,
        *,
        owner_uid: int | None = None,
        peer_uid_resolver: Callable[[object], int] = _peer_uid,
        max_connections: int = 4,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
        close_timeout_seconds: float = DEFAULT_CLOSE_TIMEOUT_SECONDS,
    ) -> None:
        if max_connections <= 0 or max_frame_bytes <= 0 or close_timeout_seconds <= 0:
            raise ValueError("host bounds must be positive")
        self.path = path
        self._handler = handler
        self._owner_uid = os.getuid() if owner_uid is None else owner_uid
        self._peer_uid_resolver = peer_uid_resolver
        self._max_connections = max_connections
        self._max_frame_bytes = max_frame_bytes
        self._close_timeout_seconds = close_timeout_seconds
        self._server: asyncio.AbstractServer | None = None
        self._channels: set[UnixRealtimeChannel] = set()
        self._handler_tasks: set[asyncio.Task[object]] = set()
        self._gate_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._accepting = False
        self._shutdown = False
        self._close_owner: asyncio.Task[object] | None = None
        self._close_done = asyncio.Event()

    @property
    def active_channel_count(self) -> int:
        return len(self._channels)

    async def start(self) -> None:
        if self._server is not None:
            return
        if self._shutdown:
            raise RuntimeError("host is closed")
        _validate_parent(self.path, self._owner_uid)
        if len(os.fsencode(self.path)) > 103:
            raise ValueError("AF_UNIX socket path is too long")
        if self.path.exists() or self.path.is_symlink():
            raise FileExistsError("refusing to replace an existing socket path")
        self._server = await asyncio.start_unix_server(self._handle, path=self.path)
        os.chmod(self.path, 0o600)
        info = self.path.lstat()
        if info.st_uid != self._owner_uid or stat.S_IMODE(info.st_mode) != 0o600:
            await self.close()
            raise PermissionError("created socket is not owner-only")
        async with self._gate_lock:
            stopped = self._shutdown
            if not stopped:
                self._accepting = True
        if stopped:
            await self.close()
            raise RuntimeError("host is closed")

    async def close(self) -> None:
        current = asyncio.current_task()
        owner = False
        async with self._close_lock:
            if self._shutdown:
                if self._close_owner is current:
                    return
                done = self._close_done
            else:
                self._shutdown = True
                self._close_owner = current
                server = self._server
                self._server = None
                owner = True
                done = self._close_done
        if not owner:
            await done.wait()
            return
        try:
            async with self._gate_lock:
                self._accepting = False
                channels = tuple(self._channels)
                handlers = tuple(self._handler_tasks)
            if server is not None:
                server.close()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        server.wait_closed(), timeout=self._close_timeout_seconds
                    )
            await asyncio.sleep(0)
            async with self._gate_lock:
                handlers = tuple(self._handler_tasks)
                channels = tuple(self._channels)
            if channels:
                await asyncio.gather(*(channel.close() for channel in channels))
            pending = tuple(task for task in handlers if task is not current and not task.done())
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            try:
                if stat.S_ISSOCK(self.path.lstat().st_mode):
                    self.path.unlink()
            except FileNotFoundError:
                pass
        finally:
            self._close_done.set()

    async def __aenter__(self) -> UnixRealtimeHost:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        channel: UnixRealtimeChannel | None = None
        task = asyncio.current_task()
        if task is None:
            writer.close()
            return
        try:
            async with self._gate_lock:
                self._handler_tasks.add(task)
                if not self._accepting:
                    return
            transport_socket = writer.get_extra_info("socket")
            peer_uid = self._peer_uid_resolver(transport_socket)
            async with self._gate_lock:
                if (
                    not self._accepting
                    or peer_uid != self._owner_uid
                    or len(self._channels) >= self._max_connections
                ):
                    return
                channel = UnixRealtimeChannel(
                    reader,
                    writer,
                    peer_uid=peer_uid,
                    max_frame_bytes=self._max_frame_bytes,
                    close_timeout_seconds=self._close_timeout_seconds,
                )
                self._channels.add(channel)
            await self._handler(channel)
        except (ConnectionError, asyncio.IncompleteReadError, ValueError):
            pass
        except Exception:
            # Product handlers must not leak exception text through the server task.
            pass
        finally:
            if channel is None:
                with contextlib.suppress(Exception):
                    writer.close()
                with contextlib.suppress(ConnectionError, OSError, TimeoutError):
                    await asyncio.wait_for(
                        writer.wait_closed(), timeout=self._close_timeout_seconds
                    )
            else:
                await channel.close()
            async with self._gate_lock:
                if channel is not None:
                    self._channels.discard(channel)
                self._handler_tasks.discard(task)
