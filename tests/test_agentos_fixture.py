from __future__ import annotations

import pytest

from simple_harness_service import ContextAuthority, Principal, ServiceError
from simple_harness_service.testing import TokenSubjectAdapter


def test_verified_subject_is_authority_and_wire_principal_is_not_accepted() -> None:
    authority = ContextAuthority(b"a" * 32, key_id="context-v1")
    principal = Principal("deploy", "home", "alice")
    adapter = TokenSubjectAdapter(authority, {"issuer|subject": principal})
    context = adapter.authenticate(
        "issuer|subject", observed_tls_binding="tls-exporter", now=10
    )
    assert context.principal == principal
    with pytest.raises(ServiceError):
        adapter.authenticate("alice", observed_tls_binding="tls-exporter", now=10)

