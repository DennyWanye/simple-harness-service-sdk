"""Privacy-safe HTTPS credential minting for TokenSeller Realtime."""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator, Mapping
from typing import Protocol, cast
from urllib.parse import urlsplit

from ..contracts import (
    MintedRealtimeCredential,
    RealtimeError,
    RealtimeErrorCode,
    RealtimeOpenRequest,
    RealtimeProfile,
)
from ..relay_control import RelayControlCodec

NATIVE_MINT_PATH = "/v1/realtime/qwen/client_secrets"
NATIVE_RELAY_PATH = "/v1/realtime/qwen"
MAX_CONTROL_RESPONSE_BYTES = 1_048_576


class TokenSellerConnectorError(RealtimeError):
    """Stable connector failure that never includes remote bodies or secrets."""

    def __init__(self, code: str) -> None:
        self.connector_code = code
        super().__init__(
            _error_code(code),
            code,
            retryable=code in {"rate_limited", "timeout", "unavailable"},
        )


class _HttpResponse(Protocol):
    status_code: int
    headers: Mapping[str, str]

    def aiter_bytes(self) -> AsyncIterator[bytes]: ...


class _HttpStream(Protocol):
    async def __aenter__(self) -> _HttpResponse: ...

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None: ...


class _HttpClient(Protocol):
    def stream(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        json: Mapping[str, object],
        timeout: float,
    ) -> _HttpStream: ...

    async def aclose(self) -> None: ...


class MintedRelayCredential(MintedRealtimeCredential):
    """Protocol credential whose representation never exposes its secret."""

    __slots__ = ()

    def __repr__(self) -> str:
        return (
            "MintedRelayCredential("
            f"expires_at_ms={self.expires_at_ms!r}, "
            f"websocket_path={self.websocket_path!r}, "
            f"capability_digest={self.capability_digest!r})"
        )


class TokenSellerHttpsCredentialMinter:
    """Mint one-use credentials without exposing the long-lived API key."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        client: _HttpClient | None = None,
        control_codec: RelayControlCodec | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._origin = _https_origin(base_url)
        if not api_key:
            raise ValueError("api_key must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._api_key = api_key
        self._client = client
        self._owns_client = client is None
        self._control = control_codec or RelayControlCodec()
        self._timeout_seconds = timeout_seconds
        self._closed = False

    def __repr__(self) -> str:
        return (
            "TokenSellerHttpsCredentialMinter("
            f"origin={self._origin!r}, timeout_seconds={self._timeout_seconds!r})"
        )

    async def mint(
        self,
        profile: RealtimeProfile,
        request: RealtimeOpenRequest,
        correlation: str,
    ) -> MintedRealtimeCredential:
        if self._closed:
            raise TokenSellerConnectorError("connector_closed")
        client = self._client
        if client is None:
            client = _new_httpx_client()
            self._client = client
        try:
            async with client.stream(
                "POST",
                f"{self._origin}{NATIVE_MINT_PATH}",
                headers={
                    "Accept-Encoding": "identity",
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=self._control.build_mint_request(profile, request, correlation),
                timeout=self._timeout_seconds,
            ) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    raise TokenSellerConnectorError(_status_code(response.status_code))
                payload = await _read_bounded_response(response)
        except TokenSellerConnectorError:
            raise
        except Exception as error:
            code = "timeout" if _is_timeout_exception(error) else "unavailable"
            raise TokenSellerConnectorError(code) from None
        try:
            parsed = self._control.parse_mint_response(payload.decode("utf-8"), profile)
            self._control.validate_minted(parsed, profile, request)
        except RealtimeError as exc:
            raise TokenSellerConnectorError(exc.code.value) from None
        except Exception:
            raise TokenSellerConnectorError("protocol_error") from None
        if parsed.websocket_path != NATIVE_RELAY_PATH:
            raise TokenSellerConnectorError("protocol_error")
        return MintedRelayCredential(
            secret=parsed.secret,
            expires_at_ms=parsed.expires_at_ms,
            websocket_path=parsed.websocket_path,
            capability=parsed.capability,
            capability_document=parsed.capability_document,
            capability_digest=parsed.capability_digest,
        )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_client and self._client is not None:
            try:
                await self._client.aclose()
            finally:
                self._client = None

    async def __aenter__(self) -> TokenSellerHttpsCredentialMinter:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()


def _new_httpx_client() -> _HttpClient:
    try:
        module = importlib.import_module("httpx")
        constructor = module.AsyncClient
        return cast(_HttpClient, constructor(follow_redirects=False))
    except (ImportError, AttributeError):
        raise TokenSellerConnectorError("realtime_dependency_unavailable") from None


def _https_origin(base_url: str) -> str:
    if not isinstance(base_url, str) or "\\" in base_url or any(
        ord(character) < 0x20 for character in base_url
    ):
        raise ValueError("base_url must be an HTTPS origin")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "https"
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


async def _read_bounded_response(response: _HttpResponse) -> bytes:
    declared = _content_length(response.headers)
    if declared is not None and declared > MAX_CONTROL_RESPONSE_BYTES:
        raise TokenSellerConnectorError("protocol_error")
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if not isinstance(chunk, bytes):
            raise TokenSellerConnectorError("protocol_error")
        if len(body) + len(chunk) > MAX_CONTROL_RESPONSE_BYTES:
            raise TokenSellerConnectorError("protocol_error")
        body.extend(chunk)
    if declared is not None and declared != len(body):
        raise TokenSellerConnectorError("protocol_error")
    return bytes(body)


def _content_length(headers: Mapping[str, str]) -> int | None:
    values = [value for key, value in headers.items() if key.lower() == "content-length"]
    if not values:
        return None
    if len(values) != 1 or not values[0].isdigit():
        raise TokenSellerConnectorError("protocol_error")
    return int(values[0])


def _status_code(status: int) -> str:
    if status == 401:
        return "unauthenticated"
    if status == 403:
        return "forbidden"
    if status == 429:
        return "rate_limited"
    if 400 <= status < 500:
        return "invalid_request"
    return "unavailable"


def _error_code(code: str) -> RealtimeErrorCode:
    try:
        return RealtimeErrorCode(code)
    except ValueError:
        return RealtimeErrorCode.UNAVAILABLE


def _is_timeout_exception(error: Exception) -> bool:
    if isinstance(error, TimeoutError):
        return True
    try:
        module = importlib.import_module("httpx")
        timeout_exception = module.TimeoutException
    except (ImportError, AttributeError):
        return False
    return isinstance(error, timeout_exception)
