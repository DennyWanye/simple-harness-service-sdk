"""Bounded TokenSeller relay WebSocket transport."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
from collections.abc import Awaitable, Callable
from typing import Protocol, cast
from urllib.parse import urlsplit

from ..contracts import RealtimeError, RealtimeErrorCode

TOKENSELLER_REALTIME_PATH = "/v1/realtime/qwen"


class RelayTransportError(RealtimeError):
    """Stable relay transport failure without secret-bearing exception text."""

    def __init__(self, code: str) -> None:
        self.transport_code = code
        mapping = {
            "unauthenticated": RealtimeErrorCode.UNAUTHENTICATED,
            "forbidden": RealtimeErrorCode.FORBIDDEN,
            "rate_limited": RealtimeErrorCode.RATE_LIMITED,
            "timeout": RealtimeErrorCode.TIMEOUT,
            "frame_too_large": RealtimeErrorCode.INVALID_REQUEST,
            "protocol_error": RealtimeErrorCode.PROTOCOL_ERROR,
            "transport_closed": RealtimeErrorCode.CANCELLED,
        }
        super().__init__(
            mapping.get(code, RealtimeErrorCode.UNAVAILABLE),
            code,
            retryable=code in {"rate_limited", "timeout", "unavailable"},
        )
        self.close_code: int | None = None


class WebSocketConnection(Protocol):
    async def send(self, message: str | bytes) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self, code: int = 1000, reason: str = "") -> None: ...


ConnectFactory = Callable[..., Awaitable[WebSocketConnection]]


class RelayWebSocketConnection:
    """Bounded text connection returned only after a path-bound connect."""

    def __init__(
        self,
        socket: WebSocketConnection,
        max_frame_bytes: int,
        write_timeout_seconds: float,
        close_timeout_seconds: float,
    ) -> None:
        self._socket = socket
        self._max_frame_bytes = max_frame_bytes
        self._write_timeout_seconds = write_timeout_seconds
        self._close_timeout_seconds = close_timeout_seconds
        self._close_lock = asyncio.Lock()
        self._closed = False

    async def send_text(self, payload: str) -> None:
        if self._closed:
            raise RelayTransportError("transport_closed")
        if _message_bytes(payload) > self._max_frame_bytes:
            raise RelayTransportError("frame_too_large")
        try:
            await asyncio.wait_for(
                self._socket.send(payload), timeout=self._write_timeout_seconds
            )
        except TimeoutError:
            raise RelayTransportError("timeout") from None
        except Exception:
            raise RelayTransportError("unavailable") from None

    async def receive_text(self) -> str | None:
        if self._closed:
            return None
        try:
            message = await self._socket.recv()
        except TimeoutError:
            raise RelayTransportError("timeout") from None
        except Exception as error:
            classification = _closed_classification(error)
            if classification is None:
                return None
            raise classification from None
        if isinstance(message, bytes):
            await self.close(code=1003)
            raise RelayTransportError("protocol_error")
        if not isinstance(message, str):
            raise RelayTransportError("protocol_error")
        if _message_bytes(message) > self._max_frame_bytes:
            await self.close(code=1009)
            raise RelayTransportError("frame_too_large")
        return message

    async def close(self, code: int = 1000, reason: str = "") -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    self._socket.close(code=code, reason=reason[:123]),
                    timeout=self._close_timeout_seconds,
                )


class RelayWebSocketTransport:
    """Connect only to the relay path returned by a validated mint response."""

    def __init__(
        self,
        base_url: str,
        *,
        connect_factory: ConnectFactory | None = None,
        max_frame_bytes: int = 1_048_576,
        max_queue_frames: int = 32,
        open_timeout_seconds: float = 10.0,
        write_timeout_seconds: float = 2.0,
        close_timeout_seconds: float = 5.0,
        ping_interval_seconds: float = 20.0,
        ping_timeout_seconds: float = 20.0,
    ) -> None:
        if max_frame_bytes <= 0 or max_queue_frames <= 0:
            raise ValueError("frame and queue bounds must be positive")
        if any(
            timeout <= 0
            for timeout in (
                open_timeout_seconds,
                write_timeout_seconds,
                close_timeout_seconds,
                ping_interval_seconds,
                ping_timeout_seconds,
            )
        ):
            raise ValueError("WebSocket timeouts must be positive")
        self._origin = _https_origin(base_url)
        self._connect_factory = connect_factory
        self._max_frame_bytes = max_frame_bytes
        self._max_queue_frames = max_queue_frames
        self._open_timeout_seconds = open_timeout_seconds
        self._write_timeout_seconds = write_timeout_seconds
        self._close_timeout_seconds = close_timeout_seconds
        self._ping_interval_seconds = ping_interval_seconds
        self._ping_timeout_seconds = ping_timeout_seconds

    def __repr__(self) -> str:
        return (
            "RelayWebSocketTransport("
            f"origin={self._origin!r}, max_frame_bytes={self._max_frame_bytes!r}, "
            f"max_queue_frames={self._max_queue_frames!r})"
        )

    async def connect(
        self, websocket_path: str, bearer_token: str
    ) -> RelayWebSocketConnection:
        if not bearer_token:
            raise RelayTransportError("unauthenticated")
        try:
            uri = _wss_uri(self._origin, websocket_path)
        except ValueError:
            raise RelayTransportError("protocol_error") from None
        factory = self._connect_factory or _websockets_connect
        try:
            connection = await factory(
                uri,
                additional_headers={"Authorization": f"Bearer {bearer_token}"},
                open_timeout=self._open_timeout_seconds,
                close_timeout=self._close_timeout_seconds,
                ping_interval=self._ping_interval_seconds,
                ping_timeout=self._ping_timeout_seconds,
                max_size=self._max_frame_bytes,
                max_queue=self._max_queue_frames,
            )
        except TimeoutError:
            raise RelayTransportError("timeout") from None
        except Exception as error:
            raise _connect_error(error) from None
        return RelayWebSocketConnection(
            connection,
            self._max_frame_bytes,
            self._write_timeout_seconds,
            self._close_timeout_seconds,
        )


async def _websockets_connect(uri: str, **kwargs: object) -> WebSocketConnection:
    try:
        module = importlib.import_module("websockets.asyncio.client")
        connect = cast(Callable[..., Awaitable[object]], module.connect)
        return cast(WebSocketConnection, await connect(uri, **kwargs))
    except (ImportError, AttributeError):
        raise RelayTransportError("realtime_dependency_unavailable") from None


def _wss_uri(base_url: str, relay_path: str) -> str:
    parsed = urlsplit(_https_origin(base_url))
    path = _relative_relay_path(relay_path)
    return f"wss://{parsed.netloc}{path}"


def _relative_relay_path(path: str) -> str:
    if path != TOKENSELLER_REALTIME_PATH:
        raise ValueError("websocket_path must be a relative absolute path")
    return path


def _https_origin(base_url: str) -> str:
    if not isinstance(base_url, str) or "\\" in base_url or any(
        ord(character) < 0x20 for character in base_url
    ):
        raise ValueError("base_url must be an HTTPS origin")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ValueError("base_url must be an HTTPS origin")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("base_url must be an HTTPS origin") from error
    host = parsed.hostname.lower()
    if "%" in host:
        raise ValueError("base_url must be an HTTPS origin")
    rendered_host = f"[{host}]" if ":" in host else host
    rendered_port = "" if port in (None, 443) else f":{port}"
    return f"https://{rendered_host}{rendered_port}"


def _connect_error(error: Exception) -> RelayTransportError:
    status = _status_code(error)
    if status == 401:
        return RelayTransportError("unauthenticated")
    if status == 403:
        return RelayTransportError("forbidden")
    if status == 429:
        return RelayTransportError("rate_limited")
    if status is not None and 500 <= status <= 599:
        return RelayTransportError("unavailable")
    if status is not None:
        return RelayTransportError("protocol_error")
    return RelayTransportError("unavailable")


def _status_code(error: Exception) -> int | None:
    response = getattr(error, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def _closed_classification(error: Exception) -> RelayTransportError | None:
    received = getattr(error, "rcvd", None)
    code = getattr(received, "code", None)
    if not isinstance(code, int):
        value = getattr(error, "code", None)
        code = value if isinstance(value, int) else None
    if code in {1000, 1001} or type(error).__name__ == "ConnectionClosedOK":
        return None
    if code == 1008:
        failure = RelayTransportError("forbidden")
    elif code in {1006, 1011, 1012, 1013, 1014}:
        failure = RelayTransportError("unavailable")
    else:
        failure = RelayTransportError("protocol_error")
    failure.close_code = code
    return failure


def _message_bytes(message: str | bytes) -> int:
    if isinstance(message, bytes):
        return len(message)
    return len(message.encode("utf-8"))
