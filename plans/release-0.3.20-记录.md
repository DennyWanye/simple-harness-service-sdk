# Service SDK 0.3.20 正式发版准备记录

日期：2026-10-28。工作范围仅为本 Service SDK 仓库。起点 `8891f016aaccdd3efc14bac6f5e6a6bdf727f981`，候选源码提交 `f34caa3c82f1da73df0ea7bdf91f055a7c179f35`；后续文档提交只收录发布说明、人工脚本、安装来源操作修订与本记录，不重建制品。人工脚本明确将 v0.3.20 标签指向候选源码提交。

## 已完成

1. 先补 `tests/test_bom_architecture.py`：版本参数加入 opt.221，固定候选字段及新 SHA，覆盖安装版本等于候选时正确哈希通过、错误哈希拒绝。未修改 BOM 前实测 16 通过、2 失败；错误分别是候选仍为 opt.217，以及错误哈希未被拒绝。改 BOM 后该文件 18 通过、0 失败。
2. BOM 的 candidate version、wheel、url、SHA 改为 opt.221；版本号 0.3.20 和 Harness `>=0.13.0.dev20260925` 要求未改。现有 Python 3.13.15 开发环境按文档安装 opt.221 后，读取实际 `CommandReceipt.execution_schema_version` 为 5，与 BOM 一致。
3. 从 Host 的指定 vendor 路径只读复制 opt.221，核对 SHA 后移除本仓库 `build/vendor/` 的 opt.217；opt.216 保留。未修改 Host 文件。旧候选完整改名为 `dist/candidate-0.3.20-opt217/`，未删除。
4. 更新 CHANGELOG、README、Realtime 说明与构建文档；保留 opt.217 历史语义。提交候选输入后，在干净工作区执行构建器；两次 wheel / sdist、authority bundle、三个锁文件的比较及清单 schema 门禁均通过。
5. 新建中文 GitHub release 正文和仅供人工执行的发布脚本。脚本先核验 SHA256SUMS，再列出标签、推送和创建 release 的命令；本次未运行。

## 制品散列

| 制品 | 原 opt.217 候选 SHA-256 | 新 opt.221 候选 SHA-256 |
|---|---|---|
| Service 0.3.20 wheel | `f1e091bfe6656148d9541035bc0b0231e26636acf9b25ca9cc16c8ce1bcad3f0` | `95a5ec79e6b12d7958f282e11c5e833f904f51505220a9d704698c14b2547bbb` |
| Harness wheel | `23623352e321600e39f9b9756ea7aa269e90a2c9ec1fb73aed1d4198283c16e1` | `bed6ed01950767e3a4e1af99ce0b26e04e94a0671d0e7f0adb51dfa01064ff23` |
| Memory 0.5.2 wheel | `deff2fa85a269a3978f2c6efcd99fda77abcb74444170361365fd00ec0164e9e` | `deff2fa85a269a3978f2c6efcd99fda77abcb74444170361365fd00ec0164e9e` |

新 Service sdist SHA-256：`c79542a56838f3a447fd0b9bc2681c1f7e6c04477488bf4286fa010c267eafe1`。

| 锁文件 | 目标 | SHA-256 |
|---|---|---|
| `realtime-lock-simple-harness-py311-macos-arm64.txt` | Python 3.11 / `aarch64-apple-darwin` | `11b63ada244d9ec1d9655bf4d9358af430b1c3d22485abcf346562e82bcf1c25` |
| `realtime-lock-simple-harness-py311-windows-x86-64.txt` | Python 3.11 / `x86_64-pc-windows-msvc` | `5288c0a8ce859a6c63ee9e9a0e97896b39f3bcc71f1350409c3667bdc3fe6b57` |
| `realtime-lock-aiphone-py313-linux-aarch64.txt` | Python 3.13 / `aarch64-manylinux_2_28` | `11b63ada244d9ec1d9655bf4d9358af430b1c3d22485abcf346562e82bcf1c25` |

## 验证命令与结果

```bash
TMPDIR="$PWD/build/tmp" .venv/bin/python -m pytest -q tests/test_bom_architecture.py tests/test_wheel_contract.py tests/test_realtime_authority_packaging.py::test_three_sdk_release_unit_and_future_download_urls tests/test_realtime_authority_packaging.py::test_target_locks_freeze_the_measured_candidate_harness
.venv/bin/python -m ruff check src tests scripts
.venv/bin/python -m mypy src/simple_harness_service scripts/build_candidate.py scripts/sync_realtime_authority.py
python3 scripts/check_architecture.py
python3 scripts/sync_realtime_authority.py --check
TMPDIR="$PWD/build/tmp" .venv/bin/python scripts/build_candidate.py --output dist/candidate-0.3.20 --planned-tag v0.3.20 --wheelhouse build/vendor
.venv/bin/python -m twine check dist/candidate-0.3.20/*.whl dist/candidate-0.3.20/simple_harness_service_sdk-*.tar.gz
(cd dist/candidate-0.3.20 && shasum -a 256 -c SHA256SUMS)
```

- 最终相关测试：23 通过、0 失败（BOM 18，轮子合同 3，直接读取 BOM 的候选打包测试 2），没有运行整套测试。先红阶段的 2 个失败已解决，不计作最终失败。
- ruff：通过；mypy：44 个源码文件通过；架构与 authority 同步检查：均通过。
- twine：3 个轮子和 1 个源码包，4 通过、0 失败。首次调用误用了候选目录作为 cwd，通配路径不存在，未执行 twine；改回仓库根目录后通过。
- SHA256SUMS：15 项通过、0 失败；候选清单另由构建器校验。构建日志保留于忽略目录 `build/candidate-0.3.20-build.log`。

## 全新安装验证

环境：`build/verify-0.3.20/`，全新 CPython 3.13.15，pip 26.2.2；未使用 `--no-deps`。公共依赖按构建文档范围从公开 PyPI 安装，之后使用 `--no-index --find-links dist/candidate-0.3.20 --find-links build/vendor` 安装 `simple-harness-service-sdk[realtime]==0.3.20`。

第一次按包名称解析轮子后，`uv pip check` 与导入成功，但 BOM 自检失败：`harness source provenance missing`。这是标准 pip 按名称从 find-links 选择轮子不会写 direct_url.json 的行为。仅在此验证环境中卸载 Harness，再用标准 pip 显式传入 `build/vendor/simple_harness_sdk-0.13.0.dev20260925+opt.221-py3-none-any.whl` 重装，依赖继续正常解析；未伪造或手写来源文件。文档已改为先显式安装 Harness，再按名称安装 Service，以避免此问题。

| 验证项 | 结果 |
|---|---|
| 无 `--no-deps` 安装 `[realtime]` | 通过 |
| `uv pip check --python build/verify-0.3.20/bin/python` | 通过，19 个包兼容 |
| `simple_harness`、`agent_orchestrator`、`simple_harness_service` 导入 | 3 通过、0 失败，来源均在全新环境 site-packages |
| `simple_harness_service.realtime` 导入 | 通过 |
| 已安装 Harness | `0.13.0.dev20260925+opt.221`，通过 |
| execution schema | 实测 5，与 BOM 一致，通过 |
| `simple_harness_service.bom.validate_installed_bom()` | 通过，返回 Service 0.3.20、Harness opt.221 |
| Harness direct_url SHA | 与 BOM 的 opt.221 SHA 一致，通过 |

## 未做与后续边界（发版准备阶段的历史状态）

- 未打 tag、未 push、未执行 gh release、未运行人工发布脚本；未生成或改写正式发布回执，现有 `RELEASE_RECEIPT.json` 仍为历史记录。
- 未更改版本号、依赖下限、任何实时协议合同、提示词或工具说明。未运行整套测试，也未做 Windows / Linux 真机验收。
- A09～A11 仍阻断在中继 `upstream_protocol_error`；不提供请求与 response 的可靠关联，打断歧义时发 `response_origin_ambiguous`。
- Memory 0.5.2 的旧 Harness 元数据约束冲突仍存在，本次全新安装不包含 Memory extra；开发环境原有 Memory 组合仅用于相应 BOM 测试。
- Host 的 `uv.lock` 目前仍指向本机忽略目录中的旧 0.3.20 轮子（本任务给定背景）；发版后 Host SDK 的 uv.lock 需要换指向 GitHub release 的轮子，属 Host 侧另一次改动。本次未改 Host 仓库任何文件。
- 候选制品与验证环境位于 Git 忽略目录，不纳入本地提交；由人工脚本发布已冻结的候选字节。

## 正式发布后回执补录

人工已完成发布：[v0.3.20 正式 release](https://github.com/DennyWanye/simple-harness-service-sdk/releases/tag/v0.3.20)。GitHub 实测发布时间为 `2026-10-27T17:46:00Z`（台北时间 2026-10-28 01:46:00）。本节取代上文准备阶段中“尚未发布、回执仍为历史记录”的状态描述；本次代理只读远端并补本地回执。

### 远端元数据与标签

使用 `gh release view v0.3.20 --repo DennyWanye/simple-harness-service-sdk --json url,tagName,targetCommitish,isDraft,isPrerelease,publishedAt,assets` 以及 `gh api repos/DennyWanye/simple-harness-service-sdk/releases/tags/v0.3.20` 获取真实数据；通过 `releases/latest` API 确认当前 latest，通过 Git ref / tag API 和本地 Git 对象双向核对附注标签。

| 字段 | 实测结果 |
|---|---|
| 版本 | 0.3.20 |
| 源码提交、剥离提交 | `f34caa3c82f1da73df0ea7bdf91f055a7c179f35` |
| 标签对象 | `ac83a9bb51eee317d00bf9118221f90dd3ec86f6` |
| 附注标签 | true |
| draft / prerelease | false / false |
| latest | true，核对时的 GitHub 状态 |
| GitHub immutable | false |
| published_at | `2026-10-27T17:46:00Z` |
| release 附件数 | 17，不含 GitHub 自动生成的源码下载链接 |

已核对 `git show v0.3.12 -- RELEASE_RECEIPT.json` 及该 tag 内文件；该 tag 的回执内容仍记录 0.3.0。本次沿用其 `simple-harness-service-release-receipt-v1` 字段结构，所有远端状态与附件散列均重新取证，没有沿用旧发布值。回执中的源码检查和 Python 3.13 候选安装结果来自前述发版准备阶段，本次未重跑；已明确其范围，移除旧回执中不适用于本次的 Python 3.11 / 公开安装等断言。

### 下载与逐项散列核对

新建此前不存在的空目录 `build/release-verify-0.3.20/`，执行：

```bash
gh release download v0.3.20 --repo DennyWanye/simple-harness-service-sdk --dir build/release-verify-0.3.20
(cd build/release-verify-0.3.20 && shasum -a 256 * > ../release-0.3.20-downloaded.sha256)
(cd build/release-verify-0.3.20 && shasum -a 256 -c ../../dist/candidate-0.3.20/SHA256SUMS)
```

核对结果：全部 17 个远端附件的集合与本地候选集合一致；下载字节实测 SHA-256 与本地候选 **17/17 一致，0 项差异**。候选 `SHA256SUMS` 中列出的 **15/15 一致，0 失败**；未列入其中的 `SHA256SUMS` 本身与 `candidate-manifest.json` 本身另与本地同名冻结文件实测比较，**2/2 一致**。候选清单内 16 项散列也与下载字节一致。回执 `assets` 的 17 项散列全部取自下载后运行 `shasum -a 256` 的结果，未使用 GitHub API 的 digest 代替实测。

Service 轮子实测 SHA-256：`95a5ec79e6b12d7958f282e11c5e833f904f51505220a9d704698c14b2547bbb`。Harness opt.221、Memory 0.5.2 以及 authority 信息从已核对的下载制品回填。GitHub 元数据快照、散列实测输出和下载附件均留在 `build/` 忽略目录。

### 本次测试与提交范围

没有找到直接读取 `RELEASE_RECEIPT.json` 的现有测试。按测试先行要求，在 `build/test_release_receipt_0_3_20.py` 编写只读取回执并与远端元数据、实测散列组成的预期值比较的专项测试：更新前 **0 通过、1 失败**（旧回执仍为 0.3.0）；更新后 **1 通过、0 失败**。

```bash
.venv/bin/python -m pytest -q build/test_release_receipt_0_3_20.py
```

本次只更新 `RELEASE_RECEIPT.json`、`CHANGELOG.md` 和本记录并本地提交。未运行整套测试，未重建候选，未 push，未改 tag 或 release，未运行发布脚本，未改 Host 仓库。Host SDK 的 `uv.lock` 换指向正式 GitHub release 轮子仍属另一次 Host 侧改动。
