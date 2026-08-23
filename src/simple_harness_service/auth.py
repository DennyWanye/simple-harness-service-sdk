"""MAC capabilities bound to an observed adapter channel."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

from .contracts import ServiceError, ServiceErrorCode
from .identity import Principal


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@dataclass(frozen=True, slots=True)
class Capability:
    claims: str
    mac: str

    @property
    def token(self) -> str:
        return f"{self.claims}.{self.mac}"

    @classmethod
    def parse(cls, token: str) -> Capability:
        claims, separator, mac = token.partition(".")
        if not separator or not claims or len(mac) != 64:
            raise ServiceError(ServiceErrorCode.UNAUTHENTICATED)
        return cls(claims, mac)


@dataclass(frozen=True, slots=True)
class AuthenticatedContext:
    principal: Principal
    channel_binding: str
    nonce: str


class ContextAuthority:
    def __init__(self, key: bytes, *, key_id: str) -> None:
        if len(key) < 32:
            raise ValueError("context authority key must contain at least 256 bits")
        if not key_id.strip():
            raise ValueError("key_id is required")
        self._key = bytes(key)
        self.key_id = key_id

    def issue(
        self,
        principal: Principal,
        *,
        channel_binding: str,
        now: float | None = None,
        ttl_seconds: float = 30.0,
        nonce: str | None = None,
    ) -> Capability:
        issued = time.time() if now is None else now
        if ttl_seconds <= 0 or ttl_seconds > 300:
            raise ValueError("capability ttl is outside the closed range")
        claims = {
            "v": 1,
            "kid": self.key_id,
            "deployment_id": principal.deployment_id,
            "household_id": principal.household_id,
            "actor_id": principal.actor_id,
            "binding": channel_binding,
            "nonce": nonce or secrets.token_hex(16),
            "iat_ms": int(issued * 1000),
            "exp_ms": int((issued + ttl_seconds) * 1000),
        }
        raw = json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
        encoded = _b64(raw)
        mac = hmac.new(
            self._key,
            b"svc-context-capability-v1\x00" + encoded.encode(),
            hashlib.sha256,
        )
        return Capability(encoded, mac.hexdigest())

    def verify(
        self,
        token: str,
        *,
        observed_channel_binding: str,
        now: float | None = None,
    ) -> AuthenticatedContext:
        capability = Capability.parse(token)
        expected = hmac.new(
            self._key,
            b"svc-context-capability-v1\x00" + capability.claims.encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(capability.mac, expected):
            raise ServiceError(ServiceErrorCode.UNAUTHENTICATED)
        try:
            claims = json.loads(_unb64(capability.claims))
            current_ms = int((time.time() if now is None else now) * 1000)
            if claims["v"] != 1 or claims["kid"] != self.key_id:
                raise ValueError
            if current_ms < claims["iat_ms"] or current_ms > claims["exp_ms"]:
                raise ServiceError(ServiceErrorCode.UNAUTHENTICATED)
            if not hmac.compare_digest(claims["binding"], observed_channel_binding):
                raise ServiceError(ServiceErrorCode.FORBIDDEN)
            principal = Principal(
                claims["deployment_id"], claims["household_id"], claims["actor_id"]
            )
            return AuthenticatedContext(principal, claims["binding"], claims["nonce"])
        except ServiceError:
            raise
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ServiceError(ServiceErrorCode.UNAUTHENTICATED) from error
