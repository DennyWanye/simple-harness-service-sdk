# Qwen native cancelled-without-usage drift proposal

## Trigger

The production TokenSeller paid barge-in smoke on 2026-08-28 observed a Qwen native
`response.done` terminal with `response.status=cancelled` and no `response.usage` field. Two
ordinary completed paid turns in the same release run included the full usage object. The
released `qwen-native/2026-08-28.2` authority required usage for every non-failed terminal, so the
relay correctly failed closed rather than guessing a charge.

Privacy-safe evidence:

- correlation: `corr_2B035XQ5GGEVVK1YJ691WQE9DP`
- top-level key digest: `41823512d9e4ac1d4cbb57c15342d4e370e4ee79981b95153db387757a4f3242`
- response key digest: `54585d5eda30140b3ad79fdb77bf149f823ee77c9a14b4889b5aad9c6c291ad9`
- terminal metadata: `frameType=response.done`, `responseStatus=cancelled`, `usagePresent=false`
- no transcript, text delta, provider message, API key, or audio payload was retained

The Alibaba Cloud server-event reference documents `response.done.usage` for normal completion and
notes that audio/text done events may also occur when interrupted or canceled, but provides no
cancelled `response.done` example. The paid observation is the wire-level authority for this
snapshot.

## Versioned resolution

- Preserve the `.2` protocol source packs unchanged.
- Add `qwen-native/2026-08-28.3` and `tokenseller.realtime-control/2026-08-28.3`.
- Publish service SDK `0.3.3`; consumers must pin the exact wheel and authority root.
- `cancelled` with valid usage settles exact usage once.
- `cancelled` without usage emits `ResponseFinished.cancelled`, releases its hold once, creates no
  UsageEvent, and forwards the terminal only after the release is durable.
- Completed and incomplete terminals still require valid exact usage. Failed terminals remain
  fatal and release without charge.

## Required verification

- full SDK, lint, type, architecture, packaging, and exact-wheel gates
- TokenSeller unit and real-Postgres settlement gates
- production paid 20 ms, 100 ms, and barge-in smoke plus legacy smoke
