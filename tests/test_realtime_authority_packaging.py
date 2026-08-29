from __future__ import annotations

import gzip
import importlib.util
import json
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "ARCHITECTURE/protocols"
PACKAGE_ROOT = ROOT / "src/simple_harness_service/realtime"
PACKAGED_ROOT = PACKAGE_ROOT / "protocols"
SCRIPT = ROOT / "scripts/sync_realtime_authority.py"
SHA256 = "0" * 64


def _load_authority_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sync_realtime_authority_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_build_module() -> ModuleType:
    scripts = str(ROOT / "scripts")
    sys.path.insert(0, scripts)
    try:
        path = ROOT / "scripts/build_candidate.py"
        spec = importlib.util.spec_from_file_location("build_candidate_test", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(scripts)


def _files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _valid_candidate_manifest() -> dict[str, Any]:
    artifact = {
        "kind": "release-evidence",
        "sha256": SHA256,
        "download_url": "https://github.com/example/release/artifact",
    }
    member = {
        "distribution": "distribution",
        "version": "1.0.0",
        "wheel": "distribution-1.0.0-py3-none-any.whl",
        "sha256": SHA256,
        "download_url": "https://github.com/example/release/distribution.whl",
    }
    pack = {
        "directory": "pack",
        "protocol_id": "protocol/1",
        "manifest_sha256": SHA256,
        "sha256sums_sha256": SHA256,
    }
    target_common = {
        "artifact_role": "runtime",
        "filename": "realtime-lock-target.txt",
        "sha256": SHA256,
    }
    evidence = {
        "compatibility_bom_sha256": SHA256,
        "metadata_sha256": SHA256,
        "wheel_metadata_sha256": SHA256,
        "build_info_sha256": SHA256,
        "release_manifest_schema_sha256": SHA256,
        "release_targets_sha256": SHA256,
        "sha256sums_sha256": SHA256,
    }
    return {
        "schema": "simple-harness-service-candidate-manifest-v2",
        "package": {
            "distribution": "simple-harness-service-sdk",
            "version": "0.3.12",
            "planned_tag": "v0.3.12",
            "requires_python": ">=3.11",
        },
        "source": {"commit": "0" * 40, "source_date_epoch": 0},
        "artifacts": {"one": artifact, "two": artifact, "three": artifact},
        "sdk_release_unit": {
            "schema": "simple-harness-three-sdk-release-unit-v1",
            "members": [
                {**member, "role": "service"},
                {**member, "role": "harness"},
                {**member, "role": "memory"},
            ],
        },
        "authority": {
            "root_sha256": SHA256,
            "root_digest_algorithm": "sha256",
            "bundle": "realtime-authority-bundle.tar.gz",
            "bundle_sha256": SHA256,
            "index_sha256": SHA256,
            "packs": [
                {**pack, "directory": f"pack-{index}"} for index in range(4)
            ],
        },
        "python_target_locks": [
            {
                **target_common,
                "id": "simple-harness-py311-macos-arm64",
                "consumer": "simple-harness",
                "python_version": "3.11",
                "python_platform": "aarch64-apple-darwin",
            },
            {
                **target_common,
                "id": "simple-harness-py311-windows-x86-64",
                "consumer": "simple-harness",
                "python_version": "3.11",
                "python_platform": "x86_64-pc-windows-msvc",
            },
            {
                **target_common,
                "id": "aiphone-py313-linux-aarch64",
                "consumer": "aiphone",
                "python_version": "3.13",
                "python_platform": "aarch64-manylinux2014",
            },
        ],
        "evidence": evidence,
        "rollback": {
            "service_sdk_version": "0.2.3",
            "schema_policy": "consumer-pin-only-no-authority-rewrite",
        },
    }


def test_packaged_authority_is_byte_identical_and_indexed() -> None:
    authority = _load_authority_module()
    index = authority.check_synced()

    assert {
        name: path.read_bytes()
        for name, path in authority._active_files(SOURCE_ROOT).items()
    } == _files(PACKAGED_ROOT)
    assert (SOURCE_ROOT / "qwen-native-2026-08-27.1").is_dir()
    assert (SOURCE_ROOT / "tokenseller-realtime-control-2026-08-27.1").is_dir()
    assert index["schema"] == "simple-harness-realtime-authority-index-v1"
    assert index["root_digest"] == {
        "algorithm": authority.ROOT_DIGEST_ALGORITHM,
            "sha256": "b9675a5c64136bb9ba7064cc78b3cc39662f7f374629bcd4731a833bbff2873d",
    }
    assert len(index["packs"]) == 4
    assert (PACKAGE_ROOT / "authority-index.json").read_bytes() == authority.canonical_json(
        index
    )


def test_authority_bundle_is_reproducible_and_normalized(tmp_path: Path) -> None:
    authority = _load_authority_module()
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"

    first_record = authority.build_bundle(first)
    second_record = authority.build_bundle(second)

    assert first.read_bytes() == second.read_bytes()
    assert first_record["sha256"] == second_record["sha256"]
    assert first.read_bytes()[4:8] == b"\0\0\0\0"
    with gzip.open(first, "rb") as compressed, tarfile.open(
        fileobj=compressed, mode="r:"
    ) as archive:
        members = archive.getmembers()
        assert [member.name for member in members] == sorted(
            member.name for member in members
        )
        assert len(members) == 69
        assert all(member.isfile() for member in members)
        assert all(member.mtime == 0 for member in members)
        assert all(member.uid == member.gid == 0 for member in members)
        assert all(member.mode == 0o644 for member in members)
        by_name: dict[str, bytes] = {}
        for member in members:
            stream = archive.extractfile(member)
            assert stream is not None
            by_name[member.name] = stream.read()
    expected = {
        f"realtime-authority/protocols/{name}": payload
        for name, payload in _files(PACKAGED_ROOT).items()
    }
    expected["realtime-authority/authority-index.json"] = (
        PACKAGE_ROOT / "authority-index.json"
    ).read_bytes()
    assert by_name == expected


def test_wheel_contains_exact_authority_bytes_and_release_schema(tmp_path: Path) -> None:
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    wheels = list(tmp_path.glob("*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as archive:
        for relative, expected in _files(PACKAGED_ROOT).items():
            assert (
                archive.read(f"simple_harness_service/realtime/protocols/{relative}")
                == expected
            )
        assert archive.read("simple_harness_service/realtime/authority-index.json") == (
            PACKAGE_ROOT / "authority-index.json"
        ).read_bytes()
        assert archive.read(
            "simple_harness_service/realtime/release-manifest.schema.json"
        ) == (PACKAGE_ROOT / "release-manifest.schema.json").read_bytes()


def test_release_schema_targets_and_dynamic_version_contract() -> None:
    schema = json.loads(
        (PACKAGE_ROOT / "release-manifest.schema.json").read_text(encoding="utf-8")
    )
    targets = json.loads(
        (ROOT / "release/realtime-release-targets.json").read_text(encoding="utf-8")
    )
    with (ROOT / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]
    build_script = (ROOT / "scripts/build_candidate.py").read_text(encoding="utf-8")

    assert schema["properties"]["schema"]["const"] == (
        "simple-harness-service-candidate-manifest-v2"
    )
    assert "sdk_release_unit" in schema["required"]
    assert schema["properties"]["artifacts"]["additionalProperties"]["required"] == [
        "kind",
        "sha256",
        "download_url",
    ]
    assert targets["rollback_service_sdk_version"] == "0.2.3"
    assert {
        (item["consumer"], item["python_version"], item["python_platform"])
        for item in targets["targets"]
    } == {
        ("simple-harness", "3.11", "aarch64-apple-darwin"),
        ("simple-harness", "3.11", "x86_64-pc-windows-msvc"),
        ("aiphone", "3.13", "aarch64-manylinux2014"),
    }
    assert "VERSION =" not in build_script
    assert 'project.get("version")' in build_script
    assert project["requires-python"] == ">=3.11"


def test_candidate_manifest_is_validated_before_write(tmp_path: Path) -> None:
    candidate = _load_build_module()
    manifest = _valid_candidate_manifest()
    destination = tmp_path / "candidate-manifest.json"

    candidate._write_candidate_manifest(
        destination,
        manifest,
        expected_target_ids=candidate.EXPECTED_TARGET_IDS,
    )

    assert json.loads(destination.read_text(encoding="utf-8")) == manifest


def test_candidate_manifest_rejects_duplicate_sdk_role_before_write(tmp_path: Path) -> None:
    candidate = _load_build_module()
    manifest = deepcopy(_valid_candidate_manifest())
    manifest["sdk_release_unit"]["members"][2]["role"] = "harness"
    destination = tmp_path / "candidate-manifest.json"

    with pytest.raises(RuntimeError, match="candidate manifest schema violation"):
        candidate._write_candidate_manifest(
            destination,
            manifest,
            expected_target_ids=candidate.EXPECTED_TARGET_IDS,
        )

    assert not destination.exists()


def test_candidate_manifest_rejects_duplicate_or_extra_target_before_write(
    tmp_path: Path,
) -> None:
    candidate = _load_build_module()
    duplicate = deepcopy(_valid_candidate_manifest())
    duplicate["python_target_locks"][1]["id"] = duplicate["python_target_locks"][0]["id"]
    extra = deepcopy(_valid_candidate_manifest())
    extra["python_target_locks"].append(deepcopy(extra["python_target_locks"][0]))

    for index, manifest in enumerate((duplicate, extra)):
        destination = tmp_path / f"candidate-manifest-{index}.json"
        with pytest.raises(RuntimeError, match="candidate manifest schema violation"):
            candidate._write_candidate_manifest(
                destination,
                manifest,
                expected_target_ids=candidate.EXPECTED_TARGET_IDS,
            )
        assert not destination.exists()


def test_candidate_manifest_rejects_unknown_property() -> None:
    candidate = _load_build_module()
    manifest = _valid_candidate_manifest()
    manifest["unexpected"] = True

    with pytest.raises(RuntimeError, match="unexpected property"):
        candidate._validate_candidate_manifest(
            manifest,
            expected_target_ids=candidate.EXPECTED_TARGET_IDS,
        )


def test_release_scripts_pass_strict_mypy() -> None:
    subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "scripts/build_candidate.py",
            "scripts/sync_realtime_authority.py",
        ],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )


def test_three_sdk_release_unit_and_future_download_urls(tmp_path: Path) -> None:
    candidate = _load_build_module()
    wheel = tmp_path / "simple_harness_service_sdk-0.3.12-py3-none-any.whl"
    wheel.write_bytes(b"candidate-wheel-bytes")

    unit = candidate._sdk_release_unit(
        metadata={
            "distribution": "simple-harness-service-sdk",
            "version": "0.3.12",
            "requires_python": ">=3.11",
        },
        planned_tag="v0.3.12",
        wheel=wheel,
    )

    assert unit["schema"] == "simple-harness-three-sdk-release-unit-v1"
    assert [member["role"] for member in unit["members"]] == [
        "service",
        "harness",
        "memory",
    ]
    assert [member["version"] for member in unit["members"]] == [
        "0.3.12",
        "0.6.2",
        "0.5.2",
    ]
    assert unit["members"][0]["download_url"] == (
        "https://github.com/DennyWanye/simple-harness-service-sdk/releases/download/"
        "v0.3.12/simple_harness_service_sdk-0.3.12-py3-none-any.whl"
    )
    assert all(
        member["download_url"].startswith("https://github.com/")
        for member in unit["members"]
    )
    assert all(len(member["sha256"]) == 64 for member in unit["members"])
