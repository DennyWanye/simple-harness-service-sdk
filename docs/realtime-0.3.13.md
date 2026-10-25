# Realtime 0.3.13 / 0.3.14 接口与交接

本版源码补充工具目录、可选自动工具循环、上下文注入与播放统计。Qwen 真线路能力仍待中继恢复后确认。OpenAI 适配器只用于离线编码与 fixture 校验，未开放真实连接。

## 工具目录与调度

`RealtimeOpenRequest.tools` 是 function schema 列表，默认空列表。每项包含 `type="function"`、唯一 `name`、对象类型的 `parameters`，可选 `description` 与 `strict`。目录按紧凑 UTF-8 JSON 计总字节：构造请求时上限 65,536 字节，打开会话时再按实际 `capability.limits.tool_payload_bytes` 检查。

为避免离线环境引入新的运行依赖，schema 使用明确的受限子集：`type`（含类型列表）、`properties`、`required`、`additionalProperties`（布尔值或子 schema）、`items`、`enum`、`const`、`minimum`、`maximum`、`minLength`、`maxLength`、`minItems`、`maxItems`、`anyOf`、`oneOf`、`allOf`、`title`、`description`。嵌套最多 32 层；`$ref`、远程 schema、正则等其他关键词直接拒绝。请求格式或 schema 错误抛 `ValueError`；协商阶段的目录超限抛 `RealtimeError(INVALID_REQUEST)`。

Qwen 在初次 `session.update` 下发非空目录。0.3.14 起，初次 `session.updated` 按语义确认目录：function 名称集合、每个工具的 `parameters.required` 集合及 `properties` 键集合相同即可；兼容平铺工具和中继的 `{type, function:{…}}` 包裹。描述空白、默认值、additionalProperties、字段或工具顺序等非语义差异不阻断，只产生 `session.tools.normalized` 无正文诊断。缺失、额外、重复或语义不匹配工具报 `unsupported`。只有通过确认后才产生 `SessionReady(tools_acknowledged=True)`；空目录不要求工具回显，该标志为 `False`。

宿主可向 `RealtimeClient(..., tool_dispatcher=dispatcher)` 注入 `ToolDispatcher`：

```python
from simple_harness_service import ToolCallRequested

class HostTools:
    async def dispatch(self, call: ToolCallRequested) -> str:
        return await execute_host_tool(call.name, call.arguments_json)
```

只有 `response.function_call_arguments.done` 会进入调度；delta 仅保留已有的无内容事件类型诊断。SDK 检查 JSON、重复键、非有限数字、参数总字节、工具名与 schema。合法调用携带当前 `generation`；非法调用不出现在供手工执行的事件流中，直接回写 `{"error":"invalid_arguments"}`。自动调度超时默认 30 秒，可用 `tool_dispatch_timeout` 调整；超时、异常、空或超限输出分别回写稳定的工具错误，不记录异常正文。

结果仍遵循“写入结果 → 等待精确的 `conversation.item.created` 回执 → `response.create`”。未注入 dispatcher 时，合法调用仍由宿主消费 `ToolCallRequested` 并调用 `submit_tool_result()`。同一会话重复 `call_id` 关闭会话并报协议错误；同一 `RealtimeClient` 对相同 `external_session_id` 跨 generation 共享去重集合，重放调用不重复执行。新 generation 建立后旧会话停止接受运行操作，旧调用与异步执行完成后的旧结果均不下发。去重仅覆盖当前 client 实例，不承诺进程重启后的持久去重或 Qwen 会话恢复。

## 上下文原语

```python
await session.append_message("The task is complete.", role="system", trigger_response=True)
await session.update_instructions("Answer briefly.", trigger_response=False)
await session.out_of_band_response("Summarize the current task.")
```

`append_message` 默认 `role="user"`，只允许 `user`、`system`。编码为 `conversation.item.create(message/input_text)`，收到与所请求角色、内容和完成状态一致的 item 回执后，按 `trigger_response` 决定是否请求回复。

Qwen 的 `update_instructions` 编码为第二次 `session.update(instructions)`；收到包含对应 instructions 的 `session.updated` 后，按开关决定是否发送 `response.create`。OpenAI 离线适配器先更新 session instructions，确认后可发 `response.create(instructions)`，因此 `trigger_response=False` 能更新指令而不生成回复。

同一会话的注入串行进行，等待回执期间接收循环继续运行。与注入请求关联的上游拒绝映射为 `unsupported`，写入无内容诊断，并使当前方法抛出同一错误。Qwen 只有一个待确认注入时，无关联 ID 的请求类型拒绝也映射为 `unsupported`；认证、限流等其他错误保留原分类。回执超时为 `timeout` 并终结会话，避免在结果不明时自动重发。

`out_of_band_response(text)` 在 Qwen 直接抛 `unsupported`；OpenAI 离线编码为 `response.create({instructions, conversation: "none"})`。Qwen 文本 message 与指令更新能否在真线路使用仍待试验，离线编码成功不代表上游支持。关闭或过期会话抛 `cancelled`，未 ready 抛 `busy`，非法或超限文本抛 `invalid_request`。

## 播放回执与统计

产品到本地通道的输出 `call.audio_ack` 新增可选整数 `played_audio_ms`，表示本次 generation 输出音频中实际播放的累计毫秒数。老客户端可继续省略该字段。该值必须非负、单调，且不能超过已确认帧所包含的音频时长；序号重复的 ACK 可以推进播放位置。输入方向 ACK 不允许带此字段。

`LocalRealtimeChannelController.playback_snapshot()` 返回不可变 `RealtimePlaybackStats(sent_audio_bytes, sent_audio_ms, played_audio_ms)`。发送统计按成功写入本地通道的 PCM 字节数、协商采样率与声道数计算，在 `ResponseFinished` 前后均可查询，不把已发送时长等同于已播放时长。捕获的旧 generation 流及等待背压期间过期的输出块停止下发。`ManagedRealtimeSession.is_current_generation` 提供只读的当前代有效性，本地通道也会据此丢弃过期输出。

## 同环境安装与元数据限制

Harness 依赖为 `simple-harness-sdk>=0.13.0.dev20260925`，不把本地版本号放入范围下限，因此 `+opt.217/218` 等可匹配。BOM 主线下限为 `0.13.0.dev20260925`，本地版本以安装时实测为准，并检查实际 execution schema=5；候选实测版本及 SHA 单独存于 `harness.candidate`。`memory` extra 维持原来的 Memory 0.5.2 URL 与 SHA-256。宿主私有轮子目录为 `simple_harness/backend/vendor/`。在宿主侧或把轮子复制到本仓库 `vendor/` 后，示意安装如下：

```bash
SDK_WHEELS="$PWD/build/vendor"
uv pip install --python .venv/bin/python --no-deps \
  --find-links "$SDK_WHEELS" \
  'simple-harness-sdk>=0.13.0.dev20260925' \
  "$SDK_WHEELS/simple_harness_memory_sdk-0.5.2-py3-none-any.whl"
uv pip install --python .venv/bin/python --no-deps -e .
.venv/bin/python -c 'import simple_harness, agent_orchestrator, simple_harness_service, simple_harness_service.realtime'
```

本次实测 Harness 轮子已包含 `agent_orchestrator`，无需另装编排轮子。上方示意安装请使用[构建说明](build-and-release.md)中的实际分步命令；Memory 0.5.2 的 `simple-harness-sdk<0.7` 元数据约束仍存在，需要显式 `--no-deps`。

2026-10-25 已实测确认四个包同环境导入，Harness execution schema 为 5，Memory schema 为 4。BOM 已写入实际轮子 SHA-256，移除待核验状态。安装后的来源验证支持与 BOM 文件名及 SHA 同时相符的本地轮子，Memory 的原始 URL 与 SHA 继续保留。完整测试、严格类型和候选复现检查结果见[阶段记录](../plans/2026-10-25-stage2/阶段2-记录.md)。

0.3.14 的来源校验不再将候选 opt 号当作所有安装的唯一版本。安装候选实测版本时继续精确核对冻结 SHA；其他满足下限的本地版本需登记其自身轮子名称和 archive SHA，再核对实际 schema。其他版本的登记散列不等于已与候选快照散列比较，私有轮子仍由宿主提供。
