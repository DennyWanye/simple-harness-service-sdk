# Service SDK 0.3.13 阶段 2 状态

2026-10-25：阶段 2 源码与完整离线门禁已通过。Python 3.13 同环境可导入 `simple_harness`、`agent_orchestrator`、`simple_harness_service`、`simple_harness_service.realtime`；编排包随 opt.216 轮子提供。实测 Harness execution schema 5、Memory schema 4；两个依赖轮子的 SHA-256 与安装来源已核验。

本仓库全套 410 条测试通过，严格 mypy 的 43 个源文件通过，ruff、公开接口、架构门禁和四包 authority 同步检查通过。正式候选绑定源码提交，并携带两个依赖轮子和三个消费端锁；具体制品散列与复现结果见[阶段 2 记录](plans/2026-10-25-stage2/阶段2-记录.md)。

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
