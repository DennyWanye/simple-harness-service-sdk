# 验收标准：统一 Realtime Service API 与原生 Provider Relay

## 范围

- 包含：
  - `simple-harness-service-sdk` 提供产品无关的 Realtime 会话 API、事件模型、能力发现和 Provider adapter 接口。
  - 首个生产 adapter 使用 Qwen Omni Realtime WebSocket beta 原生 wire contract。
  - TokenSeller 新增版本化的 Qwen-native Realtime relay；长期 Provider key 永不下发客户端，relay 继续拥有鉴权、计费、并发、限额和隐私安全观测。
  - AIPhone 与 Simple Harness 只依赖 service-sdk 公共 API，不再各自实现 Qwen/OpenAI wire codec。
  - 架构预留 OpenAI Realtime adapter，并用独立 conformance fixture 证明新增 adapter 不需要修改产品 API 或 Qwen adapter。
  - 现有 TokenSeller `2026-07-voice-v1.1` 入口在迁移期保持兼容；新入口达到行为与计费门禁后才允许产品切换。
- 明确不包含：
  - 本轮不启用或付费验证真实 OpenAI Realtime Provider；没有 OpenAI 凭证时只交付 adapter seam、官方协议 fixture 和 conformance 测试。
  - 本轮不实现本地语音模型；本地模型后续通过同一 adapter 接口接入。
  - 不删除旧 TokenSeller Realtime 协议，不迁移历史计费记录，不更改文本 Chat/Harness durable command authority。

## Release slices

| Slice | 独立交付边界 | 高风险子系统 |
|---|---|---|
| S1 | service-sdk Realtime core、Qwen adapter、OpenAI extension seam、conformance kit | SDK / Provider codec |
| S2 | TokenSeller Qwen-native versioned relay、旧 v1.1 兼容、计费与观测 | Provider relay / Auth+Billing |
| S3 | AIPhone 从产品内 bridge 迁移到 service-sdk，真机电话式验收 | SDK consumer / Mobile audio+UI |
| S4 | Simple Harness 接入同一 service-sdk，隔离 userdata 的真实桌面 UI 验收 | SDK consumer / Desktop UI |

每个 slice 独立 plan、testcase、提交和 gate receipt；后续 slice 只能消费前一 slice 的不可变候选制品，不能依赖未提交源码工作树。

## 功能验收条款

| ID | 功能点 | 验收条件（可验证） | 优先级 |
|---|---|---|---|
| AC-1 | 产品无关 Realtime API | service-sdk 导出稳定的 session/open/send-audio/receive-event/close API、闭合错误码、音频格式与 Provider capability；测试产品只替换 adapter 配置即可在 Qwen 与 OpenAI fixture 间运行，产品代码不出现 Provider wire 字段。 | 必须 |
| AC-2 | Qwen 原生 adapter | Qwen adapter 按冻结的 Qwen3.5 Omni Realtime 原生协议编解码 nested `session.audio.input/output`、`input_audio_buffer.append`、VAD、transcription 与 `response.audio.*` / `response.done` 事件；原生 Provider frame 不经过 OpenAI GA 重命名，协议 fixture 与真实 Qwen 会话均通过。 | 必须 |
| AC-3 | TokenSeller Qwen-native relay | 新的版本化入口使用 ephemeral token 接入并原样代理 Qwen 数据面；TokenSeller 仍执行 key 隔离、Origin/身份校验、并发、限额、一次且仅一次计费及安全关闭。真实生产会话返回非空转写和音频回复，账单与 Provider terminal 一一对应。 | 必须 |
| AC-4 | OpenAI 扩展边界 | 新增 OpenAI Realtime adapter 只增加 adapter/codec/config，不修改产品 API、Qwen adapter 或产品状态机；官方协议 fixture 覆盖 session、audio append、response、error 与 close。真实 OpenAI 凭证和生产启用明确保持未启用。 | 必须 |
| AC-5 | AIPhone 消费统一 SDK | AIPhone 删除产品内 Provider wire/ephemeral/WebSocket codec ownership，保留麦克风、AEC、播放、UI 与产品策略；安装不可变 service-sdk 候选后，真机完成连续电话式两轮对话和一次打断，无额外“开始说话”按钮。 | 必须 |
| AC-6 | Simple Harness 消费统一 SDK | Simple Harness 通过同一公共 API 和同一 Qwen adapter 建立 Realtime 会话；产品只提供凭证、endpoint、音频/UI adapter。在隔离 userdata 的真实 Tauri UI 中完成至少一轮非空语音问答，既有文本聊天不回归。 | 必须 |
| AC-7 | 版本与迁移兼容 | service-sdk、AIPhone、Simple Harness 与 TokenSeller 通过显式 protocol/capability negotiation 拒绝不兼容组合；旧 `2026-07-voice-v1.1` 在迁移期继续通过回归，新协议不静默替换旧入口。每个消费者钉死候选 wheel 版本和 SHA-256。 | 必须 |
| AC-8 | 可观测与失败语义 | 每个会话可用 opaque correlation 对齐产品、SDK、relay 和 Provider；日志只含阶段、稳定错误/关闭分类、帧字节计数和时序，不含 token、PCM、转写或回复正文。正常关闭不得上报 `upstream_error`；上游结构化错误、transport close、timeout 和客户端关闭可区分。 | 必须 |

## 非功能 / 边界

- 安全：长期 TokenSeller/Provider key 不进入 URL、普通日志或客户端持久化；ephemeral token 最小 TTL、单会话绑定且不可重放。
- 音频：输入统一描述为 16 kHz、mono、PCM signed 16-bit little-endian、无 WAV header；输出采样率来自 Provider capability，不靠字段名猜测。SDK 不持久化原始音频。
- 延迟：SDK/relay 不因跨协议兼容而无证据地聚合音频；Qwen 真实 20 ms/640 B 与 100 ms/3200 B fixture 均需有决定性结果，产品采用已验证边界。
- 顺序与幂等：重复 terminal、迟到音频、关闭竞态和 reconnect 不得重复播放、重复提交 turn 或重复计费。
- 兼容：现有文本 Chat、Harness command、Memory、Tool 和旧 Realtime endpoint 都属于回归面。
- 发布：生产部署必须精确 SHA、备份、受控 fast-forward、容器/迁移/health 检查；消费者只接收已发布且哈希固定的 wheel。

## Assurance contract 摘要

- Profile：standard。
- 受保护资产：Provider/relay 凭证、用户音频与转写、钱包与 UsageEvent、会话隔离、旧客户端可用性。
- 可信假设：本机开发账户与固定工具可信；TLS、官方 Provider endpoint 和已审核 wheel hash 可信；Provider 可用性本身不受本项目控制。
- 范围内失败/对手：协议漂移、错误映射、token 重放、跨会话帧、重复计费、竞态关闭、超限载荷、版本错配和日志泄露。
- 明确范围外条件：Provider 全局故障、宿主机被攻陷、真实 OpenAI 账号/额度、WebRTC/SIP、本地模型实现。
- 最大可接受影响：单个会话可明确失败并安全关闭；不得泄露密钥/内容、跨会话播放、重复扣费或破坏旧入口。

## 测试场景矩阵

| scenario_id | input_class | exact_input | primary_risk | gate_type | required | manual_required | terminal_expectation | quality_bar |
|---|---|---|---|---|---|---|---|---|
| RT-S1 | 中文事实问答 | “你好，今天我们先聊一句：请用一句话介绍你自己。” | 首轮真实价值与完整音频链路 | positive-value | 是 | 是 | speech start/stop、非空转写、response.done、可听回复 | 回复语义相关、音频清晰且没有尾随错误 |
| RT-S2 | 多轮上下文 | 连续十轮自然追问，第十轮问“把刚才最重要的两点总结一下。” | 长会话状态、顺序和一次计费 | positive-value | 是 | 是 | 十轮均唯一 terminal，第十轮引用此前上下文 | 总结与前文一致，无跨轮字幕/音频串线 |
| RT-S3 | 生成中打断 | 在 AI 正在说话时说“停一下，改成只说一句。” | barge-in、cancel/truncate 与重复播放 | positive-value | 是 | 是 | 旧音频及时停止，新 turn 完成 | 不重播旧回答，新回答遵循一句话要求 |
| RT-S4 | Provider/网络异常 | 会话中断开 relay 或注入允许的上游错误 fixture | 诚实失败、错误分类和计费安全 | negative-safety | 是 | 否 | 稳定分类错误并安全关闭，零重复结算 | 不适用 |
| RT-S5 | 冷启动 | 全新 release/隔离 userdata，首次配置凭证后直接进入语音页 | stateful init 与 adapter 注册 | positive-value | 是 | 是 | 首次进入即可建立会话并完成一轮 | 无需暖重启或隐藏初始化步骤 |

## LLM / Provider 行为变异清单

- 乱序：`response.done` 早于迟到 delta 时，SDK 丢弃迟到载荷且不跨入下一 turn。
- 重复：重复 `response.done`、transcription terminal 或相同音频 delta 只能产生一次产品 terminal/播放/结算。
- Schema 违约：缺失 type/id、错误字段位置、未知枚举必须映射为稳定 protocol error，不得猜测字段。
- 极端载荷：超长文本、超大 base64、奇数字节 PCM 和超过 frame 上限均在边界处拒绝且不记录正文。
- 拒绝/沉默：Provider 返回 error、只建 session 不产生 VAD、或超过 inactivity deadline 时，产品得到可操作分类并可新建会话，不伪造回复。

## 测试义务矩阵

| obligation_id | type | ac_id | risk | min_decisive_test | required_reason |
|---|---|---|---|---|---|
| TO-A1 | delivery | AC-1 | — | 两个 fixture adapter 运行同一 SDK consumer conformance suite | 证明产品 API 与 Provider wire 解耦 |
| TO-A2 | delivery | AC-2 | — | Qwen fixture codec + 20ms/100ms 两次真实短会话 | 证明原生 Qwen wire 和音频边界 |
| TO-A3 | delivery | AC-3 | — | TokenSeller 真实 relay 会话并核对 UsageEvent/ledger | 证明生产数据面、鉴权和计费闭环 |
| TO-A4 | delivery | AC-4 | — | OpenAI 官方 fixture adapter conformance，产品源码 diff 为零 | 证明未来扩展不侵入产品 |
| TO-A5 | delivery | AC-5 | — | AIPhone 真机两轮 + barge-in + 可听性人工检查 | 直接证明手机产品价值 |
| TO-A6 | delivery | AC-6 | — | 隔离 Simple Harness Tauri 实例完成语音问答并 smoke 文本聊天 | 直接证明桌面消费者与无回归 |
| TO-A7 | delivery | AC-7 | — | 旧 endpoint 回归、错误版本拒绝、wheel hash/provenance 验证 | 防兼容和供应链漂移 |
| TO-A8 | delivery | AC-8 | — | 四类关闭/错误日志关联、敏感信息反向扫描 | 证明可诊断且不泄露 |
| TO-R1 | change-risk | AC-3 | FAIL-BILLING-DUP | 重复/乱序 terminal 与关闭竞态测试 | 共享计费状态存在副作用和幂等风险 |
| TO-R2 | change-risk | AC-5 | FAIL-CROSS-SESSION | late/duplicate audio 与 session generation 测试 | 防旧音频进入新会话 |
| TO-R3 | change-risk | AC-6 | FAIL-TEXT-REGRESSION | Simple Harness 既有文本 Chat critical smoke | 修改共享 SDK 依赖与启动装配 |
| TO-R4 | change-risk | AC-7 | FAIL-WIRING | public exports、adapter registry、产品入口接线 smoke | 防服务实现存在但产品未接入 |

## 完成的定义（DoD 摘要）

- 四个 release slice 分别取得有效 gate receipt；不得用后续 slice 掩盖前一 slice 的失败。
- 全部必须 AC 和 required obligation 有当前提交态 PASS 证据。
- Qwen 真实付费验证、AIPhone 真机、Simple Harness 真实 UI 均通过；OpenAI 仅声明 fixture/conformance readiness，不冒充生产可用。
- 各仓提交干净、不可变制品与 SHA 对齐、架构与 testcase 索引同步。
- TokenSeller 生产部署与 post-deploy checks 通过，旧 v1.1 入口仍可用。
