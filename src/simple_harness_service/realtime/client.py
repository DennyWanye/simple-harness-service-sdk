"""Product-facing entry point for SDK-owned Realtime sessions."""

from __future__ import annotations

import asyncio
import contextlib
import re
import secrets
import time

from .contracts import (
    RealtimeError,
    RealtimeErrorCode,
    RealtimeOpenRequest,
    RealtimeProfile,
)
from .observability import (
    RealtimeDiagnostics,
    RealtimeDiagnosticSnapshot,
    RealtimeDiagnosticStage,
)
from .ports import CredentialMinter, RealtimeProviderAdapter, RealtimeTransport
from .relay_control import RelayControlCodec
from .session import ManagedRealtimeSession

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CORRELATION = re.compile(r"^corr_[0-9A-HJKMNP-TV-Z]{26}$")


def new_correlation() -> str:
    return "corr_" + "".join(secrets.choice(_CROCKFORD) for _ in range(26))


def _failure_operation_kind(error: Exception, fallback: str) -> str:
    value = getattr(error, "diagnostic_kind", None)
    if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_.-]{0,127}", value):
        return value
    return fallback


class RealtimeClient:
    def __init__(
        self,
        profile: RealtimeProfile,
        credential_minter: CredentialMinter,
        transport: RealtimeTransport,
        adapter: RealtimeProviderAdapter,
        *,
        control_codec: RelayControlCodec | None = None,
        diagnostics: RealtimeDiagnostics | None = None,
        open_timeout: float = 5.0,
        write_timeout: float = 2.0,
        tool_ack_timeout: float = 5.0,
        close_timeout: float = 5.0,
    ) -> None:
        if profile.capability != adapter.capability:
            raise RealtimeError(RealtimeErrorCode.INVALID_REQUEST, "profile/adapter mismatch")
        if any(
            timeout <= 0
            for timeout in (open_timeout, write_timeout, tool_ack_timeout, close_timeout)
        ):
            raise ValueError("Realtime timeouts must be positive")
        self._profile = profile
        self._minter = credential_minter
        self._transport = transport
        self._adapter = adapter
        self._control = control_codec or RelayControlCodec()
        self._diagnostics = diagnostics or RealtimeDiagnostics()
        self._open_timeout = open_timeout
        self._write_timeout = write_timeout
        self._tool_ack_timeout = tool_ack_timeout
        self._close_timeout = close_timeout
        self._generation = 0
        self._generation_lock = asyncio.Lock()
        self._correlation_lock = asyncio.Lock()
        self._used_correlations: set[str] = set()

    async def open(self, request: RealtimeOpenRequest) -> ManagedRealtimeSession:
        correlation = await self._reserve_generated_correlation()
        return await self._open_reserved(request, correlation)

    def diagnostics_snapshot(self) -> RealtimeDiagnosticSnapshot:
        return self._diagnostics.snapshot()

    async def _open_with_correlation(
        self,
        request: RealtimeOpenRequest,
        correlation: str,
    ) -> ManagedRealtimeSession:
        """Open for an SDK-owned local channel using its validated opaque correlation."""

        _validate_correlation(correlation)
        async with self._correlation_lock:
            if correlation in self._used_correlations:
                raise RealtimeError(
                    RealtimeErrorCode.INVALID_REQUEST,
                    "correlation has already been used",
                )
            self._used_correlations.add(correlation)
        return await self._open_reserved(request, correlation)

    async def _reserve_generated_correlation(self) -> str:
        async with self._correlation_lock:
            for _ in range(4):
                correlation = new_correlation()
                if correlation not in self._used_correlations:
                    self._used_correlations.add(correlation)
                    return correlation
        raise RealtimeError(
            RealtimeErrorCode.INTERNAL,
            "unable to allocate an opaque correlation",
            retryable=True,
        )

    async def _open_reserved(
        self,
        request: RealtimeOpenRequest,
        correlation: str,
    ) -> ManagedRealtimeSession:
        started_ns = time.monotonic_ns()
        self._diagnostics.emit(
            correlation=correlation,
            stage=RealtimeDiagnosticStage.OPEN_STARTED,
        )
        try:
            session = await self._open_reserved_inner(request, correlation)
        except Exception as error:
            self._diagnostics.emit(
                correlation=correlation,
                stage=RealtimeDiagnosticStage.OPEN_FAILED,
                stable_code=_stable_code(error),
                duration_ms=_duration_ms(started_ns),
            )
            raise
        self._diagnostics.emit(
            correlation=correlation,
            stage=RealtimeDiagnosticStage.OPEN_COMPLETED,
            generation=session.generation,
            duration_ms=_duration_ms(started_ns),
        )
        return session

    async def _open_reserved_inner(
        self,
        request: RealtimeOpenRequest,
        correlation: str,
    ) -> ManagedRealtimeSession:
        capability = self._adapter.capability
        if not capability.features.supports(request.required_features):
            raise RealtimeError(RealtimeErrorCode.UNSUPPORTED, "required feature unavailable")
        if request.input_audio != capability.input_audio:
            raise RealtimeError(RealtimeErrorCode.UNSUPPORTED, "input audio unavailable")
        if request.output_audio != capability.output_audio:
            raise RealtimeError(RealtimeErrorCode.UNSUPPORTED, "output audio unavailable")
        mint_started_ns = time.monotonic_ns()
        self._diagnostics.emit(
            correlation=correlation,
            stage=RealtimeDiagnosticStage.MINT_STARTED,
        )
        try:
            credential = await self._minter.mint(self._profile, request, correlation)
        except Exception as error:
            self._diagnostics.emit(
                correlation=correlation,
                stage=RealtimeDiagnosticStage.MINT_FAILED,
                stable_code=_stable_code(error),
                operation_kind=_failure_operation_kind(error, "mint.failure"),
                duration_ms=_duration_ms(mint_started_ns),
            )
            raise
        self._diagnostics.emit(
            correlation=correlation,
            stage=RealtimeDiagnosticStage.MINT_COMPLETED,
            duration_ms=_duration_ms(mint_started_ns),
        )
        self._control.validate_minted(credential, self._profile, request)
        connect_started_ns = time.monotonic_ns()
        self._diagnostics.emit(
            correlation=correlation,
            stage=RealtimeDiagnosticStage.CONNECT_STARTED,
        )
        try:
            connection = await asyncio.wait_for(
                self._transport.connect(
                    credential.websocket_path,
                    credential.secret,
                ),
                timeout=self._open_timeout,
            )
        except TimeoutError as error:
            self._diagnostics.emit(
                correlation=correlation,
                stage=RealtimeDiagnosticStage.CONNECT_FAILED,
                stable_code=RealtimeErrorCode.TIMEOUT,
                duration_ms=_duration_ms(connect_started_ns),
            )
            raise RealtimeError(RealtimeErrorCode.TIMEOUT, retryable=True) from error
        except Exception as error:
            self._diagnostics.emit(
                correlation=correlation,
                stage=RealtimeDiagnosticStage.CONNECT_FAILED,
                stable_code=_stable_code(error),
                duration_ms=_duration_ms(connect_started_ns),
            )
            raise
        self._diagnostics.emit(
            correlation=correlation,
            stage=RealtimeDiagnosticStage.CONNECT_COMPLETED,
            duration_ms=_duration_ms(connect_started_ns),
        )
        try:
            open_payload, open_event_id = self._control._build_bound_session_open(
                credential, correlation
            )
            await asyncio.wait_for(
                connection.send_text(open_payload),
                timeout=self._open_timeout,
            )
            created = await asyncio.wait_for(
                connection.receive_text(),
                timeout=self._open_timeout,
            )
            if created is None:
                raise RealtimeError(RealtimeErrorCode.UNAVAILABLE, retryable=True)
            self._control.validate_session_created(created, credential, open_event_id)
            async with self._generation_lock:
                self._generation += 1
                generation = self._generation
            session = ManagedRealtimeSession(
                connection=connection,
                adapter=self._adapter,
                control=self._control,
                credential=credential,
                request=request,
                correlation=correlation,
                generation=generation,
                diagnostics=self._diagnostics,
                write_timeout=self._write_timeout,
                tool_ack_timeout=self._tool_ack_timeout,
                close_timeout=self._close_timeout,
            )
            await asyncio.wait_for(session.start(), timeout=self._open_timeout)
            return session
        except TimeoutError as error:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    connection.close(1000, "open timeout"),
                    timeout=self._close_timeout,
                )
            raise RealtimeError(RealtimeErrorCode.TIMEOUT, retryable=True) from error
        except Exception:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    connection.close(1000, "open failed"),
                    timeout=self._close_timeout,
                )
            raise


def _validate_correlation(correlation: str) -> None:
    if not isinstance(correlation, str) or _CORRELATION.fullmatch(correlation) is None:
        raise RealtimeError(
            RealtimeErrorCode.INVALID_REQUEST,
            "correlation must match the SDK opaque format",
        )


def _stable_code(error: Exception) -> RealtimeErrorCode:
    if isinstance(error, RealtimeError):
        return error.code
    if isinstance(error, TimeoutError):
        return RealtimeErrorCode.TIMEOUT
    return RealtimeErrorCode.UNAVAILABLE


def _duration_ms(started_ns: int) -> int:
    return max(0, (time.monotonic_ns() - started_ns) // 1_000_000)
