# Realtime 0.3.19 五条合同与接入

本版完善身份、用户附带事件、控制错误、首因诊断和显式恢复状态。离线合同通过不等于 Host 真线路放行；live6 两个失败 item 的嵌套身份未被原探针保存，仍不得直接认定为 user 或 assistant。文字 Provider 请求拒绝不由本包修复。

0.3.20 补充用户输入角色元数据合同，新增 origin 并保留旧快照读取；见[角色合同说明](realtime-0.3.20.md)。

## 身份与用户附带事件

`session.identity_table.lookup(item_id)` 返回不可变的 `RealtimeItemIdentity`，包含 canonical_id、role、source、generation、confirmed、aliases。表关联 speech_started、committed、completed/user/input_audio、上下文确认的服务端 ID/原请求别名及嵌套 output_item 身份。

原请求别名可供来源查询；附带帧仍需使用实际确认或输入事件登记的规范 item ID，不把未回显的客户端 ID 当成新的提供方身份。role/source 能区分历史资料与音频输入，表中 user 不代表已获得业务工具授权。Host 仍应依据公开 context_ack_id 隔离自己注入的资料。

缺失、null、非字符串、空串、纯空白均为无标识。已知 user 的无 response_id 内容事件沿用 0.3.18：非空音频转写发用户事件，其余忽略，记录 user_side_response_event_ignored/provider_event_type。未知身份继续拒绝，诊断原因 unresolved_item_missing_response_id；已知助手缺作用域继续拒绝，原因 suspected_assistant_missing_response_id，表示“疑似助手输出缺 response_id”。不补造 ID 或借当前活跃 response。

同一显式恢复状态内，新一代保留上一代已确认 user 身份；未确认输入和助手身份不成为跨代 user。旧会话在解码前检查代际，不能修改新一代共享身份表。

## 控制错误与首因诊断

`tokenseller.error` 继续严格按冻结清单校验字段集合、枚举、布尔、关联 ID 和 retry_after_ms 范围。当前支持 fatal=true 的终止错误，与既有控制门禁一致；fatal=false 归类为 non_terminal_error，而不是伪装成合法终止通知。原始错误正文不会被复制到 SDK 事件或诊断。

合法控制终止产生 `SessionFailed`，除既有 code/retryable 外，提供 control_code、message_class、related_event_id、client_event_type、fatal、retry_after_ms 和 failure。合法 `SessionClosed` 保留既有 reason/initiator/disposition，补 client_event_type/failure 等可选原因信息。新增元数据不参与既有终态事件的值相等比较。

控制帧校验失败为 control.decode，合法错误通知为 control.terminal，合法关闭通知为 control.closed；初次开线控制错误为 control.open。校验失败只暴露字段名及封闭原因分类，不记录非法字段值；若 related_event_id 能在已发送事件中核实，仍关联触发的客户端事件类型。

`event.failure` 是不可变 `RealtimeFailureContext`，包含：

- stage、event_type、generation、field_names；字段名只取固定白名单，不含字段值。
- validation_field、validation_reason；原因使用封闭分类，不复制异常消息。
- 经校验的相关事件 ID、客户端事件类型及合法控制错误的 code/message_class/布尔与退避信息。

会话保存首个终止原因，后继关闭和汇总不覆盖它。控制解码、adapter 解码/应用、收发失败、上下文/工具 ACK 超时均关联该结构。诊断快照中筛选 session_terminal 或 open_failed 可查看对应 failure；共享快照按 correlation/generation 区分连接。可恢复的工具发送歧义单独记 attempt 原因，不冒充已经终止的会话。

## 重连、同号重绑与新进程恢复

同一 RealtimeClient 自动复用逻辑会话状态。创建新客户端或同号重绑时，应显式共享同一个状态对象；不能依赖跨客户端全局缓存：

```python
from simple_harness_service import RealtimeClient, RealtimeRecoveryState

recovery = RealtimeRecoveryState()
client = RealtimeClient(profile, minter, transport, adapter, recovery_state=recovery)
session = await client.open(request)
```

宿主可将 `recovery.snapshot()` 作为受信应用状态保存，再在新进程用 `RealtimeRecoveryState.from_snapshot(snapshot)` 恢复并注入新客户端。快照不含正文、指令、音频或凭据，只含逻辑会话身份、代数、工具调用/已完成输入去重集合及消息键/摘要/确认状态。宿主负责快照的持久化、原子保存及同一逻辑会话的单写入者约束；本包不增加数据库，也不自动保存文件。

恢复后显式重建连接、等待 ready、更新指令并回灌所需历史。SDK 不保存或自动重放输入 PCM，也不自行恢复未完成的业务执行。已见工具调用按 call_id 去重；已完成用户输入按 item ID 去重，避免重放再次触发业务。结果不明的旧工具执行应由宿主查询业务权威状态，不凭空重做。

跨连接资料或播报需提供稳定业务键：

```python
await session.append_message(
    "The report is ready.",
    trigger_response=True,
    idempotency_key="report-completed-1",
)
```

同键、同正文/角色/开关且已确认时，不再次注入或续答；同键改载荷为 invalid_request。发送或 ACK 结果不明的键返回 busy，禁止自动重放。该保证是至多一次请求，不保证音频已经实际播放；宿主仍需播放回执和显式补播决策。未提供键时保留原追加语义，不能自动推断两段相同正文是否是同一次播报。

恢复诊断包括 recovery.started/restored、recovery.context_acknowledged、recovery.message_deduplicated 和 recovery.first_user_transcript。输入来源和上下文资料仍分开查询；恢复快照不是 Qwen 原生 resume，能力标志 resume 仍为 False。

## 五条合同的测试入口

| 合同 | 测试文件 |
|---|---|
| 身份关联 | tests/test_realtime_identity_table.py |
| live6 用户附带顺序与未知/助手拒绝 | tests/test_realtime_live6_sidecars.py |
| 控制通知与校验失败 | tests/test_realtime_control_failures.py |
| 首个失败、发送与 ACK 超时 | tests/test_realtime_failure_context.py |
| 重连、重绑、快照、去重和新转写 | tests/test_realtime_recovery.py |

live6 夹具只保留脱敏后的类型、角色、状态、内容类型和行顺序，身份统一替换为合成关联。原探针缺失的嵌套 ID 使用明确占位，不能据此认定两个失败 item 的真实角色。控制测试采用现场“用户附带事件→控制错误→终止”的顺序，字段值取冻结协议向量；原证据未保存的真实 code/message_class 仍待新诊断在线上重验。
