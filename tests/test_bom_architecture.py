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

ROOT = Path(__file__).resolve().parents[1]


def test_public_api_snapshot() -> None:
    expected = json.loads((ROOT / "tests/public-api.json").read_text(encoding="utf-8"))
    assert sorted(simple_harness_service.__all__) == sorted(expected)


def test_compatibility_bom_and_installed_harness_provenance() -> None:
    bom = load_bom()
    assert bom["service"]["version"] == "0.1.0"
    assert bom["harness"]["execution_schema"] == 5
    assert bom["memory"]["memory_schema"] == 4
    assert validate_installed_bom() == {"service": "0.1.0", "harness": "0.5.0"}


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
