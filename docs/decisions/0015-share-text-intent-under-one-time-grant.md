# 0015 share_text：SEND intent 走 0012 一次性授权

- 日期：2026-11-05
- 状态：accepted（用户当轮要求实现 Mika 借鉴项“用 intent 代替界面操作”，并要求过 0012 授权门）
- 关联：0012、0013

## 背景

中文输入目前靠 `input_text method=paste`，要先占用设备剪贴板（0013）。Mika 的
`phone_share_text` 用系统分享 intent 直接把文字交给目标 App。intent 把文字送出设备内
当前 App 的边界，按外部写处理。

## 决策

1. 新增白名单动作 `share_text(package, text)`，执行
   `am start -a android.intent.action.SEND -t text/plain --es android.intent.extra.TEXT <text> -p <package>`；
   参数用 POSIX 单引号转义交给设备端 sh，不读写剪贴板。
2. 必须显式声明 `effect_class=external_mutation`，否则拒绝；不默认补上，避免任何按声明拦截的路径漏判。
3. 复用 0012 的 grant：`session authorize` 带完整 share_text action 与相同 text，不带 post/opener。
   没有屏上目标可核对，因此不读设备；grant 绑定 run、device、action 摘要（含 package 与 text）、
   文案摘要、600 秒有效、派发前原子消费。share grant 与 tap grant 互不通用。
4. Journey 回放遇到 share_text 照常暂停交还（0012 不变）。
5. 派发前在脚本内做实时锁屏门禁，只在明确解锁证据下派发：keyguard 明确未显示、display 0 焦点已知且非凭据界面、树可读非空且无锁屏/凭据控件。display 0 凭据信号优先于全局 showing=false；keyguard 标志严格三态解析，只有字面 false 算未锁；未知（含无效值）一律拦截，返回 locked，不输入 PIN。（PR #14 Codex 两轮 P1）

## 非目标

- 不证明目标 App 收到或发出内容；目标 App 内的选人、发送是另外的 external_mutation。
- 不做批量分享、自动重试、通用授权平台；不改变 input_text/paste 路径。

## 验证

离线单测（mock adb）：引号与中文按 POSIX 拆分后原样还原、不触碰剪贴板、无 grant 不派发、锁屏/未知/凭据界面下 SEND 次数为 0、
换包名/换文案/重复使用拒绝、share 与 tap grant 不互通。未实机运行，未发送任何内容。
