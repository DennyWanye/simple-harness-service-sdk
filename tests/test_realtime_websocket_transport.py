from __future__ import annotations

import asyncio
from collections import deque

import pytest

from simple_harness_service.realtime.ports import RealtimeTransport
from simple_harness_service.realtime.transports.websocket import (
    RelayTransportError,
    RelayWebSocketTransport,
)


class FakeSocket:
    def __init__(self, incoming: list[str | bytes] | None = None) -> None:
        self.incoming = deque(incoming or [])
        self.sent: list[str | bytes] = []
        self.closes: list[tuple[int, str]] = []

    async def send(self, message: str | bytes) -> None:
        self.sent.append(message)

    async def recv(self) -> str | bytes:
        return self.incoming.popleft()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closes.append((code, reason))


@pytest.mark.asyncio
async def test_relay_transport_binds_returned_path_and_redacts_bearer() -> None:
    socket = FakeSocket(["server-event"])
    calls: list[tuple[str, dict[str, object]]] = []

    async def connect(uri: str, **kwargs: object) -> FakeSocket:
        calls.append((uri, kwargs))
        return socket

    transport = RelayWebSocketTransport(
        "https://relay.example", connect_factory=connect, max_frame_bytes=128
    )
    connection = await transport.connect("/v1/realtime/qwen", "eph_secret")
    await connection.send_text("client-event")

    assert await connection.receive_text() == "server-event"
    assert socket.sent == ["client-event"]
    uri, kwargs = calls[0]
    assert uri == "wss://relay.example/v1/realtime/qwen"
    assert kwargs["additional_headers"] == {"Authorization": "Bearer eph_secret"}
    assert kwargs["max_size"] == 128
    assert "eph_secret" not in repr(transport)

    await connection.close()
    await connection.close()
    assert socket.closes == [(1000, "")]


@pytest.mark.asyncio
async def test_relay_connection_rejects_binary_and_oversize_frames() -> None:
    binary_socket = FakeSocket([b"provider-binary"])

    async def binary_connect(uri: str, **kwargs: object) -> FakeSocket:
        return binary_socket

    transport = RelayWebSocketTransport(
        "https://relay.example", connect_factory=binary_connect, max_frame_bytes=8
    )
    connection = await transport.connect("/v1/realtime/qwen", "eph_secret")
    with pytest.raises(RelayTransportError, match="protocol_error"):
        await connection.receive_text()
    assert binary_socket.closes == [(1003, "")]

    text_socket = FakeSocket()

    async def text_connect(uri: str, **kwargs: object) -> FakeSocket:
        return text_socket

    connection = await RelayWebSocketTransport(
        "https://relay.example", connect_factory=text_connect, max_frame_bytes=4
    ).connect("/v1/realtime/qwen", "eph_secret")
    with pytest.raises(RelayTransportError, match="frame_too_large"):
        await connection.send_text("12345")
    assert text_socket.sent == []


@pytest.mark.asyncio
async def test_relay_transport_rejects_absolute_or_unbound_paths() -> None:
    called = False

    async def connect(uri: str, **kwargs: object) -> FakeSocket:
        nonlocal called
        called = True
        return FakeSocket()

    transport = RelayWebSocketTransport("https://relay.example", connect_factory=connect)
    with pytest.raises(RelayTransportError, match="protocol_error"):
        await transport.connect("wss://attacker.example/v1/realtime/qwen", "eph_secret")
    for path in (
        "/v1/realtime/../qwen",
        "/v1/realtime/%2e%2e/qwen",
        "/v1/realtime\\qwen",
        "/v1/realtime/qwen?token=secret",
    ):
        with pytest.raises(RelayTransportError, match="protocol_error"):
            await transport.connect(path, "eph_secret")
    assert not called


def test_relay_transport_canonicalizes_origin_before_repr() -> None:
    transport = RelayWebSocketTransport("HTTPS://Relay.Example:443/")
    assert "https://relay.example" in repr(transport)
    with pytest.raises(ValueError, match="HTTPS origin"):
        RelayWebSocketTransport("https://user:secret@relay.example/?token=secret")
    with pytest.raises(ValueError, match="HTTPS origin"):
        RelayWebSocketTransport("https:\\relay.example")


def test_relay_transport_matches_the_public_port_shape() -> None:
    transport: RealtimeTransport = RelayWebSocketTransport("https://relay.example")
    assert transport is not None


@pytest.mark.asyncio
async def test_relay_connection_close_is_bounded_and_idempotent() -> None:
    close_started = asyncio.Event()

    class BlockingCloseSocket(FakeSocket):
        async def close(self, code: int = 1000, reason: str = "") -> None:
            close_started.set()
            await asyncio.Event().wait()

    socket = BlockingCloseSocket()

    async def connect(uri: str, **kwargs: object) -> FakeSocket:
        return socket

    connection = await RelayWebSocketTransport(
        "https://relay.example",
        connect_factory=connect,
        close_timeout_seconds=0.01,
    ).connect("/v1/realtime/qwen", "eph_secret")

    await asyncio.wait_for(connection.close(), timeout=0.1)
    assert close_started.is_set()
    await connection.close()


@pytest.mark.asyncio
async def test_relay_send_timeout_is_bounded_redacted_and_cancels_socket_write() -> None:
    send_started = asyncio.Event()
    send_cancelled = asyncio.Event()

    class BlockingSendSocket(FakeSocket):
        async def send(self, message: str | bytes) -> None:
            send_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                send_cancelled.set()

    socket = BlockingSendSocket()

    async def connect(uri: str, **kwargs: object) -> FakeSocket:
        return socket

    connection = await RelayWebSocketTransport(
        "https://relay.example",
        connect_factory=connect,
        write_timeout_seconds=0.01,
    ).connect("/v1/realtime/qwen", "eph_secret")

    with pytest.raises(RelayTransportError, match=r"^timeout$") as caught:
        await asyncio.wait_for(connection.send_text("secret-payload"), timeout=0.1)
    assert caught.value.code.value == "timeout"
    assert caught.value.retryable
    assert send_started.is_set()
    assert send_cancelled.is_set()
    assert "secret-payload" not in str(caught.value)


@pytest.mark.asyncio
async def test_relay_send_preserves_caller_cancellation() -> None:
    send_started = asyncio.Event()

    class BlockingSendSocket(FakeSocket):
        async def send(self, message: str | bytes) -> None:
            send_started.set()
            await asyncio.Event().wait()

    socket = BlockingSendSocket()

    async def connect(uri: str, **kwargs: object) -> FakeSocket:
        return socket

    connection = await RelayWebSocketTransport(
        "https://relay.example", connect_factory=connect
    ).connect("/v1/realtime/qwen", "eph_secret")
    task = asyncio.create_task(connection.send_text("client-event"))
    await asyncio.wait_for(send_started.wait(), timeout=0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "expected", "retryable"),
    [
        (1000, None, None),
        (1001, None, None),
        (1006, "unavailable", True),
        (1008, "forbidden", False),
    ],
)
async def test_relay_receive_close_matrix(
    code: int, expected: str | None, retryable: bool | None
) -> None:
    class Received:
        def __init__(self, value: int) -> None:
            self.code = value

    class Closed(Exception):
        def __init__(self, value: int) -> None:
            self.rcvd = Received(value)

    class ClosedSocket(FakeSocket):
        async def recv(self) -> str | bytes:
            raise Closed(code)

    async def connect(uri: str, **kwargs: object) -> FakeSocket:
        return ClosedSocket()

    connection = await RelayWebSocketTransport(
        "https://relay.example", connect_factory=connect
    ).connect("/v1/realtime/qwen", "eph_secret")
    if expected is None:
        assert await connection.receive_text() is None
    else:
        with pytest.raises(RelayTransportError, match=expected) as caught:
            await connection.receive_text()
        assert caught.value.retryable is retryable
        assert caught.value.close_code == code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected", "retryable"),
    [
        (401, "unauthenticated", False),
        (403, "forbidden", False),
        (429, "rate_limited", True),
        (503, "unavailable", True),
        (418, "protocol_error", False),
    ],
)
async def test_relay_connect_status_is_stable_and_redacted(
    status: int, expected: str, retryable: bool
) -> None:
    class Response:
        status_code = status

    class InvalidStatus(Exception):
        response = Response()

    async def connect(uri: str, **kwargs: object) -> FakeSocket:
        raise InvalidStatus("secret response body and headers")

    transport = RelayWebSocketTransport(
        "https://relay.example", connect_factory=connect
    )
    with pytest.raises(RelayTransportError, match=expected) as caught:
        await transport.connect("/v1/realtime/qwen", "eph_secret")
    assert caught.value.retryable is retryable
    assert "secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_relay_connect_cancellation_is_not_reclassified() -> None:
    async def connect(uri: str, **kwargs: object) -> FakeSocket:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await RelayWebSocketTransport(
            "https://relay.example", connect_factory=connect
        ).connect("/v1/realtime/qwen", "eph_secret")
