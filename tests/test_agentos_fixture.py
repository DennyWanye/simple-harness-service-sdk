from __future__ import annotations

import pytest

from simple_harness_service import ContextAuthority, Principal, ServiceError
from simple_harness_service.testing import TokenSubjectAdapter


def test_verified_subject_is_authority_and_wire_principal_is_not_accepted() -> None:
    authority = ContextAuthority(b"a" * 32)
    principal = Principal("deploy", "home", "alice")
    adapter = TokenSubjectAdapter(authority, {"issuer|subject": principal})
    context = adapter.authenticate(
        "issuer|subject", observed_tls_binding="tls-exporter", now=10
    )
    assert context.principal == principal
    with pytest.raises(ServiceError):
        adapter.authenticate("alice", observed_tls_binding="tls-exporter", now=10)


def test_agentos_subjects_are_isolated_and_cross_binding_fails() -> None:
    authority = ContextAuthority(b"a" * 32)
    alice = Principal("deploy", "home", "alice")
    bob = Principal("deploy", "home", "bob")
    adapter = TokenSubjectAdapter(
        authority, {"issuer|alice": alice, "issuer|bob": bob}
    )
    alice_context = adapter.authenticate(
        "issuer|alice", observed_tls_binding="tls-alice", now=10
    )
    bob_context = adapter.authenticate(
        "issuer|bob", observed_tls_binding="tls-bob", now=10
    )
    assert alice_context.principal != bob_context.principal
    capability = authority.issue(
        alice, channel_binding="tls-alice", now=10, nonce="fixed"
    )
    with pytest.raises(ServiceError):
        authority.verify(
            capability.token, observed_channel_binding="tls-bob", now=10
        )
