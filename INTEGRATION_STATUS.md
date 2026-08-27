# Simple Harness Service SDK 0.3.0 candidate status

The 0.3.0 source candidate implements the approved Realtime Provider Program S1 boundary. It is
not a release until its scoped source commit, reproducible wheel/sdist, authority bundle, three
target locks, installed-wheel Python 3.11/3.13 matrix, download-back hashes, and consumer handoff
are complete.

The release unit uses the published immutable Harness
[`v0.6.2`](https://github.com/DennyWanye/simple-harness-sdk/releases/tag/v0.6.2) wheel SHA-256
`ffb7c0619851f3c936fcc1d0cf527d07f49e87770291b85e57fe87032ac02c2e` and Memory
[`v0.5.2`](https://github.com/DennyWanye/simple-harness-memory-sdk/releases/tag/v0.5.2) wheel
SHA-256 `deff2fa85a269a3978f2c6efcd99fda77abcb74444170361365fd00ec0164e9e`.
Both dependency releases passed exact public download-back verification and the joint Python
3.11/3.13 compatibility matrix before the service candidate was locked.

The SDK owns provider-neutral Realtime session semantics and concrete relay/local transport
composition, but no Provider credentials, product microphone/playback policy, TokenSeller billing,
database, or execution worker. Qwen and OpenAI wire contracts remain confined to their adapters;
the OpenAI adapter has no enabled live connector in 0.3.0. The packaged authority root SHA-256 is
`2296e55ca88a02ea800a7a70c3b83a2859badb9466539ee17fe3197fcc0c5802`.

The currently published service fallback remains 0.2.3. Historical 0.1.3 release facts follow.

## Historical 0.1.3 release

[`v0.1.3`](https://github.com/DennyWanye/simple-harness-service-sdk/releases/tag/v0.1.3) is the
current non-Draft, non-Prerelease Latest release. Its annotated tag binds source commit
`28ff026d4d99a970ffd7f9a6ae4fb9fbee7228bd`.
The published wheel SHA-256 is
`6f56b71989bb4293bfa076d8b507a49e8e5b66bffa82132c8aec56fd7420d137`; the sdist SHA-256 is
`a03d15ab4ce9301161424889570a263ebbb0726113c5b2737cbcd5b9ba3dc0d7`; and the candidate
manifest SHA-256 is `510d9b34e45d474904d2ce77ae5d6217a4d677a7c4f49fb6627c93d77d71fe85`.
All assets were independently downloaded from the public Release and verified byte-for-byte.
Python 3.11 and 3.13 exact public wheel installs with the `memory` extra passed BOM, import, and
CLI smoke gates.

Interactive chat now starts every turn after a terminal outcome as a fresh Harness run while
preserving the external session identity. This matches Harness 0.5's public rule that terminal runs
reject continuations and lets Agent Memory provide cross-run conversation continuity.

The prior candidate whose manifest SHA-256 begins `558c0a9b8a70` (wheel SHA-256
`5c286c9e1c4fdbde1d2d8034b89e6da2d414e200cb789d0dd3aac8f2c07f6181`, source commit
`edee174100eff7f99af01d7f4e2ac7afa877cae4`) is **withdrawn** after independent audit. It must
not be tagged, released, or used downstream.

The later candidate at commit `a326bc826e7d5bc11041e8ef0a7a35b9080d645c` (manifest SHA-256
`a69056117bb55f01ee9bc8b4465add49824eb292ef519d8f2a19fef7589e0c35`) is also
**withdrawn** after re-audit. Its receipts omitted Harness command kind and its credential
activation did not fsync the parent directory entry.

The SDK owns no SQLite schema, durable ingress, output store, or execution worker. Harness 0.5.2
is the sole durable command authority. The optional `memory` extra pins Memory SDK 0.5.1.
Credential admission uses the reusable create-once owner-only manifest component; operators must
load and validate it before opening the AF_UNIX socket. Installed BOM validation requires an exact,
unique SHA-256 in each dependency's `direct_url.json`; a URL-only installer record fails closed.
Receipts preserve Harness command kind so a no-run durable cancel closes as exit 6 without
misprojecting start/continue. An active physical provider call may remain recoverable
`WAITING/PENDING` with CLI timeout 4 under Harness's public default reconciliation; SVC never
forges terminal cancellation.
