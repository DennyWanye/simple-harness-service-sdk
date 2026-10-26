# Changelog

## 0.3.19

- 增加可查询的身份表，关联输入、上下文服务端别名、已完成输入音频消息和嵌套输出身份；显式恢复状态保留跨代已确认用户身份。
- 控制错误严格校验并区分合法终止与字段校验失败，向终态事件和诊断暴露脱敏原因及已核实的客户端关联。
- 终止出口保留首个致错事件、白名单字段、阶段和代数；不记录正文、音频或异常消息。
- 增加恢复快照、工具/已完成输入去重及消息 idempotency_key；不自动重放 PCM，不重复请求已确认播报。
- 保留 live6 两个未知 item 的拒绝，增加现场顺序与恢复反例；未将无完整字段的控制终止归为同一根因。

## 0.3.18

- 增加共用标识判定：缺失、null、非字符串、空串及纯空白均视为无标识，Qwen 已知用户 item 的同族事件统一按用户侧合同处理。
- Qwen/OpenAI 的内容身份、生命周期、工具回执、ASR、会话和事件标识使用同一必需身份校验，非空真实 ID 与正文原样保留。
- 补齐八类无标识、未知/助手/旧连接拒绝及历史回灌后助手活跃/结束交错反例，保留助手输出正例和来源隔离。

## 0.3.17

- Qwen 在统一入口识别无 response_id 的用户侧 item 事件，覆盖 audio、audio_transcript、content_part、output_item、text 与工具参数事件，避免逐分支遗漏。
- 已确认 user 消息与 speech_started/committed 的 item 可产生用户转写；其余附带事件忽略并记录 user_side_response_event_ignored 及 provider_event_type，不污染助手输出或完成状态。
- 保留未知身份、无效作用域和助手输出校验，补充真实五键 audio.done 序列、同类完成变体、助手交错及 generation 隔离测试。

## 0.3.16

- Qwen 缺少 response_id 的 audio_transcript 仅在已确认用户消息或 speech_started 的 item 上按用户转写解码；非空发用户事件，空内容忽略并记 user_side_transcript_ignored 诊断。
- 保留未知、未确认、错误角色及助手 item 的严格作用域校验，不补造 response_id；附带事件不重复确认、不触发新回答，连接继续接收下一轮。
- 补充适配器与真实会话离线反例，覆盖重复、迟到、generation 隔离和交错助手输出。

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
