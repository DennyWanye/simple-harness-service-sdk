from __future__ import annotations

import asyncio
import os
import struct
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

from simple_harness_service.realtime.transports.unix_local import (
    UnixRealtimeChannel,
    UnixRealtimeHost,
)


class FakeSession:
    def __init__(self, actions: list[str]) -> None:
        self.actions = actions

    async def close(self) -> None:
        self.actions.append("session")


class FakeWriter:
    def __init__(self, actions: list[str]) -> None:
        self.actions = actions
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.actions.append("channel")

    async def wait_closed(self) -> None:
        return None


@pytest.fixture
def short_socket_dir() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="shrt-", dir="/tmp") as value:
        path = Path(value)
        os.chmod(path, 0o700)
        yield path


@pytest.mark.asyncio
async def test_unix_channel_closes_session_before_channel_once() -> None:
    actions: list[str] = []
    writer = FakeWriter(actions)
    channel = UnixRealtimeChannel(
        asyncio.StreamReader(), cast(asyncio.StreamWriter, writer), peer_uid=os.getuid()
    )
    channel.bind_session(FakeSession(actions))

    await channel.close()
    await channel.close()

    assert actions == ["session", "channel"]


@pytest.mark.asyncio
async def test_unix_channel_enforces_frame_bound() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(struct.pack("!I", 9))
    reader.feed_eof()
    writer = FakeWriter([])
    channel = UnixRealtimeChannel(
        reader, cast(asyncio.StreamWriter, writer), peer_uid=os.getuid(), max_frame_bytes=8
    )

    with pytest.raises(ValueError, match="configured bound"):
        await channel.receive_frame()
    assert channel.closed


@pytest.mark.asyncio
async def test_unix_channel_write_timeout_is_bounded_and_cancels_drain() -> None:
    drain_started = asyncio.Event()
    drain_cancelled = asyncio.Event()

    class BlockingDrainWriter(FakeWriter):
        async def drain(self) -> None:
            drain_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                drain_cancelled.set()

    writer = BlockingDrainWriter([])
    channel = UnixRealtimeChannel(
        asyncio.StreamReader(),
        cast(asyncio.StreamWriter, writer),
        peer_uid=os.getuid(),
        write_timeout_seconds=0.01,
    )
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(channel.send("server-event"), timeout=0.1)
    assert drain_started.is_set()
    assert drain_cancelled.is_set()


@pytest.mark.asyncio
async def test_unix_channel_decodes_text_and_rejects_invalid_utf8() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(struct.pack("!I", 2) + b"\xff\xfe")
    reader.feed_eof()
    channel = UnixRealtimeChannel(
        reader,
        cast(asyncio.StreamWriter, FakeWriter([])),
        peer_uid=os.getuid(),
    )

    with pytest.raises(ValueError, match="UTF-8"):
        await channel.receive()


@pytest.mark.asyncio
async def test_unix_host_authenticates_peer_and_frames_round_trip(
    short_socket_dir: Path,
) -> None:
    socket_path = short_socket_dir / "realtime.sock"
    handled = asyncio.Event()

    async def handler(channel: UnixRealtimeChannel) -> None:
        payload = await channel.receive_frame()
        await channel.send_frame(payload.upper())
        handled.set()

    host = UnixRealtimeHost(socket_path, handler)
    await host.start()
    assert socket_path.stat().st_mode & 0o777 == 0o600
    reader, writer = await asyncio.open_unix_connection(socket_path)
    writer.write(struct.pack("!I", 3) + b"pcm")
    await writer.drain()
    length = struct.unpack("!I", await reader.readexactly(4))[0]
    assert await reader.readexactly(length) == b"PCM"
    await asyncio.wait_for(handled.wait(), timeout=1)
    writer.close()
    await writer.wait_closed()
    await host.close()
    await host.close()
    assert not socket_path.exists()


@pytest.mark.asyncio
async def test_unix_host_rejects_wrong_peer_before_handler(
    short_socket_dir: Path,
) -> None:
    socket_path = short_socket_dir / "realtime.sock"
    called = False

    async def handler(channel: UnixRealtimeChannel) -> None:
        nonlocal called
        called = True

    host = UnixRealtimeHost(
        socket_path,
        handler,
        peer_uid_resolver=lambda socket: os.getuid() + 1,
    )
    await host.start()
    reader, writer = await asyncio.open_unix_connection(socket_path)
    assert await asyncio.wait_for(reader.read(), timeout=1) == b""
    writer.close()
    await writer.wait_closed()
    await host.close()
    assert not called


@pytest.mark.asyncio
async def test_unix_channel_close_is_concurrent_recursive_and_bounded() -> None:
    actions: list[str] = []
    writer = FakeWriter(actions)
    channel = UnixRealtimeChannel(
        asyncio.StreamReader(),
        cast(asyncio.StreamWriter, writer),
        peer_uid=os.getuid(),
        close_timeout_seconds=0.01,
    )
    release = asyncio.Event()

    class RecursiveBlockingSession:
        async def close(self) -> None:
            actions.append("session")
            await channel.close()
            await release.wait()

    channel.bind_session(RecursiveBlockingSession())
    first = asyncio.create_task(channel.close())
    second = asyncio.create_task(channel.close())
    await asyncio.wait_for(asyncio.gather(first, second), timeout=0.1)

    assert actions == ["session", "channel"]


@pytest.mark.asyncio
async def test_unix_shutdown_closes_active_handler_and_rejects_restart(
    short_socket_dir: Path,
) -> None:
    socket_path = short_socket_dir / "realtime.sock"
    handler_started = asyncio.Event()

    async def handler(channel: UnixRealtimeChannel) -> None:
        handler_started.set()
        await asyncio.Event().wait()

    host = UnixRealtimeHost(socket_path, handler, close_timeout_seconds=0.05)
    await host.start()
    reader, writer = await asyncio.open_unix_connection(socket_path)
    await asyncio.wait_for(handler_started.wait(), timeout=0.1)
    await asyncio.wait_for(host.close(), timeout=0.2)

    assert host.active_channel_count == 0
    assert await asyncio.wait_for(reader.read(), timeout=0.1) == b""
    writer.close()
    await writer.wait_closed()
    with pytest.raises(RuntimeError, match="closed"):
        await host.start()
