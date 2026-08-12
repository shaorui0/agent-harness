---
name: harness-scope-gate
description: 中大型改动开工前，把边界（目标产出/不做什么/验收标准/允许碰哪些路径）问清楚，写成一个 confirmed 的 scope artifact。PreToolUse 的 scope gate 只认这个 artifact 的 `covers` 字段，不做 cwd/branch 推断。用户说 "/harness-scope-gate"、"先把边界问清楚"、"这个改动开工前先澄清一下" 时触发，或 model 自己判断当前任务规模够大时主动调用。
---

# harness-scope-gate

见 `docs/scope-and-spec-gate-design.md`：这是"开工前澄清"的强制点，但强制力不在触发（触发永远是判断，不能被 hook 硬性检测），在闸门——写完这个 artifact 之前，`harness-envelope.py` 会把 `covers` 范围内的写操作全部 deny。

## 步骤

### 1. 取 slug

从任务内容起一个简短的 kebab-case 名字，比如 `scope-gate-mechanism`。这是 artifact 文件名，也是后面 `/harness-spec-review` 用来定位同一个 scope 的 key。

### 2. 逐题问，一次一题

用 `AskUserQuestion`，不要把下面几题塞进一次调用的多个选项里，一题问完再问下一题：

- **目标产出**：这次改动做完之后，什么东西是新增/变了的
- **不做什么**：明确排除在这次范围外的东西——没有这一题，"边界清晰"这个词就是空话
- **验收标准**：怎么才算做完，越可核查越好（对照 `bestpractice_agentic_control_primitives` 的 Spec 定义："machine-checkable acceptance criteria"）
- **允许碰哪些路径**（`covers`）：这一题的答案直接写进 artifact，是 PreToolUse hook **唯一**认的边界。回答"整个仓库"也可以，但要显式说出来，不是默认值

### 3. 落盘

```yaml
# .harness/scope/<slug>.yml
name: <slug>
status: draft            # 全部问完、用户确认无误后改成 confirmed
goal: ...
non_goals: [...]
acceptance_criteria: [...]
covers: [...]            # 相对路径前缀列表，PreToolUse 按这个匹配
risk_budget: ...
confirmed_at: null        # status 改成 confirmed 时补上 ISO8601 时间
```

`status` 不是 `confirmed`，PreToolUse 不认——这是有意的两步状态，给用户一个"问完了但还没拍板"的中间态。

### 4. 报告

```
【已澄清】
- scope: <slug>
- covers: <路径列表>

【下一步】
- covers 范围内现在可以动手了
- 改完之后跑一次 /harness-spec-review <slug> 做收敛判定，否则 Stop hook 会一直提醒
```

## 硬规则

1. 逐题问，不批量塞选项。
2. `covers` 必须显式列出来，不用 cwd/branch 猜——猜是这套机制在架构评审里明确否掉的方案。
3. `covers` 留空等于什么都不覆盖，等于 scope gate 对这个 scope 形同没生效，不是"覆盖全部"。
4. 没有 `status: confirmed`，PreToolUse 不认，写操作照样被挡。
