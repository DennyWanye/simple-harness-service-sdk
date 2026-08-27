# Changelog

## 0.3.0 — candidate

- Add the provider-neutral full-duplex Realtime contracts, client, bounded session FSM, stable
  domain events, interruption tombstones, Tool acknowledgement, and single terminal ownership.
- Add native Qwen Omni and offline-only OpenAI semantic adapters without exporting Provider wire
  fields through the root public API.
- Add exact TokenSeller HTTPS/WSS composition plus owner-only AF_UNIX and authenticated loopback
  WebSocket transports for AIPhone and Simple Harness.
- Package four byte-identical authority packs, a deterministic cross-language bundle, three target
  locks, and a release manifest binding service 0.3.0, Harness 0.6.2, and Memory 0.5.2.
- Retain the durable command/chat public API and the thin no-database/no-worker architecture.

## 0.2.3

- Bound cancel dispatch waiting to five seconds while preserving and supervising the single cached
  physical cancel task; an unresponsive RPC now returns `CancelPending` without freezing the UI.

## 0.2.2

- Gate cancel snapshot observation on the accepted receipt, supervise detached cancel tasks, keep
  cancel-pending runs observable, coordinate output with active prompts, preserve history across
  iterative terminal restarts, and recover broken stdout through stable stderr diagnostics.

## 0.2.1

- Preserve pending prompt input while durable observation runs, enforce observe/cancel deadlines,
  recover renderer failures through safe flat reconciliation, and keep local help/clear operations
  free of durable or global-output side effects.

## 0.2.0

- Add the product-configurable terminal chat UI, durable chat controller, safe Markdown renderer,
  accessibility fallbacks, and public `ChatUiConfig` composition boundary.

## 0.1.3

- Pin the formal Harness 0.5.2 wheel and compatibility BOM. SVC behavior, public API, transport
  contracts, and stateless architecture remain unchanged.

## 0.1.2

- Pin the formal Harness 0.5.1 wheel and compatibility BOM, exposing its closed durable Tool
  catalog fingerprint authority without adding SVC persistence or workers.

## 0.1.1

- Interactive `chat` now treats a completed Harness run as terminal and starts the next turn
  as a fresh run under the same session, allowing Agent Memory to carry conversation context
  without submitting an invalid continuation to a terminal run.

## 0.1.0 — candidate

- Adds closed authenticated health/start/continue/get/cancel services over Harness 0.5 public
  command APIs without a second database or execution worker.
- Adds domain-separated stable identity projection, channel-bound MAC capabilities, owner-only
  AF_UNIX transport, ConversationClient, CLI engine, conformance helpers, and compatibility BOM.
- Adds closed terminal outcome projection, active-chat durable cancellation, create-once owner-only
  credential manifests, and strict installed direct-URL hash validation.
- Preserves public Harness command kind in every receipt/snapshot, distinguishes no-run cancel from
  start/continue projection, and atomically activates credentials only after file, directory, and
  parent-directory durability barriers.
- This candidate is not tagged or published pending independent audit.
