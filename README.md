# Simple Harness Service SDK

Thin authenticated service and transport framework for Simple Harness SDK. Harness remains the
only durable command and execution authority; this package owns no database or execution worker.

Version 0.2.1 adds a product-configurable Claude-style terminal chat UI with durable observation
deadlines and renderer-failure recovery while retaining Harness
0.5.2 as the only durable command authority. Interactive TTYs receive command completion,
history, multiline input, status feedback, safe Markdown rendering, and terminal restoration;
`NO_COLOR`, `TERM=dumb`, narrow, redirected, and screen-reader sessions use deterministic flat
text. Existing `ask`, `status`, `cancel`, exit codes, and the product-neutral `main(argv)` entry
remain compatible. Products may pass `ChatUiConfig` through the additive
`main(argv, chat_ui_config=...)` composition argument.
