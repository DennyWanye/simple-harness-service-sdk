from __future__ import annotations

import pytest

from simple_harness_service import ContextAuthority, IdentityProjector, Principal, ServiceError


def test_projection_is_stable_domain_and_principal_separated() -> None:
    projector = IdentityProjector(b"p" * 32, namespace="consumer.example")
    alice = Principal("deploy", "home", "alice")
    bob = Principal("deploy", "home", "bob")
    first = projector.project("run", alice, "external")
    assert first == projector.project("run", alice, "external")
    assert first != projector.project("session", alice, "external")
    assert first != projector.project("run", bob, "external")
    assert first != IdentityProjector(b"q" * 32, namespace="consumer.example").project(
        "run", alice, "external"
    )


def test_capability_mac_expiry_and_channel_binding() -> None:
    authority = ContextAuthority(b"a" * 32, key_id="context-v1")
    principal = Principal("deploy", "home", "alice")
    token = authority.issue(
        principal, channel_binding="connection-a", now=10, ttl_seconds=5, nonce="nonce"
    ).token
    context = authority.verify(token, observed_channel_binding="connection-a", now=12)
    assert context.principal == principal
    assert context.nonce == "nonce"
    with pytest.raises(ServiceError):
        authority.verify(token, observed_channel_binding="connection-b", now=12)
    with pytest.raises(ServiceError):
        authority.verify(token, observed_channel_binding="connection-a", now=16)
    claims, mac = token.split(".")
    with pytest.raises(ServiceError):
        authority.verify(f"{claims}.{mac[:-1]}0", observed_channel_binding="connection-a", now=12)

