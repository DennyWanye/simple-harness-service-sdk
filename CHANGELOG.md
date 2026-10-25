# Changelog

## 0.3.15

- Qwen 消息确认按最早待确认请求的角色、input_text 类型与正文匹配，支持服务端分配或保留原 item ID，并保留原请求的确认关联。
- 会话的只读 context_ack_id 暴露最近已确认消息的服务端 ID；重复、迟到或越序回显不误确认后续消息，工具结果与输入音频项目继续走各自路径。
- Qwen instructions 确认按去首尾空白后的内容比较，历史回灌不触发回复，播报在有效确认后仅续答一次。

## 0.3.14

- Qwen 工具回显按 function 名称、required 集合与 properties 键集合确认，支持中继规范化后的 function 包裹；非语义差异仅记无正文诊断，缺失、额外或不匹配工具报 unsupported。
- Harness 依赖改为 `>=0.13.0.dev20260925`，BOM 的运行兼容规则与候选轮子快照分开，允许后续本地版本并实测校验 execution schema 5。
- 候选仍携带实际核验的轮子；三平台运行锁通过候选约束文件冻结该次版本，不把候选 opt 号写回产品依赖。

## 0.3.13

- 增加工具目录下发与回显校验、可选宿主自动工具循环、参数 schema 校验与 generation 去重。
- 增加消息追加、指令更新、带外回复接口和不支持能力的明确错误。
- 输出回执支持实际播放位置，提供已发送 PCM 字节、时长与实际播放位置快照。
- Harness 精确钉版为私有 opt.216（execution schema 5），Memory extra 保持 0.5.2（memory schema 4），两包同环境导入与 410 条测试通过。
- 正式候选携带两个经 SHA-256 校验的依赖轮子，支持私有 wheelhouse 定位、三平台带散列运行锁及两次构建字节复现。
- Memory 旧 Harness 元数据约束仍需采用文档中的分步 `--no-deps` 安装组合；真线路由 Claude 后续验收。

## 0.3.12

- Allow product compositions to batch consecutive local PCM frames into one upstream audio write
  while preserving local sequence acknowledgement semantics.
- Flush a partial input batch before barge-in or controlled shutdown so tail audio is not dropped.

## 0.3.11

- Treat Qwen's observed empty completed input transcript as an ordered no-op so a transient
  micro-VAD noise turn cannot terminate an otherwise healthy audio response.
- Keep missing and non-string completed transcripts fail-closed as protocol errors.

## 0.3.10

- Allow provider compositions to disable client-initiated WebSocket keepalive while retaining
  automatic replies to server Ping frames.
- Keep positive validation for configured Ping intervals and timeouts and preserve the existing
  generic transport defaults for providers that own client-side keepalive.

## 0.3.9

- Preserve the provider transport's exact send-timeout classification by keeping the local
  producer deadline outside the bounded WebSocket write deadline.
- Emit privacy-safe `provider_send_failed` diagnostics so a stalled relay write is distinguishable
  from local product backpressure.

## 0.3.8

- Preserve one opaque Realtime correlation across product, SDK, and relay logs.
- Classify WebSocket connect, send, receive, close direction, and ping timeout failures without retaining payloads or exception text.

## 0.3.7

- Record content-free mint HTTP/transport/response categories and WebSocket close codes so
  production failures remain distinguishable without retaining response bodies or exception text.

## 0.3.6

- Accept Qwen's observed empty `conversation.item.input_audio_transcription.delta` as an ordered
  no-op while retaining its event ID for duplicate suppression; non-empty and malformed deltas
  keep their existing semantics.

## 0.3.5

- Add content-free Provider event kind and JSON-shape fingerprints at receive, decode-failure,
  apply-failure, and transport-failure boundaries so live Realtime failures identify the exact
  SDK stage without retaining audio, text, instructions, credentials, or exception bodies.
- Sample high-rate audio diagnostics and always emit terminal input/output totals so diagnostic
  backpressure cannot hide the final failure event during a long call.

## 0.3.4

- Export the provider-neutral local Realtime protocol constants, PCM frame types, and codecs from
  `simple_harness_service.realtime` so product consumers never import SDK-internal modules.

## 0.3.0

- Add the provider-neutral full-duplex Realtime contracts, client, bounded session FSM, stable
  domain events, interruption tombstones, Tool acknowledgement, and single terminal ownership.
- Add native Qwen Omni and offline-only OpenAI semantic adapters without exporting Provider wire
  fields through the root public API.
- Add exact TokenSeller HTTPS/WSS composition plus owner-only AF_UNIX and authenticated loopback
  WebSocket transports for AIPhone and Simple Harness.
- Package four byte-identical authority packs, a deterministic cross-language bundle, three target
  locks, and a release manifest binding service 0.3.0, Harness 0.6.2, and Memory 0.5.2.
- Retain the durable command/chat public API and the thin no-database/no-worker architecture.

## 0.2.3

- Bound cancel dispatch waiting to five seconds while preserving and supervising the single cached
  physical cancel task; an unresponsive RPC now returns `CancelPending` without freezing the UI.

## 0.2.2

- Gate cancel snapshot observation on the accepted receipt, supervise detached cancel tasks, keep
  cancel-pending runs observable, coordinate output with active prompts, preserve history across
  iterative terminal restarts, and recover broken stdout through stable stderr diagnostics.

## 0.2.1

- Preserve pending prompt input while durable observation runs, enforce observe/cancel deadlines,
  recover renderer failures through safe flat reconciliation, and keep local help/clear operations
  free of durable or global-output side effects.

## 0.2.0

- Add the product-configurable terminal chat UI, durable chat controller, safe Markdown renderer,
  accessibility fallbacks, and public `ChatUiConfig` composition boundary.

## 0.1.3

- Pin the formal Harness 0.5.2 wheel and compatibility BOM. SVC behavior, public API, transport
  contracts, and stateless architecture remain unchanged.

## 0.1.2

- Pin the formal Harness 0.5.1 wheel and compatibility BOM, exposing its closed durable Tool
  catalog fingerprint authority without adding SVC persistence or workers.

## 0.1.1

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
