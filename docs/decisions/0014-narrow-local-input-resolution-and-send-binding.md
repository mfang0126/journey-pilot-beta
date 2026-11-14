# 0014 窄口径本地输入解除与发送时现场绑定

- 日期：2026-10-23
- 状态：accepted（用户要求修复完整中文输入与既有失败恢复）
- 关联：0012、0013

## 背景

`r_40953600` 的 `send-search-text` 在旧版默认 adb 方法下把中文交给
`adb shell input text`，触发 `InputShellCommand` NPE。请求记录已是终态
（`state=done`，`outcome_unknown`），但 `open_actions` 按策略保留
`action_finished(outcome=action_failed)`，因此同一 run 的后续动作全部被阻断。
0013 曾禁用 `session resolve-input`，因为当时的实现没有绑定原始 payload 摘要，
也没有排除底层调用的异常分支。

## 决策

1. 重新开放 `session resolve-input`，但只接受一个已知失败的本地输入，且必须同时满足：
   - 调用者提交原始 `request_id` 与完整 `action`；用与 `session act` 相同的
     payload 重算密钥摘要，必须与 `request_once` 持久记录的 `digest` 完全一致；
   - 请求记录为 `state=done`（不是 `intent`/`retry`），结果为
     `outcome_unknown` 且 `step_id` 对应当前唯一未闭合动作；
   - 该步骤恰好一次 `action_started` 与一次失败 `action_finished`，动作为
     `input_text`、`effect_class=local_input`、默认 `method=adb`、文本含非 ASCII；
   - 终态事件带正常脚本完成指标（正数 `script_ms`、至少一次 `tree_reads`、
     `locator_index` 非 null）。适配器在启动失败、超时/被杀、无 JSON 分支
     不返回这些指标，因此这些分支被排除。只读核对 Phone Harness：Android
     `_input_text` 经同步 `subprocess.run` 调 adb，脚本在最后一次 adb 返回后才打印
     metrics；因此指标也证明本地 adb 客户端调用已返回。这只证明同步包装调用返回
     且脚本走到最终打印，不证明远端从未发生瞬时副作用；NPE 原文未持久化，
     不作为可复核证据；
   - 失败动作结束后、任何后续动作开始前，必须存在一次持久树观察；其密钥摘要、
     节点数与几何必须与失败动作前最后一次树观察完全相同。命令不读取设备，因此
     不依赖当前屏幕。这只证明动作刚结束时的可见状态未变，不宣称不可见的瞬时
     副作用绝对未发生，也不描述现在屏幕是什么。
2. 成功时追加 `action_resolved` 事件，`open_actions` 据此关闭该步骤；历史事件、
   请求记录与未知结论不改写。重复调用同一原始请求返回 `already_resolved`。
3. 死锁修复：`resolve-input` 本身不读设备，因此允许对同一原 run 使用**仍记录在
   租约文件中的原 lease token，即使已过期**。它仍在设备锁内执行，
   要求 reservation 仍属于该 run，且没有其他 run 的活跃 lease；不新建 lease、
   token 或 reservation，不刷新过期时间，不抢占其他占用。token 不符或占用属于
   其他 run 时返回 `device_busy`。原摘要、脚本指标和前后树观察证据一项不减弱。
4. `session handoff` 在状态为 `nonsecure`、`ordinary_wake_allowed=true` 时，先记录
   并执行一次普通 wake（复用现有 wake 模板，不输凭据），再读树和续租；secure/
   凭据界面仍要求人工解锁，不尝试 wake 或输入凭据。
5. 发送时现场绑定：`external_mutation` 除 grant 与动作摘要外，还必须在
   `authorization.text` 提交同一文案。消费 grant 前做一次新鲜树读取，要求该文案
   仍在树中、发送 locator 唯一命中，否则返回 `precondition_mismatch` 且不消费 grant。
6. grant 只覆盖“准备 → 绑定的打开编辑框 tap → 输入同一文案 → 发送”这一段。
   `session authorize` 可带 `opener` tap 并绑定其摘要；准备后只允许摘要完全一致的
   第一个 opener tap，其他任何动作在派发前使该 run 的 prepared grant 失效；重新准备
   会使旧 prepared grant 失效。未提供 opener 时不允许准备后导航。离开帖子/编辑
   overlay 后不能再发送。
7. 非 ASCII 输入端到端仍走 `method=paste`。本机 `scripts/clipboard-page.py`
   在局域网地址上提供单次随机路径的 COPY 页，文案只存在进程内存，退出即清除；
   手机侧打开页面、点击 COPY、返回并粘贴都必须经 Journey Pilot 记录，不使用裸 adb，
   不使用未记录的 `adb reverse`。该页面依赖手机可访问本机局域网地址。

## 非目标

- 不证明发送按钮真实语义、平台受理、评论未折叠或未被删除。
- 不在编辑 overlay 中重新证明帖子标题/作者；overlay 不暴露身份，发送时只做
  文案与目标 locator 的现场核对。
- 不解除任意未知动作；非本窄口径的未知仍须现场核查。
- 不保证所有非 ASCII 输入场景；只覆盖已证明的 adb 方法与剪贴板路线。

## 验证

新增离线测试：原 payload 摘要不符拒绝、非终态拒绝、缺少正常脚本指标拒绝、
可见树变化拒绝、匹配时关闭且保留历史；发送缺少现场文案拒绝且不消费 grant、
目标歧义拒绝、准备后其他动作使 grant 失效。全量 243 项通过；未操作手机、
未发送、未提交。
