# 本地快速开始

当前适用范围：同一台 Mac、同一用户、已连接并授权的 Android。设备通过 `adb` 发现，观察与动作经 Phone Harness 执行；手机未连接或主机不可达时，没有远程手机访问能力。已实现的是只读任务、探索会话、受控 Journey 重放，以及 `trace` / `review` / `lessons` / `reconcile` 经验闭环；发布、回复、交易、照片上传和账号操作不在当前自动化范围内。

## 本机入口

源码检出的唯一入口是 `scripts/journey-pilot`。它解析自身真实路径（包括符号链接），把 `<repo>/src` 前置到 `PYTHONPATH`，再从任意 cwd 启动；不安装依赖、不改系统 Python、不改 `JOURNEY_PILOT_HOME`。

项目内另有两个 Claude Code 项目级 skill 发现入口，均为相对符号链接：

```text
.claude/skills/journey-pilot      -> ../../skills/journey-pilot
.claude/skills/execution-review   -> ../../skills/execution-review
```

它们只让项目内 Claude Code 找到 `SKILL.md`，不是 hook、watcher 或自动触发器。任务前的经验检索、任务后的复盘仍需 Agent 显式执行。

## 三步使用

以下命令用实际 checkout 路径替换 `REPO`，可在任意 cwd 运行。

```sh
REPO="$HOME/Projects/journey-pilot"   # 本机 checkout；其他机器改成实际路径
```

### 1. 确认启动器、版本和共享 Store

```sh
"$REPO/scripts/journey-pilot" capabilities
```

输出是 JSON。核对 `data.store.root` 是否为你希望所有 Agent 共用的目录。默认是 `~/.local/share/journey-pilot`；`JOURNEY_PILOT_HOME` 可覆盖，启动器只继承、不修改该变量。

从仓库根和从 `/tmp` 等任意 cwd 用绝对路径调用，`data.store.root` 和 `data.program_version` 应一致。`capabilities` 不访问设备，也不创建 Store。

### 2. 检查设备是否真的可用

```sh
"$REPO/scripts/journey-pilot" status
```

需要手机已连接、解锁并授权 USB 调试。`status` 可以只靠 `adb` 列出设备；没有 Phone Harness 时 `ready` 为 false。无设备返回 `disconnected`，多设备未指定 `device_id` 返回 `ambiguous_device`。

### 3. 运行一个只读任务

先发现已登记任务，再调用：

```sh
"$REPO/scripts/journey-pilot" task-list
```

```sh
printf '%s\n' '{"task":"xhs-read-post","device_id":"<status 中的设备>","title":"<精确标题>","author":"<精确作者>"}' \
  | "$REPO/scripts/journey-pilot" task --request-stdin
```

`xhs-read-post` 只读取本人精确标题/作者的笔记正文和当前可见评论；`coverage=visible_only`，不是完整评论或完整正文验收。请求走 stdin，避免把标题等文本放进进程参数。

任务正文、标题、评论等运行内容只在本次调用的返回包中 live 返回。落盘的 `task-result.json` 和 `request_once` 重试缓存只保留白名单元数据，并带 `content_retained=false`；不要从缓存、历史摘要或 `inspect` 恢复正文。任务级调用每次都是新的只读巡查；动作层用同一 `request_id` 重试时不会重新点击，但缓存结果同样不含正文。

## 任务前查经验，任务后做复盘

开始登记任务前，按精确 `task_key` 检索已有经验。结果是参考材料，`executable` / `runnable` 始终为 false，不授予 Journey `verified`，也不能替代当前现场核验：

```sh
printf '%s\n' '{"task_key":"xhs-read-post","limit":20}' \
  | "$REPO/scripts/journey-pilot" lessons --request-stdin
```

无匹配时返回空列表；不要为填满模板编造经验。

任务结束后先看 trace 摘要，再用 `skills/execution-review/SKILL.md` 的六问写复盘。`trace` 只返回事件计数、计时、缺失指标和可验证引用，不复制 UI 正文：

```sh
printf '%s\n' '{"run_id":"<run_id>"}' \
  | "$REPO/scripts/journey-pilot" trace --request-stdin
```

需要引用某段事件时，在请求里加 `seq_start` / `seq_end`，返回带 digest 的引用。保存复盘时，`fact` 必须带能通过校验的引用；`hypothesis` 可以没有引用。`lesson` 可选，且只接受 `rule` / `applies_when` / `invalid_when`：

```sh
printf '%s\n' '{"run_id":"<run_id>","task_key":"xhs-read-post","questions":{"completion":"...","cost":"...","repetition":"...","cause":"...","scope":"...","next_experiment":"..."},"claims":[{"kind":"fact","text":"...","references":[{"run_id":"<run_id>","seq_start":1,"seq_end":3,"digest":"<trace 返回的 digest>"}]}]}' \
  | "$REPO/scripts/journey-pilot" review --request-stdin
```

复盘成功状态是 `recorded`，只表示已保存；不表示任务完成，也不改变 Journey 资格。旧复盘不会被覆盖，再次保存生成新版本。`review` 和 `lessons` 都是显式调用；项目 skill 不会自动触发复盘，也不安装 hook。

`reconcile` 只处理无未闭合动作的闲置占用；存在未知动作效果时保持占用并返回 `outcome_unknown`，不会重放动作或清除未知记录。

## 源码启动器与已安装包的区别

| | `scripts/journey-pilot`（源码） | 已安装包 |
|---|---|---|
| 是否需要安装 | 不需要；只要求本机 `python3`（>= 3.11） | 需要专用 venv 中 `pip install -e <repo>` |
| 运行哪份代码 | 当前 checkout 的 `src/` | 安装时指向的包 |
| 调用方式 | 任意 cwd 下用绝对路径调用 | PATH 上的 `journey-pilot` 或 venv 内绝对路径 |
| 共享数据 | 同一 `JOURNEY_PILOT_HOME` / 默认 Store | 相同 |

启动器解析自身真实路径（包括符号链接），把 `<repo>/src` 前置到 `PYTHONPATH`，再 `exec python3 -m journey_pilot.cli`；不安装依赖、不写系统 Python。

本机已做一次离线可编辑安装检查：用 `/opt/homebrew/bin/python3`（3.14.7，已有 setuptools 80.10.2）创建临时 venv，执行 `pip install --no-index --no-build-isolation --no-deps -e <candidate>` 成功，未下载依赖；安装后的 `journey-pilot capabilities` 和 `task-list` 从 `/tmp` cwd 返回 `ok`。默认 `python3`（3.12.12）没有 setuptools，不能用 `--no-build-isolation` 直接构建；源码启动器不需要构建后端，不受影响。不要安装进系统 Python。两种方式不要混用而不核对版本；先比较 `capabilities` 的 `data.program_version` 和 `data.store.root`。

隔离测试 Store 可以用临时目录：

```sh
tmp=$(mktemp -d)
JOURNEY_PILOT_HOME="$tmp" "$REPO/scripts/journey-pilot" capabilities
```

## Agent 入口

把启动器的绝对路径和项目 skill [skills/journey-pilot/SKILL.md](../skills/journey-pilot/SKILL.md) 交给接手 Agent；项目内 Claude Code 还可通过 `.claude/skills/` 的相对链接发现它。Agent 第一步运行 `capabilities`，以它的 `commands`、`limitations` 和 `command_requests` 为准；不要从旧文档推断未实现能力。完整开发接手协议见内部 checkout 的 START-HERE.md；该文件不进入导出候选。

当前支持边界：

- 只读：`status`、`task-list`、`read-post` / `task`、`discover`、`inspect`。
- 探索：`session start`、`observe`、`act`、`session finish`、`session handoff`。
- Journey：`propose`、`validate`、`run`、`resume`、`cancel`；`run` 只执行当前环境中已 verified 的版本。
- 经验闭环：`trace`、`review`、`lessons`、`reconcile`；复盘和经验是参考材料，不是可执行能力，也不授予 `verified`。
- 外部写：默认暂停。用户明确批准单次发送后，可在标题/作者现场可见时用 `session authorize` 准备一次性 grant，再用同一 run/device、同一文案和同一 action/locator 执行；不建设通用授权平台、批量发送或自动重试引擎。
- 非 ASCII 输入：默认 adb 方法在触碰设备前拒绝；先把完全相同文案放入手机剪贴板，再用 `input_text` 的 `method=paste`，粘贴后字段文本必须完全一致。可用 `scripts/clipboard-page.py` 在局域网提供一次性 COPY 页（文案只存内存），手机侧仍用 Journey 的 launch/observe/act 打开并点击 COPY，不用裸 adb。已失败的默认 adb 中文输入只有在原始 payload 摘要、终态请求、脚本完成指标和失败后持久树观察都吻合时才可 `session resolve-input`；该命令不读设备，同一原 run 的原 lease token 即使过期也接受，但不新建/刷新占用、遇其他 run 占用即拒绝。普通 nonsecure 锁屏由 `session handoff` 先记录一次 ordinary wake 再读树续租；secure 锁屏要求人工解锁。
- 截图：`observe source=screenshot` 会剥离 macOS 多显示器警告前缀，从 PNG 签名开始返回 base64。
- 照片、账号、登录、回复、发布、交易均无当前动作入口。

`recorded` 只表示动作和证据已保存，不是业务成功；`completed` 需要冻结后置条件通过；薄任务完成不等于 Journey `verified`。不要把合成示例、源码检查或单次只读结果说成实机验收。

完整契约见 [agent-interface](agent-interface.md)，薄任务编写见 [task-scripts](task-scripts.md)。
