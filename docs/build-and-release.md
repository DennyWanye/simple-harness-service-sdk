# 构建与私有发布

0.3.14 已通过完整离线功能与质量门禁。私有 Harness 主线同时包含 `simple_harness`、`agent_orchestrator`，无需额外编排轮子。真线路 A06/A07 等 Claude 用探针跑；本次只生成候选，不发布或推送。

## 制品与 BOM

本次候选实测快照如下，存于 `build/vendor/`，`build/` 不提交。这不是运行依赖的 opt 钉版：

| 制品 | SHA-256 | 实际 schema |
|---|---|---|
| `simple_harness_sdk-0.13.0.dev20260925+opt.217-py3-none-any.whl` | `23623352e321600e39f9b9756ea7aa269e90a2c9ec1fb73aed1d4198283c16e1` | execution 5 |
| `simple_harness_memory_sdk-0.5.2-py3-none-any.whl` | `deff2fa85a269a3978f2c6efcd99fda77abcb74444170361365fd00ec0164e9e` | memory 4 |

Harness 原始轮子来自宿主 `simple_harness/backend/vendor/`，只读复制。Memory 从 `pyproject.toml` 原 GitHub release URL 下载，先核验 URL 固定的 SHA，再安装。BOM 的 Harness 主线记录为 `0.13.0.dev20260925`，本地版本以安装时实测为准，并核对 execution schema 5；`harness.candidate` 单独记录该次冻结制品。BOM 的 `wheelhouse:` 表示候选内同名的私有轮子，不代表已经发布的网络地址；候选构建器会校验两轮子的实际字节并将其复制到候选目录。

## Python 3.13 安装组合

Memory 0.5.2 元数据声明 `simple-harness-sdk>=0.4,<0.7`，与本包的 Harness 主线下限冲突。`uv lock --find-links build/vendor` 已实际尝试并确认不可解析。依任务约定保留 Memory extra；原 `uv.lock` 留作历史快照，不能用 `uv sync --frozen` 或默认同步的 `uv run` 安装当前组合，也不宣称 `pip check` 的旧约束兼容。

在 Python 3.13 的本仓库 `.venv` 中分步安装；依赖必须显式装齐：

```bash
SDK_WHEELS="$PWD/build/vendor"
uv pip install --python .venv/bin/python \
  'httpx>=0.28.1,<1' 'websockets>=15,<16' 'prompt-toolkit>=3.0.52,<4' \
  'pytest>=8.4,<9' 'pytest-asyncio>=1.1,<2' 'mypy>=1.17,<2' 'ruff>=0.12,<1' \
  'hatchling>=1.27,<2' 'build>=1.3,<2' 'twine>=7,<8' pip \
  'deepseek-recipe==0.1.1' 'aiosqlite>=0.19' 'numpy>=1.24,<2.5' \
  'pydantic>=2' 'structlog>=24' 'packaging>=25,<27'
uv pip install --python .venv/bin/python --find-links "$SDK_WHEELS" \
  --no-deps 'simple-harness-sdk>=0.13.0.dev20260925'
uv pip install --python .venv/bin/python --no-deps \
  "$SDK_WHEELS/simple_harness_memory_sdk-0.5.2-py3-none-any.whl"
uv pip install --python .venv/bin/python --no-deps -e .
```

要通过安装来源门禁，使用标准 pip 按同一轮子重新登记本地 URL 与 archive SHA；不要伪造或手写 `direct_url.json`。当前 uv 安装的 archive provenance 缺少散列时会明确失败。

```bash
SDK_HARNESS_VERSION=$(.venv/bin/python -c 'import importlib.metadata as m; print(m.version("simple-harness-sdk"))')
.venv/bin/python -m pip install --no-deps --force-reinstall \
  "$SDK_WHEELS/simple_harness_sdk-${SDK_HARNESS_VERSION}-py3-none-any.whl" \
  "$SDK_WHEELS/simple_harness_memory_sdk-0.5.2-py3-none-any.whl"
.venv/bin/python -c 'import simple_harness, agent_orchestrator, simple_harness_service, simple_harness_service.realtime; from simple_harness_service import validate_installed_bom; print(validate_installed_bom(include_memory=True))'
```

新环境安装候选 Service 轮子时也使用 `--no-deps`，传入候选目录的精确轮子。先安装其显式依赖，再安装两个 SDK 轮子，保持同一解释器。

## 验证与候选

开发只运行改动对应的测试，主体完成后运行一次本仓库全套：

```bash
mkdir -p build/tmp
TMPDIR="$PWD/build/tmp" uv run --frozen --no-sync python -m pytest -q
.venv/bin/python -m mypy src/simple_harness_service scripts/build_candidate.py scripts/sync_realtime_authority.py
.venv/bin/python -m ruff check src tests scripts
python3 scripts/check_architecture.py
python3 scripts/sync_realtime_authority.py --check
```

提交全部候选输入并保证工作区干净，再生成正式候选：

```bash
TMPDIR="$PWD/build/tmp" .venv/bin/python scripts/build_candidate.py \
  --output dist/candidate-0.3.14 --planned-tag v0.3.14 --wheelhouse build/vendor
.venv/bin/python -m twine check dist/candidate-0.3.14/*.whl \
  dist/candidate-0.3.14/simple_harness_service_sdk-*.tar.gz
```

构建器保留两次发行包字节比较、两次 authority bundle 比较、三个目标锁文件的两次比较，以及完整清单 schema 校验。产品元数据继续使用 `>=0.13.0.dev20260925`；候选构建器通过独立约束文件冻结 `harness.candidate.version`，即使 wheelhouse 中同时有多个 opt 版本，也只解析本次实测版本。运行锁使用 `--find-links` 解析私有 Harness，移除机器专用路径并保留精确版本与 SHA。Linux ARM64 的平台基线为 `aarch64-manylinux_2_28`（glibc ≥ 2.28），因为必需的 deepseek-recipe 0.1.1 无 manylinux2014 轮子；未声明旧 glibc 支持，也未执行 Linux 真机运行验收。截止时间更新为 `2026-10-25T00:00:00Z`，因为旧截止日期早于该主线必需的 deepseek-recipe 0.1.1 发布时间。

候选清单绑定 Service 0.3.14、本次实测 Harness 轮子、Memory 0.5.2、四个 authority pack、三份消费端锁与源码提交。源码 wheel 和 sdist 均需两次字节一致；Memory 旧元数据约束不因打包而改变。候选只供私有分发，任何未来地址均为晋级定位，不代表本次已上传。发布端只能分发冻结字节，不能重建、覆盖资产或移动标签。
