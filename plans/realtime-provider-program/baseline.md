# Realtime Provider Program baseline

Captured on 2026-08-27 before business implementation. Existing red results are frozen as evidence, not accepted as release-green and not attributed to this program. Product repositories' own known-failure files were not changed.

## Frozen source heads

| Repository | Head | Branch at capture |
|---|---|---|
| `simple-harness-service-sdk` | `f966c6c3ca7d36f5cebb88656334871b8451c3e6` | `main` |
| `TokenSeller` | `136b2002014fe2cf7a3e8206f2351431f3ddc979` | `main` |
| `AIPhone` | `154081982450b303902efbe77399078e79f28a8c` | `codex/stabilize-composite-timeouts` |
| `simple_harness` | `362e51496d06fe14f8cfdc1909f25381ee427e3b` | `main` |

All four worktrees contained pre-existing unrelated changes or untracked evidence. They must be preserved and excluded from scoped Realtime commits.

## `simple-harness-service-sdk`

- `pytest`: **114 passed** in 8.87 s.
- Ruff: **PASS**.
- mypy: **PASS**, 19 source files.
- thin-architecture verifier: **PASS**.
- `uv build`: **PASS**, version 0.2.3 sdist and wheel emitted to `/tmp/realtime-provider-sdk-baseline-dist`.

## TokenSeller

The repository `.env` points at an unavailable/invalid localhost:5432 database for tests. The test baseline therefore used the repository's healthy Compose PostgreSQL at `localhost:5433`, database/user/password `token_seller`/`postgres`/`postgres`, without changing `.env`.

- Prisma migrations: **PASS**, 40 applied and no pending migration.
- Prisma schema validation: **PASS**; only the existing deprecated `metrics` preview warning.
- API typecheck: **PASS**.
- API build: **PASS**.
- Full Jest with deterministic local DB and `--forceExit`: **116 suites total; 1149 tests passed, 2 failed**.
  - `provider-registry.service.spec.ts`: expected stale catalog model `gpt-5.2`.
  - `auto-routing.spec.ts`: expected stale cheapest model `m2-e2e-min`, current result `ali-deepseek-v4-flash`.
- Existing harness condition: Jest keeps Realtime heartbeat/detach handles open without `--forceExit`. New S2 tests must close timers deterministically and must not worsen this baseline.

## AIPhone

Plan-test runner state: `/tmp/aiphone-realtime-program-baseline-state.json`. Plan-local signatures: `verification/aiphone-baseline-known-failures.json`.

- Runner: **12/15 shards passed, 3 existing red, 0 new red after classification**.
- Passing evidence: **1423 tests passed** across mobile-host, root unit, safe integration, and CLI-surface shards; root mypy, mobile-host Ruff passed.
- Existing red:
  - root Ruff scans pre-existing untracked verification artifacts (`root-ruff:exit=1`);
  - mobile-host mypy has 10 errors in existing `file_access.py` and `camera_capture.py` (`mobile-host-mypy:exit=1`);
  - stale manifest shard references missing `tools/verify_known_pytest_failures.py` (`root-h0-residual-verifier:exit=2`).
- Agent Runtime was run through its supported installed-venv path, because `uv run --project agent_runtime` intentionally triggers the release-only build hook:
  - pytest: **409 passed, 1 skipped**;
  - Ruff: **PASS**;
  - mypy: **PASS**, 36 source files.
- Direct `uv build` without release identity is expected to fail `build-identity-invalid`; S1/S3 release gates must use the repository's candidate builder with exact source SHA and `SOURCE_DATE_EPOCH`.

## Simple Harness

Plan-test runner state: `/tmp/simple-harness-realtime-program-baseline-state.json`. Plan-local signatures: `verification/simple-harness-baseline-known-failures.json`.

- Runner: **14/17 shards passed, 3 existing red, 0 new red after classification**.
- Passing shards include all other backend alpha ranges, capability, companion and SDK adapter suites; frontend Vitest/typecheck/build; Rust test/check.
- Existing red:
  - `backend-m-r`: **1485 passed, 26 skipped, 1 failed**; independently reproduced as `backend/tests/test_process_list_error.py::test_process_list_with_query`, whose live process-name query is host-snapshot-sensitive;
  - root tests: existing `root-tests:exit=1` signature;
  - frontend lint: existing `frontend-lint:exit=1` signature.

## Regression rule

Each release slice must preserve all currently passing shards. Existing red is allowed only when the exact plan-local signature and root cause remain unchanged; no new failure may be reclassified during implementation without an explicit plan defect record and user-visible decision.
