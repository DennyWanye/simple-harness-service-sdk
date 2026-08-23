from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from simple_harness_service import (
    Principal,
    UnixServiceServer,
    load_credentials,
    provision_credentials,
)


def test_credentials_create_once_are_owner_only_and_stable(tmp_path: Path) -> None:
    path = tmp_path / "credentials"
    first = provision_credentials(path, namespace="consumer.example")
    second = provision_credentials(path, namespace="consumer.example")
    assert first == second
    assert first.identity_key_id == first.projector().key_id
    assert first.context_key_id == first.authority().key_id
    assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert {stat.S_IMODE(item.stat().st_mode) for item in path.iterdir()} == {0o600}


@pytest.mark.parametrize("name", ["identity.key", "context.key", "credential-manifest.json"])
def test_credentials_fail_closed_on_missing_component(tmp_path: Path, name: str) -> None:
    path = tmp_path / "credentials"
    provision_credentials(path, namespace="consumer.example")
    (path / name).unlink()
    with pytest.raises(PermissionError):
        load_credentials(path, expected_namespace="consumer.example")
    with pytest.raises(PermissionError):
        provision_credentials(path, namespace="consumer.example")


def test_credentials_fail_closed_on_namespace_key_and_mode_drift(tmp_path: Path) -> None:
    path = tmp_path / "credentials"
    provision_credentials(path, namespace="consumer.example")
    with pytest.raises(PermissionError):
        load_credentials(path, expected_namespace="consumer.other")
    context = path / "context.key"
    context.write_bytes(b"x" * 32)
    os.chmod(context, 0o600)
    with pytest.raises(PermissionError):
        load_credentials(path, expected_namespace="consumer.example")
    os.chmod(context, 0o644)
    with pytest.raises(PermissionError):
        load_credentials(path, expected_namespace="consumer.example")


def test_server_construction_rejects_credentials_before_socket_admission(
    tmp_path: Path,
) -> None:
    credential_path = tmp_path / "credentials"
    provision_credentials(credential_path, namespace="consumer.example")
    (credential_path / "context.key").unlink()
    socket_path = tmp_path / "svc.sock"
    with pytest.raises(PermissionError):
        UnixServiceServer.from_credentials(
            socket_path,
            object(),  # type: ignore[arg-type]
            credential_path,
            namespace="consumer.example",
            principal_for_uid=lambda _: Principal("deploy", "home", "alice"),
        )
    assert not socket_path.exists()
