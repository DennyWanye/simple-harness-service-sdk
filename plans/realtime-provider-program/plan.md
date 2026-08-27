# Plan：Realtime Provider Program

<!-- plan-status: finalized -->

## 主要矛盾

决定成败的核心问题不是“再接一个 WebSocket”，而是把一个长生命周期、双向、低延迟、可打断且会产生计费副作用的媒体 session，放进现有三套产品/服务中，同时保持四个 authority 彼此隔离：

1. service-sdk 拥有产品无关的 Realtime session 语义、顺序、背压和 Provider adapter；
2. AIPhone / Simple Harness 只拥有麦克风、播放、UI 和产品策略；
3. TokenSeller 拥有长期凭证、鉴权、并发、限额、计费和生产 relay；
4. Qwen / OpenAI 的原生 wire contract 只存在于各自 adapter/strategy 中。

当前 service-sdk 的 `ServicePort` 是五个短 RPC（`src/simple_harness_service/client.py:19-51`），Unix client 每次调用新建连接（`src/simple_harness_service/transports/unix.py:224-276`）；AIPhone 自己持有 mint、WSS、Provider event mapping（`../AIPhone/agent_runtime/src/aiphone_agent_runtime/realtime_voice_bridge.py:44-165`）；TokenSeller 旧 gateway 把所有 `/v1/realtime/*` 都收入旧协议（`../TokenSeller/apps/api/src/modules/relay/realtime/realtime.gateway.ts:263-267`），并向 DashScope 发送旧 `OpenAI-Beta` 头（同文件 `903-912`）；Simple Harness 则把旧 VAD→ASR→Harness→TTS 明确标为待 Realtime 替换（`../simple_harness/backend/deskpet/voice_runtime.py:4-9`）。因此正确解法是并行 Realtime 子系统、共享控制面 kernel、Provider-native strategy 和产品本地 transport 分层，而不是扩展 durable RPC、复制旧 gateway 或继续让产品解析 vendor JSON。

## 事实源、范围与冻结基线

- 验收事实源：`acceptance.md`，覆盖 AC-1 至 AC-8、TO-A1 至 TO-A8、TO-R1 至 TO-R4。
- 保障事实源：`assurance-contract.json`，profile=`standard`；最大影响边界是不允许凭证/内容泄露、跨会话播放、重复计费、文本聊天回归或旧入口静默破坏。
- 架构事实源：`ARCHITECTURE/ARCHITECTURE.md` 与 `ARCHITECTURE/protocols/*` 四个 append-only authority pack。
- 基线事实源：`baseline.md`；四仓现有红只按本计划 verification 目录的 exact signature 隔离，产品仓 known-failure 文件不变。
- 计划挑战事实源：`verification/plan-iteration-20260827/`；五个 required specialist 全部完成。三项关键假设由
  `verification/spikes/receipt.md` 实码验证，架构已改为不依赖 Qwen barge-in 的未文档化事件顺序。
- 校准提交：service-sdk `f966c6c3`、TokenSeller `136b2002`、AIPhone `15408198`、Simple Harness `362e5149`。
- 现有无关工作树修改全部保留；每个 slice 只提交本计划列出的 scoped paths。
- 本轮明确不做：真实 OpenAI 付费/生产启用、WebRTC/SIP、本地模型、历史账单迁移、旧 Realtime 删除、文本 Harness authority 改造。

## 最佳实践调研与本项目适配

| 实践 | 官方/当前证据 | 本项目适配 | 结论 |
|---|---|---|---|
| server-to-server Realtime 使用 WebSocket；browser/mobile 直连更适合 WebRTC | OpenAI 官方 Realtime WebSocket guide；`OOS-WEBRTC-SIP` | 产品不持有 Provider 长期凭证，实际链路是 WebView/phone→本地 daemon/backend→TokenSeller WSS | 本轮使用本地 IPC + server-side WSS；不让产品直连 Provider，不实现 WebRTC |
| Provider 原生事件由独立 codec/adapter 解释 | Qwen client/server events 和 OpenAI client/server events authority packs | Qwen 与 OpenAI 的音频事件名、session nesting、错误和 capability 不同 | semantic API 稳定；adapter 独立；禁止 Qwen→OpenAI GA→domain 的二次映射 |
| 协议协商使用 exact version 与 canonical capability | RFC 8785；control authority pack | Python/TypeScript 跨语言，需要 byte-identical manifest | 仅整数 JSON number、拒绝 duplicate key/NaN/Infinity；SHA-256 锁定 HTTP mint、token binding、WSS created 三段 |
| exactly-once 副作用由 durable idempotency identity + unique constraint 保证 | TokenSeller 现有 `WalletUsageSettlement.usageEventId @unique`（`../TokenSeller/apps/api/prisma/schema.prisma:550-568`） | Provider response id 在 pre-auth 时还不存在，不能作为 hold identity | relay 先生成 `relay_turn_id`，HMAC 得 `turn_key`；Provider response id 后续 CAS 绑定；terminal 前写 durable settlement intent |
| optional transport dependency 不污染 core import | service-sdk 当前是 pure typed wheel，根 `__all__` 有快照（`src/simple_harness_service/__init__.py:3-67`） | 两个消费者都需要 WSS，但 durable command 用户不应被迫加载它 | 新增 `[realtime]` extra，核心 domain/fixture 无网络 import；WSS/HTTP connector 延迟 import |
| 单一 terminal owner 防止 close callback 反向误分类 | TokenSeller 当前 upstream `close/error` 都调用 `disposePair`（`../TokenSeller/apps/api/src/modules/relay/realtime/realtime.gateway.ts:923-947`），主动 teardown 也会关闭 upstream（同文件 `2195-2231`） | 旧路径存在正常关闭告警误报，新 native path 必须从结构上消除 | shared terminal owner CAS；先夺权再关 socket；callback 只观察 owner，不另建错误 authority |

不采用的备选：

- 不把 Realtime 方法加入现有 `ServicePort`：会迫使所有 durable adapter 实现流式生命周期并破坏现有 conformance。
- 不复制 2500 行 legacy gateway：会复制鉴权、容量、计费和 teardown authority，未来 OpenAI 再复制第三次。
- 不直接复活 Simple Harness 旧 `/ws/audio` pipeline：它是本地 ASR/TTS 串联，不是 Provider Realtime，且默认关闭是已批准行为。
- 不在产品层引入 `provider == "qwen"` 分支：这会违反 AC-1/AC-4，并让后续三个 SDK 无法一起更新。
- 不用音频秒数估算新 Qwen 账单：官方 terminal 已给 exact modality token usage；旧 `RealtimeBillingService.settleTurn()` 的 `ceil(audioSeconds)`（`../TokenSeller/apps/api/src/modules/relay/realtime/realtime-billing.ts:105-130`）只保留给 legacy。

## 解剖麻雀：一条完整 Qwen 语音 turn

1. 产品 composition 创建 `RealtimeClient(profile, credential_minter, transport, adapter)`；UI 只调用 `open()`。
2. SDK 的 `TokenSellerHttpsCredentialMinter` 通过 HTTP control mint `POST /v1/realtime/qwen/client_secrets`，校验完整 manifest、RFC 8785 digest、exact SDK/control/wire version、model snapshot、Provider cost revision、TokenSeller USD wallet pricing revision 和 16k/24k PCM format。
3. SDK 用 `Authorization: Bearer eph_*` 打开 `WSS /v1/realtime/qwen`，第一帧发送 `tokenseller.session.open`。TokenSeller 在 path/protocol/digest/Origin 校验通过后才 consume token、占用容量并连接 DashScope。
4. TokenSeller Qwen strategy 用 Bearer-only、workspace endpoint 和 exact snapshot 连接 Provider；Qwen 原生 `session.*` / `response.audio.*` 不重命名。`tokenseller.*` control frame 独立 demux。
5. 产品每个 PCM chunk 先过本地 24-byte header 与 generation/sequence ACK window，再由 SDK 校验 16k mono s16le、queue/frame 上限，Qwen adapter 编码 `input_audio_buffer.append`。
6. 原生 `input_audio_buffer.committed` 触发 TokenSeller 创建 `relay_turn_id/turn_key` 与 wallet hold；`response.created` CAS 绑定 Provider response id。正常时只有一个 current turn；barge-in 过渡最多保留一个 `CANCELLING_PREDECESSOR` 和一个 `NEW_PENDING_SUCCESSOR`，各自独立 identity/hold/terminal/settlement。
7. 原生 transcript/audio event 经 Qwen adapter 投影成稳定 domain event；SDK 用 generation + response/item/output/content identity 去重，产品只播放当前 response 的音频。
8. `response.done` 先由 relay 校验 usage totals/details 和 turn binding，BigInt 汇总 CNY Provider cost numerator，再按 mint 冻结的 `usd_atoms_per_cny_minor` 与 margin 两次 aggregate-ceil 得 USD atoms，唯一写 settlement intent/RequestLog/UsageEvent/WalletUsageSettlement；成功或确定 release disposition 后才向客户端转发 terminal。
9. SDK 只发一个 `ResponseFinished`。若用户打断，产品先停本地播放，再调用 `cancel_response()`；迟到旧音频因 tombstone/generation 被丢弃。
10. hangup 使用 `tokenseller.session.close`/ack；relay terminal owner 先锁定 `initiator=client, disposition=clean`，最多 5 秒完成/移交 settlement，再物理 close 1000。正常关闭不得变成 `upstream_error`。

这个模式同样适用于未来 OpenAI：步骤 1、2、5、8、9、10 的产品/domain/control 语义不变，只替换 provider adapter/strategy 和 authority pack。

## 文件影响总览

### S1 — service-sdk 0.3.0 candidate

| 文件 | 职责 | 本次改动 |
|---|---|---|
| `pyproject.toml` | 版本/依赖/打包 | 升级 service 0.3.0；exact Harness 0.6.2 / Memory 0.5.2；新增 `[realtime]` extra；打包 protocol authority |
| `src/simple_harness_service/realtime/contracts.py` | 稳定 domain contract | audio format、feature、open request、闭合 event union、error/close enums |
| `.../realtime/ports.py` | SPI | ProviderAdapter、Transport、CredentialMinter、Session protocols |
| `.../realtime/client.py` | 产品入口 | `RealtimeClient.open` 与 profile 注入 |
| `.../realtime/session.py` | 生命周期 | ordering、tombstone、backpressure、terminal owner、tool FSM |
| `.../realtime/relay_control.py` | TokenSeller control | mint/open/created/close codec、canonical digest |
| `.../realtime/local.py` | 本地 IPC codec | 24-byte PCM header、control JSON、ACK window、generation |
| `.../realtime/adapters/qwen_wire.py` / `qwen_omni.py` | Qwen | native codec + semantic adapter |
| `.../realtime/adapters/openai_wire.py` / `openai.py` | OpenAI offline seam | 官方 fixture codec + adapter；live connector disabled |
| `.../realtime/connectors/tokenseller.py` | relay composition | 具体 `TokenSellerHttpsCredentialMinter` + returned-path-bound `RelayWebSocketTransport`；HTTP/WSS secret redaction |
| `.../realtime/transports/websocket.py` | WSS | bounded optional primitive；无 provider mapping |
| `.../realtime/transports/unix_local.py` | AIPhone local lifecycle | `UnixRealtimeHost/Channel`，peer UID、accept/start/stop、session-before-channel teardown |
| `.../realtime/transports/loopback_websocket.py` | Simple Harness local lifecycle | `LoopbackWebSocketRealtimeHost/Channel`，authority path、Origin、first-frame secret、idempotent close |
| `.../realtime/testing/conformance.py` | 可复用测试 | 两 adapter 同 consumer suite、fake transport/mint |
| `src/simple_harness_service/realtime/protocols/**` | wheel authority | 四 pack 的 byte-identical packaged copy |
| `src/simple_harness_service/__init__.py`, `tests/public-api.json` | 公共 API | 仅导出 provider-neutral symbols |
| `compatibility-bom.json`, `realtime-release-manifest.schema.json`, `tests/test_wheel_contract.py`, `scripts/build_candidate.py` | release | 0.3.0/0.6.2/0.5.2 三 SDK release unit；Python/TypeScript authority bundle、3.11/3.13 realtime lock、可重复构建 |

### S2 — TokenSeller Qwen-native relay

| 文件 | 职责 | 本次改动 |
|---|---|---|
| `apps/api/prisma/schema.prisma` + 新 migration | durable binding | additive immutable native binding（含 correlation）、field-complete `RealtimeTurn`、insert-only wallet pricing revision/activation、modality usage/pricing fields |
| `realtime-client-secret.service.ts` | eph binding | provider/path/control/wire/digest/model snapshot 固定；legacy default 不变 |
| `realtime-handshake.ts` | auth kernel | 分离 shared checks；native 首帧前不 consume；legacy 顺序保留 |
| `realtime-router.ts` | exact upgrade route | `/v1/realtime` → legacy；`/v1/realtime/qwen` → native；未知拒绝 |
| `realtime-native.controller.ts` / `.dto.ts` | native HTTP control | exact mint schema、manifest+digest、voice/model/version 白名单 |
| `realtime-control.ts` | control codec | 共享 authority pack、strict schema、open/close/limits/error |
| `realtime-kernel.ts` | shared control plane | capacity/key gate/credential/idle/terminal owner/turn milestone |
| `realtime-strategy.ts` | provider SPI | dial、validate、milestone、cancel、failure/close classification |
| `qwen-native.strategy.ts` / `qwen-native.codec.ts` | Qwen data plane | Bearer-only native passthrough、semantic milestone extraction |
| `realtime-native-session.ts` | session state | one current + bounded cancelling predecessor/new successor、turn binding、terminal/tombstone、controlled close |
| `realtime-native-billing.ts` | exact money | HMAC turn key、CNY numerator→USD atom revision、hold、durable intent、reconcile |
| `realtime-wallet-pricing-lifecycle.ts` | revision lifecycle authority | append-only ACTIVATE/DEACTIVATE、mint eligibility、7/3/1-day metadata alerts、deploy preflight 与 rollback |
| `realtime-native-observability.ts` | privacy logs | opaque correlation、stage/code/bytes/timing only |
| `apps/api/src/modules/relay/realtime/authority/**`, sync/check script | immutable TypeScript handoff | 从 S1 release manifest 导入 exact bundle；构建前校验 authority root digest，禁止手抄/漂移 |
| `realtime.module.ts`, `main.ts` | wiring | 注册 router/native service，drain 两种 session；旧 endpoint 继续可用 |
| `scripts/deploy.sh`, `docker-compose.prod.yml` | production release | expected/target SHA、backup hash、SHA-tagged predecessor image、migration/health/native+legacy smoke、explicit rollback |

### S3 — AIPhone consumer

| 文件 | 职责 | 本次改动 |
|---|---|---|
| `agent_runtime/pyproject.toml` + wheel lock/artifact tests | immutable dependency | 钉死 service-sdk 0.3.0 wheel URL/SHA；移除产品直持 WSS dependency（若无其他使用） |
| `realtime_voice_bridge.py` | 旧 Provider ownership | 删除；composition 改为 SDK `RealtimeClient` |
| `realtime_voice_contracts.py` | 旧 local/domain contract | 删除或仅留兼容 re-export；新代码只用 SDK local/domain contract |
| `realtime_voice_server.py` | daemon owner | 保留 daemon/credential ownership；SDK local Unix server + session profile |
| `realtime_voice_controller.py` | UI reducer | 保留状态/UI/audio策略；消费 SDK event；barge-in=停播+cancel+旧 generation 丢弃 |
| `realtime_audio.py` | 设备音频 | 保留 16k capture/24k playback/AEC；适配 SDK PCM format |
| `application.py`, `voice_ui.py`, `composition.py`, `config.py`, `production_services.py`, `arm64_entrypoint.py` | production wiring | 替换 daemon 与 UI 两个 authoritative constructor；从 `.env`/配置给 SDK TokenSeller endpoint/API key/profile；不传 vendor wire |
| phone UI modules/tests | 电话式 UI | 唯一主按钮 start/hangup/retry；session ready 后自动持续聆听；无“开始说话” |

### S4 — Simple Harness consumer

| 文件 | 职责 | 本次改动 |
|---|---|---|
| `backend/pyproject.toml`, `backend/vendor/*`, `backend/deskpet-backend.spec`, candidate manifest/tests | immutable dependency | vendored service-sdk 0.3.0 wheel+SHA；收集 package data/distribution metadata；冻结 executable 启动时校验 |
| `backend/deskpet/realtime_voice.py` | product composition | SDK profile、mint/WSS、loopback local WSS server；不解析 Provider wire |
| `backend/main.py` | route/health/lifecycle | authority 唯一路径 `/ws/realtime-voice`；startup 不自动 mint/connect；shutdown clean close；旧 `/ws/audio` 仍 disabled |
| `tauri-app/src/ws/RealtimeChannel.ts` | local transport | control JSON + 24-byte binary PCM/ACK；不含 Qwen/OpenAI 字段 |
| `tauri-app/src/hooks/useRealtimeVoice.ts` | product state | start/hangup、mic/playback、generation、barge-in、错误显示 |
| `useAudioRecorder.ts`, `useAudioPlayer.ts` | browser audio | 复用 16k capture/24k playback，清理 alert/正文 console，接 SDK negotiated format |
| `voiceAvailability.ts`, `ChatView.tsx`, `App.tsx` | UI | disabled placeholder → 单一电话式入口；不在 mount 自动申请 mic 或连接 |
| frontend/backend tests + E2E | regression | isolated userdata real Tauri voice + text Chat critical smoke |

## Complexity inventory

| 复杂度表面 | 是否新增 | 理由 / 绑定 |
|---|:---:|---|
| 新公共 API | 是 | AC-1；独立 Realtime API，避免破坏 durable `ServicePort` |
| 新可选依赖 | 是 | AC-2/AC-3；`httpx`/`websockets` 仅 `[realtime]`，core import 不加载网络 |
| 新协议 authority | 是 | AC-2/AC-4/AC-7；四个 append-only pack 已冻结并校验 hash |
| 新抽象层 | 是 | AC-1/AC-4；ProviderAdapter/Strategy 是后续 OpenAI 不改产品的必要扩展点 |
| 新本地 binary framing | 是 | AC-5/AC-6、FAIL-CROSS-SESSION/FAIL-OVERSIZE；24-byte header 与 ACK window |
| 新 TokenSeller 持久化状态 | 是 | AC-3、FAIL-BILLING-DUP；turn/settlement intent 和 exact usage breakdown |
| 新后台任务 | 是 | AC-3；仅 TokenSeller bounded settlement reconciler，不在 SDK 建 durable worker |
| 新 route | 是 | AC-3/AC-7；`/v1/realtime/qwen` 与 legacy 精确隔离 |
| 新日志类别 | 是 | AC-8、FAIL-CLOSE-CLASS/FAIL-LOG-LEAK；只记录稳定 metadata |
| 可复用现有实现 | 是 | TokenSeller eph/auth/capacity/credential pool/holds；产品现有 audio engine/hooks；SDK release pipeline |
| 不新增 | — | WebRTC/SIP、本地模型、OpenAI live、旧 endpoint 删除、文本 authority、原始音频存储 |

## Assurance、攻击面与停止追踪点

### 入口与 trust boundary

```text
untrusted mic PCM / product control
  -> authenticated local IPC (peer UID or loopback secret + Origin)
  -> trusted SDK domain/session
  -> TLS TokenSeller control + WSS using one-use eph
  -> trusted TokenSeller control plane
  -> TLS official Provider endpoint using server-held key
```

- `ADV-UNTRUSTED-CLIENT` 可发送 wrong Origin、重放 eph、乱序/超限 frame；每个边界 strict validate，先做非破坏检查再 consume token。
- `ASSET-CREDENTIALS`：长期 key 只在 product daemon/backend 与 TokenSeller server；eph 不进 URL、普通日志或持久化客户端状态。
- `ASSET-CONTENT`：SDK/relay 只流转 PCM/文本，不持久化；日志禁止正文和 base64。
- `ASSET-BILLING`：只有 TokenSeller durable turn/UsageEvent/Wallet settlement 能改变钱包；SDK/product 无账单 authority。
- `ASSET-SESSION`：SDK generation + identity tuple 和 relay turn binding 双层隔离。
- `ASSET-COMPAT`：legacy route、text chat、durable command 均跑独立 regression。
- 停止追踪点：可信开发/生产 host、TLS 实现、官方 endpoint、已核验 wheel hash；Provider 全局可用性、host compromise、OpenAI live、WebRTC/SIP、本地模型均按 contract OOS，不把它们变成阻断项。

### 失败语义

- `FAIL-PROTOCOL-DRIFT`：authority vectors + strict codec + exact negotiation；unknown code/schema fail closed。
- `FAIL-TOKEN-REPLAY`：path/origin/protocol/digest 先验，单次 atomic consume；secret repr/log redaction。
- `FAIL-CROSS-SESSION`：generation + local sequence + response/item/output/content tuple；terminal tombstones。
- `FAIL-BILLING-DUP`：turn_key unique + provider response CAS + settlement intent unique + existing wallet accumulator。
- `FAIL-CLOSE-CLASS`：single terminal owner；initiator/disposition 决定日志与 close，不由后到 callback 改写。
- `FAIL-OVERSIZE`：control/provider/local 三层 frame/decoded-audio/queue hard limit。
- `FAIL-VERSION-SKEW`：manifest exact match、wheel version/SHA、legacy/new route exact routing。
- `FAIL-LOG-LEAK`：structured allowlist fields + artifact-level negative scan，禁止异常字符串直出。

## Release 依赖与提交策略

```text
S1 service-sdk v0.3.0 wheel + SHA + conformance receipt
  -> S2 TokenSeller native relay consumes exact authority bundle/root digest
     -> production SHA + real Qwen/ledger receipt
       -> S3 AIPhone consumes exact wheel and production protocol
       -> S4 Simple Harness consumes same exact wheel and production protocol
```

- S3 与 S4 只在 S2 production gate 后并行；二者不能依赖 service-sdk 源码路径。
- 每个 slice 在自己的仓库产生 scoped commit、candidate hash、testcase 和 gate receipt；失败可单独回滚。
- TokenSeller 部署只通过改造后的 `scripts/deploy.sh --expected-current-sha <old> --target-sha <new>`：clean/ancestor preflight、pg_dump+SHA、SHA-tagged新旧镜像、migration/container/public-health/native+legacy smoke；成功 admission 后才清理且保留 predecessor。回滚使用 retained predecessor image，不回滚 additive schema。远程只使用 canonical `ssh chinzy`。
- AIPhone 当前分支名不因本计划自动重命名；发布提交保持 scoped。Simple Harness/TokenSeller 的现有无关 dirty files 不纳入提交。

## 任务清单

### S1 Task 1 — 冻结 0.3.0 domain API 与 public surface  [AC-1, AC-4, AC-7]

- 改动：创建 `realtime/contracts.py`、`ports.py`、`client.py`、包 `__init__.py`；根 `__init__.py` 只 re-export provider-neutral types；更新 `tests/public-api.json`。
- 代码方式：closed dataclass union；`RealtimeClient.open()` 注入 profile/adapter/minter/transport；provider、model、voice 不进入产品 request；open 前异常、open 后单 terminal event。
- 验证：public API snapshot；mypy strict；测试 consumer 在 Qwen/OpenAI fake profile 间只替换 composition。
- 依赖：无。覆盖 TO-A1、TO-A4、TO-R4。

### S1 Task 2 — 实现 session ordering/backpressure/terminal/tool FSM  [AC-1, AC-8]

- 改动：`realtime/session.py`、`observability.py`。
- 代码方式：generation；event-id dedupe；response/item/output/content tombstone；one response terminal；bounded input/output queues；CAS close owner；`submit_tool_result` 五阶段 FSM。
- 验证：乱序/重复/迟到/超限/close-cancel race；failed response 顺序；tool ack timeout/重试矩阵；日志字段 allowlist。
- 绑定：FAIL-CROSS-SESSION、FAIL-CLOSE-CLASS、FAIL-OVERSIZE、FAIL-LOG-LEAK。

### S1 Task 3 — 实现 control、local framing 与 optional transports  [AC-1, AC-5, AC-6, AC-7]

- 改动：`relay_control.py`、`local.py`、`connectors/tokenseller.py`、`transports/websocket.py`、`unix_local.py`、`loopback_websocket.py`、`pyproject.toml`。
- 代码方式：strict control schema + RFC8785 digest；24-byte header；双向 ACK window；具体 HTTPS mint + mint-returned path-bound relay WSS；具体 AF_UNIX peer-UID host/channel；具体 loopback WSS `/ws/realtime-voice` Origin+first-frame-secret host/channel；`call.start` 是唯一 upstream open trigger，`call.stop`/shutdown 先 close SDK session 再 close local channel；全部 close 幂等。网络依赖仅 `[realtime]` extra 且 bounded close/max_size/no secret repr。
- 验证：authority vectors、digest、fragment/oversize/generation；fake HTTP/WSS 完整 mint→open→created→close；AF_UNIX 与 loopback accepted-channel lifecycle；wrong local auth/Origin；mount/startup 无 mint；core import 在未装 extra 时成功。
- 绑定：FAIL-TOKEN-REPLAY、FAIL-VERSION-SKEW、FAIL-OVERSIZE。

### S1 Task 4 — Qwen native adapter  [AC-2, AC-8]

- 改动：`adapters/qwen_wire.py`、`qwen_omni.py`。
- 代码方式：读取 packaged authority；nested audio config、unique event_id、native `response.audio.*`、usage/error/terminal mapping；cancel yes/truncate no；不出现 GA rename。
- 验证：全部 Qwen positive/negative/lifecycle/tool/terminal vectors；20ms 640B 与 100ms 3200B fake/real spike；schema drift fail closed。
- 依赖：Tasks 1-3。覆盖 TO-A2。

### S1 Task 5 — OpenAI offline adapter seam  [AC-4]

- 改动：`adapters/openai_wire.py`、`openai.py`。
- 代码方式：官方 frozen fixture 的 session/audio/response/error/close；live connector 配置默认拒绝；不得 import/修改 Qwen adapter。
- 验证：同一 consumer conformance；source diff guard 确认产品与 Qwen adapter 未因 OpenAI fixture 改动。
- 依赖：Tasks 1-3。覆盖 TO-A4；`OOS-OPENAI-LIVE` 保持未启用。

### S1 Task 6 — authority packaging、release pipeline 与 candidate  [AC-2, AC-4, AC-7]

- 改动：packaged `protocols/**`、deterministic `realtime-authority-bundle.tar`、`realtime-release-manifest.schema.json`、3.11/3.13 `[realtime]` lock manifests、sync/check script、BOM、wheel tests、`scripts/build_candidate.py`、README。
- 代码方式：三 SDK release unit 固定为 service 0.3.0 + Harness 0.6.2 + Memory 0.5.2；release manifest 列出 wheel/sdist、四 pack、TypeScript vectors、每 pack SHA256SUMS、authority root digest、两种 Python target lock、BOM、BUILD_INFO、candidate manifest。build script 从 package metadata 取 version，修复当前硬编码 0.1.3；双构建可重复。
- 验证：`SP-SDK-MATRIX` 已证明 3.11.15/3.13.13 源码兼容（各 104 passed）；candidate 必须在 clean 3.11/3.13 venv 安装 exact wheel 后通过完整 BOM/provenance、pytest/ruff/mypy/architecture/wheel/twine，并证明 bundle 与 wheel package data byte-identical。
- 依赖：Tasks 1-5。S1 独立 gate 后发布 immutable wheel。

### S2 Task 1 — additive schema 与 migration  [AC-3, AC-7]

- 改动：Prisma schema + migration。
- 代码方式：`RealtimeClientSecret` 新增一个 field-complete immutable native binding：correlation、provider/path/control/wire/sdk/digest/public+upstream model/voice/provider-cost revision/wallet-revision id+digest；legacy rows default legacy。新增 insert-only `RealtimeWalletPricingRevision`（canonical bytes/digest/validFrom/validUntil/source/FX/margin/ceilings）与 append-only activation event（revisionId、`ACTIVATE|DEACTIVATE`、effectiveAt、actorId、reason、eventId）；禁止 update/delete activation history，禁止删除被 token/session/turn/UsageEvent 引用的 revision。mint 时仅当 `effectiveAt <= now` 的最新 activation event 为 `ACTIVATE` 才有资格被选。`RealtimeTurn` 冻结 relay session/turn、turnKey/reservation/providerResponse CAS、user/parentApiKey/public+upstream model/credential、RequestLog/platformRequest/UsageEvent identities、四 modality usage、Provider cost与wallet revision inputs、terminal snapshot、settlement intent、wallet disposition、terminalForwardState。`UsageEvent` 加四 modality token/price/revision fields。
- 验证：migration apply/rollback-safe preflight、Prisma generate/typecheck、legacy seed/read；revision insert-only/引用后不可删/activation audit；完整 binding round-trip/tamper；每个 recovery 必需字段均能仅从 turn row 重建。
- 绑定：FAIL-TOKEN-REPLAY、FAIL-BILLING-DUP、FAIL-VERSION-SKEW。

### S2 Task 2 — exact route、native mint 与 handshake kernel  [AC-3, AC-7]

- 改动：`realtime-router.ts`、native controller/DTO、client secret/handshake/module/main wiring。
- 代码方式：legacy exact `/v1/realtime`；native exact `/v1/realtime/qwen`；mint 按 rollover authority 先筛选最新有效 activation event 为 `ACTIVATE`、且覆盖 `now+35min` horizon 的 revision，再按 greatest validFrom、lexicographically greatest id 确定唯一版本，返回 schema-valid 动态 wallet binding + 完整 manifest/digest并原子持久化；SDK 校验 static fields、wallet revision schema/digest 与 full capability digest，不硬编码示例 revision。upgrade 先做 path/header/origin，首个 control open 再逐字段比较 correlation/control/sdk/wire/provider/model/voice/provider-cost/wallet revision+digest/capability digest，只有全等才 conditional atomic consume/connect。
- 验证：对每个 bound field 各有一个独立 tamper negative；全部断言 `consumeFirstConnect` 与 Provider connect 未调用；rollover 覆盖 pre-valid/no-current/insufficient-horizon/overlap-latest/old-session-snapshot/activation-rollback；token replay 与 legacy mint/upgrade regression。
- 依赖：Task 1。覆盖 TO-A3、TO-A7。

### S2 Task 3 — shared kernel 与 Qwen-native strategy  [AC-2, AC-3, AC-8]

- 改动：control/kernel/strategy/qwen codec/native session/observability files；`apps/api/src/modules/relay/realtime/authority/**` 与 bundle sync/check；existing legacy gateway 只改 exact routing/shared primitive wiring，不改旧 wire behavior。
- 代码方式：复用 auth/capacity/key gate/credential pool/limits/idle；Qwen Bearer-only exact model/endpoint，native frame validate/passthrough；正常一个 current turn，barge-in 时仅允许 `CANCELLING_PREDECESSOR + NEW_PENDING_SUCCESSOR` 两槽，旧音频从 speech_started 起隔离，successor 独立 hold/CAS/settlement；single terminal owner；`tokenseller.*` control 独立。
- 验证：构建前 TypeScript authority root digest 必须等于 S1 release manifest/consumer receipt；cross-language authority vectors与 `barge-in-overlap-matrix.json`；把 `SP-BARGE-OVERLAP` 45 permutations 原样移植为 Jest；no GA mapping/header；capacity/revocation/timeout/error/controlled-close；legacy snapshots unchanged。
- 依赖：Task 2。绑定 FAIL-PROTOCOL-DRIFT、FAIL-CLOSE-CLASS、FAIL-OVERSIZE、FAIL-LOG-LEAK。

### S2 Task 4 — exact turn billing 与 recovery  [AC-3, AC-8]

- 改动：`realtime-native-billing.ts`、turn repository/reconciler、Usage service additive API。
- 代码方式：HMAC turn_key；preauth before response；response.created CAS；terminal usage safe-integer/equality validation。先 BigInt 聚合 CNY numerator，再按 `wallet-pricing-revision.json` 的 `usd_atoms_per_cny_minor` aggregate-ceil，随后 margin aggregate-ceil；token ceilings 算 23 USD-cent hold。turn row 是唯一 recovery source；RequestLog+UsageEvent/settlement intent 的 identity 先持久化，wallet commit/release CAS，terminal 仅在 durable disposition 后 forward；无 terminal 且 Qwen resume=false 时 idempotent release + platform loss。
- 验证：新 authority exact fixtures completed=`1900954216/2091049638` base/charge atoms，cancelled=`569319048/626250953`，hold=23；真实 Postgres 在 turn create、hold、response CAS、terminal snapshot、intent、wallet commit、terminal-forward 八点逐一 crash/restart；最终每 turn 恰好一 RequestLog/UsageEvent/WalletUsageSettlement 或一次 release，绝不重复/漏归属。
- 依赖：Tasks 1-3。覆盖 TO-A3、TO-R1。

`RealtimeTurn` recovery 状态表（所有 transition 以 `turn_key` CAS；网络转发不作为数据库事务的一部分）：

| Durable state | 必有字段/写入 | crash 后唯一动作 |
|---|---|---|
| `TURN_CREATED` | 完整 user/apiKey/model/credential/correlation、deterministic RequestLog/platformRequest/UsageEvent ids、两 pricing revision bytes+digest | session 已失且无 terminal：一次 release/platform-loss；否则重试同 reservation hold |
| `HOLD_BOUND` | reservation id + 23-cent ceiling snapshot | 等待唯一 response CAS；进程丢失且无可恢复 terminal：一次 release |
| `RESPONSE_BOUND` | unique provider response id | 只接受该 response terminal；barge predecessor/successor 各有独立 row |
| `TERMINAL_SNAPSHOTTED` | exact usage/status、deterministic terminal event id、terminal payload digest；内容本身不落日志 | failed/malformed→release；valid completed/incomplete/cancelled→继续 intent |
| `INTENT_PERSISTED` | 同一 DB transaction upsert RequestLog + UsageEvent exact atoms + settlement intent | 调用现有 wallet idempotent settlement；unique requestLogId/usageEventId/turnKey 防重复 |
| `WALLET_SETTLED` / `WALLET_RELEASED` | settlement id 或 release disposition | 标记 terminal eligible；不得再次扣/释 |
| `TERMINAL_FORWARDING` / `TERMINAL_FORWARDED` | deterministic event id + disposition | crash 可重发同 event id；SDK dedupe，得到 exactly-once domain terminal |

不允许从 live mutable ModelPricing/FxRateCache 重建；reconciler 只能使用 turn row 冻结值。每个状态的 predecessor、允许 CAS、唯一约束和
注入 crash 预期写成 machine-readable fixture，Postgres 测试逐状态重启。

### S2 Task 5 — 旧入口 close 分类回归修复  [AC-7, AC-8]

- 改动：legacy gateway/session specs，最小引入 shared terminal owner observation。
- 代码方式：主动 close 先锁 owner；upstream callback 若 owner 已存在不再 warn/emit error；真正 upstream 1006 保持 error。
- 验证：client 1000、shutdown、rollover、budget、idle、revocation、upstream 1000/1001/1006、error+close；所有资源/hold 各释放一次。
- 依赖：Task 3。绑定 FAIL-CLOSE-CLASS；保留 legacy 外部行为。

### S2 Task 6 — candidate、生产部署与真实 Qwen 证据  [AC-2, AC-3, AC-7, AC-8]

- 改动：env example/runbook、`scripts/deploy.sh`、`docker-compose.prod.yml`、health/admission smoke、deploy evidence；不记录 secret/content。
- 代码方式：部署入口强制 expected-current SHA 与 target SHA、clean+ancestry、pg_dump+SHA、SHA-tagged image/label、保留 predecessor；migration 在受控 admission 阶段运行并核验。`RealtimeWalletPricingLifecycleService` 在 API startup、每次 native mint、pricing health probe、deploy preflight 执行相同 lifecycle 检查；按 revisionId/threshold/process-lifetime 去重，仅发 revision id、threshold、remaining_ms 元数据告警。preflight 必须证明 active wallet revision 覆盖 35min horizon、7 天阈值前已有 schema-valid successor staged，且 predecessor 失去 mint horizon 前 successor 已 ACTIVATE；重叠期新 mint 按确定性规则选版本，旧 session 继续冻结快照。回滚通过 append successor `DEACTIVATE`，仅在 predecessor 仍覆盖 mint horizon 时可 append predecessor `ACTIVATE`，不改历史 revision/event。精确模型/region/workspace/provider-cost+wallet revision；20ms 与 100ms 两次短会话，再做一次真实 barge-in metadata-only trace；核对 terminal、RequestLog、UsageEvent、wallet、logs；旧 v1.1 smoke。
- 验证：unit/integration/e2e/typecheck/build；fake clock 证明每个 revision 在 7/3/1 天阈值迁移各恰好告警一次且同一 band 重复检查被去重；startup/mint/health/preflight 四入口共用同一判定；activation/deactivation/overlap/rollback/no-eligible revision tests；backup/hash、exact HEAD+image label、migration/container/public health、native+legacy WSS postcheck；rollback 命令能用 predecessor image 恢复应用且保留 additive schema。真实 barge-in 顺序只作为证据，不改变已冻结的安全两槽规则。
- 依赖：Tasks 1-5 与 S1 wheel/authority SHA。覆盖 TO-A2、TO-A3、TO-A7、TO-A8。

### S3 Task 1 — AIPhone immutable SDK cutover  [AC-5, AC-7]

- 改动：agent runtime dependency/lock/artifact checks、`provenance.py`、`application.py`、`voice_ui.py`、composition/config/entrypoint。
- 代码方式：安装三 SDK exact release unit；daemon authoritative `build_harness_application()` 与 UI authoritative constructor 都切到 SDK concrete local/relay components；`.env` apiKey/baseUrl 只进入 `TokenSellerHttpsCredentialMinter`；产品不持 provider/local wire implementation。
- 验证：同一 service wheel SHA + authority root digest receipt；production assembly source/import oracle 禁止 `RealtimeRelayBridge` 与旧 local client constructor；ARM64 wheelhouse admission、敏感配置/log scan。
- 依赖：S1 candidate + S2 production。

### S3 Task 2 — 删除产品 Provider bridge ownership  [AC-1, AC-5, AC-8]

- 改动：删除 bridge；contracts 改 SDK import；server/controller tests 迁移到 SDK fake/local conformance。
- 代码方式：daemon 保留 credential/process authority，SDK owns mint/WSS/codec/session；controller 只处理 domain events/audio/UI。
- 验证：AIPhone 源码禁止 Qwen/OpenAI wire field、eph mint、websocket connect；真实 `application.py`/`voice_ui.py` composition integration fake 两轮；call.stop/app shutdown 断言 session-before-channel clean close。
- 依赖：Task 1。覆盖 TO-A5、TO-R4。

### S3 Task 3 — 电话式 UI、barge-in 与真机发布  [AC-5, AC-8]

- 改动：controller/audio/UI/build/deploy tests。
- 代码方式：单主按钮 start/hangup/retry；ready 后自动录音；AI speaking 时本地 stop + cancel；generation reset；不显示“开始说话”。
- 验证：自动 state/unit/integration；Xperia 真机连续两轮+一次打断、可听性/灰屏/锁屏恢复；current build exact artifact receipt。
- 依赖：Task 2。覆盖 RT-S1、RT-S3、TO-A5、TO-R2。

### S4 Task 1 — Simple Harness immutable SDK/backend composition  [AC-6, AC-7]

- 改动：vendored wheel/manifest/pyproject、`backend/deskpet-backend.spec`、new backend realtime module、main route/health/shutdown。
- 代码方式：exact service wheel+SHA 与 authority root digest；PyInstaller 收集 `simple_harness_service` data+distribution metadata；backend owns SDK/relay WSS；SDK concrete loopback host 使用 authority 唯一路径 `/ws/realtime-voice`；mount/startup 不 mint/connect；legacy voice remains disabled。
- 验证：backend unit/integration、fresh startup no Provider call/mic；frontend/backend 都从 authority/generated constant 得同一路径；local.auth 必须首帧、Origin fail-closed、call.start-only upstream open、call.stop/shutdown session-before-channel close；真实 frozen executable 用 `importlib.resources`/`importlib.metadata` 启动并校验 provenance。
- 依赖：S1 candidate + S2 production。

### S4 Task 2 — Tauri phone-style voice UI  [AC-6, AC-8]

- 改动：RealtimeChannel/hook/audio hooks/voiceAvailability/ChatView/App/tests。
- 代码方式：mic placeholder 替换为单 start/hangup/retry；user gesture 后申请 mic；authority `/ws/realtime-voice` + first-frame secret；24-byte frame+ACK；SDK domain messages；barge-in stop+cancel；不在 console 输出 audio/content。
- 验证：Vitest state/transport/audio/cleanup；mount no auto-connect；error retry；text InputBar remains usable。
- 依赖：Task 1。

### S4 Task 3 — 隔离真实桌面验收与文本回归  [AC-6, AC-7, AC-8]

- 改动：E2E/testcase/evidence only。
- 代码方式：使用 fresh `DESKPET_USER_DATA_DIR` 启动 current Tauri build；不 reset/迁移用户数据；完成语音问答后同实例发送文本消息。
- 验证：RT-S1、RT-S5、TO-A6、TO-R3；截图、应用/SDK/relay correlation、可听性人工结论；关闭后无残留会话。
- 依赖：Tasks 1-2。

## 关键 spike（实现前/实现中必须保留原始输出）

1. **三 SDK matrix（已完成）**：fresh Python 3.11.15/3.13.13 + Harness 0.6.2 + Memory 0.5.2 各 104 tests PASS；完整 installed-wheel BOM/provenance 留作 S1 candidate gate。证据见 `verification/spikes/receipt.md`。
2. **FX atoms（已完成）**：ECB cross-rate 的整数 revision 得 completed `1900954216/2091049638`、cancelled `569319048/626250953` base/charge atoms、hold 23 cents；Node BigInt PASS。
3. **Barge overlap（已完成）**：45 个合法 permutation + cancel-no-active 纯状态 spike PASS；S2 必须原样移植 oracle。
4. **SDK optional import**：无 `[realtime]` extra 的 clean venv 导入 core/domain；有 extra 的 venv 跑 concrete fake HTTP/WSS + local host open/close。
5. **跨语言 canonical digest**：Python 与 Node 对 capability manifest 计算相同 `aa47849bbd8c980cd32ab3d2a8f952fbee55b7c59ce1951839484fe0fb589727`，同时验证 wallet revision SHA；duplicate key/non-integer number 均拒绝。
6. **TokenSeller route/consume**：真实 `ws` upgrade fake server 证明 legacy/native listener 不互抢；对完整 binding 每字段 tamper 都在 `consumeFirstConnect` 与 Provider connect 前结束。
7. **Prisma settlement crash**：在 turn create、hold、response CAS、terminal snapshot、intent、wallet commit、terminal forwarding 八个 checkpoint 注入 crash/restart；最终每 turn恰好一 RequestLog/UsageEvent/WalletUsageSettlement 或一次 release。
8. **Qwen real media**：production-like relay 分别发送 20ms/640B 与 100ms/3200B PCM，记录是否接受、首音延迟和唯一 terminal；再做一次 barge-in metadata-only trace，证明真实顺序可被已冻结两槽机接受，而不是用真实顺序改 oracle。
9. **Simple Harness WebView local wire**：Tauri WebView 使用 authority `/ws/realtime-voice` 发送首帧 auth 和 24-byte binary frame，Python SDK decode/ACK，确认 byte order、Origin、no-mount-connect 和 shutdown 顺序。

## 测试与门禁

### 每个 slice 的便宜门

- S1：`uv run pytest`、`uv run ruff check .`、`uv run mypy`、`python scripts/check_architecture.py`、wheel install/public API/BOM/repro build。
- S2：`pnpm --filter api test -- --runInBand`（按现有脚本实际路由）、`pnpm --filter api typecheck`、`pnpm --filter api build`、Prisma migration tests、legacy/native WSS integration。
- S3：AIPhone root + `agent_runtime` pytest/ruff/mypy、artifact build/install smoke、local SDK fake integration。
- S4：backend pytest/ruff/mypy 的既有 shard runner、`npm test`、`npm run typecheck`、`npm run build`、text-chat critical smoke。

### 昂贵/真实门

- S2：真实 DashScope 两种 chunk、至少 RT-S1/RT-S4、DB/ledger核对、production deployment postchecks。
- S3：当前 AIPhone release 真机 RT-S1 两轮 + RT-S3 barge-in；屏幕、音频、日志和 exact artifact 证据。
- S4：fresh isolated userdata 的真实 Tauri RT-S1/RT-S5 + 同实例 text chat；不得用浏览器 mock 冒充 Tauri。
- Program：RT-S2 十轮在至少一个真实产品消费者上完成；每 turn 唯一 terminal/settlement，第十轮上下文一致。

### Black-box oracle 冻结目标

- `testcase/realtime-provider-program/` 中按 AC 建立 provider-neutral、Qwen relay、AIPhone device、Simple Harness UI、legacy/text regression 用例。
- 实现前冻结 exact input、terminal expectation、billing identity、日志禁词和 artifact hash；失败后不得把 expected 改成实现现状。
- `MANUAL_TEST=required`：AIPhone 真机与 Simple Harness 当前 Tauri build；无法执行则该 slice BLOCKED，不以 unit/integration 替代。

## Plan challenge finding closure map

| Stable finding | 计划内关闭点 | closure 必查证据 |
|---|---|---|
| `sdk-harness-version-conflict` | S1-6 三 SDK release unit | `SP-SDK-MATRIX` + 3.11/3.13 installed-wheel BOM gates |
| `billing-currency-domain-unresolved` | architecture 6.6、wallet revision authority、S2-4 | ECB inputs、integer fixtures、23-cent hold、revision validity/digest |
| `realtime-concrete-connectors-undefined` | S1-3 concrete connectors/local hosts | fake full entry/exit + lifecycle/secret/auth negatives |
| `native-turn-recovery-attribution-incomplete` | S2-1/S2-4 recovery table | eight Postgres crash points and deterministic identities |
| `authority-artifact-handoff-undefined` | S1-6 manifest/bundle + S2/S3/S4 named consumers | same wheel SHA + authority root digest + frozen metadata admission |
| `native-token-correlation-binding-not-persisted` | S2-1/S2-2 immutable binding | one pre-consume negative per bound field, zero consume/connect calls |
| `aiphone-production-assembly-scope-missing` | S3-1 `application.py` + `voice_ui.py` | production constructor/import oracle |
| `simple-harness-local-route-contract-conflict` | local authority + S4-1/2 | exact `/ws/realtime-voice` frontend/backend/auth/open/close wiring |
| `production-deploy-exact-sha-not-executable` | release strategy + S2-6 deploy/compose | expected/target SHA, backup hash, image label, smokes, retained-image rollback |
| `barge-in-turn-overlap-assumption-unspiked` | architecture two-slot + S2-3 | 45-permutation spike/Jest + real metadata-only trace |
| `wallet-pricing-revision-rollover-undefined` | rollover authority + S2-1/2/6 lifecycle service | append-only activation eligibility、deterministic 35min horizon selection、7/3/1-day alert owner+fake clock、old-session snapshot、staging/overlap/rollback tests |

## AC / Task 可追溯矩阵

| AC | Tasks | 决定性出口 |
|---|---|---|
| AC-1 | S1-1/2/3, S3-2 | 两 adapter 同 consumer conformance；产品无 vendor wire |
| AC-2 | S1-4/6, S2-3/6 | authority vectors + 两次真实 Qwen chunk |
| AC-3 | S2-1/2/3/4/6 | production relay + exact UsageEvent/ledger |
| AC-4 | S1-1/5 | offline OpenAI fixture；产品/Qwen source diff guard |
| AC-5 | S3-1/2/3 | AIPhone 真机两轮 + barge-in + 单按钮 |
| AC-6 | S4-1/2/3 | isolated real Tauri voice + text smoke |
| AC-7 | S1-6, S2-1/2/5/6, S3-1, S4-1 | exact negotiation、legacy regression、wheel SHA |
| AC-8 | S1-2/4, S2-3/4/5/6, S3-2/3, S4-2/3 | close/error matrix + correlation + leak scan |

## 停止条件与回滚

- authority pack 与官方真实 Provider 行为冲突：停止对应 slice，新增 versioned authority proposal；不得原地改旧 pack 或静默兼容。
- exact pricing/usage 无法由 Provider terminal证明：S2 不上线计费路径；保留 legacy，不以 audio seconds 代替。
- migration 无法 additive/回滚安全：S2 阻断，不部署。
- production real Qwen、AIPhone 真机或 Tauri current-build gate 失败：对应 slice 不标绿，保留前一 immutable release。
- 发现需要改变 assurance profile/trusted boundary/最大影响：回到用户做 scope approval；不由 challenger 自行扩范围。
- 回滚：S1 消费者 pin 回旧 wheel；S2 exact SHA 回滚应用但保留 additive schema；S3/S4 回滚产品 commit；legacy route 始终保留。

## 完成定义

- 四个 slice 各有 scoped commit、immutable artifact/SHA、testcase、有效 gate receipt；所有 AC/required obligation 在当前提交态有证据。
- TokenSeller production exact SHA、migration、container、health、真实 Qwen、账单和 legacy smoke 全绿。
- AIPhone current build 真机和 Simple Harness isolated current Tauri build 的人工验收全绿。
- OpenAI 只声明 fixture/conformance readiness，明确 live/production 未启用。
- 各仓不混入用户现有无关改动；文档、architecture、testcase index、runbook 与最终 refs 同步。
