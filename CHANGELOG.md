# Changelog

## Unreleased

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
