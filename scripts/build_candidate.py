#!/usr/bin/env python3
"""Build a byte-reproducible, content-addressable 0.1.1 candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.1.1"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


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


def build(output: Path, *, planned_tag: str) -> dict[str, object]:
    if planned_tag != f"v{VERSION}":
        raise RuntimeError("planned tag differs from package version")
    if output.exists() and any(output.iterdir()):
        raise RuntimeError("candidate output directory must be empty")
    dirty = run("git", "status", "--porcelain=v1", "--untracked-files=all")
    if dirty:
        raise RuntimeError("candidate inputs must be committed and clean")
    commit = run("git", "rev-parse", "HEAD")
    env = dict(os.environ, SOURCE_DATE_EPOCH="0", PYTHONHASHSEED="0")
    with tempfile.TemporaryDirectory(prefix="simple-harness-service-repro-") as raw:
        temporary = Path(raw)
        builds = [temporary / "first", temporary / "second"]
        hashes: list[dict[str, str]] = []
        for directory in builds:
            directory.mkdir()
            run("uv", "build", "--out-dir", str(directory), env=env)
            distributions = sorted(
                path
                for path in directory.iterdir()
                if path.name.endswith((".whl", ".tar.gz"))
            )
            if len(distributions) != 2:
                raise RuntimeError("build must produce exactly one wheel and one sdist")
            hashes.append({path.name: sha256(path) for path in distributions})
        if hashes[0] != hashes[1]:
            raise RuntimeError("candidate distributions are not byte-for-byte reproducible")
        output.mkdir(parents=True, exist_ok=True)
        for name in hashes[0]:
            shutil.copy2(builds[0] / name, output / name)

    wheel = next(output.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        metadata_name = f"simple_harness_service_sdk-{VERSION}.dist-info/METADATA"
        metadata_bytes = archive.read(metadata_name)
        wheel_bytes = archive.read(f"simple_harness_service_sdk-{VERSION}.dist-info/WHEEL")
    (output / "METADATA").write_bytes(metadata_bytes)
    (output / "WHEEL").write_bytes(wheel_bytes)
    shutil.copy2(
        ROOT / "src/simple_harness_service/compatibility-bom.json",
        output / "compatibility-bom.json",
    )
    artifacts = {
        path.name: sha256(path)
        for path in sorted(output.iterdir())
        if path.is_file()
    }
    build_info = {
        "schema": "simple-harness-service-build-info-v1",
        "version": VERSION,
        "planned_tag": planned_tag,
        "commit": commit,
        "source_date_epoch": 0,
        "artifacts": {
            name: digest
            for name, digest in artifacts.items()
            if name.endswith((".whl", ".tar.gz"))
        },
    }
    (output / "BUILD_INFO.json").write_text(
        json.dumps(build_info, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    sums = {
        path.name: sha256(path)
        for path in sorted(output.iterdir())
        if path.is_file()
    }
    (output / "SHA256SUMS").write_text(
        "".join(f"{digest}  {name}\n" for name, digest in sorted(sums.items())),
        encoding="utf-8",
    )
    manifest = {
        "schema": "simple-harness-service-candidate-manifest-v1",
        "version": VERSION,
        "planned_tag": planned_tag,
        "commit": commit,
        "artifacts": build_info["artifacts"],
        "bom_sha256": sha256(output / "compatibility-bom.json"),
        "metadata_sha256": sha256(output / "METADATA"),
        "wheel_metadata_sha256": sha256(output / "WHEEL"),
        "build_info_sha256": sha256(output / "BUILD_INFO.json"),
        "sha256sums_sha256": sha256(output / "SHA256SUMS"),
    }
    (output / "candidate-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--planned-tag", default=f"v{VERSION}")
    args = parser.parse_args()
    print(json.dumps(build(args.output, planned_tag=args.planned_tag), sort_keys=True))


if __name__ == "__main__":
    main()
