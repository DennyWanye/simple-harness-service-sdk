"""Canonical JSON length-frame codec."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Protocol

from .contracts import FRAME_MAX_BYTES, JsonObject, ServiceError, ServiceErrorCode


def canonical_payload(value: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    except (TypeError, ValueError) as error:
        raise ServiceError(ServiceErrorCode.INVALID_REQUEST) from error


def encode_frame(value: Mapping[str, object]) -> bytes:
    payload = canonical_payload(value)
    if len(payload) > FRAME_MAX_BYTES:
        raise ServiceError(ServiceErrorCode.PAYLOAD_TOO_LARGE)
    return len(payload).to_bytes(4, "big") + payload


def decode_payload(payload: bytes) -> JsonObject:
    if len(payload) > FRAME_MAX_BYTES:
        raise ServiceError(ServiceErrorCode.PAYLOAD_TOO_LARGE)
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ServiceError(ServiceErrorCode.INVALID_REQUEST) from error
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ServiceError(ServiceErrorCode.INVALID_REQUEST)
    return value


class FrameDecoder:
    """Incremental decoder that handles fragmented and coalesced frames."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[JsonObject]:
        self._buffer.extend(data)
        frames: list[JsonObject] = []
        while len(self._buffer) >= 4:
            size = int.from_bytes(self._buffer[:4], "big")
            if size > FRAME_MAX_BYTES:
                raise ServiceError(ServiceErrorCode.PAYLOAD_TOO_LARGE)
            if len(self._buffer) < 4 + size:
                break
            payload = bytes(self._buffer[4 : 4 + size])
            del self._buffer[: 4 + size]
            frames.append(decode_payload(payload))
        return frames


class AsyncFrameReader(Protocol):
    async def readexactly(self, size: int) -> bytes: ...


async def read_frame(reader: AsyncFrameReader) -> JsonObject:
    header = await reader.readexactly(4)
    size = int.from_bytes(header, "big")
    if size > FRAME_MAX_BYTES:
        raise ServiceError(ServiceErrorCode.PAYLOAD_TOO_LARGE)
    payload = await reader.readexactly(size)
    return decode_payload(payload)
