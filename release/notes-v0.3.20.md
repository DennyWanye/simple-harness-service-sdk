# simple-harness-service-sdk v0.3.20

版本：0.3.20。候选源码提交：`f34caa3c82f1da73df0ea7bdf91f055a7c179f35`。正式发布须使用该提交上的冻结制品；发布说明和验证记录在后续本地文档提交中补齐，制品不再重建。

## 本版内容

- 用户语音按输入来源登记，角色元数据矛盾记录诊断，不覆盖已确认身份。
- 身份表保存首次来源并兼容旧快照，保留既有作用域、代数与工具授权边界。
- 候选冻结 Harness `0.13.0.dev20260925+opt.221`（原 opt.217），实测 execution schema 为 5。运行依赖下限仍为 `>=0.13.0.dev20260925`。

## 冻结制品

| 轮子 | SHA-256 |
|---|---|
| `simple_harness_service_sdk-0.3.20-py3-none-any.whl` | `95a5ec79e6b12d7958f282e11c5e833f904f51505220a9d704698c14b2547bbb` |
| `simple_harness_sdk-0.13.0.dev20260925+opt.221-py3-none-any.whl` | `bed6ed01950767e3a4e1af99ce0b26e04e94a0671d0e7f0adb51dfa01064ff23` |
| `simple_harness_memory_sdk-0.5.2-py3-none-any.whl` | `deff2fa85a269a3978f2c6efcd99fda77abcb74444170361365fd00ec0164e9e` |

附件还包含源码包、BOM、METADATA、WHEEL、BUILD_INFO、候选清单、authority 索引及四份合同的打包制品。下载后先核对 `SHA256SUMS`，只分发冻结字节。

## 三个平台锁文件

| 锁文件 | 目标 | SHA-256 |
|---|---|---|
| `realtime-lock-simple-harness-py311-macos-arm64.txt` | Python 3.11 / `aarch64-apple-darwin` | `11b63ada244d9ec1d9655bf4d9358af430b1c3d22485abcf346562e82bcf1c25` |
| `realtime-lock-simple-harness-py311-windows-x86-64.txt` | Python 3.11 / `x86_64-pc-windows-msvc` | `5288c0a8ce859a6c63ee9e9a0e97896b39f3bcc71f1350409c3667bdc3fe6b57` |
| `realtime-lock-aiphone-py313-linux-aarch64.txt` | Python 3.13 / `aarch64-manylinux_2_28` | `11b63ada244d9ec1d9655bf4d9358af430b1c3d22485abcf346562e82bcf1c25` |

Linux ARM64 要求 glibc ≥ 2.28。锁文件已重复生成并比较字节，本次未做 Windows / Linux 真机运行验收。

## 验证与安装

相关测试 23 通过、0 失败；ruff、mypy、架构与 authority 同步检查通过。wheel、sdist、authority bundle 和三份锁文件均重复构建字节一致；twine 检查 4 项通过。全新 Python 3.13.15 环境的 `[realtime]` 安装不使用 `--no-deps`，`uv pip check`、三个 SDK 入口导入、Harness opt.221 版本、execution schema 5 和 BOM 来源自检均通过。具体离线安装步骤见 `docs/build-and-release.md`。

Memory 0.5.2 轮子随候选冻结，但其旧 Harness 元数据约束仍冲突；本次全新安装验收只覆盖 `[realtime]`，不声明 `[memory]` 可直接解析。

## 已知限制

- A09～A11 阻断在中继 `upstream_protocol_error`，未宣称真线路验收通过。
- 不提供请求与 response 的可靠关联，打断歧义时发 `response_origin_ambiguous`。
- Host SDK 的 `uv.lock` 发版后需要换指向 GitHub release 的轮子，属于 Host 侧另一次改动。
