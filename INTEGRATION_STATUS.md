# Service SDK 0.3.15 状态

0.3.15 修复阶段 3a 的真实消息 ACK 合同：Qwen 按完整消息内容与最早发送顺序确认，兼容服务端新 item ID 和原 ID；原请求与服务端 ID 分别关联，确认后才允许按开关续答。instructions 确认忽略首尾空白。保留工具结果确认、重复/迟到处理与 generation 隔离。

本仓库新增交接六用例和额外反例，夹具只使用授权复现脚本中的示例文本及回显结构，无 Host 桥反向依赖、site-packages 补丁、原始音频或凭据。相关测试 175 条、全套 439 条均通过，严格 mypy 43 源文件、ruff、格式、公开接口、架构与 authority 门禁通过；候选结果见[阶段记录 0.3.15 节](plans/2026-10-25-stage2/阶段2-记录.md)。本次不跑新真线路、不发版、不推送，Host 六项/联合回环与 A08～A11、A13 复验留给 Claude。

以下为旧版本历史记录，不代表 0.3.15 的验收结论。

# Service SDK 0.3.14 状态

0.3.14 修复两处阶段 3a 真线路阻断：Qwen 工具目录按 function 名称和参数键集合语义确认，兼容中继包裹与非语义规范化；Harness 产品依赖改为 `>=0.13.0.dev20260925`，实际本地版本和 schema 安装时验证。BOM 与候选快照分开，正式候选仍冻结轮子字节与三平台锁。

真实回显仅从用户授权的 phase0/s02 样本读取工具结构，测试未复制原始日志、转写、音频或凭据。本次实际环境已升级到 opt.217，相关测试 120 条、全套 423 条均通过，严格 mypy 43 源文件及 ruff、公开接口、架构、authority 检查通过；正式候选源码 `2f8a06c83c51b27f7fb85f430a3b61c8d2e83086`，目录 `dist/candidate-0.3.14/`，wheel SHA-256 `bc2e563c6373d8f17ecaee660469d6000db749aee111428999f0de7bfcb2e08b`；两次构建字节一致，twine 4 项和新环境四包导入/完整 BOM 均通过。详细结果见[阶段记录的 0.3.14 节](plans/2026-10-25-stage2/阶段2-记录.md)。真线路复验仍等 Claude 用探针跑，不发版、不推送。

以下为 0.3.13 与更早版本的历史结果，不能用于替代 0.3.14 的验收。

# Service SDK 0.3.13 阶段 2 状态

2026-10-25：阶段 2 源码与完整离线门禁已通过。Python 3.13 同环境可导入 `simple_harness`、`agent_orchestrator`、`simple_harness_service`、`simple_harness_service.realtime`；编排包随 opt.216 轮子提供。实测 Harness execution schema 5、Memory schema 4；两个依赖轮子的 SHA-256 与安装来源已核验。

本仓库全套 410 条测试通过，严格 mypy 的 43 个源文件通过，ruff、公开接口、架构门禁和四包 authority 同步检查通过。正式候选绑定源码提交，并携带两个依赖轮子和三个消费端锁；正式候选源码为 `8ce257d6bbcffa4f745a12279d89dac88736299f`，轮子 SHA-256 为 `de11691051d373440cdf9165b4c2a52e188204ef4b830b81341baa6d16d0c346`，清单 SHA-256 为 `85ab775587aea0ea48699a9819a78f8eef48728ef159a27d415af1034d0f28d2`；候选目录 `dist/candidate-0.3.13/`。三个轮子及 sdist 的 twine 检查 4 项通过，新 Python 3.13 环境四包导入与完整安装 BOM 通过。具体制品散列与复现结果见[阶段 2 记录](plans/2026-10-25-stage2/阶段2-记录.md)。

Memory 0.5.2 的元数据仍声明 Harness `<0.7`，因此全 extra 的 `uv lock` 不可解析；保留原 extra，按[构建说明](docs/build-and-release.md)先安装私有 Harness，再以 `--no-deps` 安装 Memory 和本包。原 `uv.lock` 是历史快照，不能直接用于同步当前组合。正式候选的三个 Realtime 运行锁独立解析并验证。Linux ARM64 因 deepseek-recipe 必需轮子要求，将最低基线明确为 glibc ≥ 2.28；本次只验证跨平台依赖解析，不代表 Linux 真机执行通过。

接口说明见 [Realtime 0.3.13](docs/realtime-0.3.13.md)。A06/A07 和 Qwen 上下文真线路项等 Claude 用探针跑；本次未发版、未推送。OpenAI 保持仅离线支持，既有 conformance fixture 未修改。

以下保留既有版本的历史记录，不代表 0.3.13 的验收结论。

# Simple Harness Service SDK 0.3.0 release status

[`v0.3.0`](https://github.com/DennyWanye/simple-harness-service-sdk/releases/tag/v0.3.0)
is the released Realtime Provider Program S1 boundary. Its annotated tag peels to source commit
`2b52c3fba50b9cb6a7d482ccd4e430007962c1dd`. The independently rebuilt candidate, all 15 public
release assets, and the downloaded copies were byte-identical. The wheel SHA-256 is
`a88ce965ec90c547d4f2cc76e1ace1c21ee1aaddabdfdd2fcfed3ed038bb7cfe`; the sdist SHA-256 is
`cf3b9cde7c05dab91dba2c8f8b3f970f0f148c905af4cf1a396cdd6612277332`; and the candidate
manifest SHA-256 is `326ce978f61ced2a019a920060dfd91fc764d750ed6f932da2dab9a032104a35`.

The release unit uses the published immutable Harness
[`v0.6.2`](https://github.com/DennyWanye/simple-harness-sdk/releases/tag/v0.6.2) wheel SHA-256
`ffb7c0619851f3c936fcc1d0cf527d07f49e87770291b85e57fe87032ac02c2e` and Memory
[`v0.5.2`](https://github.com/DennyWanye/simple-harness-memory-sdk/releases/tag/v0.5.2) wheel
SHA-256 `deff2fa85a269a3978f2c6efcd99fda77abcb74444170361365fd00ec0164e9e`.
Both dependency releases passed exact public download-back verification and the joint Python
3.11/3.13 compatibility matrix before the service candidate was locked.

The SDK owns provider-neutral Realtime session semantics and concrete relay/local transport
composition, but no Provider credentials, product microphone/playback policy, TokenSeller billing,
database, or execution worker. Qwen and OpenAI wire contracts remain confined to their adapters;
the OpenAI adapter has no enabled live connector in 0.3.0. The packaged authority root SHA-256 is
`2296e55ca88a02ea800a7a70c3b83a2859badb9466539ee17fe3197fcc0c5802`.

Fresh standard-pip installs from the public wheel URL passed `pip check`, public imports, and the
full installed BOM/provenance gate under CPython 3.11.15 and 3.13.13. `uv` currently omits the
archive hash from PEP 610 direct-URL records and is deliberately rejected by this provenance gate;
consumer release installs must use the exact standard-pip URL+SHA path or an equivalent trusted
artifact receipt.

The supported consumer rollback pin remains 0.2.3. Historical 0.1.3 release facts follow.

## Historical 0.1.3 release

[`v0.1.3`](https://github.com/DennyWanye/simple-harness-service-sdk/releases/tag/v0.1.3) was an
earlier non-Draft, non-Prerelease release. Its annotated tag binds source commit
`28ff026d4d99a970ffd7f9a6ae4fb9fbee7228bd`.
The published wheel SHA-256 is
`6f56b71989bb4293bfa076d8b507a49e8e5b66bffa82132c8aec56fd7420d137`; the sdist SHA-256 is
`a03d15ab4ce9301161424889570a263ebbb0726113c5b2737cbcd5b9ba3dc0d7`; and the candidate
manifest SHA-256 is `510d9b34e45d474904d2ce77ae5d6217a4d677a7c4f49fb6627c93d77d71fe85`.
All assets were independently downloaded from the public Release and verified byte-for-byte.
Python 3.11 and 3.13 exact public wheel installs with the `memory` extra passed BOM, import, and
CLI smoke gates.

Interactive chat now starts every turn after a terminal outcome as a fresh Harness run while
preserving the external session identity. This matches Harness 0.5's public rule that terminal runs
reject continuations and lets Agent Memory provide cross-run conversation continuity.

The prior candidate whose manifest SHA-256 begins `558c0a9b8a70` (wheel SHA-256
`5c286c9e1c4fdbde1d2d8034b89e6da2d414e200cb789d0dd3aac8f2c07f6181`, source commit
`edee174100eff7f99af01d7f4e2ac7afa877cae4`) is **withdrawn** after independent audit. It must
not be tagged, released, or used downstream.

The later candidate at commit `a326bc826e7d5bc11041e8ef0a7a35b9080d645c` (manifest SHA-256
`a69056117bb55f01ee9bc8b4465add49824eb292ef519d8f2a19fef7589e0c35`) is also
**withdrawn** after re-audit. Its receipts omitted Harness command kind and its credential
activation did not fsync the parent directory entry.

The SDK owns no SQLite schema, durable ingress, output store, or execution worker. Harness 0.5.2
is the sole durable command authority. The optional `memory` extra pins Memory SDK 0.5.1.
Credential admission uses the reusable create-once owner-only manifest component; operators must
load and validate it before opening the AF_UNIX socket. Installed BOM validation requires an exact,
unique SHA-256 in each dependency's `direct_url.json`; a URL-only installer record fails closed.
Receipts preserve Harness command kind so a no-run durable cancel closes as exit 6 without
misprojecting start/continue. An active physical provider call may remain recoverable
`WAITING/PENDING` with CLI timeout 4 under Harness's public default reconciliation; SVC never
forges terminal cancellation.
