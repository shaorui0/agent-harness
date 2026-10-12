---
name: harness-onboard
description: 给一个已有的项目仓库装上 harness 机制：生成 .harness/project.yml（自主边界、权威入口）、从现有文档蒸馏出第一版 STATE.md、并提交。用户说 "/harness-onboard"、"给这个项目装上 harness"、"把这个 repo 接进来"、"这个项目也用这套机制" 时触发。
---

# harness-onboard

把一个已有仓库接进 harness。做完之后，这个目录下的每个 session 都会自动带上项目状态和边界。

## 步骤

### 1. 摸底（并行）

先搞清楚这个仓库是什么。按正交维度并行调查，参考 `/harness-explore` 的拆法：

- 仓库结构与入口文档（README / CLAUDE.md / docs 的索引）
- git log 最近的走向，当前分支，未提交的东西
- 已有的运维边界痕迹（生产环境名、危险命令、现有 hook 或 CI gate）

摸底的产出是三份清单：**权威入口候选**、**当前在做什么**、**危险动作有哪些**。

### 2. 装目录

```bash
/Users/rshao/work/context-infrastructure/projects/agent-harness/bin/harness init <repo-path>
```

生成 `.harness/{project.yml,STATE.md,journal/,archive/}`。

### 3. 填 project.yml

三件事需要判断，其余用默认值：

**context_files** — 权威口径的入口，指向仓库里真正说了算的那几个文件。只填路径，不要多。
一个仓库超过 3 个权威入口，说明它本身就没有单一事实来源，这一点要在报告里指出来。

**autonomy** — 这是最需要跟用户确认的一段。至少问清楚：

- 哪些环境可以直接动，哪些绝对不能碰（k8s context、集群名、数据库）
- 哪些只读命令希望免确认（这决定了日常有多少次无谓的停顿）
- 写操作是否要限定目录

**默认从严**：拿不准的一律不放进 `allow_prefixes`，放进 `deny_patterns`。
放宽一条的成本是下次多按一次回车，放松错一条的成本是动了不该动的东西。

**description** — 一句话，会出现在每个 session 开头。

### 4. 蒸馏第一版 STATE.md

从现有文档里提炼，不要凭空写。按 `templates/STATE.md` 的骨架填：现在在做什么、已确立的事实、已做的决策、开放问题、下一步、已知的坑。

这一步经常会发现仓库里有互相矛盾的说法（一份文档说 A，另一份说 A 已被推翻）。
**发现矛盾就记进开放问题，不要自己裁决**，然后在报告里列出来。真正的裁决交给第一次 `/harness-compact`。

### 5. 提交

```
harness onboard: <项目名>

.harness/ 已装：envelope <n 条 allow / m 条 deny>，权威入口 <k 个>
STATE.md 首版：<x 条事实 / y 个开放问题>
```

### 6. 报告

```
【已装】
- .harness/project.yml：envelope n allow / m deny，context_files k 个
- STATE.md 首版：x 条事实 / y 个开放问题

【需要你确认的边界】
- 问题：<比如 prod context 名字对不对>

【发现的矛盾】
- 文档 A 说 X，文档 B 说 X 已被推翻，未裁决，已记入开放问题

【下一步】
- 跑一次 /harness-compact 做首次收敛
```

## 硬规则

1. envelope 默认从严，拿不准的进 deny。
2. STATE.md 只从现有材料蒸馏，不凭空补。
3. 发现矛盾记进开放问题，不自己裁决。
4. 不动仓库原有的文档结构，这一步只增加 `.harness/`。真正的整理是 `/harness-compact` 的事。
