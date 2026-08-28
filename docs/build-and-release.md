# Build and release

The release authority is the locally produced byte-reproducible candidate. GitHub Releases may
only distribute those frozen bytes; they must not rebuild or overwrite them.

```bash
uv sync --frozen --group dev --extra memory --extra realtime
uv run --frozen pytest -q
uv run --frozen ruff check src tests scripts
uv run --frozen mypy src/simple_harness_service scripts/build_candidate.py scripts/sync_realtime_authority.py
python3 scripts/check_architecture.py
python3 scripts/sync_realtime_authority.py --check
python3 scripts/build_candidate.py --output /tmp/svc-candidate --planned-tag v0.3.6
uv run --frozen twine check /tmp/svc-candidate/*.whl \
  /tmp/svc-candidate/simple_harness_service_sdk-*.tar.gz
```

The candidate builder requires a committed clean tree. It performs two distribution builds, two
authority-bundle builds, and two dependency-lock compilations and rejects any byte drift. The
manifest must bind the exact service 0.3.6, Harness 0.6.2, and Memory 0.5.2 release-unit wheels,
the four-pack authority root, and all three consumer target locks. Before writing the candidate
manifest, the builder validates the complete document against the packaged offline schema and
enforces one `service`, `harness`, and `memory` member plus the exact three unique target IDs.

Before promotion, install the exact wheel with its `memory,realtime` extras in clean Python 3.11
and 3.13 environments, run `validate_installed_bom(include_memory=True)`, compare the packaged
authority bytes with the release bundle, and download every published asset back. Never move a tag
or use asset clobber. Use standard `python -m pip install` with the exact `URL#sha256=...` for the
installed-provenance gate. `uv` currently drops the archive hash from PEP 610 `direct_url.json` and
therefore fails closed by design; a URL-only record is not release provenance.
