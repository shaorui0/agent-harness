---
name: harness-spec-review
description: 对一个已经 confirmed 的 scope 做收敛判定——独立无上下文的 reviewer 打分，有限轮次收敛到 CLEARED / NOT_CLEARED / DONE_WITH_CONCERNS，写回 scope artifact。用户说 "/harness-spec-review"、"这个 spec 收个尾"、"看看这个改动做完没有"、"哪些 scope 还没收敛" 时触发，或自己手上这个 scope 的改动做完之后主动跑一次。
---

# harness-spec-review

见 `docs/scope-and-spec-gate-design.md`：这是"收敛点判定"层。**纯 pull——只有被显式调用时才扫 scope**。没有 hook 会替你发现"有 scope 没收敛"，这是有意的：收敛是判断，判断不挂事件（DESIGN.md 原则一），而且一个 session 去发现另一个 session 手上的活儿，只会把别人的任务列表拽进自己的上下文。

## 步骤

### 1. 定位 artifact

传了 slug 就只看 `.harness/scope/<slug>.yml`，**别的 artifact 一律不碰**。

没传 slug：先看这轮对话里自己实际动过的是哪个 scope，只收敛那个。真要盘点时，才扫当前项目根的 `.harness/scope/*.yml` 列出 `status: confirmed` 且 `review.status` 未收敛的条目——**列出来给用户看，不要自动挨个收敛**：别的 scope 可能是另一个 session 正在做的活儿，替它宣布"做完了"是错的。用户点名哪个再动哪个。

扫描范围只在当前项目根（`find_project_root` 找到的那个 `.harness/`），不往上、不跨仓库。

### 2. 独立 reviewer 打分

派一个**无上下文**的 reviewer subagent（`Agent` 工具，普通 agent 不是 fork——fork 会带着这轮对话的上下文，等于自己评自己，没有意义）。喂给它 `goal` / `non_goals` / `acceptance_criteria` / `covers`，让它对照实际的代码/文档改动，按 5 维度打分：

- Completeness — acceptance_criteria 每一条是否真的做到
- Consistency — 改动内部是否自相矛盾
- Clarity — 别人接手能不能看懂做了什么
- Scope — 有没有超出或漏掉 `covers` 声明的范围
- Feasibility — 剩下没做的部分是否可行

### 3. 有限轮次收敛

有问题就按 reviewer 的意见改，重派一轮。**最多 3 轮**。**连续 2 轮 reviewer 提的是同样的问题就停**，不再重试，把问题记进 `concerns`——借的是 `autoplan` 的 Pre-Gate Verification 思路（有限重试，仍缺失就带警告继续，不是无限等）。

### 4. 写回 artifact

```yaml
review:
  status: CLEARED | NOT_CLEARED | DONE_WITH_CONCERNS
  rounds: <n>
  concerns: [...]
```

- `CLEARED`：acceptance_criteria 全部通过。
- `DONE_WITH_CONCERNS`：收敛了，但留了明确记录的未解决项。
- `NOT_CLEARED`：还没收敛完就被打断，或者卡在同一个问题连续 2 轮——**不建议长期停在这个状态**，但没有任何 hook 会催，下次盘点时它会出现在未收敛列表里。

**可选：同步 pipeline artifact。** 如果这个项目根下存在同名 `.harness/pipeline/<slug>.yml`（大需求执行五段式，见 `docs/big-req-pipeline-design.md`），顺手把这里的 `review.status` 同步进它的 `stages.offline_gate.per_child.<slug>`。找不到就静默跳过，不报错——这是纯 additive 的一步，不改上面的判断逻辑本身，也不是这个 skill 的必需步骤。

### 5. 报告

```
【收敛结果】
- scope: <slug>
- status: <CLEARED|NOT_CLEARED|DONE_WITH_CONCERNS>
- 轮数: <n>

【未解决】
- <concern>（没有就留空）
```

## 硬规则

1. reviewer 必须是无上下文的独立 subagent，不能自己评自己，也不能用带这轮对话历史的 fork。
2. 最多 3 轮，连续 2 轮无新意见就停——不是"直到满意为止"。
3. 只收敛用户点名的、或自己这轮真的改过的 scope。别人开的 scope 只报告，不代判。
