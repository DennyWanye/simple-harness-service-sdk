# Simple Harness Service SDK 0.1.0 candidate status

No `v0.1.0` tag or GitHub Release exists. The current candidate must pass independent audit and
downstream AIPhone integration before promotion.

The prior candidate whose manifest SHA-256 begins `558c0a9b8a70` (wheel SHA-256
`5c286c9e1c4fdbde1d2d8034b89e6da2d414e200cb789d0dd3aac8f2c07f6181`, source commit
`edee174100eff7f99af01d7f4e2ac7afa877cae4`) is **withdrawn** after independent audit. It must
not be tagged, released, or used downstream.

The SDK owns no SQLite schema, durable ingress, output store, or execution worker. Harness 0.5.0
is the sole durable command authority. The optional `memory` extra pins Memory SDK 0.5.1.
Credential admission uses the reusable create-once owner-only manifest component; operators must
load and validate it before opening the AF_UNIX socket. Installed BOM validation requires an exact,
unique SHA-256 in each dependency's `direct_url.json`; a URL-only installer record fails closed.
