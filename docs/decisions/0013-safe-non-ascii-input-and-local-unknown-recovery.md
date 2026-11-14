# 0013 非 ASCII 输入改剪贴板粘贴；未知本地输入不提供自动解除

- 日期：2026-10-23
- 状态：accepted（用户要求继续修复发送流程的根因；收尾复核后禁用 resolve-input）
- 取代：无
- 更正：曾实现的 `session resolve-input` 未满足摘要绑定与进程结束证明，已禁用为
  `capability_gap`，不修改任何未知记录。
- 后续：0014 以原 payload 摘要、终态请求、正常脚本指标和相同的动作前/后可见树
  重新开放窄口径解除；不是本轮禁用理由的推翻。

## 背景

实机 run `r_40953600`（lease `l_f70aab3669079f60`，device `<device-serial>`）在
`input_text` 输入中文搜索词时触发
`java.lang.NullPointerException ... InputShellCommand.sendText`，动作已记录
`action_started`，结果被保守归为 `outcome_unknown`，后续动作被未闭合动作阻断。
现场 `observe` 显示搜索框仍是原搜索词，没有评论输入或发送。

既有实测（`experiments/baseline/2026-10-20-capability-matrix.md`）已证明：
Android 16 的 `adb shell input text` 对非 ASCII 抛 NPE；本机 HTTP 页 +
`adb reverse` + Chrome COPY + `input keyevent 279` 的剪贴板路线可粘贴中文（复制
2.48s、粘贴 0.062s），但当时只在设置搜索框验证，未在目标 App 输入框复测。当前
Phone Harness Android `type_text` 仍是 `adb shell input text` 的实现，不能直接改外部仓库。

## 决策

1. `input_text` 默认 `method=adb` 在构建动作脚本时检查文本；含非 ASCII 直接
   `unsupported_input`，在获取设备锁/触碰手机前返回。这样 Unicode NPE 不再产生新的
   未知动作。
2. 新增 `method=paste`：要求调用者先把完全相同文案放入手机剪贴板；动作点击目标、
   发送 `KEYCODE_PASTE(279)`，再读一次树并返回字段文本。会话层要求字段文本与
   `text` 完全一致，否则 `assertion_failed`，不返回 recorded。该动作不负责设置剪贴板，
   也不依赖未验证的 Chrome 自动化。
3. `session resolve-input` 已禁用为 `capability_gap`，不读取设备、不追加或修改任何
   事件。收尾复核发现：调用者提供的 `text`、`field_locator`、`expected_text` 只与新鲜
   树比对，没有与原始 action 的 durable payload 摘要绑定；事件也不保存原始输入正文。
   同时没有 PID/进程或底层 adb 子进程结束证明。因此不能据此安全关闭未知动作，未知
   记录继续保留。实现与测试已移除，仅保留明确拒绝。
4. 授权发送中输入失败不再登记 grant 文案：文案检查改为输入前只读校验，成功后才
   盖上 `text_entered_at`。失败输入不会让旧 grant 进入发送；由于没有安全解除接口，
   未闭合动作仍会阻断后续发送。
5. `observe source=screenshot` 在 base64 前定位 PNG 签名并剥离 macOS 多显示器警告
   前缀；找不到签名则失败，不返回伪 PNG。

## 非目标

- 不修改外部 Phone Harness；不安装 ADBKeyboard、不切换系统输入法、不写系统设置。
- 不把本地 HTTP/Chrome 剪贴板准备流程并入 Journey Pilot；本轮只要求剪贴板内容已由
  用户或外部步骤准备好。
- 不新增通用 `clear unknown`、不重放未知动作、不更换 run 绕过。
- 不在目标 App 输入框宣称剪贴板路线已实机验证；该验证仍需用户现场完成。

## 验证

离线测试：非 ASCII 在设备前拒绝、paste 使用 279 且校验字段、paste 不一致返回
assertion_failed、失败输入不登记 grant，以及 `session resolve-input` 返回
`capability_gap`、不调用设备且事件文件字节不变。相关测试通过；本轮未操作手机、
未发送、未提交。
