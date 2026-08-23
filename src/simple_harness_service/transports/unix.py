"""Owner-only AF_UNIX reference transport with observed peer authentication."""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import socket
import stat
import struct
from collections.abc import Callable, Mapping
from dataclasses import fields
from pathlib import Path
from typing import Any

from ..auth import ContextAuthority
from ..codec import encode_frame, read_frame
from ..contracts import (
    CancelRequest,
    CommandKind,
    CommandOutcome,
    CommandReceipt,
    CommandSnapshot,
    CommandState,
    ContinueRequest,
    GetRequest,
    HealthSnapshot,
    JsonObject,
    RunState,
    ServiceError,
    ServiceErrorCode,
    StartRequest,
)
from ..credentials import load_credentials
from ..identity import Principal
from ..service import HarnessService, ServiceAdapterPort

CONNECT_TIMEOUT_SECONDS = 2.0
WRITE_TIMEOUT_SECONDS = 2.0
RPC_TIMEOUT_SECONDS = 5.0


def _peer_uid(sock: Any) -> int:
    if hasattr(socket, "SO_PEERCRED"):
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return int(struct.unpack("3i", raw)[1])
    if hasattr(socket, "LOCAL_PEERCRED"):
        raw = sock.getsockopt(0, socket.LOCAL_PEERCRED, 12)
        return int(struct.unpack_from("=I", raw, 4)[0])
    raise RuntimeError("platform does not expose AF_UNIX peer credentials")


def _validate_parent(path: Path, owner_uid: int) -> None:
    parent = path.parent
    info = parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or parent.is_symlink():
        raise PermissionError("socket parent must be a real directory")
    if info.st_uid != owner_uid or stat.S_IMODE(info.st_mode) != 0o700:
        raise PermissionError("socket parent must be owner-only mode 0700")


def _validate_socket(path: Path, owner_uid: int) -> None:
    _validate_parent(path, owner_uid)
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or path.is_symlink():
        raise PermissionError("service path must be an AF_UNIX socket")
    if info.st_uid != owner_uid or stat.S_IMODE(info.st_mode) != 0o600:
        raise PermissionError("service socket must be owner-only mode 0600")


class UnixServiceServer:
    def __init__(
        self,
        path: Path,
        service: HarnessService,
        authority: ContextAuthority,
        *,
        principal_for_uid: Callable[[int], Principal],
        owner_uid: int | None = None,
        peer_uid_resolver: Callable[[Any], int] = _peer_uid,
    ) -> None:
        self.path = path
        self._service = service
        self._authority = authority
        self._principal_for_uid = principal_for_uid
        self._owner_uid = os.getuid() if owner_uid is None else owner_uid
        self._peer_uid_resolver = peer_uid_resolver
        self._server: asyncio.AbstractServer | None = None

    @classmethod
    def from_credentials(
        cls,
        path: Path,
        adapter: ServiceAdapterPort,
        credential_path: Path,
        *,
        namespace: str,
        principal_for_uid: Callable[[int], Principal],
        owner_uid: int | None = None,
        peer_uid_resolver: Callable[[Any], int] = _peer_uid,
    ) -> UnixServiceServer:
        """Fail closed on credential drift before a socket can be admitted."""
        bundle = load_credentials(credential_path, expected_namespace=namespace)
        return cls(
            path,
            HarnessService(adapter, bundle.projector()),
            bundle.authority(),
            principal_for_uid=principal_for_uid,
            owner_uid=owner_uid,
            peer_uid_resolver=peer_uid_resolver,
        )

    async def start(self) -> None:
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

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        try:
            if stat.S_ISSOCK(self.path.lstat().st_mode):
                self.path.unlink()
        except FileNotFoundError:
            pass

    async def __aenter__(self) -> UnixServiceServer:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            transport_socket = writer.get_extra_info("socket")
            peer_uid = self._peer_uid_resolver(transport_socket)
            if peer_uid != self._owner_uid:
                raise ServiceError(ServiceErrorCode.FORBIDDEN)
            principal = self._principal_for_uid(peer_uid)
            binding = secrets.token_hex(32)
            capability = self._authority.issue(
                principal, channel_binding=binding, ttl_seconds=300
            ).token
            await self._send(
                writer,
                {"type": "hello", "binding": binding, "capability": capability},
            )
            while not reader.at_eof():
                request = await asyncio.wait_for(read_frame(reader), RPC_TIMEOUT_SECONDS)
                token = request.get("capability")
                if not isinstance(token, str):
                    raise ServiceError(ServiceErrorCode.UNAUTHENTICATED)
                context = self._authority.verify(
                    token, observed_channel_binding=binding
                )
                if context.principal != principal:
                    raise ServiceError(ServiceErrorCode.FORBIDDEN)
                response = await self._dispatch(request, context)
                await self._send(writer, response)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except ServiceError as error:
            await self._safe_error(writer, error.code)
        except TimeoutError:
            await self._safe_error(writer, ServiceErrorCode.TIMEOUT)
        except (TypeError, ValueError):
            await self._safe_error(writer, ServiceErrorCode.INVALID_REQUEST)
        except Exception:
            await self._safe_error(writer, ServiceErrorCode.INTERNAL)
        finally:
            writer.close()
            await writer.wait_closed()

    async def _dispatch(self, request: JsonObject, context: Any) -> JsonObject:
        request_id = request.get("request_id")
        op = request.get("op")
        payload = request.get("payload")
        if (
            not isinstance(request_id, str)
            or not isinstance(op, str)
            or not isinstance(payload, dict)
        ):
            raise ServiceError(ServiceErrorCode.INVALID_REQUEST)
        if op == "health":
            result: object = await self._service.health()
        elif op == "start":
            result = await self._service.start(StartRequest(**payload), context)
        elif op == "continue":
            result = await self._service.continue_(ContinueRequest(**payload), context)
        elif op == "get":
            result = await self._service.get(GetRequest(**payload), context)
        elif op == "cancel":
            result = await self._service.cancel(CancelRequest(**payload), context)
        else:
            raise ServiceError(ServiceErrorCode.INVALID_REQUEST)
        return {"ok": True, "request_id": request_id, "result": _result_json(result)}

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, value: Mapping[str, object]) -> None:
        writer.write(encode_frame(value))
        await asyncio.wait_for(writer.drain(), WRITE_TIMEOUT_SECONDS)

    async def _safe_error(
        self, writer: asyncio.StreamWriter, code: ServiceErrorCode
    ) -> None:
        with contextlib.suppress(ConnectionError, TimeoutError):
            await self._send(writer, {"ok": False, "error": code.value})


class UnixServiceClient:
    """Short-RPC client; each call obtains a fresh connection-bound capability."""

    def __init__(self, path: Path, *, owner_uid: int | None = None) -> None:
        self.path = path
        self._owner_uid = os.getuid() if owner_uid is None else owner_uid

    async def health(self) -> HealthSnapshot:
        value = await self._rpc("health", {})
        return HealthSnapshot(bool(value["serving"]), str(value["detail"]))

    async def start(self, request: StartRequest) -> CommandReceipt:
        return _receipt(await self._rpc("start", _request_json(request)))

    async def continue_(self, request: ContinueRequest) -> CommandReceipt:
        return _receipt(await self._rpc("continue", _request_json(request)))

    async def get(self, external_command_id: str) -> CommandSnapshot:
        value = await self._rpc("get", {"external_command_id": external_command_id})
        receipt = _receipt(_object(value["receipt"]))
        return CommandSnapshot(
            receipt,
            value["output_state"],
            value.get("output_text"),
            value.get("error_code"),
            None if value.get("run_state") is None else RunState(str(value["run_state"])),
            CommandOutcome(str(value["outcome"])),
        )

    async def cancel(self, request: CancelRequest) -> CommandReceipt:
        return _receipt(await self._rpc("cancel", _request_json(request)))

    async def _rpc(self, op: str, payload: Mapping[str, object]) -> JsonObject:
        try:
            _validate_socket(self.path, self._owner_uid)
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(self.path), CONNECT_TIMEOUT_SECONDS
            )
            try:
                transport_socket = writer.get_extra_info("socket")
                if _peer_uid(transport_socket) != self._owner_uid:
                    raise ServiceError(ServiceErrorCode.FORBIDDEN)
                hello = await asyncio.wait_for(read_frame(reader), RPC_TIMEOUT_SECONDS)
                capability = hello.get("capability")
                if hello.get("type") != "hello" or not isinstance(capability, str):
                    raise ServiceError(ServiceErrorCode.UNAUTHENTICATED)
                request_id = secrets.token_hex(16)
                writer.write(
                    encode_frame(
                        {
                            "request_id": request_id,
                            "op": op,
                            "payload": dict(payload),
                            "capability": capability,
                        }
                    )
                )
                await asyncio.wait_for(writer.drain(), WRITE_TIMEOUT_SECONDS)
                response = await asyncio.wait_for(read_frame(reader), RPC_TIMEOUT_SECONDS)
                if response.get("ok") is not True:
                    raw_code = response.get("error")
                    code = (
                        ServiceErrorCode(raw_code)
                        if isinstance(raw_code, str)
                        else ServiceErrorCode.INTERNAL
                    )
                    raise ServiceError(code)
                if response.get("request_id") != request_id:
                    raise ServiceError(ServiceErrorCode.INVALID_REQUEST)
                return _object(response.get("result"))
            finally:
                writer.close()
                await writer.wait_closed()
        except TimeoutError as error:
            raise ServiceError(ServiceErrorCode.TIMEOUT) from error
        except (ConnectionError, FileNotFoundError, OSError) as error:
            raise ServiceError(ServiceErrorCode.UNAVAILABLE) from error


def _request_json(value: StartRequest | ContinueRequest | CancelRequest) -> JsonObject:
    return {field.name: getattr(value, field.name) for field in fields(value)}


def _result_json(value: object) -> JsonObject:
    if isinstance(value, HealthSnapshot):
        return {"serving": value.serving, "detail": value.detail}
    if isinstance(value, CommandReceipt):
        return {
            "command_id": value.command_id,
            "run_id": value.run_id,
            "accept_seq": value.accept_seq,
            "state": value.state.value,
            "version": value.version,
            "kind": value.kind.value,
        }
    if isinstance(value, CommandSnapshot):
        return {
            "receipt": _result_json(value.receipt),
            "output_state": value.output_state.value,
            "output_text": value.output_text,
            "error_code": value.error_code,
            "run_state": None if value.run_state is None else value.run_state.value,
            "outcome": value.outcome.value,
        }
    raise ServiceError(ServiceErrorCode.INTERNAL)


def _object(value: object) -> JsonObject:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ServiceError(ServiceErrorCode.INVALID_REQUEST)
    return value


def _receipt(value: JsonObject) -> CommandReceipt:
    return CommandReceipt(
        str(value["command_id"]),
        str(value["run_id"]),
        int(value["accept_seq"]),
        CommandState(str(value["state"])),
        int(value["version"]),
        CommandKind(str(value["kind"])),
    )
