# 快速制作薄任务脚本

用途：给下一个 Agent 新增已授权的手机只读任务。当前实现是经审查的 Python 模块，不是任意代码加载器，也不等同于 verified Journey。

## 先发现，再探索

在项目根目录运行 `PYTHONPATH=src python3 -m journey_pilot.cli task-list`。先核对已有任务、参数和入口条件；入口不满足时先用现有 session/observe/act 探索，不猜页面、不盲点坐标。进入后台再启动 App 可能丢失面板，必须重新核对。

## 新任务只写什么

参考 `src/journey_pilot/douyin_ai.py` 和 `src/journey_pilot/read_post.py`：

1. 定义 ROUTE：id、App、步间预算、观察新鲜度。
2. 定义业务函数 `_read(ctx)`：页面识别、语义定位、流程分支、成功条件、结果提取。
3. 定义 `execute(store, request)`：校验业务参数，调用 `task_runtime.execute`。
4. 在 `task_registry.py` 显式登记模块、必需参数和入口条件。不要动态导入用户提供的文件路径。
5. 正常任务只调用 `ctx.observe`、`ctx.tap`、`ctx.act`、`ctx.wait_for`、`ctx.require`；不要复制 session、设备锁、计时和落盘代码。

## 共用接口

- `ctx.observe()`：新鲜无障碍树；`ctx.observe('pixels')`：本机 Vision OCR，保存归一化截图和识别结果。OCR 文字不是确定事实。
- `ctx.tap(predicate, name, source=...)`：唯一定位，检查当前观察新鲜度、正坐标及像素置信度；动作后失效缓存。禁止直接用旧 x/y。
- `ctx.act`：仅 launch_app、scroll、back 导航；不开放输入、发送、点赞、发布或任意坐标入口。
- `ctx.wait_for(predicate, timeout=...)`：有界等待结果，不把加载骨架当正文；预算是调用间检查，不承诺中断正在执行的底层调用。
- `ctx.require(condition, message)`：条件失败即交还 Agent；返回业务字典之前必须验证身份与结果。
- `ctx.request` 提供参数；参数必须真正用于定位或核验。只记参数摘要不算绑定。

任务 Python 属于受信任项目代码；上述封装不是沙箱，不能防御恶意模块绕过底层接口。公共 API 的动作白名单也不自动证明某次点击无业务副作用，仍需审核具体目标。

## 两个现有样本

- `xhs-read-post`：输入 device_id/title/author；从个人入口或目标正文开始。卡片中心被底部导航遮住时先滚动，不拿“树里存在”当作可点击。
- `douyin-ai`：输入 device_id；可选 expected_summary、wait_seconds（大于 0 且不超过 30）。从当前视频的评论面板或 AI 面板开始，不负责任意视频页开评论。优先识别 AI 字样；OCR 漏读时，仅允许使用经截图确认的当前布局结构锚点：评论标题旁的空白 TextView。缺失或重复就停止，App 布局变化须重新确认。

抖音结果是平台生成内容，可能有事实错误；`coverage=accessibility_exposed` 表示包含树暴露的屏外文本，不是完整视频/完整评论验收。`comment_analysis_verified=false`、`chat_message_sent=false` 不应改成成功宣传。

## 最小验收与收尾

- 只补关键回归：正常返回、错误身份/歧义拒绝、具体已发现的误点或权限风险。
- 真机跑过，并从同一已知入口复跑；保留失败样本，不用新结果覆盖失败记录。
- 核对第二任务新增期间公共执行层是否被修改；若确实缺能力，明确补公共能力，不暗中复制一套。
- 正文仅当次回传，不默认写 `evidence/task-result.json`；`task-result.json` 仅保留状态/计时等元数据。`task_finished` 记录终态，`trace` 可生成强引用，按 execution-review 六问保存经验。会话 recorded、任务 completed、Journey verified 分开解释。
- 用 task-list 验证可发现性；用真实参数变体验证目标确实变化。更新当前计划与证据，不自动 commit/push。

调用：`PYTHONPATH=src python3 -m journey_pilot.cli task --request-stdin`，stdin 为 JSON，如 `{"task":"douyin-ai","device_id":"<已核对的设备>"}`。兼容旧 `read-post` 命令。
