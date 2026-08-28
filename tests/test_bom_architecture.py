from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import simple_harness_service
from simple_harness_service import (
    ServiceError,
    load_bom,
    validate_installed_bom,
    validate_metadata_requirements,
)
from simple_harness_service.bom import _validate_direct_url

ROOT = Path(__file__).resolve().parents[1]


def test_public_api_snapshot() -> None:
    expected = json.loads((ROOT / "tests/public-api.json").read_text(encoding="utf-8"))
    assert sorted(simple_harness_service.__all__) == sorted(expected)


def test_compatibility_bom_and_installed_harness_provenance() -> None:
    bom = load_bom()
    assert bom["service"]["version"] == "0.3.9"
    assert bom["harness"]["version"] == "0.6.2"
    assert bom["memory"]["version"] == "0.5.2"
    assert bom["harness"]["execution_schema"] == 6
    assert bom["memory"]["memory_schema"] == 4
    # The uv development environment deliberately demonstrates fail-closed behavior:
    # its direct_url.json retains the URL but drops the archive digest.
    with pytest.raises(ServiceError, match="SHA provenance missing"):
        validate_installed_bom()


def test_thin_architecture_gate() -> None:
    result = subprocess.run(
        ["python3", "scripts/check_architecture.py"],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    assert result.stdout.strip() == "THIN_ARCHITECTURE_PASS"


def test_metadata_validator_rejects_lookalike_distribution() -> None:
    bom = load_bom()
    harness = bom["harness"]
    memory = bom["memory"]
    requirements = [
        f"simple-harness-sdk-evil @ {harness['url']}#sha256={harness['sha256']}",
        f"simple-harness-memory-sdk @ {memory['url']}#sha256={memory['sha256']} ; "
        "extra == 'memory'",
    ]
    with pytest.raises(ServiceError):
        validate_metadata_requirements(requirements)


@pytest.mark.parametrize(
    "archive_info",
    [
        {},
        {"hashes": {}},
        {"hashes": {"sha256": "0" * 64}},
        {"hashes": {"sha256": "d" * 64, "sha512": "e" * 128}},
        {"hash": "sha512=" + "e" * 128, "hashes": {"sha256": "d" * 64}},
    ],
)
def test_direct_url_requires_one_exact_sha256(archive_info: object) -> None:
    item = {"url": "https://example.invalid/wheel.whl", "sha256": "d" * 64}
    direct_url = json.dumps({"url": item["url"], "archive_info": archive_info})
    with pytest.raises(ServiceError):
        _validate_direct_url("harness", item, direct_url)


def test_direct_url_accepts_only_exact_url_and_bytes_identity() -> None:
    item = {"url": "https://example.invalid/wheel.whl", "sha256": "d" * 64}
    valid = json.dumps(
        {"url": item["url"], "archive_info": {"hashes": {"sha256": item["sha256"]}}}
    )
    _validate_direct_url("harness", item, valid)
    pip_valid = json.dumps(
        {
            "url": item["url"],
            "archive_info": {
                "hash": f"sha256={item['sha256']}",
                "hashes": {"sha256": item["sha256"]},
            },
        }
    )
    _validate_direct_url("harness", item, pip_valid)
    wrong_url = valid.replace("wheel.whl", "other.whl")
    with pytest.raises(ServiceError):
        _validate_direct_url("harness", item, wrong_url)
