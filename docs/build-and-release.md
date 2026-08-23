# Build and release

The release authority is the locally produced byte-reproducible candidate. GitHub Releases may
only distribute those frozen bytes; they must not rebuild or overwrite them.

```bash
uv sync --frozen --group dev
uv run --frozen pytest -q
uv run --frozen ruff check .
uv run --frozen mypy src tests
python3 scripts/check_architecture.py
python3 scripts/build_candidate.py --output /tmp/svc-candidate --planned-tag v0.1.2
uv run --frozen twine check /tmp/svc-candidate/*.whl /tmp/svc-candidate/*.tar.gz
```

Before promotion, install the exact wheel with its `memory` extra in clean Python 3.11 and 3.13
environments, run `validate_installed_bom(include_memory=True)`, compare a clean rebuild byte for
byte, and download every published asset back. Never move a tag or use asset clobber.
