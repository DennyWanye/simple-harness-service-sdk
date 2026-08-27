#!/usr/bin/env python3
"""Build a byte-reproducible, content-addressable service SDK candidate."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
import zipfile
from collections.abc import Mapping
from email.parser import BytesParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sync_realtime_authority import (
    INDEX_PATH,
    ROOT_DIGEST_ALGORITHM,
    build_bundle,
    check_synced,
    sha256_file,
)

ROOT = Path(__file__).resolve().parents[1]
PROJECT_FILE = ROOT / "pyproject.toml"
COMPATIBILITY_BOM = ROOT / "src/simple_harness_service/compatibility-bom.json"
RELEASE_SCHEMA = (
    ROOT / "src/simple_harness_service/realtime/release-manifest.schema.json"
)
RELEASE_TARGETS = ROOT / "release/realtime-release-targets.json"
AUTHORITY_BUNDLE_NAME = "realtime-authority-bundle.tar.gz"
RELEASE_REPOSITORY_URL = (
    "https://github.com/DennyWanye/simple-harness-service-sdk/releases/download"
)
EXPECTED_TARGET_IDS = frozenset(
    {
        "aiphone-py313-linux-aarch64",
        "simple-harness-py311-macos-arm64",
        "simple-harness-py311-windows-x86-64",
    }
)
EXPECTED_SDK_ROLES = frozenset({"service", "harness", "memory"})
_SUPPORTED_SCHEMA_KEYS = frozenset(
    {
        "$id",
        "$schema",
        "additionalProperties",
        "allOf",
        "const",
        "contains",
        "enum",
        "items",
        "maxContains",
        "maxItems",
        "minContains",
        "minItems",
        "minLength",
        "minProperties",
        "pattern",
        "properties",
        "required",
        "title",
        "type",
        "uniqueItems",
    }
)


def sha256(path: Path) -> str:
    """Backward-compatible helper retained for existing release callers."""

    return sha256_file(path)


def run(*args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        args,
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
    )
    return result.stdout.strip()


def _read_json(path: Path, *, schema: str) -> dict[str, Any]:
    try:
        value: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise RuntimeError(f"invalid release input: {path.name}") from None
    if not isinstance(value, dict) or value.get("schema") != schema:
        raise RuntimeError(f"invalid release input: {path.name}")
    return value


def _read_release_schema() -> dict[str, Any]:
    try:
        value: Any = json.loads(RELEASE_SCHEMA.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise RuntimeError("candidate manifest schema invalid") from None
    if (
        not isinstance(value, dict)
        or value.get("$schema") != "https://json-schema.org/draft/2020-12/schema"
    ):
        raise RuntimeError("candidate manifest schema invalid")
    _check_supported_schema(value, path="$schema")
    return value


def _check_supported_schema(schema: Mapping[str, Any], *, path: str) -> None:
    unsupported = set(schema) - _SUPPORTED_SCHEMA_KEYS
    if unsupported:
        raise RuntimeError(
            f"candidate manifest schema uses unsupported keywords at {path}: "
            f"{sorted(unsupported)}"
        )
    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, dict):
            raise RuntimeError(f"candidate manifest schema invalid at {path}.properties")
        for name, child in properties.items():
            if not isinstance(name, str) or not isinstance(child, dict):
                raise RuntimeError(f"candidate manifest schema invalid at {path}.properties")
            _check_supported_schema(child, path=f"{path}.properties.{name}")
    for keyword in ("additionalProperties", "items", "contains"):
        child = schema.get(keyword)
        if isinstance(child, dict):
            _check_supported_schema(child, path=f"{path}.{keyword}")
        elif child is not None and (
            keyword != "additionalProperties" or not isinstance(child, bool)
        ):
            raise RuntimeError(f"candidate manifest schema invalid at {path}.{keyword}")
    all_of = schema.get("allOf")
    if all_of is not None:
        if not isinstance(all_of, list) or not all_of:
            raise RuntimeError(f"candidate manifest schema invalid at {path}.allOf")
        for index, child in enumerate(all_of):
            if not isinstance(child, dict):
                raise RuntimeError(f"candidate manifest schema invalid at {path}.allOf")
            _check_supported_schema(child, path=f"{path}.allOf[{index}]")


def _matches_type(value: object, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    raise RuntimeError(f"candidate manifest schema type unsupported: {expected}")


def _schema_violation(path: str, message: str) -> None:
    raise RuntimeError(f"candidate manifest schema violation at {path}: {message}")


def _validate_schema_value(value: object, schema: Mapping[str, Any], *, path: str) -> None:
    expected_type = schema.get("type")
    if expected_type is not None and (
        not isinstance(expected_type, str) or not _matches_type(value, expected_type)
    ):
        _schema_violation(path, f"expected {expected_type}")
    if "const" in schema and value != schema["const"]:
        _schema_violation(path, "const mismatch")
    enum = schema.get("enum")
    if enum is not None and (not isinstance(enum, list) or value not in enum):
        _schema_violation(path, "enum mismatch")

    if isinstance(value, str):
        minimum = schema.get("minLength")
        if minimum is not None and (not isinstance(minimum, int) or len(value) < minimum):
            _schema_violation(path, "string too short")
        pattern = schema.get("pattern")
        if pattern is not None and (
            not isinstance(pattern, str) or re.search(pattern, value) is None
        ):
            _schema_violation(path, "pattern mismatch")

    if isinstance(value, dict):
        minimum_properties = schema.get("minProperties")
        if minimum_properties is not None and (
            not isinstance(minimum_properties, int) or len(value) < minimum_properties
        ):
            _schema_violation(path, "not enough properties")
        required = schema.get("required", [])
        if not isinstance(required, list) or not all(
            isinstance(name, str) for name in required
        ):
            raise RuntimeError(f"candidate manifest schema invalid at {path}.required")
        missing = [name for name in required if name not in value]
        if missing:
            _schema_violation(path, f"missing properties {missing}")
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise RuntimeError(f"candidate manifest schema invalid at {path}.properties")
        additional = schema.get("additionalProperties", True)
        for name, item in value.items():
            child = properties.get(name)
            if isinstance(child, dict):
                _validate_schema_value(item, child, path=f"{path}.{name}")
            elif additional is False:
                _schema_violation(path, f"unexpected property {name}")
            elif isinstance(additional, dict):
                _validate_schema_value(item, additional, path=f"{path}.{name}")

    if isinstance(value, list):
        minimum_items = schema.get("minItems")
        maximum_items = schema.get("maxItems")
        if minimum_items is not None and (
            not isinstance(minimum_items, int) or len(value) < minimum_items
        ):
            _schema_violation(path, "not enough items")
        if maximum_items is not None and (
            not isinstance(maximum_items, int) or len(value) > maximum_items
        ):
            _schema_violation(path, "too many items")
        if schema.get("uniqueItems") is True:
            identities = [
                json.dumps(item, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                for item in value
            ]
            if len(identities) != len(set(identities)):
                _schema_violation(path, "duplicate items")
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                _validate_schema_value(item, items, path=f"{path}[{index}]")
        contains = schema.get("contains")
        if isinstance(contains, dict):
            matches = 0
            for item in value:
                try:
                    _validate_schema_value(item, contains, path=path)
                except RuntimeError as error:
                    if not str(error).startswith("candidate manifest schema violation"):
                        raise
                else:
                    matches += 1
            minimum_contains = schema.get("minContains", 1)
            maximum_contains = schema.get("maxContains")
            if not isinstance(minimum_contains, int) or matches < minimum_contains:
                _schema_violation(path, "contains minimum not met")
            if maximum_contains is not None and (
                not isinstance(maximum_contains, int) or matches > maximum_contains
            ):
                _schema_violation(path, "contains maximum exceeded")

    all_of = schema.get("allOf", [])
    if not isinstance(all_of, list):
        raise RuntimeError(f"candidate manifest schema invalid at {path}.allOf")
    for child in all_of:
        if not isinstance(child, dict):
            raise RuntimeError(f"candidate manifest schema invalid at {path}.allOf")
        _validate_schema_value(value, child, path=path)


def _validate_candidate_manifest(
    manifest: Mapping[str, object], *, expected_target_ids: frozenset[str]
) -> None:
    _validate_schema_value(manifest, _read_release_schema(), path="$")
    release_unit = manifest.get("sdk_release_unit")
    members = release_unit.get("members") if isinstance(release_unit, dict) else None
    roles = [item.get("role") for item in members] if isinstance(members, list) else []
    if len(roles) != 3 or set(roles) != EXPECTED_SDK_ROLES:
        raise RuntimeError("candidate manifest SDK roles invalid")
    locks = manifest.get("python_target_locks")
    target_ids = [item.get("id") for item in locks] if isinstance(locks, list) else []
    if (
        len(target_ids) != 3
        or len(set(target_ids)) != 3
        or set(target_ids) != expected_target_ids
    ):
        raise RuntimeError("candidate manifest target locks invalid")


def _write_candidate_manifest(
    path: Path,
    manifest: Mapping[str, object],
    *,
    expected_target_ids: frozenset[str],
) -> None:
    _validate_candidate_manifest(manifest, expected_target_ids=expected_target_ids)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def project_metadata() -> dict[str, str]:
    with PROJECT_FILE.open("rb") as stream:
        document = tomllib.load(stream)
    project = document.get("project")
    if not isinstance(project, dict):
        raise RuntimeError("project metadata missing")
    name = project.get("name")
    version = project.get("version")
    requires_python = project.get("requires-python")
    optional = project.get("optional-dependencies")
    if (
        name != "simple-harness-service-sdk"
        or not isinstance(version, str)
        or not version
        or not isinstance(requires_python, str)
        or not isinstance(optional, dict)
        or "realtime" not in optional
    ):
        raise RuntimeError("project release metadata is incomplete")
    return {
        "distribution": name,
        "version": version,
        "requires_python": requires_python,
    }


def release_targets() -> tuple[dict[str, Any], list[dict[str, str]]]:
    policy = _read_json(
        RELEASE_TARGETS, schema="simple-harness-service-realtime-targets-v1"
    )
    cutoff = policy.get("dependency_cutoff")
    rollback = policy.get("rollback_service_sdk_version")
    targets = policy.get("targets")
    if (
        not isinstance(cutoff, str)
        or not cutoff.endswith("Z")
        or not isinstance(rollback, str)
        or not rollback
        or not isinstance(targets, list)
        or len(targets) != 3
    ):
        raise RuntimeError("release target policy is incomplete")
    required = {
        "id",
        "consumer",
        "python_version",
        "python_platform",
        "artifact_role",
    }
    normalized: list[dict[str, str]] = []
    for target in targets:
        if (
            not isinstance(target, dict)
            or set(target) != required
            or any(not isinstance(target[key], str) or not target[key] for key in required)
        ):
            raise RuntimeError("release target policy is incomplete")
        normalized.append({key: target[key] for key in sorted(required)})
    ids = [target["id"] for target in normalized]
    if len(ids) != len(set(ids)):
        raise RuntimeError("release target ids must be unique")
    return policy, normalized


def _validate_lock(path: Path) -> None:
    try:
        payload = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise RuntimeError(f"target lock invalid: {path.name}") from None
    if (
        not payload.endswith("\n")
        or "simple-harness-service-sdk" in payload
        or str(ROOT) in payload
        or "file://" in payload
        or ("--hash=sha256:" not in payload and "#sha256=" not in payload)
    ):
        raise RuntimeError(f"target lock invalid: {path.name}")


def compile_target_locks(
    output: Path,
    *,
    policy: dict[str, Any],
    targets: list[dict[str, str]],
    env: dict[str, str],
) -> list[dict[str, str]]:
    output.mkdir(parents=True)
    result: list[dict[str, str]] = []
    for target in targets:
        filename = f"realtime-lock-{target['id']}.txt"
        destination = output / filename
        run(
            "uv",
            "pip",
            "compile",
            str(PROJECT_FILE),
            "--extra",
            "realtime",
            "--python-version",
            target["python_version"],
            "--python-platform",
            target["python_platform"],
            "--only-binary",
            ":all:",
            "--generate-hashes",
            "--no-header",
            "--no-annotate",
            "--no-emit-package",
            "simple-harness-service-sdk",
            "--exclude-newer",
            str(policy["dependency_cutoff"]),
            "--output-file",
            str(destination),
            env=env,
        )
        _validate_lock(destination)
        result.append(
            {
                **target,
                "filename": filename,
                "sha256": sha256_file(destination),
            }
        )
    return result


def _artifact_kind(name: str) -> str:
    if name.endswith(".whl"):
        return "python-wheel"
    if name.endswith(".tar.gz") and name != AUTHORITY_BUNDLE_NAME:
        return "python-sdist"
    if name == AUTHORITY_BUNDLE_NAME:
        return "cross-language-authority-bundle"
    if name.startswith("realtime-lock-"):
        return "python-target-lock"
    return "release-evidence"


def _release_download_url(planned_tag: str, filename: str) -> str:
    if Path(filename).name != filename:
        raise RuntimeError("release artifact filename invalid")
    return f"{RELEASE_REPOSITORY_URL}/{planned_tag}/{filename}"


def _artifact_record(path: Path, planned_tag: str) -> dict[str, str]:
    return {
        "kind": _artifact_kind(path.name),
        "sha256": sha256_file(path),
        "download_url": _release_download_url(planned_tag, path.name),
    }


def _sdk_release_unit(
    *, metadata: dict[str, str], planned_tag: str, wheel: Path
) -> dict[str, object]:
    bom = _read_json(COMPATIBILITY_BOM, schema="simple-harness-service-bom-v1")
    service = bom.get("service")
    if (
        not isinstance(service, dict)
        or service.get("distribution") != metadata["distribution"]
        or service.get("version") != metadata["version"]
    ):
        raise RuntimeError("service compatibility BOM mismatch")
    members: list[dict[str, str]] = [
        {
            "role": "service",
            "distribution": metadata["distribution"],
            "version": metadata["version"],
            "wheel": wheel.name,
            "sha256": sha256_file(wheel),
            "download_url": _release_download_url(planned_tag, wheel.name),
        }
    ]
    for role in ("harness", "memory"):
        item = bom.get(role)
        if not isinstance(item, dict):
            raise RuntimeError("three-SDK compatibility BOM mismatch")
        distribution = item.get("distribution")
        version = item.get("version")
        url = item.get("url")
        digest = item.get("sha256")
        wheel_name = Path(urlparse(str(url)).path).name
        if (
            not isinstance(distribution, str)
            or not isinstance(version, str)
            or not isinstance(url, str)
            or not url.startswith("https://github.com/")
            or not isinstance(digest, str)
            or len(digest) != 64
            or not wheel_name.endswith(".whl")
        ):
            raise RuntimeError("three-SDK compatibility BOM mismatch")
        members.append(
            {
                "role": role,
                "distribution": distribution,
                "version": version,
                "wheel": wheel_name,
                "sha256": digest,
                "download_url": url,
            }
        )
    return {
        "schema": "simple-harness-three-sdk-release-unit-v1",
        "members": members,
    }


def _dist_metadata(wheel: Path, expected_version: str) -> tuple[bytes, bytes]:
    with zipfile.ZipFile(wheel) as archive:
        metadata_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        wheel_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/WHEEL")
        ]
        if len(metadata_names) != 1 or len(wheel_names) != 1:
            raise RuntimeError("wheel metadata closure invalid")
        metadata_bytes = archive.read(metadata_names[0])
        wheel_bytes = archive.read(wheel_names[0])
    parsed = BytesParser().parsebytes(metadata_bytes)
    if (
        parsed.get("Name") != "simple-harness-service-sdk"
        or parsed.get("Version") != expected_version
    ):
        raise RuntimeError("wheel project metadata mismatch")
    return metadata_bytes, wheel_bytes


def _copy_release_input(source: Path, output: Path) -> None:
    if not source.is_file() or source.is_symlink():
        raise RuntimeError(f"release input missing: {source.name}")
    shutil.copyfile(source, output / source.name)


def build(output: Path, *, planned_tag: str) -> dict[str, object]:
    metadata = project_metadata()
    version = metadata["version"]
    if planned_tag != f"v{version}":
        raise RuntimeError("planned tag differs from package version")
    if output.exists() and any(output.iterdir()):
        raise RuntimeError("candidate output directory must be empty")
    authority = check_synced()
    policy, targets = release_targets()
    dirty = run("git", "status", "--porcelain=v1", "--untracked-files=all")
    if dirty:
        raise RuntimeError("candidate inputs must be committed and clean")
    commit = run("git", "rev-parse", "HEAD")
    if len(commit) != 40:
        raise RuntimeError("candidate source commit invalid")
    env = dict(os.environ, SOURCE_DATE_EPOCH="0", PYTHONHASHSEED="0")
    with tempfile.TemporaryDirectory(prefix="simple-harness-service-repro-") as raw:
        temporary = Path(raw)
        distribution_builds = [temporary / "dist-first", temporary / "dist-second"]
        distribution_hashes: list[dict[str, str]] = []
        for directory in distribution_builds:
            directory.mkdir()
            run("uv", "build", "--out-dir", str(directory), env=env)
            distributions = sorted(
                path
                for path in directory.iterdir()
                if path.name.endswith((".whl", ".tar.gz"))
            )
            if len(distributions) != 2:
                raise RuntimeError("build must produce exactly one wheel and one sdist")
            distribution_hashes.append(
                {path.name: sha256_file(path) for path in distributions}
            )
        if distribution_hashes[0] != distribution_hashes[1]:
            raise RuntimeError("candidate distributions are not byte-for-byte reproducible")

        bundle_paths = [
            temporary / "authority-first.tar.gz",
            temporary / "authority-second.tar.gz",
        ]
        bundle_records = [build_bundle(path) for path in bundle_paths]
        if bundle_records[0]["sha256"] != bundle_records[1]["sha256"]:
            raise RuntimeError("authority bundle is not byte-for-byte reproducible")

        lock_roots = [temporary / "locks-first", temporary / "locks-second"]
        lock_records = [
            compile_target_locks(root, policy=policy, targets=targets, env=env)
            for root in lock_roots
        ]
        if lock_records[0] != lock_records[1]:
            raise RuntimeError("target dependency locks are not byte-for-byte reproducible")

        output.mkdir(parents=True, exist_ok=True)
        for name in distribution_hashes[0]:
            shutil.copyfile(distribution_builds[0] / name, output / name)
        shutil.copyfile(bundle_paths[0], output / AUTHORITY_BUNDLE_NAME)
        for lock in lock_records[0]:
            shutil.copyfile(lock_roots[0] / lock["filename"], output / lock["filename"])

    wheel = next(output.glob("*.whl"))
    metadata_bytes, wheel_bytes = _dist_metadata(wheel, version)
    (output / "METADATA").write_bytes(metadata_bytes)
    (output / "WHEEL").write_bytes(wheel_bytes)
    for source in (COMPATIBILITY_BOM, INDEX_PATH, RELEASE_SCHEMA, RELEASE_TARGETS):
        _copy_release_input(source, output)

    sdk_release_unit = _sdk_release_unit(
        metadata=metadata, planned_tag=planned_tag, wheel=wheel
    )
    initial_artifacts = {
        path.name: _artifact_record(path, planned_tag)
        for path in sorted(output.iterdir())
        if path.is_file()
    }
    build_info = {
        "schema": "simple-harness-service-build-info-v2",
        "package": {**metadata, "planned_tag": planned_tag},
        "source": {"commit": commit, "source_date_epoch": 0},
        "artifacts": initial_artifacts,
        "sdk_release_unit": sdk_release_unit,
        "authority_root_sha256": authority["root_digest"]["sha256"],
        "python_target_locks": lock_records[0],
    }
    (output / "BUILD_INFO.json").write_text(
        json.dumps(build_info, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    sums = {
        path.name: sha256_file(path)
        for path in sorted(output.iterdir())
        if path.is_file()
    }
    (output / "SHA256SUMS").write_text(
        "".join(f"{digest}  {name}\n" for name, digest in sorted(sums.items())),
        encoding="utf-8",
    )
    manifest_artifacts = {
        path.name: _artifact_record(path, planned_tag)
        for path in sorted(output.iterdir())
        if path.is_file()
    }
    manifest: dict[str, object] = {
        "schema": "simple-harness-service-candidate-manifest-v2",
        "package": {**metadata, "planned_tag": planned_tag},
        "source": {"commit": commit, "source_date_epoch": 0},
        "artifacts": manifest_artifacts,
        "sdk_release_unit": sdk_release_unit,
        "authority": {
            "root_sha256": authority["root_digest"]["sha256"],
            "root_digest_algorithm": ROOT_DIGEST_ALGORITHM,
            "bundle": AUTHORITY_BUNDLE_NAME,
            "bundle_sha256": sha256_file(output / AUTHORITY_BUNDLE_NAME),
            "index_sha256": sha256_file(output / INDEX_PATH.name),
            "packs": [
                {
                    key: pack[key]
                    for key in (
                        "directory",
                        "protocol_id",
                        "manifest_sha256",
                        "sha256sums_sha256",
                    )
                }
                for pack in authority["packs"]
            ],
        },
        "python_target_locks": lock_records[0],
        "evidence": {
            "compatibility_bom_sha256": sha256_file(output / COMPATIBILITY_BOM.name),
            "metadata_sha256": sha256_file(output / "METADATA"),
            "wheel_metadata_sha256": sha256_file(output / "WHEEL"),
            "build_info_sha256": sha256_file(output / "BUILD_INFO.json"),
            "release_manifest_schema_sha256": sha256_file(output / RELEASE_SCHEMA.name),
            "release_targets_sha256": sha256_file(output / RELEASE_TARGETS.name),
            "sha256sums_sha256": sha256_file(output / "SHA256SUMS"),
        },
        "rollback": {
            "service_sdk_version": policy["rollback_service_sdk_version"],
            "schema_policy": "consumer-pin-only-no-authority-rewrite",
        },
    }
    _write_candidate_manifest(
        output / "candidate-manifest.json",
        manifest,
        expected_target_ids=frozenset(target["id"] for target in targets),
    )
    return manifest


def main() -> None:
    metadata = project_metadata()
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--planned-tag", default=f"v{metadata['version']}")
    args = parser.parse_args()
    print(json.dumps(build(args.output, planned_tag=args.planned_tag), sort_keys=True))


if __name__ == "__main__":
    main()
