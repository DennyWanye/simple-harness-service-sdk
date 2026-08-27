# Architecture diff closure

Final strict diff review: **PASS**.

- Wallet pricing authority closes CNY provider cost to USD wallet atoms with a complete SHA-bound revision, exact ECB cross-rate inputs, validity window, aggregate rounding, margin, modality ceilings, completed/cancelled fixtures, and 23-cent hold.
- Mint/session fixtures create and expire their ephemeral token inside the revision half-open validity window.
- Qwen barge-in authority uses two turn/response identities, both terminal orders, late predecessor audio quarantine, independent holds/UsageEvents, duplicate-terminal idempotency, and cancel-no-active behavior.
- Simple Harness local endpoint is uniformly `/ws/realtime-voice` in architecture and authority.
- Wallet rollover eligibility is derived from the latest effective append-only `ACTIVATE`/`DEACTIVATE` event; the named lifecycle service owns startup/mint/health/preflight checks, metadata-only 7/3/1-day alerts with per-band dedupe, and append-only rollback.
- All affected JSON, manifest-vector closure, SHA256SUMS, wallet revision digest, and capability/mint/open/created digest checks pass.

Capability digest: `aa47849bbd8c980cd32ab3d2a8f952fbee55b7c59ce1951839484fe0fb589727`.
