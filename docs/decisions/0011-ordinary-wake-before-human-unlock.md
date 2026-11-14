# 0011 普通锁屏先唤醒＋上滑，凭据界面才交人工

- 日期：2026-10-23
- 状态：accepted（用户当轮明确授权）
- 取代：无

## 背景

2026-10-23 18:09 的 `status` 实测：`wakefulness=Dozing`、`keyguard.showing=true`、
`secure=false`、`input_restricted=false`、`foreground=NotificationShade`。
随后一次 `wake` 动作返回 `recorded`，但 `observe` 仍报 locked。旧 wake 脚本只唤醒一次、
上滑一次、等待固定 1.5 秒，没有验证；历史实测也出现过第一次上滑后 keyguard 仍显示、
第二次才清除的过渡态。`status` 只读探测因此被误当成“必须人工解锁”，过早交还用户。

## 决策

1. `status` 仍是只读命令，新增 `lock` 评估：`kind`、`requires_human`、
   `ordinary_wake_allowed`。`secure=false` 只表示可尝试普通唤醒，不表示已解锁。
2. `session start` 在 `ordinary_wake_allowed` 时先记录并执行一次 `wake`：
   唤醒键＋上滑，最多三次；每次重读真实 keyguard/bouncer 状态；最后必须通过一次
   无障碍树读取才返回 ready。窗口尺寸取 `wm size` 的最后一个匹配值。
3. 只有实际出现 bouncer/PIN/密码/验证界面（`requires_human=true`）才交人工。
   普通 keyguard 上滑仍未清除时返回实际 locked，不伪造成功，也不要求输入 PIN。
4. wake 的尝试次数、keyguard 标志、`requires_human`、`verified_by` 作为
   `action_started`/`action_finished` 的 `wake` 诊断持久化；不绕过 Journey 事件日志。
5. 阻止启动的会话在返回 locked 前关闭租约与设备占用，避免下一次重试被 device_busy
   挡住。

## 非目标

- 不输入 PIN/密码/验证码，不修改系统锁屏设置。
- 不修改外部 Phone Harness；只使用其 `shell()` 与 `ui()` 接口。
- `run`/`validate` 的预检仍要求实际就绪；需要普通解锁时先走 `session start` 或任务入口。
