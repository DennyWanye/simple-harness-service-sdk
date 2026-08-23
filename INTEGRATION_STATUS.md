# Simple Harness Service SDK 0.1.0 release status

`v0.1.0` is formally published as a non-Draft, non-Prerelease GitHub Latest Release:
https://github.com/DennyWanye/simple-harness-service-sdk/releases/tag/v0.1.0

The annotated tag peels to source commit `682b9fd82770af9e5949f16f6888757eb784e094`.
The published wheel SHA-256 is
`086961c454f31e469c7450d57ceac21f39dd3009648d10026a9cae5a27e4fec6`; the sdist SHA-256 is
`8962d95d5a423a2100f1f7d0b9f2ccc7e581402257588ec14bcff3e03cc61b1f`; and the candidate
manifest SHA-256 is `fdeadc66190f74d4c6ffa3cf29a3ceb22b949a277bc075790ad9bbdfddec7ea4`.
All assets were independently downloaded from the public Release and verified byte-for-byte.
Python 3.11 and 3.13 exact public wheel installs with the `memory` extra passed BOM, import, and
CLI smoke gates.

The prior candidate whose manifest SHA-256 begins `558c0a9b8a70` (wheel SHA-256
`5c286c9e1c4fdbde1d2d8034b89e6da2d414e200cb789d0dd3aac8f2c07f6181`, source commit
`edee174100eff7f99af01d7f4e2ac7afa877cae4`) is **withdrawn** after independent audit. It must
not be tagged, released, or used downstream.

The later candidate at commit `a326bc826e7d5bc11041e8ef0a7a35b9080d645c` (manifest SHA-256
`a69056117bb55f01ee9bc8b4465add49824eb292ef519d8f2a19fef7589e0c35`) is also
**withdrawn** after re-audit. Its receipts omitted Harness command kind and its credential
activation did not fsync the parent directory entry.

The SDK owns no SQLite schema, durable ingress, output store, or execution worker. Harness 0.5.0
is the sole durable command authority. The optional `memory` extra pins Memory SDK 0.5.1.
Credential admission uses the reusable create-once owner-only manifest component; operators must
load and validate it before opening the AF_UNIX socket. Installed BOM validation requires an exact,
unique SHA-256 in each dependency's `direct_url.json`; a URL-only installer record fails closed.
Receipts preserve Harness command kind so a no-run durable cancel closes as exit 6 without
misprojecting start/continue. An active physical provider call may remain recoverable
`WAITING/PENDING` with CLI timeout 4 under Harness's public default reconciliation; SVC never
forges terminal cancellation.
