# Simple Harness Service SDK 0.1.2 release status

[`v0.1.2`](https://github.com/DennyWanye/simple-harness-service-sdk/releases/tag/v0.1.2) is the
current non-Draft, non-Prerelease Latest release. Its annotated tag binds source commit
`920a0054ded4101d96195f087035669f4ef818a8`.
The published wheel SHA-256 is
`699c1a8744bdda45cd784b3e28b63273873fa967e203da4c6552c2ee5302c4a7`; the sdist SHA-256 is
`23fe0fc6005f7d33672c681c9e0135925139a6a0533a4297e57b92183f2f4e30`; and the candidate
manifest SHA-256 is `7ede2185f537274c38a8f7de2908013a080e94606f9aa9d719b19d02a28c43a3`.
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

The SDK owns no SQLite schema, durable ingress, output store, or execution worker. Harness 0.5.1
is the sole durable command authority. The optional `memory` extra pins Memory SDK 0.5.1.
Credential admission uses the reusable create-once owner-only manifest component; operators must
load and validate it before opening the AF_UNIX socket. Installed BOM validation requires an exact,
unique SHA-256 in each dependency's `direct_url.json`; a URL-only installer record fails closed.
Receipts preserve Harness command kind so a no-run durable cancel closes as exit 6 without
misprojecting start/continue. An active physical provider call may remain recoverable
`WAITING/PENDING` with CLI timeout 4 under Harness's public default reconciliation; SVC never
forges terminal cancellation.
