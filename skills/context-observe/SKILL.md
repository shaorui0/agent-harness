---
name: context-observe
description: 扫描一个项目自上次观测以来发生的事（git log / .harness journal / Claude Code 自动记忆 / 手写日记），按🔴🟡🟢分级 append 进 .context/OBSERVATIONS.md，幂等、只记录不判断去留。用户说 "/context-observe"、"记一下今天干了什么"、"扫一下这段时间的动静" 时触发；也由 periodic/context-observe-cron.sh 定时调用。
---

# context-observe

`.context/` 的 L1 Observer。目标不是「当前状态是什么」（那是 `.harness/STATE.md` + `/harness-compact` 的事），
而是「这段时间发生过什么、有哪些值得留下来的模式」。append-only，WAL 风格：写入阶段只归类，不判断该不该留、该不该晋升。

## 前置

当前目录（或其上级）必须有 `.context/`。没有就先跑：

```bash
/Users/rshao/work/context-infrastructure/projects/agent-harness/bin/harness context-init .
```

## 步骤

### 1. 幂等检查

读 `.context/.last-observe`。没有 stamp 就当作首次全量观测（窗口 = 有 git 历史以来，但只挑近期有代表性的）。
有 stamp 就窗口 = stamp 之后。

再读 `.context/OBSERVATIONS.md` 尾部，如果已经有 `Date: <今天>` 的 header，直接跳过，不重复扫描，回复一句 `skip: 今天已有条目`。

### 2. 收集输入（按 project.yml 的 sources 开关，逐路收集，不要漏）

读 `.context/project.yml` 看哪些源开着。四路互相独立，缺一路就跳过那一路，不报错：

- **git_log**：`git log --since=<窗口起> --stat` 看这段时间的 commit 和改动面。commit message 比 diff 更可靠地说明「为什么」。
- **harness_journal**：如果存在 `.harness/journal/*.jsonl`，读窗口内的记录（Stop hook 每轮无条件写的那份 WAL）。这是「过程中做了什么」的原始记录，比记忆更细粒度。
- **claude_memory**：计算这个项目的 Claude Code slug（绝对路径把所有非字母数字字符换成 `-`），读：
  ```bash
  ls ~/.claude/projects/ | grep -i <项目名关键词>
  cat ~/.claude/projects/<slug>/memory/MEMORY.md
  ```
  同一个仓库从根目录和从子目录进入会落到不同的记忆目录，`project.yml` 的 `claude_memory.extra_roots` 里列了的都要按同样方式各扫一遍。只看窗口内新增或修改的记忆文件（按文件 mtime）。
- **manual_journal**：`.context/journal/YYYY-MM-DD.md`，窗口内如果用户手写了当天笔记，整篇当一路输入（这是人话，权重比自动扫描高，出现矛盾时优先信这个）。

调查量大时按上面四路并行派 subagent，每路只回结构化清单（时间、来源、一句话内容），不要回叙述。分类判断留给你自己做，不要外包。

### 3. 分级分类

把收集到的每一条按交通灯打标：

- 🔴：跨项目通用的方法论 / 硬约束或红线 / 影响全局架构方向的决策。这类条目未来大概率要晋升到某份权威文档，先记下来。
- 🟡：这个项目当前活跃部分的关键进展、有权衡的技术决策、未来几周还用得上的背景。
- 🟢：日常执行流水、一次性 debug、只对当天有效的上下文。

判断依据是**复用价值**，不是「花了多少功夫」。一次性解决的报错即使调查了很久也是 🟢；一句话总结出的可复用规律即使当时只讨论了两句也是 🔴。

信息密度优先：宁可少记，不凑数。同一件事在多路输入里重复出现，合并成一条,不要重复。

### 4. 追加

在 `.context/OBSERVATIONS.md` 末尾追加：

```markdown
## Date: YYYY-MM-DD

- 🔴 <一句话，带出处：commit sha / journal 时间 / 记忆文件名>
- 🟡 <...>
- 🟢 <...>
```

**只 append，不改写、不删除已有内容。** 这一步不做 GC，不做晋升判断——那是以后 `/context-reflect`（还没建）要做的事，
现在只负责把原始信号变成结构化记录。

写完更新 stamp：

```bash
date -u +%FT%TZ > .context/.last-observe
```

### 5. 提交（如果这个仓库本来就会把 .context/ 纳入版本控制）

`.context/` 是否进 git 由 `harness context-init` 时是否传了 `--local-ignore` 决定，跑之前先看一眼：

```bash
git check-ignore -q .context/OBSERVATIONS.md && echo "local-only, 不提交" || echo "tracked, 需要提交"
```

tracked 的话一次 observe 一个 commit：

```
context observe: <项目名> <窗口起>~<窗口止>

OBSERVATIONS: +<n 条>（🔴 x / 🟡 y / 🟢 z）
```

### 6. 报告

```
========= 【本次观测】 ======================
- 窗口：<起> ~ <止>
- 扫描源：git_log(n commits) / harness_journal(m 条) / claude_memory(k 个文件) / manual_journal(有/无)
- 新增：x 条（🔴 a / 🟡 b / 🟢 c）

========= 【🔴 值得关注】 ====================
- <条目>（出处）

========= 【下一步】 ========================
- 攒够几轮之后跑 /context-reflect 做晋升（未建）
```

## 硬规则

1. 只 append，不改写、不删除 `.context/OBSERVATIONS.md` 里已有的内容。
2. 分类判断留给自己做，subagent 只回结构化清单，不外包判断。
3. 判不准归哪个交通灯的，宁可标 🟡 存疑，不要漏记也不要瞎归 🔴。
4. 同一条信息在多路输入里重复出现，合并成一条，不写两遍。
5. 每天同一个窗口只写一次，幂等检查没过就直接跳过，不追加第二份 `Date:` header。
