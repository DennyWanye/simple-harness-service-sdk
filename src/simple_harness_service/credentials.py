"""Create-once owner-only credentials for service admission."""

from __future__ import annotations

import json
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from .auth import ContextAuthority
from .identity import IdentityProjector

_SCHEMA = "simple-harness-service-credentials-v1"
_MANIFEST = "credential-manifest.json"
_IDENTITY = "identity.key"
_CONTEXT = "context.key"


@dataclass(frozen=True, slots=True)
class CredentialBundle:
    namespace: str
    identity_key: bytes
    context_key: bytes
    identity_key_id: str
    context_key_id: str

    def projector(self) -> IdentityProjector:
        value = IdentityProjector(self.identity_key, namespace=self.namespace)
        if value.key_id != self.identity_key_id:
            raise PermissionError("identity credential key ID drift")
        return value

    def authority(self) -> ContextAuthority:
        value = ContextAuthority(self.context_key)
        if value.key_id != self.context_key_id:
            raise PermissionError("context credential key ID drift")
        return value


def provision_credentials(path: Path, *, namespace: str) -> CredentialBundle:
    """Create the complete credential set once, or validate an existing set."""
    if not namespace.strip():
        raise ValueError("namespace is required")
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        return load_credentials(path, expected_namespace=namespace)
    _validate_directory(path)
    identity_key = secrets.token_bytes(32)
    context_key = secrets.token_bytes(32)
    projector = IdentityProjector(identity_key, namespace=namespace)
    authority = ContextAuthority(context_key)
    manifest = {
        "schema": _SCHEMA,
        "namespace": namespace,
        "identity_key_id": projector.key_id,
        "context_key_id": authority.key_id,
    }
    try:
        _write_new(path / _IDENTITY, identity_key)
        _write_new(path / _CONTEXT, context_key)
        _write_new(
            path / _MANIFEST,
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode() + b"\n",
        )
        _fsync_directory(path)
    except BaseException:
        # A partial create is intentionally not repaired on the next invocation.
        # Admission fails closed until an operator removes the never-activated set.
        raise
    return load_credentials(path, expected_namespace=namespace)


def load_credentials(path: Path, *, expected_namespace: str) -> CredentialBundle:
    """Load a complete credential set, rejecting missing, drifted, or unsafe files."""
    _validate_directory(path)
    expected = {_IDENTITY, _CONTEXT, _MANIFEST}
    if {entry.name for entry in path.iterdir()} != expected:
        raise PermissionError("credential set is incomplete or contains unexpected files")
    identity_key = _read_secret(path / _IDENTITY)
    context_key = _read_secret(path / _CONTEXT)
    try:
        manifest = json.loads(_read_secret(path / _MANIFEST))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PermissionError("credential manifest is malformed") from error
    projector = IdentityProjector(identity_key, namespace=expected_namespace)
    authority = ContextAuthority(context_key)
    expected_manifest = {
        "schema": _SCHEMA,
        "namespace": expected_namespace,
        "identity_key_id": projector.key_id,
        "context_key_id": authority.key_id,
    }
    if manifest != expected_manifest:
        raise PermissionError("credential manifest identity or namespace drift")
    return CredentialBundle(
        expected_namespace,
        identity_key,
        context_key,
        projector.key_id,
        authority.key_id,
    )


def _validate_directory(path: Path) -> None:
    info = path.lstat()
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode):
        raise PermissionError("credential path must be a real directory")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise PermissionError("credential directory must be owner-only mode 0700")


def _read_secret(path: Path) -> bytes:
    info = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise PermissionError("credential must be a regular file")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise PermissionError("credential file must be owner-only mode 0600")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        return os.read(descriptor, 4097)
    finally:
        os.close(descriptor)


def _write_new(path: Path, value: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(value)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = ("CredentialBundle", "load_credentials", "provision_credentials")
