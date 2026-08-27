from __future__ import annotations

import zipfile
from pathlib import Path

import pytest


@pytest.mark.parametrize("suffix", [".so", ".dylib", ".pyd"])
def test_source_tree_contains_no_native_runtime(suffix: str) -> None:
    root = Path(__file__).resolve().parents[1]
    assert not list((root / "src").rglob(f"*{suffix}"))


def assert_pure_wheel(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert not any(name.endswith((".so", ".dylib", ".pyd")) for name in names)
        wheel_metadata = archive.read(
            "simple_harness_service_sdk-0.3.1.dist-info/WHEEL"
        ).decode()
        assert "Root-Is-Purelib: true" in wheel_metadata
        assert "Tag: py3-none-any" in wheel_metadata
        assert "simple_harness_service/compatibility-bom.json" in names
