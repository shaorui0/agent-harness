---
name: harness-req-split
description: 把一个"大需求"拆成 N 个各自能判定 pass/fail 的子 scope，每个子 scope 走一遍 harness-scope-gate 的逐题澄清流程，并起一份 pipeline artifact 把后续阶段串起来。用户说 "/harness-req-split"、"这是个大需求先拆一下"、"帮我把这个需求拆成能验证的小块" 时触发，或 model 判断当前需求明显跨越多个独立可验证单元时主动建议。
---

# harness-req-split

大需求执行五段式（拆解→开发迭代→规模验证→管线→交叉验证）里的第①段。这个 skill **不是** `harness-scope-gate` 的替代品，是它的薄外层：`harness-scope-gate` 负责把*一个*边界问清楚，这个 skill 负责先判断"这一个大需求该拆成几个边界"，然后对每一份都重复 scope-gate 的逐题流程。scope-gate 本身不改。

## 步骤

### 1. 问 group 级目标

用 `AskUserQuestion` 问一题："这个大需求做完之后，整体上什么算成功"——这是组级别的 goal，不是任何一个子 scope 的 acceptance_criteria，先问这题是为了后面判断拆法对不对：任何一种拆法，子 scope 全部完成时应该正好等于这个目标达成，多了或少了都说明拆错了。

### 2. 提出候选拆法，MECE 校验

给出一个拆分方案（几个子需求，各自负责什么），向用户确认。拆分必须互斥且完备（MECE）：

- 两个子需求的改动会碰到同一批文件/同一个组件 → 不互斥，合并成一个
- 子需求全部做完但 group 级目标没达成 → 不完备，漏了一块
- 拆不出正交单元（比如需求本身就是单点、不可再分）→ 如实告诉用户"这个需求本身够小，不需要拆"，直接建议走 `harness-scope-gate` 单开一个 scope，不要为了走这个流程硬拆

拆分数量没有固定上限，但超过 5 个子需求时提示用户：这可能不是一次"大需求"该有的粒度，考虑先做一轮更粗的分组。

### 3. 对每个子需求重复 scope-gate 的逐题流程

对确认下来的每一个子需求，重复 `harness-scope-gate` 步骤 2-4：`AskUserQuestion` 逐题问（目标产出 / 不做什么 / 验收标准 / `covers`），一题一题来，不批量塞选项。

在这四题之外，**多问一题**：给 3-10 个真实场景（不是假想的边界情况，是这个子需求实际会遇到的具体输入/操作）。

落盘时沿用 `harness-scope-gate` 原有 schema，新增两个可选字段：

```yaml
# .harness/scope/<child-slug>.yml —— 在 harness-scope-gate 现有 schema 上新增
name: <child-slug>
status: draft            # 同 scope-gate：问完确认后改成 confirmed
goal: ...
non_goals: [...]
acceptance_criteria: [...]
covers: [...]
risk_budget: ...
confirmed_at: null
scenarios: [...]          # 新增，可选：3-10 个真实场景
group: <parent-slug>      # 新增，可选：反向指向下面的组文件
```

这两个字段是纯 additive，不影响 `harness-envelope.py`/`harness-spec-review` 对老 artifact 的处理——它们从不读这两个字段。

### 4. 落"组"文件

```yaml
# .harness/scope/groups/<parent-slug>.yml
name: <parent-slug>
kind: requirement-group
status: draft | confirmed
big_requirement: <大需求原话，一句话>
children: [<child-slug-1>, <child-slug-2>, ...]
created_at: <ISO8601>
```

放在 `.harness/scope/groups/` 子目录，不跟子 scope 平铺在 `.harness/scope/` 下——这是有意的：`harness-envelope.py` 和 `harness-spec-review` 扫描 scope 目录时用的是非递归 `glob("*.yml")`，子目录下的文件不会被扫到、也就不会被误判成"一个待收敛的普通 scope"。这样不需要改动这两处现有代码。

### 5. 起 pipeline artifact 骨架

```yaml
# .harness/pipeline/<parent-slug>.yml
name: <parent-slug>
status: draft
requirement_group: .harness/scope/groups/<parent-slug>.yml
created_at: <ISO8601>
updated_at: <ISO8601>

stages:
  decomposition:
    children: [<child-slug-1>, <child-slug-2>, ...]
  dev_iteration:
    status: not_started
    notes:
  scale_verification:
    status: not_started
    evidence: []
    notes:
  offline_gate:
    per_child: {}
  online_monitor:
    status: not_configured
    dashboards: []
    last_check_at:
  feedback_loop:
    fed_back_cases: []
  cross_validation:
    contradictions: []
    reviewer_rounds:

gate_decision: blocked
```

这份文件是指针和"没地方放的新状态"的集合，不复制 `scope/groups/<slug>.yml` 或各子 scope 的内容——那些内容仍然只活在各自的文件里，pipeline artifact 只存引用。

### 6. 报告

```
【已拆解】
- 大需求: <一句话>
- 子scope: <slug列表>
- pipeline: .harness/pipeline/<parent-slug>.yml

【下一步】
- 每个子scope改完，先按 acceptance_criteria 本地过一遍（本地快反馈，没有专门机制），再跑一次 /harness-spec-review <child-slug> 做收敛（没有hook会催，得自己记得）
- 涉及集群/规模的改动，跑 /harness-scale-check
- 子scope都收敛之后，跑 /harness-cross-verify 做交叉核查
```

## 硬规则

1. 逐题问，不批量塞选项——四道原有题目 + 场景题都要一题一题来。
2. 拆分必须先证明 MECE。拆不出正交单元就如实说明这个需求不需要拆，退回单一 `harness-scope-gate` 流程,不能为了拆而拆。
3. `scenarios`/`group` 是可选、纯 additive 字段，不改 `harness-scope-gate`/`harness-envelope.py`/`harness-spec-review` 里任何一行既有逻辑。
4. "组"文件必须落在 `.harness/scope/groups/`，不与普通 scope 平铺。
5. pipeline artifact 只存指针，不复制①里已经落盘的内容。
