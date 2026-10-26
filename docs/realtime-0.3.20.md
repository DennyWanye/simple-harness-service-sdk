# Realtime 0.3.20：用户语音来源与角色元数据

这条线路在阶段 0 首日原始帧中就存在如下顺序：同一个 item ID 先收到 speech_started，随后收到 message/input_audio 的 conversation.item.created，但 role 为 assistant、status 为 in_progress，最后收到用户 transcription.completed。live7 五条分支再次复现。0.3.19 会用 created 的 role 覆盖输入身份，随后在 ASR 完成时触发 conflicting item role。本版直接适配此合同，不等待中继修正 role，不将它误记为 tokenseller.error。

## 判定规则

- speech_started、committed 登记的 item 是用户输入，后续角色元数据不能将它改成助手。
- conversation.item.created 的 message 只要 content 含 input_audio，也建立用户来源；不要求 role=user 或 status=completed 才能识别。in_progress 先登记未完成输入，committed/ASR 完成继续确认身份。
- 已登记用户输入的 created 元数据即使更换角色，也不会覆盖来源、误消费待确认的上下文消息或自动触发 response.create。
- 助手新增身份仍须由带真实 response_id 的 response.output_item.added 登记。普通 created 的角色声明不能单独建立助手来源，无作用域的未知输出仍拒绝。既有合法作用域输出和嵌套 user 合同保持。
- 首次已确认来源保持稳定；真正的助手来源不能仅凭后来的 created 角色声明被提升为用户。输入/输出来源复用同一 ID 的冲突仍拒绝，这与本次“输入来源已知、角色字段标错”不同。

RealtimeItemIdentity 新增 origin，记录第一次有效登记的来源；source 继续表示最近的有效观察，保留既有查询语义。角色矛盾不能刷新旧输入的登记代数。origin 随快照保存；读取 0.3.19 的旧快照时，缺失 origin 使用已保存的 source 回退，不补造已经丢失的历史顺序。新增快照字段由本版读取。

## 矛盾诊断

不一致时会话继续运行，记录：

```text
operation_kind = item_role_metadata_mismatch
provider_event_type = conversation.item.created
item_id = <提供方 item 身份>
declared_role = assistant
```

declared_role 只接受 user/assistant/system 或缺失、非法值的固定标记 missing/invalid；不保存任意角色字符串、转写正文或音频。重复事件不重复发布诊断，旧 generation 不修改身份表或发布新诊断。正确的 user 声明不产生矛盾诊断。

来源为 user 不等于获得业务工具授权。Host 仍需依据 context_ack_id 和来源表区分自己注入的资料与真实输入，恢复时继续使用 0.3.19 的显式状态及稳定播报键。

## 离线证据

tests/fixtures/qwen-input-role-metadata.json 保留 live7 五条共同事件顺序，以及阶段 0 一轮的 96 个服务端事件，直到两个 response.done。事件类型、角色、状态和相对关联保持；身份、正文、工具参数和 PCM 换为合成夹具，没有复制真实音频、转写、凭据或原始长日志。

tests/test_realtime_input_role_metadata.py 覆盖五条分支在真实 ManagedRealtimeSession 中继续到工具请求/response 完成，以及完整轮次的适配器解码、首次来源、混合内容、去重、旧代隔离、上下文 ACK 不被消费、快照兼容和助手拒绝边界。live7 原分支止于 ASR 拒绝；测试的后续响应接自阶段 0 的合法序列，不冒充各分支已在线到达的后续帧。0.3.19 五份合同继续运行，不修改旧绿灯断言。

这些是离线回放。Host 安装候选后仍须复验五条真线路分支及后续业务、恢复和独立评审，不据此宣称原 A08～A11 全部通过。
