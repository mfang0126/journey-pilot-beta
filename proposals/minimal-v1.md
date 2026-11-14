# 最小 V1：跨 Agent 的 Android 共享经验

状态：计划规格；本轮只读快路线授权见 [0003](../docs/decisions/0003-read-only-fast-route.md)。详细规则以 docs/experience-rules.md 为准。

## 首版交付

本轮先交付 `read-post` 只读快捷入口，复用现有 JP 执行与留痕。它是手工固化的语义路线，不等于自动提炼或 verified Journey；不改变以下通用闭环的验收要求。

后续专用流程采用 [0004：通用底座 + 薄任务脚本](../docs/decisions/0004-thin-task-scripts.md)：允许经审查的少量业务 Python，公共执行机制不随业务重写；不扩展为从不可信配方加载任意代码。

当前切片范围为 XHS 精确标题/作者读取与抖音当前评论面板的 AI 总结读取；统一登记入口 task/task-list，保留 read-post 兼容入口。抖音任意播放页导航、主动发问与评论分析证明不在本切片承诺内。接口见 [task-scripts](../docs/task-scripts.md)，实施状态仅在 .planning。

本地 journey-pilot CLI、配套 skill、Phone Harness 适配器、文件式共享 Journey 和运行日志、可选 Jev 检查接口。首版不引入 app-automating 或 SQLite。

## 工作流程

指定视频链接 → AI 总结保留为验证目标（授权见 0004），不保证它是当前下一步。按 [0005](../docs/decisions/0005-flexible-learning-loop.md)，先文档反思与 review、对照现状，再选择实现切片；状态只在 .planning，实机证据只在 experiments。日常工作循环的唯一规范见 [experience-rules §0](../docs/experience-rules.md#0-日常工作循环)。

1. Agent 读能力与现场，查询已有经验。
2. 适用 verified Journey：绑定参数，一次调用执行；否则根据候选/失败经验探索。
3. 所有经过入口的动作由工具自动记录，不依赖 Agent 写日志。
4. 程序进行条件等待和关键断言；必要时 Jev；未知时暂停交还当前 Agent。
5. 探索结束自动生成摘要；Agent 提炼候选，工具校验并按重复验证证据晋级。
6. 后来的 Agent 查询相同目录，换参数复用。失败按环境/逻辑/未知结果分类，修复新建版本。

## 初始默认

候选需满足重复验证和参数覆盖才晋级；每次 validate 只执行一次。资格按版本与环境分别维护；定位要求唯一；恢复前重新观察；未知效果动作不重复。外部写操作在首版重放中暂停交还 Agent，不建设通用权限执行平台。具体数值与例外只以 experience-rules.md 为准。

Jev 初版只作中间确认，最终成功由确定性后置条件验证；规则可完成的任务不依赖它。日志/配方分开，长期保留脱敏运行摘要；运行数据不入 Git。执行器须返回读取结果，不仅留下操作日志。

## 实施与验收入口

- [经验规则](../docs/experience-rules.md)
- [接口](../docs/agent-interface.md)
- 实施计划
- [验收](../docs/requirements.md)

核心验收：A 学会流程，B 无原聊天上下文，发现并换参数一调用执行；遇到变化可交回现场、核验后续行，修复能复用。实际通过情况看活动计划和实验证据，不由本规格推断已经通过或一项都没做。
