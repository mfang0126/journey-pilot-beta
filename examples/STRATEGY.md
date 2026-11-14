# Journey Pilot：配置兼容与现场交接

只有一条 Journey 执行路径：每个定位动作读取当前页面，唯一匹配后执行；页面变化后重新观察，终点核验业务条件。暂不支持 `batch` / `adaptive` 模式，显式传入会拒绝，避免旧示例被误用。

## 只改 key 的兼容

`tap` 和 `read_field` 可以声明主 locator 与有序备用 locator：

```json
{
  "step_id": "profile",
  "action": "tap",
  "effect_class": "navigation",
  "locator": {"description": "我"},
  "fallback": {
    "alternatives": [{"text": "我"}, {"description": "个人主页"}]
  }
}
```

支持 `resource_id`、`text`、`text_contains`、`description`、`class`。同一个 locator 的多个字段是 AND 条件。未知字段、空值和未知 `decision_method` 拒绝，不静默丢弃约束。备用列表最多 8 项，只用于已经确认语义相同的控件。

主定位零匹配才尝试备用定位；全部备选复用这一次新鲜树。任何一个定位出现多个匹配立即暂停，不越过歧义尝试另一个目标。`locator_index=0` 表示主定位，`1..8` 对应备用项。不会跨页面复用旧树或坐标。

## Jev、Agent 与学习的分工

- 兼容且唯一：程序直接执行。
- 已知字段变化：通过上述备用 key 兼容。
- 薄任务的安全候选仍有歧义：现有 TaskContext 可交 Jev 有限选择；低置信刷新一次，仍不确定则交还 Agent。通用 Journey 的备用 locator 暂不接 Jev。
- 没有安全候选或任务语义变化：Agent 基于新鲜现场探索，记录失败点与已有效果，不自动整段重跑。
- 确认真实业务成功后：Agent 根据证据整理配置，显式 propose → validate。日志不会自动生成正确配方，更不会自动产生 verified。

双模型模块尚未接入实际执行，也没有真实视觉供应商验收。`resume` 支持从可信检查点续跑，要求相同配方和参数，且没有未解决的动作效果；不满足条件时需重新探索。

## 可用命令

以下示例只提出候选，不操作手机。它读取当前页面中的一个标题，**不是完整小红书帖子读取任务**，也未获实机验证：

```bash
PYTHONPATH=src python3 -m journey_pilot.cli propose --request-stdin < examples/journeys/read_visible_title.json
```

后续 validate 用返回的 journey_id、显式 device_id 和唯一 request_id，以 JSON stdin 调用。普通 run 只接受已验证版本。当前通用 Journey 不展开 `{{title}}` 等模板；不要把这种字面值当参数绑定。

## 性能证据

本轮没有实机提速数据。此前“26s → 13s”“每个任务只需两次观察”“失败自动转 adaptive”“自动生成 batch 配置”的描述均撤回。

动作事件新增 `metrics.tree_reads/tree_ms/explicit_wait_ms/script_ms`，用于统计生成的动作脚本中显式的读树和后置条件等待。它们不包含 Phone Harness 内部隐藏调用、session.start、单独 observe、进程启动或所有底层等待，不能当成全任务总计。墙钟动作耗时另记 `duration_ms`。

同设备、参数、起点各至少 5 次的 baseline/replay 对照仍按 requirements.md 执行；失败保留，报告完成数、中位数、范围、模型调用及观察/等待成本。
