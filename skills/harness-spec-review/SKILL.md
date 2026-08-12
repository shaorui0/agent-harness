---
name: harness-spec-review
description: 对一个已经 confirmed 的 scope 做收敛判定——独立无上下文的 reviewer 打分，有限轮次收敛到 CLEARED / NOT_CLEARED / DONE_WITH_CONCERNS，写回 scope artifact 解开 Stop hook 的 spec-review guard。用户说 "/harness-spec-review"、"这个 spec 收个尾"、"看看这个改动做完没有" 时触发，或某个 scope 的所有改动做完之后主动跑一次。
---

# harness-spec-review

见 `docs/scope-and-spec-gate-design.md`：这是"收敛点判定"的强制层。`harness-stop-contract.py` 会在 Stop 时检查所有 `status: confirmed` 的 scope，`review.status` 没到 `CLEARED`/`DONE_WITH_CONCERNS` 就拦一次——这个 skill 是唯一能把状态推过去的地方。

## 步骤

### 1. 定位 artifact

`.harness/scope/<slug>.yml`。没传 slug 就扫 `.harness/scope/*.yml`，挑 `status: confirmed` 且 `review.status` 还没收敛的那些，一个一个来。

### 2. 独立 reviewer 打分

派一个**无上下文**的 reviewer subagent（`Agent` 工具，普通 agent 不是 fork——fork 会带着这轮对话的上下文，等于自己评自己，没有意义）。喂给它 `goal` / `non_goals` / `acceptance_criteria` / `covers`，让它对照实际的代码/文档改动，按 5 维度打分：

- Completeness — acceptance_criteria 每一条是否真的做到
- Consistency — 改动内部是否自相矛盾
- Clarity — 别人接手能不能看懂做了什么
- Scope — 有没有超出或漏掉 `covers` 声明的范围
- Feasibility — 剩下没做的部分是否可行

### 3. 有限轮次收敛

有问题就按 reviewer 的意见改，重派一轮。**最多 3 轮**。**连续 2 轮 reviewer 提的是同样的问题就停**，不再重试，把问题记进 `concerns`——这是防止无限循环撞上 Stop hook guard 的硬性上限，借的是 `autoplan` 的 Pre-Gate Verification 思路（有限重试，仍缺失就带警告继续，不是无限等）。

### 4. 写回 artifact

```yaml
review:
  status: CLEARED | NOT_CLEARED | DONE_WITH_CONCERNS
  rounds: <n>
  concerns: [...]
```

- `CLEARED`：acceptance_criteria 全部通过。
- `DONE_WITH_CONCERNS`：收敛了，但留了明确记录的未解决项——Stop hook 认这个状态，不会一直拦。
- `NOT_CLEARED`：还没收敛完就被打断，或者卡在同一个问题连续 2 轮——**不建议长期停在这个状态**，Stop hook 会持续提醒直到改成上面两个之一。

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
3. 没写成 `CLEARED` 或 `DONE_WITH_CONCERNS`，就还是"未收敛"，Stop hook 会一直拦。
