# 0012 单次外部写授权：现场身份＋动作＋文案绑定

- 日期：2026-10-23
- 状态：accepted（用户当轮明确批准两条小红书评论）
- 取代：无

## 背景

用户明确批准在两条目标帖子下各发送一条评论，并要求补齐发送流程。两条帖子的标题与
作者只在设备现场核对，不写入仓库；两处文案完全相同，仓库只保存其密钥摘要。
现场核实：点击评论框进入编辑 overlay 后，树只暴露 EditText 与“发送”，标题/作者不可见；
因此不能在发送时重新核对帖子身份，也不能用调用者自报身份代替现场证据。

旧行为是所有 `external_mutation` 无条件暂停并返回 `needs_authorization`。仅把发送标成
`navigation` 会绕过守卫，不能作为方案。

## 决策

1. 新增 `session authorize`（CLI 与 `session.authorize`）。它在当前会话设备上做一次新鲜
   无障碍树读取，只有精确标题和作者同时出现在树中才签发授权；否则返回
   `precondition_mismatch`，不产生 grant。
2. 授权请求必须给出完整的待执行 `tap` 动作（当前只允许 `tap`，且必须声明
   `effect_class=external_mutation`）和精确文案 `text`。grant 绑定：
   - `run_id` 与 `device_id`；
   - 动作及其 locator 的密钥摘要；
   - 文案的密钥摘要；
   - 标题与作者的密钥摘要；
   - 准备时的 observation_id 与 600 秒有效期。
   明文标题、作者、文案不写入 grant 或事件，只保存摘要。
3. 同一 run/device 内，`input_text` 携带 `authorization.grant_id` 时比较文案摘要并登记
   `text_entered_at`；摘要不符则拒绝。发送时必须携带同一 grant，动作/locator 摘要必须
   完全一致且已登记同一文案。
4. grant 在设备输入前用跨进程文件锁原子标记 `consumed`，因此最多派发一次。发送仍经
   `request_once`：同一 `request_id` 重试只返回原结果；未知结果会保留未闭合动作，
   换新 ID 也被阻断。
5. 未携带有效 grant 的 `external_mutation` 继续返回 `needs_authorization`；不带
   `effect_class=external_mutation` 的动作不会被此机制授权，也明确不把发送标成
   navigation 当成方案。

## 非目标

- 不建设通用外部写授权平台、批量发送、频率调度或自动重试。
- 不在编辑 overlay 中重新证明帖子身份；grant 的连续性依赖同一 run、短有效期和一次性消费。
- 不证明按钮真实语义、平台受理结果或评论未被折叠/删除；发送后仍需现场复核和截图。
- 不改变 Journey 的 `external_mutation` 暂停策略；本轮例外仅限上述两条帖子、同一文案和
  各自的一次发送。

## 验证

新增 6 项离线测试：未授权暂停、现场身份必须精确、非 tap/非 external 拒绝、文案与 locator
不匹配拒绝、跨 run 拒绝、一次性消费与未知结果不重派。全量 231 项通过；未操作手机、
未提交、未修改外部 Phone Harness。
