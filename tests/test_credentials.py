from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

import pytest

from simple_harness_service import (
    Principal,
    UnixServiceServer,
    load_credentials,
    provision_credentials,
)
from simple_harness_service import credentials as credential_module


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


def test_provision_fsync_and_atomic_activation_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    original_write = credential_module._write_new
    original_sync = credential_module._fsync_directory
    original_rename = os.rename

    def write(path: Path, value: bytes) -> None:
        original_write(path, value)
        events.append(f"write:{path.name}")

    def sync(path: Path) -> None:
        original_sync(path)
        events.append(f"sync:{path.name}")

    def rename(source: Path, target: Path) -> None:
        original_rename(source, target)
        events.append("rename")

    monkeypatch.setattr(credential_module, "_write_new", write)
    monkeypatch.setattr(credential_module, "_fsync_directory", sync)
    monkeypatch.setattr(os, "rename", rename)
    path = tmp_path / "credentials"
    provision_credentials(path, namespace="consumer.example")
    assert events == [
        f"sync:{tmp_path.name}",
        "write:identity.key",
        "write:context.key",
        "write:credential-manifest.json",
        "sync:.credentials.provisioning",
        "rename",
        f"sync:{tmp_path.name}",
    ]


@pytest.mark.parametrize("fault_at", range(1, 7))
def test_provision_fsync_faults_never_succeed_or_regenerate_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault_at: int
) -> None:
    path = tmp_path / "credentials"
    original_fsync = os.fsync
    calls = 0
    generated = 0

    def token_bytes(size: int) -> bytes:
        nonlocal generated
        generated += 1
        return bytes([generated]) * size

    def failing_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == fault_at:
            raise OSError("durability fault")
        original_fsync(descriptor)

    monkeypatch.setattr(secrets, "token_bytes", token_bytes)
    monkeypatch.setattr(os, "fsync", failing_fsync)
    with pytest.raises(OSError, match="durability fault"):
        provision_credentials(path, namespace="consumer.example")
    generated_after_failure = generated
    monkeypatch.setattr(os, "fsync", original_fsync)
    if path.exists():
        bundle = provision_credentials(path, namespace="consumer.example")
        assert bundle.identity_key == bytes([1]) * 32
        assert bundle.context_key == bytes([2]) * 32
    else:
        with pytest.raises(PermissionError, match="operator review"):
            provision_credentials(path, namespace="consumer.example")
    assert generated == generated_after_failure
