#!/usr/bin/env python3
"""Fail closed if the thin service grows a database, worker, or private SDK dependency."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "simple_harness_service"
FORBIDDEN_IMPORT_ROOTS = {
    "sqlite3",
    "sqlalchemy",
    "django",
    "peewee",
    "celery",
    "redis",
    "aiphone_agent_runtime",
}
FORBIDDEN_TEXT = ("CREATE TABLE", "PRAGMA ", ".sqlite", "durable_worker")


def violations() -> list[str]:
    found: list[str] = []
    for path in sorted(SOURCE.rglob("*.py")):
        relative = path.relative_to(ROOT)
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(relative))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                modules = (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                )
                for module in modules:
                    root = module.split(".", 1)[0]
                    if root in FORBIDDEN_IMPORT_ROOTS or module.startswith(
                        "simple_harness_memory"
                    ):
                        found.append(f"{relative}:{node.lineno}: forbidden import {module}")
                    if module.startswith("simple_harness."):
                        found.append(f"{relative}:{node.lineno}: private Harness import {module}")
            if (
                isinstance(node, ast.While)
                and isinstance(node.test, ast.Constant)
                and node.test.value is True
                and "transports" not in relative.parts
                and path.name != "cli.py"
            ):
                found.append(f"{relative}:{node.lineno}: unbounded worker loop")
        for marker in FORBIDDEN_TEXT:
            if marker.lower() in text.lower():
                found.append(f"{relative}: forbidden persistence marker {marker}")
        if "worker" in path.stem.lower():
            found.append(f"{relative}: worker module is forbidden")
    return found


def main() -> int:
    found = violations()
    if found:
        print("\n".join(found), file=sys.stderr)
        return 1
    print("THIN_ARCHITECTURE_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
