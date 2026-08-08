---
name: harness-compact
description: 把项目的 WAL（.harness/journal）和散落文档收敛成一份当前有效的 STATE.md，把被推翻和过期的内容带时间线归档进 .harness/archive 并生成清单，全程 git 留痕。用户说 "/harness-compact"、"收敛一下项目状态"、"整理这个项目的文档"、"哪些结论已经过期" 时触发；也由 periodic/compact-cron.sh 定时调用。
---

# harness-compact

WAL + compaction 里的 compaction。写入阶段不做判断，判断全部集中在这里。

**唯一目标**：让下一个 session 只读 `STATE.md` 就知道当前状态，不必再翻历史、不必再自己判断哪条还有效。

## 前置

当前目录（或其上级）必须有 `.harness/`。没有就先跑 `/harness-onboard`。

工作区必须干净或者你明确知道未提交的改动是什么。compaction 会产生 git 操作，混在别人的改动里没法回溯。
脏工作区时先 `git status` 看一眼，把无关改动 stash 或让用户确认。

## 步骤

### 1. 划定窗口

读 `.harness/.last-compact`（不存在就是全量首次收敛）。窗口 = 上次收敛之后的所有 journal 记录。

```bash
ls .harness/journal/*.jsonl
cat .harness/.last-compact 2>/dev/null
```

顺带记下窗口内的 git log，它比 journal 更可靠地说明代码侧发生了什么。

### 1.5 把自动记忆也算成输入源

Claude Code 的自动记忆按 cwd 分目录存放，同一个仓库从根目录和从子目录进入会落到**不同的记忆目录**，
所以记忆天然是碎的，而且它经常比文档新：写记忆的门槛远低于改文档。

```bash
ls ~/.claude/projects/ | grep -i <项目名>
cat ~/.claude/projects/<slug>/memory/MEMORY.md
```

`<slug>` 是绝对路径把所有非字母数字字符换成 `-`。SessionStart 会把当前 cwd 之外的那几份索引注入进来，
compaction 要**逐个目录**扫，不要只看被自动加载的那一份。

记忆条目按两类处理：

- **比文档新**：并进 STATE.md，出处写记忆文件名和它的日期，并在报告里点出「文档尚未反映这条」
- **比文档旧或已被推翻**：不要删记忆文件（它不在 git 里，删了没法回溯），在报告的【记忆待修正】一节列出来，
  交给用户决定是改还是留

**权威顺序是 STATE.md > journal > 自动记忆。** 记忆记的是写下那一刻的事实，
引用前先核实它提到的文件、函数、参数是否还在。

### 2. 抽取（不判断，只归类）

把窗口内的 journal 记录 + `context_files` 指向的文档 + 上一步的记忆条目，按四类拆开：

- **新确立的事实**：有出处的实测结论、测量值、代码位置
- **新做的决策**：选了什么、放弃了什么、理由
- **推翻**：明确否定了之前某条结论的内容
- **仍开放**：提出但没结论的问题

调查量大时按正交维度并行派 subagent（journal / docs / git log 各一路），每路只回结构化清单，不要回叙述。
判断和合并留给你自己做，不要外包。

### 3. 判定覆盖关系

时间线上后出现的覆盖先出现的。对每一条旧结论问一句：**窗口内有没有证据推翻它？**

- 有 → 标记为被取代，记下取代它的是哪一条、什么时候
- 没有 → 保留
- 同一个问题反复讨论 → 合并成一条，保留最后的形态

判定不确定时**保留并标注存疑**，不要猜。compaction 宁可留一条待核的，也不能悄悄删掉一条对的。

### 4. 重写 STATE.md

按 `templates/STATE.md` 的骨架重写，不是追加。每一节：

- 现在在做什么：一到三句
- 已确立的事实：每条必须有依据（file:line、命令、或 journal 日期）
- 已做的决策：每条写清取代了什么
- 开放问题：编号，写清卡在哪
- 下一步 / 已知的坑

体积上限看 `project.yml` 的 `state.max_chars`（默认 8000 字符）。超了就是没收敛干净，回到第 3 步继续合并，不要靠调大上限解决。

### 5. 归档（硬规则）

被取代和过期的内容进 `project.yml` 里 `archive.dir` 指定的目录。

**开工前先确认归档目录被 git 跟踪**：

```bash
git check-ignore -q <archive.dir>/probe && echo "IGNORED — 停下"
```

命中 gitignore 就停下报错，不要继续。把被跟踪的文档 `git mv` 进一个被忽略的目录，
等于从仓库里静默删掉它们，而这正是这套机制要防的事。`harness doctor` 也会检查这一条。

**禁止 `rm`。** 一律：

```bash
mkdir -p .harness/archive/$(date +%F)
git mv <旧文件> .harness/archive/$(date +%F)/
```

文件不在 git 里时用 `mv` 然后 `git add`，让归档本身进版本历史。

同目录下写一份 `ARCHIVE-MANIFEST.md`，每条记录四列：

| 原路径 | 归档原因 | 被什么取代 | 推翻时间 |
|---|---|---|---|

只是文档里某一段过期（而非整个文件）时，把那一段剪进 manifest 的正文，原文件里留一行指向归档路径。

### 6. 提交

一次 compaction 一个 commit：

```
harness compact: <项目名> <窗口起>~<窗口止>

STATE: <新增 n 条事实 / m 条决策 / 关闭 k 个问题>
ARCHIVE: <归档 j 个文件，见 .harness/archive/<date>/ARCHIVE-MANIFEST.md>
```

然后写 stamp：

```bash
date -u +%FT%TZ > .harness/.last-compact
```

### 7. 报告

```
========= 【收敛结果】 ======================
- STATE.md：n 条事实 / m 条决策 / k 个开放问题，<字符数>
- 归档：j 个文件 → <archive.dir>/<date>/
- commit：<sha>

========= 【被推翻的结论】 ==================
- 旧结论 → 新结论（依据）

========= 【记忆待修正】 ====================
- <记忆文件> 说 X，已被 <依据> 推翻。未改动，等你决定

========= 【仍然存疑】 ======================
- 判不了的条目，以及需要什么才能判

========= 【下一步】 ========================
- xxx
```

## 硬规则

1. 不用 `rm`。删除一律走 `git mv` 到 archive。
2. 每条归档必须写「被什么取代」。写不出来说明还没判清楚，那就别归档。
3. STATE.md 里每条事实必须有出处。没出处的降级成开放问题。
4. 判不准的保留并标注，不要猜。
5. 一次 compaction 一个 commit，不要和功能改动混在一起。
