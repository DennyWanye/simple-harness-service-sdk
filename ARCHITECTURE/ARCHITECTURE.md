<!-- last-calibrated: f966c6c3ca7d36f5cebb88656334871b8451c3e6 -->

# Simple Harness Service SDK 架构

## 1. 文档边界与校准基线

本文同时记录两件事：

1. service-sdk 及三个相关消费者当前已经存在的真实边界；
2. `acceptance.md` 已批准的 Realtime Provider Program 目标边界。

“当前”是代码事实，“目标”是后续四个 release slice 必须实现的架构约束，二者不得混写成已完成状态。
本次跨仓校准基线如下：

| 仓库 | 校准提交 | 本轮相关状态 |
|---|---|---|
| `simple-harness-service-sdk` | `f966c6c3ca7d36f5cebb88656334871b8451c3e6` | 当前只有 durable command service；本文件与验收合同是未提交规划制品 |
| `AIPhone` | `154081982450b303902efbe77399078e79f28a8c` | 已有可运行 Realtime 产品链路，但 Provider wire 仍在产品内 |
| `TokenSeller` | `136b2002014fe2cf7a3e8206f2351431f3ddc979` | 已有生产 Qwen upstream、旧 GA-shaped relay、鉴权与计费；工作树含无关用户改动 |
| `simple_harness` | `362e51496d06fe14f8cfdc1909f25381ee427e3b` | Realtime 尚未接入，旧 Voice 默认关闭 |

## 2. 当前架构（as-is）

### 2.1 service-sdk 是短 RPC 的薄适配层

根包使用显式导入和 `__all__` 冻结公共 API
（`src/simple_harness_service/__init__.py:3-29,31-67`），公开面由
`tests/public-api.json:1-37` 做快照。`ServicePort` 与 `ConversationClient` 只有
`health/start/continue/get/cancel` 五种短调用
（`src/simple_harness_service/client.py:19-30,33-51`）。

`HarnessAdapter` 是 durable Harness command API 的唯一翻译点
（`src/simple_harness_service/service.py:59-68,79-99`）；`HarnessService` 明确保持无状态，durable
lifecycle 仍由 Harness 掌握（`src/simple_harness_service/service.py:117-128`）。BOM 会核对已安装发行版版本、
其中 service-sdk 自身只核对版本；Harness/Memory 才额外核对 direct URL 与 SHA-256
（`src/simple_harness_service/bom.py:25-61,65-87`）。因此当前 BOM 不能单独证明 consumer 安装了获批的 service-sdk
wheel 字节，Realtime 也不能被伪装成
第五种 command，也不能改变现有 durable authority。

### 2.2 AIPhone 已经拥有一套产品内 Provider bridge

AIPhone 当前在生产装配时直接构造产品自己的 `RealtimeRelayBridge`
（`../AIPhone/agent_runtime/src/aiphone_agent_runtime/application.py:283-299`）。该 bridge 自行：

- 请求 TokenSeller `/realtime/client_secrets`
  （`../AIPhone/agent_runtime/src/aiphone_agent_runtime/realtime_voice_bridge.py:97-119`）；
- 建立 WSS 并携带 ephemeral Bearer token
  （`../AIPhone/agent_runtime/src/aiphone_agent_runtime/realtime_voice_bridge.py:122-136`）；
- 硬编码旧 relay 协议 `2026-07-voice-v1.1`
  （`../AIPhone/agent_runtime/src/aiphone_agent_runtime/realtime_voice_bridge.py:44-46`）；
- 发送 OpenAI GA nested `session.update`，并硬编码 semantic VAD、transcription、voice 与 PCM
  （`../AIPhone/agent_runtime/src/aiphone_agent_runtime/realtime_voice_bridge.py:573-614`）；
- 校验 relay session/version 与 upstream-shaped session 字段
  （`../AIPhone/agent_runtime/src/aiphone_agent_runtime/realtime_voice_bridge.py:728-767`）。

产品内部还定义了带 Provider event 名称的大型可选字段事件对象
（`../AIPhone/agent_runtime/src/aiphone_agent_runtime/realtime_voice_contracts.py:59-80,97-111`）。这条链路已经实现，
但本段不把“实现存在”当作当前提交的自动化、真机或真实 Provider gate 已通过；同时，它使 AIPhone 成了第二个
wire protocol authority。

### 2.3 TokenSeller 当前是 GA facade，不是 Qwen-native relay

TokenSeller 当前 gateway 以 `/v1/realtime` 为根，但实际接受该路径及所有子路径
（`../TokenSeller/apps/api/src/modules/relay/realtime/realtime.gateway.ts:45-54,263-267`）。
HTTP 控制面在 `/v1/realtime/client_secrets` mint 默认 60 秒、可由正整数环境变量覆盖 TTL 的 ephemeral token，
并且不回传长期 `tsk_*`
（`../TokenSeller/apps/api/src/modules/relay/realtime/realtime.controller.ts:66-80,88-103,139-161`）。握手禁止 URL token、
校验 Origin、一次性 consume 和底层 key 状态
（`../TokenSeller/apps/api/src/modules/relay/realtime/realtime-handshake.ts:119-145,147-182,205-260`）。

数据面连接原生 DashScope Qwen beta WSS，并携带 `OpenAI-Beta: realtime=v1`
（`../TokenSeller/apps/api/src/modules/relay/realtime/realtime.gateway.ts:899-912`），但 relay 会把客户端 GA nested
`session.update` 转成 Qwen beta flat schema
（`../TokenSeller/apps/api/src/modules/relay/realtime/realtime.gateway.ts:1539-1583`），并把 Qwen 的
`response.audio.*` 等事件重命名成 GA facade
（`../TokenSeller/apps/api/src/modules/relay/realtime/realtime-mapping.ts:199-231`）。因此旧入口并不是本项目要求的
Qwen-native 数据面。

TokenSeller 已有必须复用的成熟控制面：按 key 与全局容量准入
（`../TokenSeller/apps/api/src/modules/relay/realtime/realtime.gateway.ts:375-415`）、隐私安全帧/字节计数
（`../TokenSeller/apps/api/src/modules/relay/realtime/realtime.gateway.ts:139-149,1806-1823`）以及 transport-neutral
pre-auth/settlement（`../TokenSeller/apps/api/src/modules/relay/realtime/realtime-billing.ts:11-31,66-83,97-136`）。

当前关闭分类仍有明确技术债：upstream 的 `close`/`error` 回调无条件进入 `disposePair`
（`../TokenSeller/apps/api/src/modules/relay/realtime/realtime.gateway.ts:923-947`），而最终 teardown 主动关闭 upstream
（`../TokenSeller/apps/api/src/modules/relay/realtime/realtime.gateway.ts:2195-2231`）；若回调竞态先观察到 upstream close，成功会话的
客户端结束可能被记录为 `realtime_session_upstream_down/upstream_error`
（`../TokenSeller/apps/api/src/modules/relay/realtime/realtime.gateway.ts:2153-2182`）。

### 2.4 Simple Harness 没有 Realtime 消费者

Simple Harness 当前把旧 VAD -> ASR -> Harness -> TTS 路径明确标记为待 Realtime 替换
（`../simple_harness/backend/deskpet/voice_runtime.py:4-9`）。默认关闭时不加载重型语音 Provider，health 返回
`realtime: pending`（`../simple_harness/backend/deskpet/voice_runtime.py:33-39,60-72`）。前端全局开关固定为 false，
旧 `useAudioChannel` 只有在开关启用时才自动连接
（`../simple_harness/tauri-app/src/hooks/useAudioChannel.ts:8-40`）。这意味着 S4 是新接入，不是切换一个现有
Realtime adapter。

## 3. 核心矛盾

系统要新增一个长生命周期、双向、低延迟、可打断的媒体 session，同时必须保持以下三个权威彼此隔离：

1. Harness durable command authority；
2. 产品的设备、播放、UI 与策略 authority；
3. Provider-native wire 与 TokenSeller 鉴权/计费 authority。

若把 Realtime 塞进现有 `ServicePort`/Unix 短 RPC，会破坏 durable API 和既有 conformance；若把 Qwen 或 OpenAI JSON
留在产品里，又会继续产生三处协议实现。目标架构因此采用“并行 Realtime 子系统 + 语义 API / Provider codec /
transport / relay control plane 四层分离”。

## 4. 目标拓扑（to-be）

```text
AIPhone phone UI/audio             Simple Harness Tauri UI/audio
          \                                  /
           \  provider-neutral semantic API /
            simple-harness-service-sdk Realtime
             |  session lifecycle + stable events
             |  Qwen adapter / offline OpenAI adapter (本轮)
             |  TokenSeller credential + WSS connector
             v
       TokenSeller versioned provider relay
         | shared auth / capacity / billing / telemetry kernel
         | provider-native protocol strategy
         v
       DashScope Qwen Omni Realtime WebSocket beta

Future live enablement: same SDK API -> existing OpenAI adapter -> TokenSeller OpenAI-native strategy -> OpenAI Realtime
```

数据依赖只允许向下。产品不得 import Provider adapter 私有 codec；TokenSeller 不得理解产品 UI 状态；service-sdk
不拥有钱包、数据库、麦克风、扬声器或 durable command。

## 5. service-sdk Realtime 子系统

Realtime 是与现有 durable command service 平行的包，不向 `HarnessService`、`ServicePort` 或现有 Unix transport
追加方法：

```text
simple_harness_service/realtime/
  contracts.py          # immutable semantic request/config, audio format, event union, stable errors
  ports.py              # RealtimeSession, RealtimeTransport, CredentialMinter, ProviderAdapter protocols
  client.py             # product-facing RealtimeClient
  session.py            # lifecycle, ordering, backpressure, exact-once terminal/close
  observability.py      # content-free diagnostic projection
  relay_control.py      # TokenSeller control/mint/capability codec
  local.py              # 产品进程间 provider-neutral framing/codec
  adapters/
    qwen_omni.py        # Qwen semantic mapping
    qwen_wire.py        # Qwen beta native JSON/base64 codec
    openai.py           # 本轮交付的 offline adapter；真实连接保持禁用
    openai_wire.py      # 本轮交付的 OpenAI native codec
  transports/
    websocket.py        # WSS implementation, optional dependency
  testing/
    conformance.py      # adapter/session conformance kit
```

### 5.1 产品公共 API

- `RealtimeClient.open(request) -> RealtimeSession` 是唯一建连入口。
- `RealtimeOpenRequest` 只表达 external session id、instructions、所需 features 与期望输入/输出音频格式。
  Provider、endpoint、credential minter、model、voice 和 adapter 在 composition-time profile 注入，不进入产品状态机。
- `RealtimeSession` 是异步 context manager，公开 `send_audio(bytes)`、`events()`、`cancel_response()`、
  capability-gated `truncate_output(...)`、`submit_tool_result(call_id, output)` 与幂等 `close()`；它不暴露 raw socket。
- 公共事件使用闭合的 discriminated dataclass union，例如 `SessionReady`、`SpeechStarted`、`SpeechStopped`、
  `TranscriptDelta`、`TranscriptCompleted`、`ResponseStarted`、`OutputText`、`OutputAudio`、`ResponseFinished`、
  `ToolCallRequested`、`SessionExpiring`、`SessionClosed`、`SessionFailed`。产品不分支 Provider event type 字符串。
- open 前失败抛 `RealtimeError`；open 后失败只产生一个 `SessionFailed` terminal，随后 event stream 结束。
  terminal event 与异常不得成为双重 authority。

`submit_tool_result(call_id, output)` 是 SDK 原子语义，不让产品手动调用 `response.create`。每个 call_id 只有一个状态机：
`REQUESTED -> RESULT_SENT -> RESULT_ACKED -> FOLLOWUP_REQUESTED -> FOLLOWUP_STARTED`。adapter 先发送 Provider-native
function-call-output，等待与该 item 对应的 created/ack，再请求 follow-up response；relay 在转发 follow-up `response.create` 前
创建新的 turn/hold。产品重试同一 call_id 时从已确认 stage 继续，不能重发已 ack 的 item；在 RESULT_SENT 后 ack 超时属于
ambiguous `protocol_error` 并终止 session，避免重复工具副作用。第二步失败但 item 已 ack 时允许以同一 call_id/turn intent
重试 response.create。未知、已完成或 output 不同的重复 call_id 以 `invalid_request` 拒绝。

公共 `RealtimeAudioFormat` 明确 codec=`pcm_s16le`、sample rate、channels；输入与输出分别协商。
`SessionReady` 返回实际格式。产品传 PCM bytes，不传 base64、WAV header 或 vendor format 名。

### 5.2 Provider adapter 与 transport 分离

`RealtimeProviderAdapter` 负责 semantic command/event 与某一 Provider 原生 frame 的双向映射、capability、错误和
terminal 识别；`RealtimeTransport` 只负责有界文本帧、WSS close 与 backpressure。凭证 mint 是独立
`CredentialMinter`，保证 API key 不进入 adapter frame 或 URL。

这样未来 OpenAI adapter 可以选择不同 wire codec，甚至在后续 release 选择 WebRTC transport，而不改变产品 session
语义或 Qwen adapter。OpenAI 官方当前把 WebSocket定位为 server-to-server，并建议 browser/mobile 直连优先 WebRTC；
本项目的客户端只连接 TokenSeller relay，当前 release 有意只实现 relay WSS，WebRTC/SIP 明确不在范围内
（[OpenAI Realtime WebSocket guide](https://developers.openai.com/api/docs/guides/realtime-websocket)）。

### 5.3 Capability 不是鉴权 Capability

现有 `Capability` 是本地 service 调用鉴权，不得复用为 Provider 功能发现。Realtime 使用独立
`RealtimeFeatureSet`，至少表达 server turn detection、automatic response、interruption、input transcription、
text/audio output、cancel、truncate、resume 及支持的 input/output formats。请求的必要 feature 不满足时在建连前返回
稳定 `unsupported`，不以猜测或静默降级代替协商。

Qwen 2026-08-27 authority 支持 `response.cancel` 与 function calling，但官方 client-event contract 没有
`conversation.item.truncate`；因此 Qwen profile 固定 `cancel_response=true`、`tool_calling=true`、
`truncate_output=false`。AIPhone barge-in 的权威语义是“立即停止本地播放 + `cancel_response()` + 丢弃旧 response 的
迟到 audio”，不能调用 truncate，也不能声称 Provider conversation 已裁掉客户端未播放的尾部。

### 5.4 生命周期和顺序

- SDK 为每次 open 分配单调的 `generation`；迟到旧 generation 事件不能进入新 session。
- domain `session_id`、`turn_id`、`response_id` 与 Provider id 分开；Provider id 仅保留在 adapter 内用于去重/映射。
- 每个 response 只有一个 terminal；duplicate/late terminal 和 late audio 被丢弃并计数。
- 输入队列和输出队列都有显式 byte/frame 上限；`send_audio` 在背压时阻塞或返回稳定 `busy`，不得无限缓存 PCM。
- 关闭由单一 terminal owner CAS 管理，并记录 `CloseInitiator`（client/provider/relay/timeout/shutdown）与
  `CloseDisposition`（clean/retryable/fatal）。幂等 close 先标记 owner，再关闭 transport，防止 close callback 反向分类。

response terminal 与 session terminal 是两条正交状态机，adapter 不得自行决定失败是否终止 session：

| 输入 | domain 结果 | 后续 session |
|---|---|---|
| `response.done(completed)` | 一个 `ResponseFinished(completed, usage)` | 保持 ACTIVE，接受下一 turn |
| `response.done(cancelled/incomplete)` | 一个 `ResponseFinished(cancelled/incomplete, usage?)` | 保持 ACTIVE；旧 response 的迟到 delta 丢弃 |
| `response.done(failed)` | 先给一个 `ResponseFinished(failed)` | 随后严格按下表给唯一 `SessionFailed`，event stream 结束 |
| Provider/relay fatal error | 不伪造 response terminal；给一个 `SessionFailed` | event stream 结束 |
| clean hangup | 若有未完成 response，先给一个本地 `ResponseFinished(cancelled, usage=None)`；再给 `SessionClosed` | event stream 结束 |

每个 output delta 必须同时匹配 `generation + response_id + item_id + output_index + content_index`；完成的
content/item/response 建立逐级 tombstone。多 output item 独立聚合，重复 `event_id` 幂等丢弃；未知 response/item 的 audio
不得归入“当前 turn”。

Provider failure 映射是闭合表：auth/key 类为 `unauthenticated/fatal`；rate-limit/quota 类为 `rate_limited/retryable`；
provider server/overload/transport 类为 `unavailable/retryable`；客户端发出的字段、顺序或已冻结 schema 违约为
`protocol_error/fatal`；未知 code/status 一律 `protocol_error/fatal`。`response.done(failed)` 先按上述顺序投影 response terminal，
再投影对应 session failure；Provider `error` 没有 response identity 时只投影 session failure。两种路径均不得继续 ACTIVE。

## 6. TokenSeller：共享控制面，Provider-native 数据面

### 6.1 新旧入口隔离

旧 `/v1/realtime` 与 `2026-07-voice-v1.1` 保持原样。新入口使用显式 Provider 路径：

- `POST /v1/realtime/qwen/client_secrets`
- `WSS /v1/realtime/qwen`

未来 OpenAI 对应 `/v1/realtime/openai/...`，不得通过修改 Qwen handler 进行协议切换。路由、mint schema 与 exact
control/wire version 已由四个 authority pack 冻结；S2 只能实现该 contract，不得静默复用旧路径。当前 `isRealtimePath`
会把 `/v1/realtime/*` 全部交给旧
gateway（`../TokenSeller/apps/api/src/modules/relay/realtime/realtime.gateway.ts:263-267`），S2 必须先改为精确路由表，否则
新 Qwen path 会被旧 GA strategy 吞掉。

### 6.2 控制面 kernel

从当前 gateway 抽取一个共享 session kernel，继续拥有：ephemeral mint/consume、Provider/model/protocol 绑定、Origin、
parent key revocation、per-key/global capacity、session/turn limits、idle/transport keepalive、privacy-safe metrics、
pre-auth/settlement 与 final teardown。kernel 通过 `RelayProtocolStrategy` 注入以下行为：

- Provider upstream URL/header 与 model alias；
- client/upstream frame validation；
- semantic milestones（turn committed、response terminal、audio bytes、Provider error）；
- cancel、cap-close 与 Provider-specific close；
- capability manifest。

计费不能依赖某个 wire event 的重命名结果。strategy 把 Provider-native terminal 投影成内部
`TurnTerminal(turn_key, status, usage)`；shared kernel 以独立 idempotency key 一次结算。重复/乱序 terminal
只能命中同一 settlement owner。

### 6.3 Qwen-native 数据面

本项目不再把 TokenSeller 旧代码中的 beta-flat schema 当成 Qwen 原生协议。2026-08-27 官方
Qwen3.5 Omni Realtime 新接入推荐 nested `session.audio.input/output.format`，客户端事件要求唯一 `event_id`；
音频输出事件是 `response.audio.*`。权威冻结为
[`qwen-native/2026-08-27.1`](protocols/qwen-native-2026-08-27.1/manifest.json)：

- 北京地域 workspace-scoped WSS，Bearer header，model query；不发送旧实现的 `OpenAI-Beta` header；
- exact upstream model `qwen3.5-omni-flash-realtime-2026-03-15`，public alias 仍为
  `qwen3.5-omni-realtime`，voice=`Tina`；选择 Flash snapshot 是为了成本与行为固定，不能用移动的 alias/`latest`；
- nested audio config，16 kHz mono raw PCM s16le 输入、24 kHz mono raw PCM s16le 输出；
- `response.audio.delta/done`、`response.audio_transcript.*`、`response.done.usage` 保持官方名称与字段；
- client `event_id` 由 SDK 生成，连接内唯一，Provider event id 不上浮为 domain identity。

官方 authority 来自
[Qwen client events](https://help.aliyun.com/zh/model-studio/client-events)、
[server events](https://help.aliyun.com/en/model-studio/server-events) 和
[native WSS guide](https://help.aliyun.com/zh/model-studio/realtime)，检索日期与 source URL 已写入 manifest。
同目录的 JSON vectors 与 `SHA256SUMS` 是 Python SDK codec、TypeScript relay strategy 和黑盒测试唯一共享的
跨语言 conformance artifact；S1 发布 wheel 时必须携带同一字节，S2 只消费该不可变 artifact，禁止两仓各写一套手工 fixture。

Qwen strategy 将通过校验的 Qwen client frame 作为 JSON text 转发给 DashScope，并把通过校验的 Qwen upstream
frame 保持原生 event name 和原生 schema 返回客户端。禁止 legacy mapping、事件重命名、relay `session_id/sequence`
注入或 envelope 包装。

TokenSeller 自有控制消息若必要，使用 `tokenseller.*` namespace 或 HTTP secret 响应承载；不得伪造成 Qwen 事件。
relay 可以为限额、计费和安全读取 event type、ID 与 decoded byte count，但不得记录或持久化 PCM、transcript 或 reply。
首版新入口可以声明 `resume=false`；不得为了沿用旧 resume/sequence contract 而污染 Qwen-native frame。

SDK 内独立 `RelayControlCodec` 先按 `type` namespace demux：`tokenseller.*` 交给 control codec，其他 frame 交给
Qwen adapter。control codec 独占版本/能力、budget/limits、relay error、expiring/closed 与 clean-close ack；Qwen adapter
不得解析这些扩展，transport 也不得把它们当 Provider frame。control protocol 固定为
[`tokenseller.realtime-control/2026-08-27.1`](protocols/tokenseller-realtime-control-2026-08-27.1/manifest.json)。该 authority
冻结 mint request/response、第一帧 open、created/error/expiring、close/closed、完整 capability manifest、digest 和失败 scenarios；
Python SDK 与 TokenSeller TypeScript 必须消费同一目录，不得分别定义 DTO。

### 6.4 版本与能力握手

secret mint、WSS 第一帧及响应的完整字段由 control authority 冻结。顺序固定为：HTTP mint 返回完整 manifest+digest；WSS
upgrade 只验证 path/header/token 的不可变 binding 且暂不连接 Provider；SDK 第一帧发送 `tokenseller.session.open`；relay
验证 exact control/sdk/wire version、correlation 和 digest 后才 consume 一次性 token、连接 Provider并发送
`tokenseller.session.created`。任何 mismatch 均在 token consume 与 Provider connect 前拒绝。绑定字段包括：

- `provider=qwen`
- `wire_protocol=qwen-native`
- `wire_version=2026-08-27.1`
- `sdk_protocol_version`
- public model/voice
- 完整 capability manifest 与其稳定 digest

capability manifest 使用 RFC 8785 JSON Canonicalization Scheme 的确定性 UTF-8 字节，并额外禁止所有非整数 JSON number、
NaN/Infinity 与 duplicate key；digest=`sha256(canonical_bytes)`。manifest 至少包含 protocol/control version、model snapshot、region、
input/output format、feature booleans、frame/queue limits、Provider cost revision 和 TokenSeller wallet pricing revision。HTTP 响应返回 manifest+digest，ephemeral row
保存 digest；WSS 第一个 `tokenseller.session.created` 回显同一 digest。SDK 每次 mint 都验证完整 manifest，不跨 session
缓存，也不只信 digest；digest 只用于把 HTTP、token binding 与 WSS 三段锁成同一 contract。

WSS upgrade 必须与 token 中绑定的 provider/path/model/protocol/version/digest 一致；path/protocol mismatch 在一次性 token
consume 前拒绝，其余不兼容最迟在打开 Provider socket 前结构化失败。SDK 在发送首个 session update 前核对响应，
不接受未知字段位置或静默 downgrade。
协议注册表冻结为 `openai-ga-compatible/2026-07-voice-v1.1`（legacy）、
[`qwen-native/2026-08-27.1`](protocols/qwen-native-2026-08-27.1/manifest.json) 与本轮实现但不启用的
[`openai-native/2026-08-27.1`](protocols/openai-native-2026-08-27.1/manifest.json)。延后的只有真实 OpenAI 凭证、
付费和生产路由。禁止 `latest`、按 model 猜 dialect 或静默降级。

每个 protocol authority 目录在最终 plan 获用户批准并进入 release chain 后是 append-only：manifest 固定检索日期、官方 source URL、模型语义、事件集合和 vector 名单；
`SHA256SUMS` 固定每个输入字节。官方文档后续变化不得原地改写现有目录，必须新增 protocol version、重新生成双方 codec
conformance receipt，并显式升级 capability digest。文档 URL 不是运行时依赖，也不能在构建或运行时联网推断 schema。
本次 2026-08-28 architecture reset 发生在任何 commit/candidate/consumer 之前，旧 draft hash 已在 challenge ledger 留痕，当前 bytes 是首次
release candidate 的唯一 authority；最终 plan 获批后禁止再原地修改。
路径/protocol mismatch 必须在一次性 token consume 前拒绝；TokenSeller 当前 mint 的 `body.model` 与实际全局 upstream
model 并非同一个 authority（`../TokenSeller/apps/api/src/modules/relay/realtime/realtime.controller.ts:139-148`；
`../TokenSeller/apps/api/src/modules/relay/realtime/realtime.gateway.ts:903-912`），S2 必须把两者变为同一受控绑定。

### 6.5 载荷、队列与拒绝边界

这些是 program limit，不声称是 Provider 的最大值；relay 与 SDK 取两者中更小者：

| 边界 | 上限 / 处理 |
|---|---|
| WSS JSON text frame | 1 MiB UTF-8；binary Provider/control frame 以 1003 拒绝 |
| input append decoded PCM | 64 KiB/事件，非空、偶数字节、canonical base64；超限 1009 |
| output audio delta decoded PCM | 256 KiB/事件，非空、偶数字节；违约为 `protocol_error` |
| event/type/id | type 128 bytes；event/session/response/item/call id 128 bytes且匹配 allowlist regex |
| transcript/text delta | 64 KiB/事件、1 MiB/response 累计；不在 relay 日志保留 |
| tool arguments/result | 各 64 KiB UTF-8 JSON/string；超限在 SDK 边界拒绝 |
| SDK input queue | 256 frames 且 4 MiB，先到任一上限即 backpressure |
| SDK output queue | 512 frames 且 16 MiB；产品未消费时 cancel response 并 fail `busy`，不无限缓存 |

重复 JSON key、非法 UTF-8/base64、奇数字节 PCM、缺失必需字段、已冻结 enum 中的未知 client event 均 fail closed。
未知 upstream event 在 relay 只做有界转发与 metadata 计数，SDK 的 exact-version adapter 将其映射为
`protocol_error`，不得猜字段。20 ms/640 B 与 100 ms/3200 B 均在允许范围，relay 不擅自聚合。

### 6.6 新 Qwen 路径的计费 authority

官方 Qwen3.5 Omni `response.done.usage` 提供 input/output token 及 text/audio 明细；新路径不能沿用旧
`audioSecondMinor` 近似计费。mint 时同时绑定不可变 Provider cost revision 与 TokenSeller wallet pricing revision；后者冻结
`wallet_currency=USD`、`usd_atoms_per_cny_minor`、source/observed/valid 时间、margin BPS、conversion/margin rounding 和各 modality
最大 token ceiling。pre-auth 以该 ceiling 算出的 whole-USD-cent 上界 hold，settle 以 exact `response.done.usage` 计价。

S2 在 `UsageEvent`/pricing snapshot 中新增 `inputTextTokens`、`inputAudioTokens`、`outputTextTokens`、
`outputAudioTokens` 与四个 `providerPriceCnyMinorPer1M` 安全整数。北京 Provider cost revision 的整数值分别为
330、2700、2000、10700 分/百万 token；这些值先形成单个 `provider_cost_numerator = Σ(tokens × price)`，它不是 USD wallet
atom。禁止 IEEE-754 金额运算和“每个分项先向上取整”。转换固定为
`baseCostAtoms = ceil(provider_cost_numerator × usd_atoms_per_cny_minor / 1_000_000)`，随后
`exactChargeAtoms = ceil(baseCostAtoms × (10_000 + margin_bps) / 10_000)`。沿用 precise-settlement v2 的
atom/sub-minor accumulator；每 turn 仅在这两个聚合边界向上取整，所有输入与结果以 BigInt/decimal string 持久化。
首个 wallet revision `tokenseller-qwen35-usd-v1` 的完整 authority 是 control pack 的
`wallet-pricing-revision.json`：ECB 2026-08-27 EUR/USD=1.1645、EUR/CNY=7.8258 的 exact cross-rate 向上量化为
`14_880_267_833` USD atoms/CNY minor，margin=1000 BPS，token ceilings=8000/16000/4000/8000，whole-cent hold=23。
其 SHA-256 同时进入 capability；换 rate、margin 或 ceilings 必须新建 revision，现有 session 不受热更新影响。
rollover 由 control pack 的 `wallet-pricing-rollover.json` 冻结：revision 与 activation event 都 append-only；mint 只在 latest effective
activation event 为 `ACTIVATE` 的 revision 中，选择覆盖 `mint_time + 30min max session + 5min settlement grace` 的最新 `valid_from` revision；新 revision 至少提前 7 天 staged，可与旧 revision
重叠，重叠期新 mint 选最新 `valid_from`，旧 session/turn 始终使用 mint 时完整快照。SDK 对每次 HTTP 返回的完整 capability 做 schema、
static field 与 RFC8785 digest 校验，不把示例 wallet revision ID/digest 硬编码为唯一允许值；相同 schema 的汇率轮换不要求 SDK release。
rollback 追加 `DEACTIVATE`，只有仍满足 horizon 的 predecessor 才能重新 `ACTIVATE`。无可用 revision 时在 token 创建前
`billing_rejected`。`RealtimeWalletPricingLifecycleService` 在 API startup、每次 native mint、pricing health probe 与 deploy preflight
评估 7/3/1 天阈值，以 revision id/threshold/remaining_ms metadata 去重告警；生产 preflight 必须验证 horizon 与 staged successor。
audio-output response 按官方“text+audio 时只计 audio 输出”规则忽略其伴生 output text tokens；text-only response 才使用
output text price。`usage.total_tokens`、input/output totals 与 details 不相等、负数或非安全整数均作为 `protocol_error`，不收费。

relay 在 observed `input_audio_buffer.committed`、manual `response.create` 或 tool follow-up `response.create` 时先生成不可预测
`relay_turn_id`，并以 `turn_key = HMAC-SHA256(server_billing_key, relay_session_id || relay_turn_id)` 作为 hold、settlement intent、
UsageEvent 与 reconciliation 的唯一 idempotency identity；它不依赖尚未存在的 Provider response ID。turn row 先持久化
`relay_turn_id/reservation_id/stage`，随后第一条 `response.created` 以 unique nullable `provider_response_id` CAS 绑定该 turn；
`response.done` 必须同时命中该绑定。TokenSeller 在转发 terminal 前，以 turn_key 唯一插入 durable settlement intent
（usage snapshot、Provider cost revision、wallet pricing revision、换算与 margin 输入、status、reservation id）。turn row 还必须冻结
user/parent API key/public+upstream model/credential/RequestLog/platform request/UsageEvent identity、wallet disposition 与 terminal-forward
state，使进程在任何阶段崩溃时，启动 reconciliation 只凭同一 turn row 重建；
唯一约束保证 terminal replay/close race 不能重复扣费。

首版每个 relay session 正常时一个 current turn；barge-in 过渡期固定最多两个 live turn：一个已 tombstone 的
`CANCELLING_PREDECESSOR` 与一个 `NEW_PENDING_SUCCESSOR`，后者具有独立 `relay_turn_id/turn_key/hold`。`speech_started` 后立即停止转发
旧音频并标记 predecessor；下一 commit 可创建 successor，无需依赖旧 `response.done` 的先后顺序。`response.created` 只能 CAS 到唯一
尚未绑定的 successor；其首批输出在 hold 成功前有界缓冲。两个 response ID 分别 exactly-once terminal/settlement；旧 terminal、迟到
audio、duplicate terminal 不得影响 successor。超过一个 predecessor 或一个 successor、无法唯一绑定、未知 terminal 才
`protocol_error`。由于 Qwen `response.cancel` 无 response_id，只有存在 Provider 当前 response 时发送一次；本地 tombstone/计费 identity
不能依赖 cancel ack。tool follow-up 复用 session，但创建新的 relay turn/hold，并遵守同一两槽边界。

| Provider/relay terminal | 用户结算 | hold / 平台处理 |
|---|---|---|
| `completed` + 完整 usage | 按绑定 Provider cost + wallet pricing revisions 的 exact usage settle 一次 | commit，随后转发 terminal |
| `cancelled`/`incomplete` + 完整 usage | 按 Provider 报告的实际 usage settle 一次 | commit；迟到 audio 不再转发 |
| `failed`，无论是否带 usage | 不向用户收费 | release；Provider 成本记 platform loss |
| clean client hangup / network loss / upstream close，尚无 `response.done` | 不向用户收费 | release；部分生成成本记 platform loss |
| terminal 缺 usage、usage schema 非法或 pricing revision 不存在 | 不向用户收费并 `protocol_error` | release；不得按音频秒猜价 |
| duplicate/reordered terminal | 不新增 intent/UsageEvent | 返回已有 settlement disposition |

pre-auth milestone：VAD 为原生 `input_audio_buffer.committed`，manual 为转发 `response.create` 前。hold 未完成时 relay 可在
有界缓冲内暂存首批输出；hold 拒绝则发 Provider-native cancel、丢弃缓冲并发 `tokenseller.error`。billing signal 来自
Qwen strategy 的 semantic observation，不依赖 control event 是否成功送达。

### 6.7 关闭协议

SDK 正常挂断先发送 `tokenseller.session.close{event_id,reason:"client_hangup"}`；relay 在自己的 CAS 中先登记
`initiator=client/disposition=clean`，停止接收新 PCM，cancel 活跃 response，完成/释放 pending settlement（最多 5 秒），
发送唯一 `tokenseller.session.closed` ack，再以 WSS 1000 关闭。SDK 收到 ack 后产生唯一 `SessionClosed`；ack 超时则本地
仍 clean-close transport，但诊断标记 `close_ack_timeout`，不改写成 Provider failure。

| 物理/语义来源 | relay authority | SDK terminal |
|---|---|---|
| 上述受控 hangup | client/clean | `SessionClosed(client_hangup)` |
| socket 1000 但无受控 close，且 relay 未发起 | peer/ambiguous | `SessionFailed(unavailable, retryable)` |
| network 1001/1006、missed pong | network/retryable | `SessionFailed(unavailable, retryable)` |
| Provider error 或 abnormal close | provider/retryable-or-fatal | `SessionFailed(mapped_code)` |
| Provider clean close without session terminal | provider/protocol_error | `SessionFailed(protocol_error)` |
| idle/session deadline | relay/clean-expired | `SessionClosed(expired)` |
| server shutdown | relay/retryable | `SessionFailed(unavailable, retryable)` |

relay 与 SDK 都必须在调用物理 `close()` 前发布 terminal owner；后续 `error/close` callback 只能读取已有 disposition，不再
发错误或 warn。pending settlement/hold 的 finalizer 至多一个，5 秒后强制关闭但 durable intent 由 reconciliation 接管。

SDK close 的精确顺序是：CAS 声明本地 close intent → 发送 control close → 最多等待 5 秒 closed ack → 产生上述唯一 domain
terminal → 才调用 transport `close(1000)`。ack timeout 只增加 `close_ack_timeout` diagnostics，仍按已声明 client/clean 完成；
若在 intent 前先收到物理 close，则必须按 ambiguous/network/provider 行处理，不能倒推成 clean。

## 7. 两条目标调用链

### 7.1 建连与首轮

1. 用户点击产品唯一“开始通话”动作；产品先准备本地 capture/playback，但尚不发送 PCM。
2. 产品调用 service-sdk `RealtimeClient.open(RealtimeOpenRequest)`。
3. SDK 的 `CredentialMinter` 以长期 TokenSeller key 通过 HTTPS mint 一次性、单会话绑定 token。
4. SDK 核对 provider/wire/sdk version 与 capability digest，然后由 WSS transport 连接对应 Provider 路径。
5. Qwen adapter 按 `qwen-native/2026-08-27.1` 生成唯一 `event_id` 并发送 nested
   `session.audio.input/output` 的原生 `session.update`，解析原生 `session.created/session.updated`，投影 `SessionReady`。
6. 产品收到 `SessionReady` 后开始送 16 kHz mono PCM s16le；SDK base64 编码为 Qwen 原生 append frame。
7. Qwen VAD/response 原生事件通过 TokenSeller，SDK adapter 投影为稳定 transcript/audio/terminal event。
8. 产品只处理稳定事件：更新通话状态、播放 PCM、显示文本；TokenSeller 在内部 semantic terminal 上一次结算。

### 7.2 打断与关闭

1. 产品本地检测到用户在 AI 播放期间开始说话，立即停止本地播放并调用 `cancel_response()`。
2. Qwen adapter 发送 Provider-native cancel；旧 response 后续 audio/terminal 由 session generation/response owner 去重。
3. 用户挂断时 SDK 先声明 `client/clean` intent，发送 control close 并等待 closed ack；只在 ack/5 秒超时后关闭 WSS。
4. Provider/网络/timeout 路径分别投影稳定 `unavailable/protocol_error/timeout`，不得复用 clean close，也不得重复结算。

## 8. 产品职责

### 8.1 AIPhone

AIPhone 保留手机麦克风、GStreamer capture、AEC、播放队列/cursor、call UI、wake/screen 策略和产品状态投影。
迁移后删除产品内 secret mint、WSS、Provider session handshake、JSON/base64 codec、Provider event allowlist 与错误映射；
装配点从 `RealtimeRelayBridge` 改为已发布且 SHA 固定的 service-sdk `RealtimeClient`。

唯一进程拓扑冻结为：agent-runtime daemon 持有 TokenSeller credential、SDK `RealtimeClient`/session、WSS 与 diagnostics；
手机 UI/audio host 持有 capture/playback/AEC。两者保留现有 Realtime AF_UNIX socket 和 peer-UID 边界，但 local
client/server、provider-neutral framing 与 codec 迁入 SDK 的独立 `realtime.local` transport，不能与 Provider wire 合并，
更不能扩展现有 durable Unix RPC。AIPhone 删除 `realtime_voice_bridge.py` 的 Provider 职责；产品
`RealtimeVoiceServer` 只装配 SDK local server 与产品 audio lifecycle，`RealtimeVoiceController` 只消费 SDK stable event。

两产品的 local wire 唯一 authority 是
[`simple-harness.realtime-local/2026-08-27.1`](protocols/realtime-local-2026-08-27.1/manifest.json)，随 S1 wheel 和
TypeScript schema/vector 一起发布。它冻结 JSON control/event、generation/correlation、24-byte binary PCM header、AF_UNIX
length prefix、WSS framing、双向 sliding-window ACK/backpressure、late generation 丢弃与 terminal 顺序。AIPhone 使用 peer UID
认证的 AF_UNIX profile；Simple Harness 使用 loopback WSS + 首帧 shared-secret profile，但二者消费同一 semantic schema。

### 8.2 Simple Harness

Simple Harness 保留 Tauri UI、设备选择、capture/playback、产品配置/credential storage、health 暴露和 agent/tool/memory
产品接线。加载 App 或 text-only backend 不能自动 mint token、建 WSS 或加载旧 Whisper/CosyVoice/Silero 栈。

唯一进程拓扑冻结为：Python backend 持有 TokenSeller credential、SDK `RealtimeClient`/session、WSS 与 diagnostics；Tauri
WebView 持有 getUserMedia/capture/playback/UI。新增 loopback-only `/ws/realtime-voice`，第一条 JSON 是
`local.auth{version:"2026-08-27.1",secret}`，认证成功后方向即决定 binary PCM 含义（WebView->backend 为 16 kHz input，反向为协商后的
output）；JSON 只传 provider-neutral `call.start/stop/barge_in` 和 stable events。channel 使用 shared secret、1 MiB frame、
上述 queue limits 与 local authority 的双向 ACK window；`bufferedAmount` 只是 WebView 侧额外闸门，不是唯一背压机制。
secret、PCM 与文本不进日志。

backend 只有收到 `call.start` 才 open SDK session；`SessionReady` 后才回 `call.ready` 并允许 mic，上游失败先保持 mic disabled。
hangup/backend shutdown 先关 SDK session，再清 playback/capture/channel。`/health.voice` 返回 SDK version、protocol id、
capability digest 与 ready/disabled reason，前端只有全部匹配才启用唯一 primary action。

S4 必须新增显式 `idle -> connecting -> active -> error/idle` 电话式状态机与唯一 primary action；不能仅把
`VOICE_INPUT_ENABLED` 改为 true，因为当前 hook 在 mount 后自动连接旧 channel。既有 `/ws/audio` 旧 VoicePipeline 不成为
Realtime 入口；它仅在独立 legacy flag 显式打开时保留，默认仍禁用。

## 9. 安全、隐私与可观测

- 长期 TokenSeller/Provider key 不进入 URL、普通日志、事件、持久化 PCM 或客户端 diagnostics bundle。
- ephemeral token 最小 TTL、一次连接、单 provider/path/model/protocol/session 绑定，失败后不可降级到长期 key。
- SDK 在 open 时生成 `corr_` + 26 位 Crockford Base32 的随机 128-bit correlation；只接受该 exact pattern/length，
  经 mint JSON 与 WSS `tokenseller.session.open` 传递。它不是身份或幂等 key，碰撞/伪造只影响日志分组。产品、SDK、relay
  原样记录这一 opaque 值；session/provider/key/credential/request ID 只记录 server-secret HMAC-SHA256 截断摘要，
  不记录 raw ID，也不记录 transcript、reply、base64 或原始 PCM。
- 最小事件：secret mint、WSS connect、session update/ready、input frame/byte count、VAD milestone、first output latency、
  response terminal、settlement disposition、close initiator/disposition。
- 反向敏感信息扫描属于 release gate；日志可见性不能以记录正文来换取。

## 10. 兼容与发布边界

- Realtime API 是 additive public surface；现有 durable public API/conformance 不改。
- 根 `__all__`、`tests/public-api.json`、版本、BOM、wheel metadata 和 consumer pins 必须同步更新。
- WSS 依赖采用 bounded optional extra；core import 不打开 socket，也不拉起音频/Provider。
- S1 release manifest 独立于现有 BOM，记录 service-sdk wheel URL、version、wheel SHA-256、四个 authority pack 的
  protocol vector SHA256SUMS、
  WSS optional extra 的完整 lock closure 与 rollback version。AIPhone 在既有 exact dependency/provenance gate 核对该 manifest；
  Simple Harness 把同一 wheel 字节放入 `backend/vendor`、lock 与 PyInstaller hidden-import/metadata collection，并用相同 SHA
  测试。两个 consumer gate receipt 必须证明 wheel byte SHA 相同；不得只比 version，也不消费 sibling source tree。
- OpenAI offline adapter 是本轮交付，不是 future stub：以 2026-08-27 检索的官方
  [Realtime WebSocket guide](https://developers.openai.com/api/docs/guides/realtime-websocket)、
  [Realtime client events](https://developers.openai.com/api/reference/resources/realtime/client-events)、
  [Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations) 与
  [GPT-Realtime-2.1 model page](https://developers.openai.com/api/docs/models/gpt-realtime-2.1) 冻结
  `openai-native/2026-08-27.1` fixture pack。它覆盖 nested session/audio、append、`response.output_audio.*`、response terminal、
  error、tool call/result、cancel、truncate，并复用 control authority 的受控关闭 conformance scenario。fixture 中的
  `gpt-realtime-2.1` 只是 2026-08-27
  官方 WebSocket 示例/模型语义的离线 authority，不是已批准的生产 model pin。测试 product 只请求 Qwen/OpenAI 的共同
  feature 交集；Provider 特有 feature 通过
  `RealtimeFeatureSet` 显式 skip/unsupported。真实 credential、付费与生产路由保持关闭。
- “完整 offline adapter”精确定义为：实现 OpenAI authority manifest 的全部 client/server event、event_vector_map、
  negative/order cases、稳定错误和 control close conformance；未列入该冻结 manifest 的新 Provider 事件一律
  `protocol_error`，不在本轮自行扩展。完整不表示真实账号、网络、价格或生产 readiness。
- 四个 slice 独立提交、testcase 与 gate receipt：S1 SDK；S2 TokenSeller；S3 AIPhone；S4 Simple Harness。
- S3/S4 只能基于 S1 发布制品，S3 的成功不能替代 S4，S2 的生产 health 不能替代真实 Provider/计费验证。

## 11. 已知技术债与明确非目标

- 当前 TokenSeller gateway 把 mapping、session state、resume、计费时机、limits、socket 与 teardown 集中在单文件；S2 必须先
  提取 shared kernel/strategy seam，不能复制整个 gateway 建第二套实现。
- 当前旧协议版本 authority 在代码常量与历史 protocol freeze 文档间存在漂移；legacy 唯一运行值以代码中的
  `2026-07-voice-v1.1` 为准，S2 必须同步历史说明而不是改变旧行为。
- 当前 AIPhone 同时存在产品本地 domain codec 和 Provider codec；迁移窗口必须短，最终通过边界扫描证明无双重 wire authority。
- 当前 Simple Harness 旧 VoicePipeline 仍可显式启用；Realtime 接入不能无意加载或删除该历史路径。
- 本轮不做真实 OpenAI 付费/生产、不做 WebRTC/SIP、不实现本地模型、不删除旧 TokenSeller v1.1、不改变 Harness durable
  command、Memory、Tool 或文本 Chat authority。

## 12. 架构验收不变量

1. 产品源码中没有 Qwen/OpenAI wire field、Provider URL、base64 JSON codec 或 client-secret HTTP contract。
2. 同一测试产品对 Qwen 与 OpenAI fixture 只替换 composition adapter/profile，产品逻辑 diff 为零。
3. Qwen-native TokenSeller 路径不经过 `mapClientSessionUpdate` 或 `EVENT_RENAME`。
4. clean client close 不产生 `upstream_error`；一个 turn 只产生一个 semantic terminal 与一个 settlement owner。
5. 任何版本/capability 不匹配在 Provider connect 之前失败。
6. import/启动 service-sdk、AIPhone 或 Simple Harness 不自动打开麦克风或网络会话。
7. 真实设备/UI 证据与自动化证据分开记录，不以 fixture 冒充生产 Provider 可用。
