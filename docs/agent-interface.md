# 面向任意 Agent 的接口

本页说明当前 CLI；未来契约明确标为目标。已实现精确 `{"param":"name"}` 参数绑定、入口检查与 fail-closed 环境守卫、discover 运行摘要参考、受可信检查点约束的 resume，以及绑定单次动作的 `session authorize` 外部写授权。仍未实现任意步骤回滚、通用外部写授权平台和平台负载保证。read-post/task 是登记薄任务，资格独立于 verified Journey。行为规则见 [经验规则](experience-rules.md)，实施顺序见 工作包。

## 调用约定

`journey-pilot <command> [subcommand]` 默认输出 JSON；`--json` 可显式声明，`--help` 是人类可读帮助。请求通过 `--request-stdin` 读取一个 JSON 对象。命令放在 argv，request 对象放在 stdin；不能把 capabilities 的整个示例包装对象作为命令请求。

未安装时使用仓库内 `scripts/journey-pilot`，任意目录可调用其绝对路径，无需手设 PYTHONPATH；已有模块入口仍可用。已安装时使用 PATH 或绝对 launcher。共享目录默认 `~/.local/share/journey-pilot`，可由 `JOURNEY_PILOT_HOME` 覆盖，不随 cwd 改变。

```sh
PYTHONPATH=src python3 -m journey_pilot.cli capabilities
printf '%s\n' '{"task_key":"read-title","intent":"Read visible title","steps":[{"step_id":"title","action":"read_field","locator":{"text":"Title"}}],"postconditions":{"text_equals":"Title"}}' | PYTHONPATH=src python3 -m journey_pilot.cli propose --request-stdin
```

这两个示例不操作设备；propose 保存 candidate，不能证明页面上真的存在 Title。返回 ID 后可用 `inspect --request-stdin` 和 `{"journey_id":"返回的ID"}` 查看。

capabilities 返回 `store.root` 与其子目录、`store_env_var`、程序/适配器/schema 版本、`commands`、`actions`、`checks`、`step_checks`、`observation_sources`、`command_requests`、`defaults`、`pacing_note`、`param_types`、`field_types`、`limitations`、`errors`。`request_schema` 是公共字段说明，不是完整的每命令 JSON Schema 校验器；`command_requests` 列出最小输入。

## 命令与必要字段

| 命令 | 必要输入和当前行为 | 成功状态 |
|---|---|---|
| capabilities | 无；不操作设备 | ok |
| status | 可选 device_id；设备列表、连接/锁屏、前台 App、就绪探测；`lock` 区分 none/nonsecure/secure/unknown 并给出 `requires_human`（仅已实际看到凭据界面）与 `ordinary_wake_allowed`；不触碰设备 | ok |
| discover | 可选 task_key/query/environment_key；返回 Journey 版本，并在有 task_key/query 时附不可执行运行摘要参考（最多 20 条，另给 runs_total） | ok |
| session start | intent；可选 device_id/task_key/frozen_postconditions；后者只存密钥摘要、不执行；若 status 显示普通锁屏（`ordinary_wake_allowed`），先记录并执行一次 wake＋上滑，再实测就绪；凭据界面直接返回 locked；返回 run_id、lease_token、ready、lock、wake | ok |
| session authorize | run_id、lease_token、post.title、post.author、action、text；可选 opener；在标题和作者当前可见时做一次新鲜树读取并签发一次性授权；action 为 share_text 时不带 post/opener、不读设备，text 必须等于 action.text；返回 grant_id、摘要与过期时间 | recorded |
| session resolve-input | run_id、lease_token、原始 request_id、完整原始 action；在摘要、终态请求、脚本完成指标和动作后持久树观察都满足时追加 `action_resolved`；不读取设备；否则拒绝 | recorded |
| observe | run_id、lease_token；source 默认 tree，可用 tree/screenshot/pixels；返回 observation_id。screenshot 会在 base64 前剥离 macOS 多显示器警告前缀，从 PNG 签名开始返回；screenshot 可加 `exclude_status_bar:true`，按 `dumpsys window` 实测的状态栏高度裁掉顶部（量不到则失败、不返回图；仅 8 位 RGB/RGBA 非交错 PNG），只去掉状态栏，不保证去掉下拉通知或悬浮通知。source=tree 另返回 `tree_quality`（good/partial/unusable，阈值为 `adapter.TREE_MIN_USEFUL`/`TREE_BANDS`/`TREE_MIN_BANDS` 常量）：good 不做 OCR；partial（有文字或可点击的节点少于 5 个，或屏幕 4 条横带中覆盖不足 3 条）再跑一次 pixels OCR；unusable（树读取失败或为空）必须 OCR。OCR 补出且树里没有的行放在 `ocr_nodes`，每条带 `source:"ocr"`，坐标与 pixels 相同；`nodes` 仍只含树节点。partial 时 OCR 失败仍返回树并附 `ocr_error`；unusable 时 OCR 成功则 `nodes:[]`、附 `tree_error`，且这次观察记为 `source:"pixels"`（不是树证据，resolve-input 不会拿它解除 unknown），OCR 也失败则保持原 `locked` 失败（data 带 `tree_quality`）。树观察的 `digest` 只算树节点，`observed_at` 取树读完的时刻，OCR 耗时不刷新坐标年龄（tap_xy 的 8 秒新鲜度按树算）。平台拦截信号同时检查 `ocr_nodes`。非 good 的树每次多一次 harness 调用 | recorded |
| act | run_id、lease_token、request_id、action.type 及动作参数；external_mutation 另需 `authorization.grant_id`，input_text 可带同一 grant_id 登记已输入文案 | recorded |
| session finish | run_id、lease_token；关闭租约并保存摘要，返回 frozen_postconditions_digest，不返回/执行原始条件 | recorded |
| session handoff | run_id；活跃租约另需当前 lease_token；普通锁屏（nonsecure 且 ordinary_wake_allowed）先记录一次普通 wake，再刷新现场并转移探索租约；secure/凭据界面仍要求人工解锁 | recorded |
| inspect | run_id 或 journey_id；Journey 可指定 version；run 返回摘要与最近 200 条事件，无分页 | ok |
| propose | task_key、intent、非空 steps；source_run_ids 可选；修复另含 journey_id/base_version | ok，candidate |
| validate | journey_id、request_id；可选 version/device_id/parameters；候选只执行一次 | completed 或 recorded |
| run | journey_id、request_id；可选 version/device_id/parameters；要求该环境中 verified | completed 或 recorded |
| resume | run_id、request_id；从 paused/cancelled/blocked 运行的可信检查点续跑一次，配方与参数必须一致 | completed 或 recorded；否则 needs_replan |
| cancel | run_id；只写停止请求，返回 cancel_requested；不表示底层已停止 | ok |
| read-post | device_id、title、author；限定只读路线，详见下文 | completed |
| task-list | 无；登记任务与入口条件 | ok |
| task | task、device_id 及任务字段 | completed |
| trace | run_id；可选 seq_start、seq_end 生成区间 SHA256 引用，否则汇总事件与成本 | ok |
| review | run_id、task_key、questions 六问、claims；fact 必须引用已存在事件；可选 lesson | recorded |
| lessons | task_key；可选 limit（默认20，上限50）；只返回引用仍有效的非可执行经验 | ok |
| reconcile | run_id；可选 release=true、活跃租约的 lease_token；未知动作不解除 | recorded 或 ok |

所有命令还可能返回结构化失败。status 无设备返回 disconnected（blocked，退出码 4）；多设备未指定返回 ambiguous_device（blocked，退出码 2）。session/runner 单设备时可省略 device_id；薄任务要求显式指定。session start/finish 除普通唤醒/上滑外不隐式点击、归位或清空 App；普通唤醒按动作事件记录，凭据不代输。

## 动作、检查与默认值

- 动作：launch_app、launch_activity、tap、tap_xy、input_text、key、scroll、wait、read_field、wake、share_text。read_state/find/screenshot/assert 不是动作；读取树或截图使用 observe。`launch_activity` 可只给 `component`，或给单个 ASCII `http(s)://` 的 `data`（此时用 VIEW intent），两者都给时用 `am start -n … -d …`；不接受其他 scheme 或非 ASCII URL。`am start` 不经 Phone Harness 锁屏门，所以派发前走与 share_text 相同的门禁（见下条），有无 expect 都一样：不满足时返回 `locked`、`dispatched=false`、不发 `am start`；唯一差别是读树报 `could not get idle state`（从不 idle 的屏幕）且 display 0 焦点是不含 keyguard 的 `package/activity` 窗口时放行；`NotificationShade`、`StatusBar` 这类窗口仍返回 `locked`。门禁通过后、派发前的读树（含效果采样）明确报手机已锁时同样返回 `locked`、`dispatched=false`；其他采样失败只让该侧 effect 未知，不拦截。派发后才撞锁仍是 `outcome_unknown`。带 `data` 时可设 `reuse_tab:true`：加 Chrome extra `com.android.browser.application_id=journey-pilot`，首次仍新开一个标签页，之后同一应用 id 的启动导航到那个标签页而不再新开（仅 Chrome 的行为，实机见 experiments/2026-10-30-chrome-reuse-tab.md；旧标签页和其他方式开的标签页不受影响）。
- `share_text`：`package`（Android 包名）＋ `text`（非空，≤4000 字符，不含 NUL），必须显式声明 `effect_class=external_mutation`，否则 invalid_request（不默认补上，因为 act 与 runner 都按声明的分类拦截）。执行 `am start -a android.intent.action.SEND -t text/plain --es android.intent.extra.TEXT <text> -p <package>`，text 与 package 用 POSIX 单引号转义后交给设备端 sh，中文、引号、`$`、反引号、换行都按原文成为一个 extra；不读写剪贴板，不走 `input text`。`am` 输出含 `Error`（如包内无 SEND text/plain 入口）视为失败。派发前在同一脚本内做实时锁屏门禁（Phone Harness 的 `shell` 不经锁屏检查）：必须读到 keyguard 明确未显示（`isKeyguardShowing=false`，或 `dumpsys window policy` 的 KeyguardShowing/showing=false）、display 0 有已知非 null 焦点窗口、且无障碍树可读非空；display 0 焦点是凭据界面（KeyguardBouncer、ConfirmDeviceCredential、SIM PIN/PUK）时即使全局 showing=false 也拦截，树里出现 `_CREDENTIAL_TREE` 控件（id/pinEntry、id/passwordEntry、id/lockPatternView，与 wake 共用）同样拦截；标志缺失、字段存在但值不是完整的 `true`/`false`（空值、截断、`1`、`unknown` 等；与 wake 共用 `_flag` 解析）、焦点未知、树读取失败均按未知拦截。拦截返回 `locked`（凭据类带 `requires_human:true`），不唤醒、不上滑、不输入 PIN；此时 SEND 未派发，动作闭合，但 grant 已在派发前消费，重试需重新授权。它只把文字交给目标 App 的分享入口；目标 App 里选联系人、点发送是另外的动作，仍按 external_mutation 各自授权。`execution` 事件记录 package，不记录 text。Journey 回放遇到它照常暂停交还。未实机验证。
- tap/input_text/read_field 必须有 locator。键为 resource_id/text/text_contains/description/class，值为非空字符串。tap/read_field 可设 `fallback.alternatives`，1..8 个备用 locator；同一树依次尝试，歧义即停。
- 网页 locator（Chrome 页面内容不在无障碍树里）：键为 `css`、`web_text`（可见文字全等）、`web_text_contains`，不能与原生键混用，只用于 `tap`，不支持 fallback。经 `adb forward … localabstract:chrome_devtools_remote` 连前台可见标签页（每次调用临时建立并移除该端口），目标必须唯一命中，否则 `locator_missing` / `ambiguous_target` 且不点击；点击是真实触摸事件（`Input.dispatchTouchEvent`）。步骤 `expect`/postconditions 中 `element_exists`/`element_count` 可用网页 locator；`text_equals` 的对象形式 `{"css":…,"text":…}` 表示该元素可见文字全等。tap_xy 的限制不变。
- input_text 是替换语义：点击目标后，若字段已有文本，按其长度各发送同样次数的 `KEYCODE_DEL(67)` 与 `KEYCODE_FORWARD_DEL(112)` 清空（点击把光标放在任意位置都能清干净），再输入；随后重读树（先按 locator，命中不唯一时按目标 resource_id），要求字段文本与 `text` 完全一致，不一致或重读不到返回 `assertion_failed`，不返回 recorded。`text` 必填：缺省时在触碰设备前返回 `invalid_request`（propose、Journey 回放预检和 act 同一校验），不会默认为空串清空字段；显式 `"text": ""`（仅 adb）表示清空字段并核对为空。要追加时调用者传入完整的目标全文。会话 act 与 Journey 回放走同一检查。屏上不显示明文的字段（如密码框）因此不会报成功。
- input_text 默认 `method=adb`，只接受 ASCII；含非 ASCII 时在触碰设备前返回 `unsupported_input`，不再把 `adb shell input text` 的 Unicode NPE 变成未知动作。非 ASCII 用 `method=paste`：先把完全相同文案放到手机剪贴板，动作会点击目标、清空、发送 `KEYCODE_PASTE(279)`，再做上述字段文本核对。
- tap_xy 用 x/y，locator 仅为来源记录。坐标重放必须附 `fixed_segment`：`entry`/`exit` 检查、精确 `width`/`height`/`orientation` 、`app` 和 `target` locator；当前只允许 `com.android.settings` 导航，`exit` 是唯一步骤检查点，x/y 必须在屏幕内。每个坐标步骤执行前核对 entry、唯一 target 的当前中心坐标必须等于 x/y，执行后核对 exit。旧版没有 target 的配方拒绝；这不是完整遮挡识别，尚无固定坐标相对语义定位的提速实测证明。
- 动作效果（仅 `act`，原生 tap/tap_xy/input_text/key/scroll/launch_app/launch_activity）：结果与 `action_finished` 事件新增 `effect` 和 `tree_blind`，失败时在 `data.detail` 中。`effect`：前后树与前台窗口（`dumpsys window` 的 `mCurrentFocus` 包名/activity）都相同为 `no_effect`；包名或 activity 变了，或树的 (class, resource_id) 结构重合低于一半为 `place_changed`；其余变化为 `done`；任一侧树读不到且窗口未变时为 `null`（未知，不是 no_effect）。`tree_blind`：点击坐标（locator 命中节点中心或 tap_xy 的 x/y）在动作前的树里没有可点击或带文字/描述的节点覆盖，或动作前树读不到时为 true；无坐标动作为 false。前树复用 locator 读取（tap_xy/key 等另读一次），后树复用 expect/expect_gone/expect_app 等待的最后一次读取（三者都没有时等 settle_ms 后读一次；有等待条件且无 locator 时，前树复用上限提示所需的那次动作前读取），前后各多一次只读 `dumpsys window`；读取失败不阻断输入、不改 ok/code。原始树不进结果与事件。这是可见变化的启发式，不是业务核验：树不含勾选状态，开关类点击可能报 no_effect；动态内容（计时、视频）会让空点也报 done。网页 locator 路径、wait、read_field、wake 和 Journey 回放不带这两个字段。
- 步骤 expect/postcondition 只支持 element_exists、text_equals。原生 Journey 步骤与 session act 的 action 另可带本地等待条件：`expect_gone`（与 expect 同形的 element_exists/text_equals，要求动作后该文字或 locator **不再**出现）和 `expect_app`（前台包名全等，读 `mCurrentFocus`，前缀不算）。三者共用一次有界等待（settle_ms 后按 poll_ms 轮询），全部满足才 ok；任一未满足返回 `assertion_failed`，结果附 `unmet`（未满足的键名列表，如 `["expect_gone"]`）。只带 expect_gone/expect_app 也记一条 `check` 事件（name 仍为 `expect`），但不写 resume 检查点（检查点仍只由 expect/postcondition/fixed_segment 产生）。二者只用于原生树：与网页 locator 或网页检查同用、用在 wake 上、带 fixed_segment、包名格式不对均在触碰设备前拒绝（wake 走独立模板，不执行等待；wake 上的 expect 也不再记 check 事件）；expect_gone 可放参数引用，expect_app 不可。
- 平台阻断：验证/限流信号（操作频繁、安全验证、captcha 等）无论动作前后，出现即 `platform_blocked`，行为不变。“到上限”类提示（已达上限、次数已用完、额度已用完、limit reached、daily limit、quota exceeded 等）只在动作带等待条件时、于动作后的轮询中检查，且只有动作前屏幕上没有的同一文本才算：此时返回 `platform_blocked`，结果附 `limit_notice: true` 与 reason；动作前已在屏上的同一提示不算，也不阻止动作。上限词的某次出现若完整落在本动作自己输入的文字（`input_text` / `share_text` 的 `text`）的某次出现之内，视为输入回显（输入框或建议列表）不算；与输入只部分重合或在输入之外的上限词照常阻断。真实提示的上限词恰好只落在所输入文字内时会漏判。trace 的 `platform_blocked` 事件与对应 `action_finished` 带 `blocked_by`：`limit_notice` 或 `verification_block`（observe 的阻断恒为 `verification_block`），已列入事件隐私白名单。动作无 locator 时会为此多读一次动作前的树。网页动作路径未接入上限提示。
- 终态 postconditions 支持 all/any/not、element_exists、element_count、text_equals；element_count.equals 默认 1。空 all/any、畸形检查、app_is/field_equals/jev 终态检查均拒绝。省略终态或 `{}` 只能 recorded，不能 completed/晋级。
- 参数引用只接受精确单键对象 `{"param":"name"}`，可放在 locator 与 `fallback.alternatives` 字符串、`input_text.text`，以及 entry_state/postconditions/步骤 expect/postcondition 的 text_equals 和 element_exists/element_count locator 中；其他位置拒绝。schema 支持 object/properties/required 和 string/number/integer/boolean、default、enum，不支持 sensitive/persist 字段；不做字符串插值或 eval，未知参数名、类型不符和未知占位符均拒绝。绑定不保存原参数值；request_once 缓存结果元数据，outputs/value 只返回当次调用者，通信重试返回 `content_retained=false` 且不重执行。session 事件不保存文本 locator 或任意输入正文。task_key、step_id 等调用者标签、配方与复盘文本仍须调用者脱敏，不宣称能识别任意字段中的隐私。
- effect_class 默认 navigation；合法值为 observation/navigation/local_input/external_mutation。未带有效授权的 external_mutation 暂停返回 needs_authorization；该字段不证明按钮真实语义。
- share_text 授权：`session authorize` 带完整 share_text action 与相同 `text`，不带 post/opener；没有屏上目标可核对，grant 绑定 run、device、action 摘要（含 package 与 text）和文案摘要，600 秒、一次性。act 时带 `authorization:{grant_id,text}`；换包名、换文案、跨 run/device、过期或重复都返回 needs_authorization；share grant 不能用于 input_text 或 tap，tap 的 grant 也不能用于 share_text。未带 grant 时一律 needs_authorization，不派发。
- 外部写授权：先在帖子标题与作者都在当前树中的页面调用 `session authorize`。请求含 `post:{title,author}`、完整的待执行 `action`（当前仅 `tap`、必须声明 `effect_class=external_mutation`）和精确文案 `text`；CLI 自己做一次新鲜读树核对，不采信调用者自报。成功返回一次性 `grant_id`（默认 600 秒有效）。随后同一 run/device 内用 `input_text` 携带 `authorization.grant_id` 输入完全相同的文案；该 run 存在未消费 grant 时，不带 grant 的 input_text 也会被拒绝。再用完全相同的 action/locator 携带同一 grant 与 `authorization.text`（同一文案）执行发送。消费 grant 前 CLI 再做一次新鲜树读取：该文案必须仍在树中，发送 locator 必须唯一命中，否则返回 `precondition_mismatch` 且不消费 grant。grant 绑定 run、device、action摘要与文案摘要，发送前原子标记已用；文案不符、locator 不符、跨 run/device、过期或重复使用都返回 needs_authorization。grant 只覆盖“准备 → 绑定的打开编辑框 tap → 输入同一文案 → 发送”。authorize 可带 `opener`（一个 tap action，effect_class 不得为 external_mutation），它绑定摘要；准备后只有与摘要完全一致的第一个 opener tap 被允许，其他任何动作在派发前使 prepared grant 失效，重新准备也会使旧 prepared grant 失效。未提供 opener 时，准备后直接输入；任何额外导航都会使 grant 失效。评论编辑 overlay 可能只暴露 EditText 和发送按钮，因此身份在 prepare 时以现场树为准，发送时只核对文案与目标 locator；不证明按钮真实语义或其他副作用。
- 默认 settle_ms=800、poll_ms=2000，仅在动作有后置检查时使用；wait.seconds=1、key.key=back、scroll.direction=down、launch_app.app=Settings。wake 仅普通唤醒/滑锁，不处理凭据：唤醒后最多三次上滑，读取真实 keyguard/bouncer 状态，并用一次无障碍树读取确认可用；`secure=false` 不等于已解锁，未通过树读取不返回成功。租约空闲过期时间 1800 秒。
- 动作节奏按设备共享：普通动作间隔 3 秒，声明为 search/refresh/page 的网络动作 5 秒；每次运行最多 40 个动作、3 个网络动作、5 次滚动。计数和起始时间在执行前持久化，跨 CLI 重启不能绕过。`traffic_class` 默认 navigation，`read` 只用于 read_field；未知分类拒绝。预算耗尽返回 budget_exhausted 并暂停。这些是本地实验限制，不是平台允许速率或服务器负载保证。
- 按前台包名的最小间隔叠加在设备级间隔之上：同一包名下相邻动作至少相隔该包的间隔。内置默认 3 秒、无包名覆盖，等于不生效；`<store>/package_intervals.json`（`{"default": 3, "packages": {"com.example": 8}}`）可覆盖。只有配置可能拉长等待时才读前台（一次 `dumpsys window`，只认 `包名/Activity` 形式，弹窗等窗口标题算读不到），每次等待后重读，按动作开始时真正在前台的包计时。前台读不到时按配置中的最大间隔、设备级计时，且这次动作的时间计入之后每个包的冷却。`tap_xy` 的坐标若在冷却期间过期，返回 precondition_mismatch 且不预留预算和冷却，已等过的时间对下次重新观察仍有效。配置非法时拒绝动作（pacing 码 `pacing_config_error`，act/validate 对外为 `invalid_request`），不消耗预算。`pacing` 事件带 `package` 与 `package_interval`。
- 运行前检查 entry_state；环境字段只从设备实测值推导，声明但无法核验的字段返回 capability_gap，不匹配返回 version_mismatch。当前环境签名为 guard_version=2，只有同 guard_version 的验证记录计入晋级。

## 去重、取消与恢复边界

act/run/validate 的 request_id 由调用者生成；通信重试复用原 ID。工具在派发前持久化 intent，并用跨进程锁避免并发重复；授权发送同样走 `request_once`：grant 在设备输入前原子消费，同一 request_id 重试只返回原结果，未知结果换新 ID 会被未闭合动作或已消费 grant 继续阻断；Journey 在输入前另存设备占用，直到动作结果持久化且无未知动作才释放。崩溃或未知结果保留占用，换 request_id、启动新 session 或换入口仍被阻断；当前没有解除未知 Journey 占用的命令，必须 inspect 并由操作方核查执行进程与业务现场；reconcile 不支持对未知效果作确认后释放。已完成状态可复用，正文不缓存；需要再次读取时必须先明确前次结果和新读取意图，不能把新 ID 当通信重试。同一 ID 的不同负载拒绝。旧版无密钥请求摘要与新版不兼容，会保守返回 request_conflict。仍不能据此承诺 exactly-once，也不能没收到输出就换 ID 重试。

run/validate 同步执行；inspect 可读取当前摘要/事件，没有统一的 `data.run_state` 查询协议。cancel 写标记，session 动作前、节奏等待中、runner 步骤边界和末步后检查；无法中断已进入 Phone Harness 的单次动作，不宣称硬实时取消。

resume 已可调用，但只接受 paused/cancelled/blocked 的 Journey 运行：必须存在可信检查点、无未闭合动作、原配方摘要与参数摘要一致、设备不变，且该版本在当前环境未被 suspended/superseded。设备锁内会重新核对当前环境与生命周期资格，生命周期写入也在该锁内完成。它用新鲜观察核对上一检查点，然后创建关联 continuation run，只执行剩余步骤，共享原运行预算，标记 agent_intervened 且 promotion_eligible=false；同一检查点只能续跑一次，不支持任意步骤回滚。检查点只在带 expect/postcondition 或 fixed_segment 的步骤成功后写入；下一动作 dispatch 前会清除旧检查点，避免按旧边界重放。未知效果先 inspect 并核实现场，再决定受控探索，不盲重跑。

本地未知输入只支持窄口径解除：`session resolve-input` 必须收到原始 `request_id`、完整原始 `action` 和该 run 记录的原 `lease_token`，不读取设备。它重算与 `session act` 相同的 payload 摘要，要求与 `request_once` 记录一致且记录已是 `done`/`outcome_unknown`；要求该步骤是 run 内唯一未闭合动作、动作为默认 adb 的 `input_text`/`local_input`，终态事件带正常脚本完成指标（排除启动失败、超时/被杀、无 JSON 分支）；还要求失败后、任何后续动作前的持久树观察与失败动作前最后一次树观察的密钥摘要、节点数和几何完全相同。满足才追加 `action_resolved`，不改写历史。该证明的含义是“同步包装调用已返回，且动作刚结束时的可见状态未变”，不是“绝无瞬时副作用”，也不描述当前屏幕。为解开“锁屏无法 handoff / 未知无法 wake / 过期无法 resolve”死锁，同一原 run 的原 lease token **即使过期也被接受**：命令在设备锁内检查 reservation 仍属该 run、没有其他 run 的活跃 lease；不新建 lease/token/reservation、不刷新过期时间、不抢占其他占用，token 不符或占用属于其他 run 时返回 `device_busy`。其他未知仍须现场核查，不能用新 ID 重放或通用 clear-unknown 绕过。此前失败的 input_text 若已开始但未成功，grant 不会登记文案；发送前还会要求 `authorization.text` 与现场树一致。

session handoff 仅适用于探索会话：取得设备锁；若状态为 nonsecure 且 `ordinary_wake_allowed`，先记录并执行一次普通 wake（不输凭据），再刷新树观察、签发新 lease_token 并使旧 token 失效；secure/凭据界面直接返回 locked，要求人工解锁。正常 finished 且无未闭合动作的会话不能借 handoff 重开。有未闭合动作、cancel 标记或 platform_blocked 事件时 action_allowed=false。它不改变绑定设备，也不替代 Journey resume。

## Trace、复盘与经验

`trace` 引用格式为 `{run_id,seq_start,seq_end,digest}`，摘要只覆盖该区间的规范化事件；后续追加不会使旧引用失效，区间修改/缺失则失效。引用保证内容完整性，不保证 Agent 的文字推论正确。新 run summary 记录工作树 Python 源码的 `code_digest`，事件有 schema_version/run_id/seq/UTC 时间。

`review.questions` 必须包含 completion/cost/repetition/cause/scope/next_experiment 六个非空字符串；`claims` 中每项为 `{kind:"fact"|"hypothesis",text,references:[引用]}`。fact 必须有可验证引用，hypothesis 不因此自动成为事实。可选 `lesson` 包含 rule/applies_when/invalid_when。复盘追加保存到 `runs/<id>/reviews/<version>.json`；经验保存到 `lessons/`，不会自动生成或晋级 Journey。模板见 [execution-review skill](../skills/execution-review/SKILL.md)。

CLI 的 `harness_calls`/trace 汇总包含适配器内就绪、动作、入口/终态检查调用；不含独立 adb、绕过 CLI 的 Python 调用或进程被杀后未保存的调用。动作内 tree_reads、路线 observations 是不同口径，不能直接当总读屏。历史缺失值保持 null。skill 指导查询与复盘，不是自动触发 hook。

`reconcile` 只处理没有未知动作的遗留占用：取得锁、核对租约、新鲜只读观察后释放，不改变已结束任务的历史结论；未结束运行标为 paused。未知效果保持阻塞，需要操作方核查进程与业务状态；此命令不能证明孤儿进程停止。resume 在派发前预分配关联 continuation run，崩溃后可由原 run 的 resumed_by 定位，不能用第二次 resume 隐式重放。

## 结果包示意

```json
{
  "schema_version": 1,
  "status": "paused",
  "run_id": "r_example",
  "data": {"step_id": "open_search"},
  "error": {
    "code": "ambiguous_target",
    "message": "Two targets match",
    "retryable": false,
    "exit_code": 3,
    "status": "paused"
  },
  "next_action": null
}
```

recorded 仅表示动作/记录完成；completed 要有业务后置核验。错误目录中的 next_action 是建议命令；当前结果包 next_action 通常为 null。探索会话可用 session handoff 转移租约，Journey 可用 resume 从可信检查点续跑；两者语义不同，不能推导所有命令都具有统一 handoff 协议。

## 从零到复用

1. capabilities 确认目录与实际能力；status 获取设备条件。
2. discover 查询候选与运行摘要参考；两者都只是检索提示，仍需 run 的资格检查。最新 candidate 与旧 verified 可同时返回；仍不能仅据 executable 提示证明当前现场适用。
3. 无适用 Journey 时 session start → observe/act → session finish；需要换 Agent 接管同一探索时用 session handoff。finish/handoff 都不证明业务完成。
4. Agent 明确目标、定位与终态后 propose；日志不能自动生成正确业务配方。未 propose 的 run 可按 ID inspect，也会以不可执行参考出现在 discover。
5. validate 每次一次；当前已有连续验证、参数绑定和入口/环境检查，但不能视为泛化可靠性或完整环境隔离证明。
6. paused 时先 inspect，核实未知效果；有可信检查点且配方/参数未变时可 resume，否则通过受控探索修复并提交新候选。

## 只读快捷入口 `read-post`（独立于 Journey 晋级）

`PYTHONPATH=src python3 -m journey_pilot.cli read-post --request-stdin`；JSON 必需字段 `device_id`、精确 `title`、`author`；可选 `mode: fast|baseline`（默认 fast）、`jev: true|false`（默认不启用）。

- 前置：Android 已连接/解锁，已登录小红书；支持带“我”入口的页面或目标正文页。未知页面拒绝，不保证从任意页面自动恢复。
- 范围：打开本人精确标题/作者的笔记，确认“公开可见”，读取当前正文树及可见评论区域；`coverage=visible_only`，不承诺读完所有折叠正文或评论。
- 设备锁拒绝后不读屏/不执行；普通动作仅 launch_app/tap_xy/scroll。3 次主页滚动、2 次评论滚动上限；90 秒为步间检查预算，底层单次调用仍可能超出，不是硬实时截止。
- `fast` 在无页面变化且观察不超过 8 秒时复用节点；点击参数来源为 observation_id；`baseline` 每次点前额外重读。`tap_xy.locator` 仅为证据，不触发额外定位；需要重新定位须用 `tap`。
- 正文、评论区当前文本仅返回当次调用者；返回 run_id、路线定义 SHA256、timing、`content_retained=false`。本地保存静态 `route.json` 与只含元数据的 `task-result.json`；默认不保存正文或原始树。显式 `persist_evidence=true` 才保存原始树，调用者须先确认适合保留。历史全文证据不自动删除，也不得公开。
- `observations` 统计路线显式读树；session.start 内就绪探测计入其 span，不计入该计数。CLI 外准备/归位单独计时。session 的 recorded 和快捷任务的 completed 是不同层次；后者不授予 verified 资格。
- 每次调用都是一次新只读巡查，不承诺 request_id 去重；不自动重试异常。未知页面/歧义/低置信度交回发起 Agent。Jev 只处理封闭导航标签，不发送评论正文，亦不裁决最终成功。

## 登记式薄脚本入口

- `task-list`：返回登记任务名、必需字段、入口与效果说明，不访问手机。
- `task --request-stdin`：JSON 必需 task/device_id，其余按任务定义；拒绝未知任务，不加载用户提供的代码或路径。
- `xhs-read-post`：参数与旧 read-post 相同，两入口使用同一业务脚本及 task_runtime。
- `douyin-ai`：从已打开的抖音评论/AI 面板开始；可选 expected_summary（结果主题包含检查，不是唯一视频 ID）、wait_seconds（大于 0 且不超过 30）。返回 summary_title/summary_text、accessibility_exposed 范围、平台生成内容标记，明确未验证评论分析、未发送聊天。
- observe 增加 source=pixels：调用已安装的本机 Vision OCR，先归一化 PNG，保存截图/节点；不是大模型或 Jev。timing.observations 包括显式树与像素观察，pixel_observations 是其中子集。

详细编写方法与受信任代码边界见 [task-scripts](task-scripts.md)。任务 completed 不授予 Journey verified，不改变 run/validate 的规则。
