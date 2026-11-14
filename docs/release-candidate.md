> 历史候选记录（2026-10-23）：其中许可证和发布状态是当时的情况；当前 beta 以根目录 LICENSE 和 README 为准。

# 发布候选：可独立部分（S8）

日期：2026-10-23。状态：**候选，未发布**。本文件只记录可独立完成的安装说明、项目 skill、署名与导出边界；不授权公开发布、推送、部署或安装依赖。

## 适用边界

本文件定义安装与导出方法，不维护 S8 进度；执行状态只在活动计划，验证材料在 experiments/。文中命令不是已执行证据。公开前必须补全实际安装验证、项目许可证选择和导出物审查；源码候选检查不替代安装验收。

不替项目所有者决定版权授予。上游第三方许可不能代替 Journey Pilot 自身的 `LICENSE`。

## 独立环境安装

前置：Python >= 3.11；`adb`；`phone-harness` 可执行文件在 `PATH` 或由 `PHONE_HARNESS_BIN` 指定。`status` 的 adb 枚举只需要 adb，但就绪探测、动作、观察和重放依赖 phone-harness；纯 `capabilities`、`propose`、`task-list` 等不触设备命令不需要它。`observe --request-stdin` 的 `source=pixels` 还依赖 macOS Vision，不是通用 OCR 回退。

```sh
python3 -m venv "$HOME/.local/share/journey-pilot-venv"
"$HOME/.local/share/journey-pilot-venv/bin/python" -m pip install -e /absolute/path/to/journey-pilot
"$HOME/.local/share/journey-pilot-venv/bin/journey-pilot" capabilities
```

- 使用绝对 launcher 路径，避免不同 shell 解析到另一份 `journey-pilot`。
- 不修改系统 Python；项目自身没有声明 Python 运行时依赖，phone-harness 单独安装。
- 本地可编辑安装需要 `setuptools>=68`。本机 Homebrew Python 3.14.7 已有 setuptools 80.10.2；在独立临时 venv 复用已有构建工具，以 `--no-index --no-build-isolation --no-deps` 实测可编辑安装成功，无依赖下载。默认 Python 3.12.12 缺少后端，不代表所有环境都不可安装。
- 未安装时直接运行：`./scripts/journey-pilot capabilities`。

**跨目录检查示例**（本机离线安装验证见收口实验，不代表任意新机器通过）：

```sh
cd /tmp
JOURNEY_PILOT_HOME=/tmp/journey-pilot-store /absolute/venv/bin/journey-pilot capabilities
JOURNEY_PILOT_HOME=/tmp/journey-pilot-store /absolute/venv/bin/journey-pilot task-list
```

期望：第一条返回 `status=ok`，`data.store.root` 为 `/tmp/journey-pilot-store`，`data.program_version` 为 `0.1.0`，`data.store_env_var` 为 `JOURNEY_PILOT_HOME`；第二条列出 `xhs-read-post` 与 `douyin-ai`。这两条不证明手机可用。

## CLI 调用与同一 Store 发现

命令在 `argv`，一个 JSON 请求对象从 stdin 读取；可能敏感的值不走进程参数。

```sh
journey-pilot capabilities
printf '%s\n' '{"task_key":"read-title","intent":"Read visible title","steps":[{"step_id":"title","action":"read_field","locator":{"text":"Title"}}],"postconditions":{"text_equals":"Title"}}' \
  | journey-pilot propose --request-stdin
printf '%s\n' '{"journey_id":"<返回的 ID>"}' | journey-pilot inspect --request-stdin
```

同一 Store 的判定：

1. 每个 Agent 先运行 `capabilities`，读取 `data.store.root`。
2. 默认根为 `~/.local/share/journey-pilot`，由 `JOURNEY_PILOT_HOME` 覆盖，不随 cwd 改变。
3. 让所有 Agent 使用同一 `JOURNEY_PILOT_HOME`，再分别从不同 cwd 运行 `capabilities`，比较 `store.root`。
4. 用 `discover`、`task-list`、`inspect` 验证能读到同一批 Journey/任务/run。跨机器没有同步机制，不要复制活动中的 Store。
5. Store 使用 `schema_version=1`；不同程序版本共享同一 Store 的兼容性未验证，发布说明应要求同一版本。

`capabilities` 是能力事实来源：命令、动作、检查、默认值、请求字段和 limitations 都以它为准。`--help` 只提供人类可读帮助。

## 干净最小导出清单

导出必须用显式 allowlist，不能使用 `git archive`、直接复制整个工作树或发布当前 `.git`。当前 `git ls-files` 已跟踪 `.planning/`、`experiments/baseline/`、`research/sources/`、`HANDOVER.md` 等私有/权利未清材料。

### 必须包含

| 路径 | 说明 |
|---|---|
| `pyproject.toml` | 包名、入口、构建配置 |
| `src/journey_pilot/__init__.py` | 版本 |
| `src/journey_pilot/paths.py` | Store 根与 `JOURNEY_PILOT_HOME` |
| `src/journey_pilot/store.py` | 文件 Store |
| `src/journey_pilot/privacy.py`、`trace.py`、`review.py`、`recovery.py` | 元数据保留、事件引用、复盘、受限对账 |
| `scripts/journey-pilot` | 跨目录源码 launcher |
| `skills/execution-review/SKILL.md`、`.claude/skills/` | 复盘技能与项目同源入口 |
| `src/journey_pilot/execution.py` | 设备锁、请求去重、原子写 |
| `src/journey_pilot/pacing.py` | 跨 CLI 动作节奏与运行预算 |
| `src/journey_pilot/parameters.py` | 精确参数占位符绑定 |
| `src/journey_pilot/fixed.py` | 固定坐标段校验 |
| `src/journey_pilot/contracts.py` | 结果与能力目录 |
| `src/journey_pilot/journey.py` | Journey 校验 |
| `src/journey_pilot/adapter.py` | phone-harness/adb 适配 |
| `src/journey_pilot/runner.py` | validate/run/cancel |
| `src/journey_pilot/session.py` | 探索会话 |
| `src/journey_pilot/cli.py` | CLI 入口 |
| `src/journey_pilot/read_post.py` | 小红书薄任务 |
| `src/journey_pilot/douyin_ai.py` | 抖音薄任务 |
| `src/journey_pilot/task_runtime.py` | 薄任务公共执行层 |
| `src/journey_pilot/task_registry.py` | 显式任务登记 |
| `tests/test_cli_capabilities.py` | 能力契约回归 |
| `tests/test_contracts.py` | 结果契约回归 |
| `tests/test_execution_guards.py` | 入口与执行守卫回归 |
| `tests/test_experience_discovery.py` | 发现与运行摘要回归 |
| `tests/test_fast_read.py` | 只读路线回归 |
| `tests/test_fixed_segments.py` | 固定坐标段回归 |
| `tests/test_locator_fallback.py` | 备用定位回归 |
| `tests/test_pacing.py` | 节奏与预算回归 |
| `tests/test_parameterized_journey.py` | 参数绑定回归 |
| `tests/test_resume.py` | 检查点续跑回归 |
| `tests/test_runner_guards.py` | 执行守卫回归 |
| `tests/test_store.py` | 存储回归 |
| `tests/test_thin_tasks.py` | 薄任务回归 |
| `examples/STRATEGY.md` | 配置兼容示例 |
| `examples/journeys/read_visible_title.json` | 合成候选示例 |
| `skills/journey-pilot/SKILL.md` | 项目 skill |
| `THIRD_PARTY_NOTICES` | phone-harness 署名 |
| `docs/release-candidate.md` | 本文件 |
| `.gitignore` | 基础忽略项 |

### 链接审计后再决定

| 路径 | 未决原因 |
|---|---|
| `README.md` | 内部开发导航不直接导出；构建脚本生成独立候选README，避免引用私有材料 |
| `docs/purpose.md`、`docs/requirements.md`、`docs/experience-rules.md`、`docs/agent-interface.md`、`docs/task-scripts.md`、`docs/implementation-plan.md` | 面向使用者/开发者的契约，建议保留；需检查内部路径引用 |
| `proposals/minimal-v1.md`、`docs/decisions/0001`–`0009` | 设计历史与决策；部分引用 `.planning` 或实验证据，需逐条检查 |

### 明确排除

| 路径/对象 | 原因 |
|---|---|
| `.git/`、当前 Git 历史 | 历史中包含私有计划、研究来源、实验记录和账号相关内容；公开需新建干净历史或单独审查重写 |
| `.planning/`、`docs/archive/`、`START-HERE.md`、`AGENTS.md`、`docs/current-state.md` | 内部执行状态、授权边界、历史交接；引用私有路径 |
| `research/` 全部 | `research/sources/` 为第三方原始材料，再分发权利未清；内部摘要含未授权引用 |
| `experiments/` 全部 | 含账号正文、标题/评论、设备信息、原始 UI 树和截图 |
| `~/.local/share/journey-pilot/` 运行 Store | `runs/**` 含 tree、截图、正文和任务结果；可能含隐私 |
| `__pycache__/`、`.venv/`、`.env*`、`runtime/` | 构建产物、环境与本地配置 |
| `src/journey_pilot/dual_decision.py`、`tests/test_dual_decision.py` | 未接入 CLI，按计划不列为交付；保留会暗示未实现能力 |

`experiments/` 中的具体敏感样本包括但不限于：`experiments/xhs-publish-20261021/*.png`、`experiments/timing-probe/screen-*.png` 与 `tree-*.json`、`experiments/*/judgment-*.jsonl`、`experiments/douyin-*/**.json`。这些只作本地证据，不进入发布包。

## 隐私、历史与密钥边界

- 本轮扫描了拟导出的 `src/`、`tests/`、`examples/`、`docs/`、`README.md` 等，未发现 API key、密码、Cookie、Authorization 头或账号凭据；代码中的 `secrets.token_hex` 只生成租约/请求 ID。扫描不是泄漏保证。
- `.gitignore` 只覆盖 `__pycache__/`、`.DS_Store`、`.env`、`.venv/`、`runtime/`；它不覆盖 `experiments/`、`research/`、`.planning/` 或运行 Store。不能依赖 `.gitignore` 做发布隔离。
- 当前仓库的 Git 历史不能直接公开。即使工作树只导出 allowlist，`git log` 仍可恢复私有材料；公开仓库必须使用新目录 `git init` 或经单独授权清理历史。
- 运行产物默认在 `~/.local/share/journey-pilot`，不随仓库移动。共享前应使用临时 `JOURNEY_PILOT_HOME`，或先检查/清理 `runs/**` 中的 tree、截图和 `task-result.json`。
- 不得在发布包、issue、提交信息中记录设备 serial、账号昵称、正文、评论或截图。
- 参数 schema 不支持 `sensitive`/`persist` 字段；当前 request_once 缓存元数据、不保存 outputs/value；session 文本 locator、失败详情与任意事件字段不默认持久化。调用者标签、静态配方、复盘文本及 opt-in 原树仍需审查；上游崩溃临时文件边界未完整证明，不宣称全链路自动脱敏。

## 许可证与署名（未决）

- 仓库当前没有 `LICENSE`。Journey Pilot 自身采用何种许可证由项目所有者决定；本文件不授予任何权利，也不创建 `LICENSE`。
- `THIRD_PARTY_NOTICES` 记录 phone-harness：MIT，Copyright (c) 2026 shawn pana，上游 `https://github.com/ShawnPana/phone-harness`，审查提交 `88b3642ac4733c47daf12e0f2837bc789eda63c6`，`LICENSE` SHA-256 `18e66d0f33ecd712f960c2b76e36279c613d1cfbd425cc9537f8e84a767f35b3`。
- Journey Pilot 不打包 phone-harness 源码，但运行时调用其可执行文件，且像素观察脚本镜像其 OCR 坐标归一化。MIT 要求保留版权与许可声明；`THIRD_PARTY_NOTICES` 已包含完整 MIT 文本。
- “许可证覆盖改编来源”只能部分确认：上游 MIT 声明已保留；Journey Pilot 自身许可证未定；`research/sources/` 与实验材料权利未清，已排除。是否需要额外法律判断，由所有者决定。
- 不把上游 MIT 许可当作对 Journey Pilot 自身代码的授权声明。

## 发布前阻塞

1. **安装适用环境**：已在独立临时 venv 借用本机已有构建工具离线安装并验证 console script；未安装新依赖、未安装到用户正式环境。无已有构建后端的新机器仍需准备依赖。
2. **许可证未定**：无 `LICENSE`；需所有者决定，不能由实施者代选。
3. **导出文档**：内部 README 保留开发导航；干净候选生成独立README并在候选副本移除指向未导出内部规范的链接，链接检查无缺失。公开前仍需审查文案与许可。
4. **Git 历史未处理**：当前历史含私有材料；公开前需新历史或单独审查。
5. **权利未清材料**：`research/sources/`、实验截图/原始树不能发布；需要所有者确认是否保留内部版本。
6. **能力守卫边界**：参数绑定、入口检查、guard_version=2、节奏预算和检查点 resume 已实现并有离线测试，但仍不能宣称完整环境隔离、任意步骤恢复、平台负载保证或固定坐标提速；发布说明必须保留 `capabilities.limitations`。
7. **skill 范围**：项目 `.claude/skills/` 同源入口已被当前 Claude Code 会话发现并调用 execution-review；独立 Kimi 经绝对 launcher 查询经验并只读执行成功。未安装全局 hook，未证明任意客户端自动发现。
8. **phone-harness 版本边界未验证**：未测试其他 phone-harness 版本；发布说明应要求记录版本并重新验证。

## 本轮证据

- `JOURNEY_PILOT_HOME=<临时目录> PYTHONPATH=src python3 -m journey_pilot.cli capabilities`：`status=ok`，`store.root` 指向临时目录，`program_version=0.1.0`，`store_env_var=JOURNEY_PILOT_HOME`。
- 同临时 Store 的 `task-list`：`status=ok`，列出 `xhs-read-post`、`douyin-ai`，`qualification=registered_script_not_verified_journey`。
- `journey-pilot --version`：`journey-pilot 0.1.0`；本机 Python 3.12.12。
- 上游 `phone-harness/LICENSE` 只读核查：MIT，版权行与哈希见上；上游提交 `88b3642`（2026-09-20）。
- 仓库根目录在创建前无 `LICENSE`、无 `THIRD_PARTY_NOTICES`、无 `skills/`。
- 以上均为离线/临时 Store 检查，不是安装、设备、实机或发布验收。
- 2026-10-23 文档同步轮：临时 Store 的 `capabilities` 离线返回 `session handoff` 与 `resume` 为 implemented；defaults 含 `action_interval_seconds=3`、`network_interval_seconds=5`、`max_actions=40`、`max_network_actions=3`、`max_scrolls=5`。未运行设备命令，不构成实机验收。
