# Qwen native cancel terminal drift proposal

## Trigger

The production TokenSeller paid barge-in smoke on 2026-08-28 observed a native Qwen
`response.done` terminal with `response.status=cancelled`. The released
`qwen-native/2026-08-28.1` authority admitted only `completed`, `failed`, and `incomplete`, so the
relay correctly failed closed with `protocol_error`.

Privacy-safe evidence:

- correlation: `corr_YQVMQR774Q4TVN5QZAPJPRNKZ1`
- top-level key digest: `41823512d9e4ac1d4cbb57c15342d4e370e4ee79981b95153db387757a4f3242`
- terminal metadata: `frameType=response.done`, `responseStatus=cancelled`
- no transcript, text delta, provider message, API key, or audio payload was retained

The current Alibaba Cloud server-event reference states that completed audio/text events can also
occur when a response is interrupted or canceled, but it does not enumerate every
`response.done.status` value. The paid production observation is therefore the stronger wire-level
evidence for this snapshot.

## Versioned resolution

- Preserve `qwen-native/2026-08-28.1` and `tokenseller.realtime-control/2026-08-28.1` unchanged.
- Add `qwen-native/2026-08-28.2` with native `cancelled` terminal support.
- Add `tokenseller.realtime-control/2026-08-28.2` so mint/open/created bind the new wire version and
  capability digest atomically.
- Publish service SDK `0.3.2`; consumers must pin the exact wheel and authority root.
- A native `cancelled` terminal must include valid exact usage and maps to domain `cancelled`.
- The prior `incomplete` plus local cancel-owner projection remains accepted for compatibility.

## Verification

- full SDK tests: 310 passed
- ruff: passed
- mypy strict: passed
- thin architecture gate: passed
- active authority root: `1050b06d53ae340cd6f0183c6387fc7a8b34a5daab8245dc4de05a107d3e32b7`

TokenSeller must import the generated bundle rather than hand-editing its authority copy, then pass
the 20 ms, 100 ms, barge-in, legacy, ledger, migration, and production SHA gates.
