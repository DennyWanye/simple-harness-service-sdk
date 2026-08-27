from __future__ import annotations

import asyncio
import json
from collections import deque

import pytest

from simple_harness_service.realtime.transports.loopback_websocket import (
    LOCAL_REALTIME_PATH,
    LOCAL_REALTIME_VERSION,
    LocalAdmissionError,
    LoopbackWebSocketRealtimeChannel,
    LoopbackWebSocketRealtimeHost,
)


class FakeSession:
    def __init__(self, actions: list[str]) -> None:
        self.actions = actions

    async def close(self) -> None:
        self.actions.append("session")


class FakeSocket:
    def __init__(self, incoming: list[str | bytes], actions: list[str] | None = None) -> None:
        self.incoming = deque(incoming)
        self.actions = actions
        self.sent: list[str | bytes] = []
        self.closes: list[tuple[int, str]] = []

    async def recv(self) -> str | bytes:
        return self.incoming.popleft()

    async def send(self, message: str | bytes) -> None:
        self.sent.append(message)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.actions is not None:
            self.actions.append("channel")
        self.closes.append((code, reason))


def _auth(secret: str = "local-secret") -> str:
    return json.dumps(
        {"type": "local.auth", "version": LOCAL_REALTIME_VERSION, "secret": secret}
    )


@pytest.mark.asyncio
async def test_loopback_host_authenticates_and_closes_session_first() -> None:
    actions: list[str] = []
    socket = FakeSocket([_auth()], actions)
    host = LoopbackWebSocketRealtimeHost("local-secret", ["tauri://localhost"])

    channel = await host.accept(
        socket,
        path=LOCAL_REALTIME_PATH,
        origin="tauri://localhost",
        peer_host="127.0.0.1",
    )
    channel.bind_session(FakeSession(actions))
    await channel.close()
    await channel.close()

    assert actions == ["session", "channel"]
    assert "local-secret" not in repr(host)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "origin", "peer", "auth", "code"),
    [
        ("/ws/other", "tauri://localhost", "127.0.0.1", _auth(), "wrong_path"),
        (LOCAL_REALTIME_PATH, "https://evil.example", "127.0.0.1", _auth(), "wrong_origin"),
        (LOCAL_REALTIME_PATH, "tauri://localhost", "192.0.2.1", _auth(), "non_loopback_peer"),
        (LOCAL_REALTIME_PATH, "tauri://localhost", "::1", _auth("wrong"), "auth_failed"),
    ],
)
async def test_loopback_host_rejects_before_admission(
    path: str, origin: str, peer: str, auth: str, code: str
) -> None:
    socket = FakeSocket([auth])
    host = LoopbackWebSocketRealtimeHost("local-secret", ["tauri://localhost"])

    with pytest.raises(LocalAdmissionError, match=code):
        await host.accept(socket, path=path, origin=origin, peer_host=peer)

    assert socket.closes == [(1008, "")]
    assert host.active_channel_count == 0


@pytest.mark.asyncio
async def test_loopback_rejects_duplicate_auth_key_and_oversize_data() -> None:
    duplicate = (
        '{"type":"local.auth","version":"2026-08-27.1",'
        '"secret":"local-secret","secret":"second"}'
    )
    socket = FakeSocket([duplicate])
    host = LoopbackWebSocketRealtimeHost("local-secret", ["tauri://localhost"])
    with pytest.raises(LocalAdmissionError, match="invalid_auth"):
        await host.accept(
            socket,
            path=LOCAL_REALTIME_PATH,
            origin="tauri://localhost",
            peer_host="127.0.0.1",
        )

    socket = FakeSocket([b"12345"])
    channel = LoopbackWebSocketRealtimeChannel(socket, max_frame_bytes=4)
    with pytest.raises(LocalAdmissionError, match="frame_too_large"):
        await channel.receive()
    assert socket.closes[-1] == (1009, "")


@pytest.mark.asyncio
async def test_loopback_rejects_oversize_auth_with_1009() -> None:
    socket = FakeSocket(["x" * 129])
    host = LoopbackWebSocketRealtimeHost(
        "local-secret", ["tauri://localhost"], max_frame_bytes=128
    )

    with pytest.raises(LocalAdmissionError, match="frame_too_large"):
        await host.accept(
            socket,
            path=LOCAL_REALTIME_PATH,
            origin="tauri://localhost",
            peer_host="127.0.0.1",
        )
    assert socket.closes == [(1009, "")]


@pytest.mark.asyncio
async def test_loopback_channel_close_is_concurrent_recursive_and_bounded() -> None:
    actions: list[str] = []
    socket = FakeSocket([], actions)
    channel = LoopbackWebSocketRealtimeChannel(
        socket, close_timeout_seconds=0.01
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
    assert socket.closes == [(1000, "")]


@pytest.mark.asyncio
async def test_loopback_channel_send_timeout_is_bounded_and_cancels_socket_write() -> None:
    send_started = asyncio.Event()
    send_cancelled = asyncio.Event()

    class BlockingSendSocket(FakeSocket):
        async def send(self, message: str | bytes) -> None:
            send_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                send_cancelled.set()

    channel = LoopbackWebSocketRealtimeChannel(
        BlockingSendSocket([]), write_timeout_seconds=0.01
    )
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(channel.send("server-event"), timeout=0.1)
    assert send_started.is_set()
    assert send_cancelled.is_set()


@pytest.mark.asyncio
async def test_loopback_channel_send_preserves_caller_cancellation() -> None:
    send_started = asyncio.Event()

    class BlockingSendSocket(FakeSocket):
        async def send(self, message: str | bytes) -> None:
            send_started.set()
            await asyncio.Event().wait()

    channel = LoopbackWebSocketRealtimeChannel(BlockingSendSocket([]))
    task = asyncio.create_task(channel.send("server-event"))
    await asyncio.wait_for(send_started.wait(), timeout=0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_loopback_shutdown_cancels_pending_auth_and_rejects_new_admission() -> None:
    receive_started = asyncio.Event()

    class PendingSocket(FakeSocket):
        async def recv(self) -> str | bytes:
            receive_started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    socket = PendingSocket([])
    host = LoopbackWebSocketRealtimeHost("local-secret", ["tauri://localhost"])
    pending = asyncio.create_task(
        host.accept(
            socket,
            path=LOCAL_REALTIME_PATH,
            origin="tauri://localhost",
            peer_host="127.0.0.1",
        )
    )
    await asyncio.wait_for(receive_started.wait(), timeout=0.1)
    await host.close()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert socket.closes == [(1001, "")]

    rejected = FakeSocket([_auth()])
    with pytest.raises(LocalAdmissionError, match="host_closed"):
        await host.accept(
            rejected,
            path=LOCAL_REALTIME_PATH,
            origin="tauri://localhost",
            peer_host="127.0.0.1",
        )
    assert rejected.closes == [(1001, "")]


@pytest.mark.asyncio
async def test_loopback_pending_auth_counts_toward_capacity() -> None:
    started = [asyncio.Event(), asyncio.Event()]

    class SilentSocket(FakeSocket):
        def __init__(self, observed: asyncio.Event) -> None:
            super().__init__([])
            self.observed = observed

        async def recv(self) -> str | bytes:
            self.observed.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    host = LoopbackWebSocketRealtimeHost(
        "local-secret",
        ["tauri://localhost"],
        max_connections=2,
        first_frame_timeout_seconds=10,
    )
    pending = [
        asyncio.create_task(
            host.accept(
                SilentSocket(observed),
                path=LOCAL_REALTIME_PATH,
                origin="tauri://localhost",
                peer_host="127.0.0.1",
            )
        )
        for observed in started
    ]
    await asyncio.gather(
        *(asyncio.wait_for(observed.wait(), timeout=0.1) for observed in started)
    )

    overflow = FakeSocket([_auth()])
    with pytest.raises(LocalAdmissionError, match="capacity_busy"):
        await asyncio.wait_for(
            host.accept(
                overflow,
                path=LOCAL_REALTIME_PATH,
                origin="tauri://localhost",
                peer_host="127.0.0.1",
            ),
            timeout=0.1,
        )
    assert overflow.closes == [(1001, "")]

    await host.close()
    results = await asyncio.gather(*pending, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)


@pytest.mark.asyncio
async def test_loopback_shutdown_rejects_many_pending_sockets_concurrently() -> None:
    receive_started = [asyncio.Event() for _ in range(4)]
    close_started = 0
    close_cancelled = 0

    class BlockingSocket(FakeSocket):
        def __init__(self, observed: asyncio.Event) -> None:
            super().__init__([])
            self.observed = observed

        async def recv(self) -> str | bytes:
            self.observed.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def close(self, code: int = 1000, reason: str = "") -> None:
            nonlocal close_started, close_cancelled
            close_started += 1
            try:
                await asyncio.Event().wait()
            finally:
                close_cancelled += 1

    host = LoopbackWebSocketRealtimeHost(
        "local-secret",
        ["tauri://localhost"],
        max_connections=4,
        first_frame_timeout_seconds=10,
        close_timeout_seconds=0.01,
    )
    pending = [
        asyncio.create_task(
            host.accept(
                BlockingSocket(observed),
                path=LOCAL_REALTIME_PATH,
                origin="tauri://localhost",
                peer_host="127.0.0.1",
            )
        )
        for observed in receive_started
    ]
    await asyncio.gather(
        *(asyncio.wait_for(observed.wait(), timeout=0.1) for observed in receive_started)
    )

    loop = asyncio.get_running_loop()
    started_at = loop.time()
    await asyncio.wait_for(host.close(), timeout=0.1)
    duration = loop.time() - started_at

    assert close_started == 4
    assert close_cancelled == 4
    assert duration < 0.08
    results = await asyncio.gather(*pending, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
