# Simple Harness Service SDK

Thin authenticated service and transport framework for Simple Harness SDK. Harness remains the
only durable command and execution authority; this package owns no database or execution worker.

0.3.16 支持已确认用户消息与 speech_started 项目的无 response_id 转写：非空转为用户事件，空内容忽略并记诊断；保留消息确认及助手输出作用域校验。工具循环、上下文注入和播放位置接口见
[接口与交接说明](docs/realtime-0.3.13.md)。`RealtimeClient` 负责会话顺序、有界队列、
打断状态、工具回执和稳定事件；产品提供麦克风、播放和界面策略，选择组合配置，
无需解析 Qwen 或 OpenAI 的原生 JSON。

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
three-SDK release unit: service `0.3.16`, Harness `>=0.13.0.dev20260925`, and Memory `0.5.2`. Harness remains the
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

## 私有安装组合

Harness 使用 `>=0.13.0.dev20260925`，通过 `--find-links` 指向 `build/vendor/`，接受 `+opt.N` 本地版本。BOM 记录主线下限与 execution schema 5，返回实际安装版本；正式候选单独冻结本次实测轮子的版本与 SHA。Memory extra 仍为 0.5.2，其旧 Harness 元数据约束需要按[构建说明](docs/build-and-release.md)分步 `--no-deps` 安装，不用历史 `uv.lock` 同步当前组合。
