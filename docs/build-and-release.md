# 构建与私有发布

0.3.13 的正式候选当前受离线材料缺失阻塞。普通源码轮子可以离线构建，但不能据此宣称完整候选、BOM、同环境安装或发布门禁通过。

## 补齐候选输入

在宿主的 `simple_harness/backend/vendor/` 准备 opt.216、编排 SDK、测试和构建依赖，或将这些制品复制到本仓库 `vendor/`。本沙箱只操作当前仓库。Memory extra 保留 0.5.2 的原始 URL 与 SHA；离线重算锁文件还需要缓存中存在该精确 URL 的制品，单纯提供同名本地轮子不一定能满足直接 URL 解析。

核验实际 opt.216 轮子的 SHA-256、公开执行 schema 与私有不可变来源后，补全 `compatibility-bom.json` 的 `execution_schema`、`url`、`sha256`，将 `verification_status` 改为 `verified-offline-wheel`。不得猜测 schema 或拿 0.6.2 的散列冒充新轮子。本仓库仍使用三 SDK 候选清单；Harness 可以来自私有 GitHub 制品地址，不能上传公开仓库。

```bash
SDK_WHEELS="$PWD/vendor"
UV_OFFLINE=1 uv lock --find-links "$SDK_WHEELS"
UV_OFFLINE=1 uv sync --frozen --group dev --extra memory --extra realtime
uv run --frozen python -m pytest -q
uv run --frozen python -m ruff check src tests scripts
uv run --frozen python -m mypy src/simple_harness_service scripts/build_candidate.py scripts/sync_realtime_authority.py
python3 scripts/check_architecture.py
python3 scripts/sync_realtime_authority.py --check
```

开发期间只运行改动对应的测试。主体完成后，才允许运行一次本仓库全套。完整安装检查必须在独立虚拟环境中按精确版本与私有目录安装两个 SDK，确认 `simple_harness`、`agent_orchestrator`、`simple_harness_service` 均可导入，并检查 BOM。私有 Harness 使用精确版本身份和已核验的 BOM；Memory 仍要求原有直接 URL/SHA 来源证明。

## 正式候选

提交所有候选输入并保证工作区干净后，在仓库内使用忽略的 `dist/` 和 `build/` 目录保存制品与临时文件：

```bash
mkdir -p build/tmp
TMPDIR="$PWD/build/tmp" UV_OFFLINE=1 python3 scripts/build_candidate.py \
  --output dist/candidate-0.3.13 --planned-tag v0.3.13
uv run --frozen python -m twine check dist/candidate-0.3.13/*.whl \
  dist/candidate-0.3.13/simple_harness_service_sdk-*.tar.gz
```

构建器保留干净提交要求、两次发行包字节比较、两次 authority bundle 比较、三种目标锁文件的两次比较，以及完整清单 schema 校验。未核验私有 BOM 时，构建器会在构建前明确拒绝。不可删减门禁来生成看似完整的候选。

候选清单需绑定 Service 0.3.13、Harness opt.216、Memory 0.5.2、四个 authority pack、三个消费端锁与源码提交。晋级前还要在 Python 3.11/3.13 验证精确制品安装、Memory 来源、authority 字节及安装后 BOM。发布端只能分发冻结字节，不能重新构建、覆盖资产或移动标签。

## 本次沙箱边界

本次不执行真模型、发版、推送或依赖下载。材料缺失时，保留未验收状态，并将失败命令、通过的隔离测试、源码轮子散列和后续步骤写入阶段记录。真线路 A06/A07、联网补齐、正式发布和推送交给 Claude。
