"""Domain-separated stable identity projection."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass


def _field(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return len(encoded).to_bytes(4, "big") + encoded


@dataclass(frozen=True, slots=True)
class Principal:
    deployment_id: str
    household_id: str
    actor_id: str

    def __post_init__(self) -> None:
        for value in (self.deployment_id, self.household_id, self.actor_id):
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise ValueError("principal fields must be non-empty")

    def canonical(self) -> bytes:
        return b"".join(_field(v) for v in (self.deployment_id, self.household_id, self.actor_id))


class IdentityProjector:
    """Projects external IDs without exposing raw principal values downstream."""

    def __init__(self, key: bytes, *, namespace: str) -> None:
        if len(key) < 32:
            raise ValueError("projection key must contain at least 256 bits")
        if not namespace.strip():
            raise ValueError("namespace is required")
        self._key = bytes(key)
        self.namespace = namespace
        self.key_id = hashlib.sha256(b"svc-projection-key-id-v1\x00" + key).hexdigest()[:32]

    def project(self, kind: str, principal: Principal, external_id: str) -> str:
        if not kind or not external_id:
            raise ValueError("projection kind and external ID are required")
        payload = (
            b"simple-harness-service-id-v1\x00"
            + _field(self.namespace)
            + _field(kind)
            + principal.canonical()
            + _field(external_id)
        )
        return f"svc_{kind}_{hmac.new(self._key, payload, hashlib.sha256).hexdigest()}"

