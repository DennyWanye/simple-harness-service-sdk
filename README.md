# Simple Harness Service SDK

Thin authenticated service and transport framework for Simple Harness SDK. Harness remains the
only durable command and execution authority; this package owns no database or execution worker.

Version 0.3.5 adds a provider-neutral, full-duplex Realtime API alongside the existing durable
command and Claude-style terminal chat APIs. `RealtimeClient` owns session ordering, bounded
queues, interruption tombstones, one terminal owner, Tool acknowledgement, and stable domain
events. Products provide only microphone/playback/UI policy and select a composition profile;
they do not parse Qwen or OpenAI wire JSON.

The optional `realtime` extra installs the concrete HTTPS/WebSocket primitives. The SDK includes
an exact TokenSeller credential minter and relay transport, AF_UNIX and authenticated loopback
local transports, the native Qwen Omni semantic adapter, and an offline-only OpenAI Realtime
adapter seam. The OpenAI live connector is intentionally disabled until a separately accepted
authority pack and production admission exist.

Realtime diagnostics are opt-in through a provider-neutral sink and are no-op by default. The
bounded immutable snapshot exposes only opaque correlation, lifecycle stage, stable error or close
class, generation, sampled frame and byte counts, duration, Provider event kind, and a content-free
JSON-shape fingerprint. Terminal summaries retain exact input/output totals. Secrets, bearer
tokens, API keys, raw audio, text, instructions, and exception bodies are not accepted diagnostic
fields; sink failures never change session behavior.

Four frozen authority packs are shipped as package data and as a deterministic cross-language
bundle. A release manifest binds their root digest, three Python consumer locks, and the exact
three-SDK release unit: service `0.3.5`, Harness `0.6.2`, and Memory `0.5.2`. Harness remains the
only durable command authority; Realtime adds no database or execution worker.

The 0.2.x terminal behavior remains compatible. Interactive TTYs receive command completion,
history, multiline input, status feedback, safe Markdown rendering, and terminal restoration;
`NO_COLOR`, `TERM=dumb`, narrow, redirected, and screen-reader sessions use deterministic flat
text. Existing `ask`, `status`, `cancel`, exit codes, and the product-neutral `main(argv)` entry
remain compatible. Products may pass `ChatUiConfig` through the additive
`main(argv, chat_ui_config=...)` composition argument.

Cancel submission is deadline-bound: an unresponsive cancel RPC reports `CancelPending` after five
seconds without cancelling or duplicating the cached physical request, and the active run remains
available for conservative observation.

Terminal restoration tests compare the stable `ECHO`, `ICANON`, and `ISIG` local-mode flags plus
cursor visibility and bracketed-paste shutdown. They intentionally exclude the kernel-maintained,
transient macOS `PENDIN` bit from exact equality; the post-exit shell-marker read remains the oracle
that pending input and visible echo work normally.

Architecture boundaries, calibrated code evidence, and the approved Realtime Provider Program target
are indexed in [`ARCHITECTURE/index.md`](ARCHITECTURE/index.md).
