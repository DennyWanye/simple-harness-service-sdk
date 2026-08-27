# Architecture reset 2026-08-28

Trigger: primary/specialist findings `billing-currency-domain-unresolved`, `barge-in-turn-overlap-assumption-unspiked`, and `simple-harness-local-route-contract-conflict` exposed three high-risk architecture assumptions that were not closed by the first architecture review.

In-scope reset, no acceptance or assurance-contract change:

- Provider CNY price integers are now explicitly provider-cost inputs, not TokenSeller USD wallet atoms.
- A mint-bound TokenSeller wallet pricing revision freezes integer `usd_atoms_per_cny_minor`, margin, rounding, timestamps, and modality ceilings. Conversion rounds only after aggregate Provider cost and after aggregate margin.
- Barge-in no longer relies on undocumented old-terminal-before-new-response ordering. The relay uses a bounded predecessor/successor transition with independent turn identities and settlement.
- The Simple Harness local WebSocket endpoint is authority data: `/ws/realtime-voice`.
- Capability digest changed from `379b3d5...` to `aa47849bbd8c980cd32ab3d2a8f952fbee55b7c59ce1951839484fe0fb589727` after binding the complete wallet revision digest and validity window.
- `wallet-pricing-revision.json` freezes ECB 2026-08-27 cross-rate inputs, `14880267833` USD atoms/CNY minor, 1000 BPS margin, modality ceilings, completed/cancelled atom fixtures, a 23-cent hold, and validity `[2026-08-28, 2026-09-28)`; mint rejects outside the window before token creation.
- `barge-in-overlap-matrix.json` freezes both terminal orderings, two turn/response identities, late predecessor audio quarantine, and two exactly-once UsageEvent outcomes.
- `wallet-pricing-rollover.json` freezes insert-only revision staging, append-only `ACTIVATE`/`DEACTIVATE` eligibility, deterministic 35-minute-horizon mint selection, seven-day successor lead time, overlap selection, old-session snapshot semantics, named lifecycle-service 7/3/1-day alerts with fake-clock dedupe, and append-only activation rollback. Same-schema rate rotation does not require a consumer SDK release.

Validation: every authority JSON parsed; every pack SHA256SUMS passed; capability digest matched mint/open/created vectors; `git diff --check` passed.
