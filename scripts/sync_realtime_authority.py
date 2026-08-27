#!/usr/bin/env python3
"""Synchronize, validate, index, and bundle frozen Realtime authority packs."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import shutil
import tarfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "ARCHITECTURE" / "protocols"
PACKAGE_REALTIME_ROOT = ROOT / "src" / "simple_harness_service" / "realtime"
TARGET_ROOT = PACKAGE_REALTIME_ROOT / "protocols"
INDEX_PATH = PACKAGE_REALTIME_ROOT / "authority-index.json"

EXPECTED_PACKS = (
    "openai-native-2026-08-27.1",
    "qwen-native-2026-08-28.3",
    "realtime-local-2026-08-27.1",
    "tokenseller-realtime-control-2026-08-28.3",
)
SUM_LINE = re.compile(r"^(?P<digest>[0-9a-f]{64})  (?P<name>[^/\\]+)$")
ROOT_DIGEST_ALGORITHM = "sha256(path_utf8 + NUL + lowercase_file_sha256_ascii + LF)"
BUNDLE_PREFIX = "realtime-authority"


class AuthorityError(RuntimeError):
    """Raised when frozen authority bytes or metadata are inconsistent."""


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _regular_files(root: Path) -> dict[str, Path]:
    if not root.is_dir() or root.is_symlink():
        raise AuthorityError(f"authority root missing or invalid: {root}")
    result: dict[str, Path] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise AuthorityError(f"authority symlink forbidden: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise AuthorityError(f"authority entry is not a regular file: {path}")
        relative = path.relative_to(root).as_posix()
        if relative.startswith(".") or "/." in relative:
            raise AuthorityError(f"hidden authority entry forbidden: {relative}")
        result[relative] = path
    return result


def _active_files(root: Path) -> dict[str, Path]:
    """Return only the active release packs while retaining historical packs in source."""

    result: dict[str, Path] = {}
    for pack_name in EXPECTED_PACKS:
        for relative, path in _regular_files(root / pack_name).items():
            result[f"{pack_name}/{relative}"] = path
    return result


def _parse_sums(pack: Path) -> dict[str, str]:
    sums_path = pack / "SHA256SUMS"
    try:
        lines = sums_path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError):
        raise AuthorityError(f"invalid SHA256SUMS: {pack.name}") from None
    result: dict[str, str] = {}
    for line in lines:
        match = SUM_LINE.fullmatch(line)
        if match is None or match.group("name") in result:
            raise AuthorityError(f"invalid SHA256SUMS line: {pack.name}")
        result[match.group("name")] = match.group("digest")
    return result


def _validate_pack(root: Path, pack_name: str) -> dict[str, Any]:
    pack = root / pack_name
    files = _regular_files(pack)
    sums = _parse_sums(pack)
    expected_names = sorted(name for name in files if name != "SHA256SUMS")
    if set(sums) != set(expected_names):
        raise AuthorityError(f"SHA256SUMS closure mismatch: {pack_name}")
    for name, expected in sums.items():
        if sha256_file(files[name]) != expected:
            raise AuthorityError(f"authority digest mismatch: {pack_name}/{name}")
    try:
        manifest: Any = json.loads(files["manifest.json"].read_text(encoding="utf-8"))
    except (KeyError, OSError, UnicodeError, json.JSONDecodeError):
        raise AuthorityError(f"authority manifest invalid: {pack_name}") from None
    if not isinstance(manifest, dict) or not isinstance(manifest.get("protocol_id"), str):
        raise AuthorityError(f"authority manifest invalid: {pack_name}")
    return {
        "directory": pack_name,
        "protocol_id": manifest["protocol_id"],
        "manifest_sha256": sha256_file(files["manifest.json"]),
        "sha256sums_sha256": sha256_file(files["SHA256SUMS"]),
        "files": [
            {"path": name, "sha256": sha256_file(files[name])}
            for name in sorted(files)
        ],
    }


def authority_index(root: Path) -> dict[str, Any]:
    files = _active_files(root)
    packs = [_validate_pack(root, name) for name in EXPECTED_PACKS]
    digest = hashlib.sha256()
    for relative, path in sorted(files.items()):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256_file(path).encode("ascii"))
        digest.update(b"\n")
    return {
        "schema": "simple-harness-realtime-authority-index-v1",
        "root_digest": {
            "algorithm": ROOT_DIGEST_ALGORITHM,
            "sha256": digest.hexdigest(),
        },
        "packs": packs,
    }


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def check_synced() -> dict[str, Any]:
    source = _active_files(SOURCE_ROOT)
    target = _regular_files(TARGET_ROOT)
    if set(source) != set(target):
        missing = sorted(set(source) - set(target))
        extra = sorted(set(target) - set(source))
        raise AuthorityError(f"packaged authority file set drift; missing={missing}, extra={extra}")
    for relative in source:
        if source[relative].read_bytes() != target[relative].read_bytes():
            raise AuthorityError(f"packaged authority byte drift: {relative}")
    source_index = authority_index(SOURCE_ROOT)
    target_index = authority_index(TARGET_ROOT)
    if source_index != target_index:
        raise AuthorityError("packaged authority index drift")
    try:
        packaged_index: Any = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise AuthorityError("packaged authority index missing or invalid") from None
    if packaged_index != source_index or INDEX_PATH.read_bytes() != canonical_json(source_index):
        raise AuthorityError("packaged authority index bytes drift")
    return source_index


def sync() -> dict[str, Any]:
    source_index = authority_index(SOURCE_ROOT)
    if TARGET_ROOT.exists():
        shutil.rmtree(TARGET_ROOT)
    TARGET_ROOT.mkdir(parents=True)
    for pack_name in EXPECTED_PACKS:
        shutil.copytree(
            SOURCE_ROOT / pack_name,
            TARGET_ROOT / pack_name,
            copy_function=shutil.copyfile,
        )
    INDEX_PATH.write_bytes(canonical_json(source_index))
    return check_synced()


def build_bundle(output: Path, *, authority_root: Path = TARGET_ROOT) -> dict[str, Any]:
    index = authority_index(authority_root)
    files = _active_files(authority_root)
    members: list[tuple[str, bytes]] = [
        (f"{BUNDLE_PREFIX}/authority-index.json", canonical_json(index))
    ]
    members.extend(
        (f"{BUNDLE_PREFIX}/protocols/{relative}", path.read_bytes())
        for relative, path in sorted(files.items())
    )
    raw_tar = io.BytesIO()
    with tarfile.open(fileobj=raw_tar, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, payload in members:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o644
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            archive.addfile(info, io.BytesIO(payload))
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as destination, gzip.GzipFile(
        filename="", mode="wb", compresslevel=9, fileobj=destination, mtime=0
    ) as compressed:
        compressed.write(raw_tar.getvalue())
    return {
        "filename": output.name,
        "sha256": sha256_file(output),
        "authority_root_sha256": index["root_digest"]["sha256"],
        "member_count": len(members),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    parser.add_argument("--bundle", type=Path)
    args = parser.parse_args()
    index = check_synced() if args.check else sync()
    result: dict[str, Any] = {
        "authority_root_sha256": index["root_digest"]["sha256"],
        "pack_count": len(index["packs"]),
    }
    if args.bundle is not None:
        result["bundle"] = build_bundle(args.bundle)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
