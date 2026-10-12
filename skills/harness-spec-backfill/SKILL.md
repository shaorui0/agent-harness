---
name: harness-spec-backfill
description: 从一个（或几个）已经发生过的 Claude Code session 里，倒推出它的 spec（goal / non_goals / AC / covers）和 status（每条 AC 的 PASS/FAIL/UNKNOWN + 证据），生成一份 `status: reconstructed` 的 scope 草稿，经用户确认后写入 `.harness/scope/`。用于把没走规范流程的历史工作补登进 tracker。用户说 "/harness-spec-backfill"、"把这个 session 的 spec 恢复出来"、"给这个 session 补个 scope"、"这些 unscoped 的活儿登记一下" 时触发。
---

# harness-spec-backfill（草案）

> 状态：在 1 个真实 session 上试跑过。`harness install` 之后才会软链进 `~/.claude/skills/`。产出必须人工核对。

**它是什么**：`session_recap` 的姊妹技能。recap 产出给人读的 briefing；backfill 产出给 tracker 读的 scope 文件。脚本只做机械提取，判断交给 agent，所以做成 skill 而不是纯脚本。

**最大的风险是事后诸葛**：先看做了什么，再倒推「目标就是这个」，结果每条 AC 都是 PASS。下面的硬规则主要就是防这一点。

## 输入

- `<session-id>`、`latest`，或几个 session id（同一件事分散在多个 session 里时）
- 可选 `--repo <path>`：scope 写到哪个仓库的 `.harness/`。默认取 session 的 cwd 往上找到的项目根
- 来源可以是 tracker 的 Unscoped 视图：那里每一行都给出一条可复制的 `/harness-spec-backfill <sid>`

## 步骤

### 1. 提取

```bash
python3 <agent-harness>/tracker/session_recap.py <sid> --cwd <session cwd> > "$SCRATCHPAD/recap-<sid>.md"
```

大于 40KB 的输出先落盘再 Read。需要人类原话时，用 `--full` 或者直接 grep transcript。

### 2. 切分：这个 session 里有几件「事」

按人类回合里表达的意图来切，不按工具动作来切。

- 0 件（纯问答、查资料、闲聊）：直接报告「无可登记的工作」并停止。**不要硬造 scope**。
- 1 件：继续。
- 多件：列出候选，每件给一行描述和对应回合号，让用户选要补哪几件。

### 3. 重建 spec：只看人类说了什么

| 字段 | 来源 | 规则 |
|---|---|---|
| `goal` | 人类回合的原话 | 在 `origin.intent_quotes` 里记下所依据的回合号 |
| `non_goals` | 人类明确拒绝或排除的内容（「不要」「先不」「别动」） | 没有就留空，不要编 |
| `acceptance_criteria` | 人类说过的完成标准 | 人类没说过的，可以补成可检查的条目，但每条都要标 `inferred: true` |
| `covers` | 实际改过的文件 | 写到文件，或者 ≤ 20 个文件的小目录。**不要写 `docs/plan/` 这种宽目录**（宽目录会产生假 drift，见 docs/tracker-design.md） |
| `risk_budget` | 改动是否可逆、是否触碰线上 | 一两句话即可 |

### 4. 重建 status：每条 AC 一个判定

判定优先级从高到低：

1. **`rechecked`**：在当前仓库里重新验证。验证手段包括看文件是否存在、grep、跑只读命令、跑测试。
2. **`transcript_claim`**：现在没法重新验证，但 transcript 里有对应的 Edit/Write/Bash 动作，并且有输出可以佐证。
3. **`UNKNOWN`**：上面两种都没有。

```yaml
ac_results:
  - ac: 0
    verdict: PASS | FAIL | UNKNOWN
    source: rechecked | transcript_claim
    evidence: "helm lint ... → 0 failed" / "file:line" / "回合 7 Bash 输出"
    commit: <验证时的 HEAD>
    checked_at: <ISO 时间>
```

这个结构与将来 `/verify` 写入的 `ac_results` 一致：backfill 是一次性补登，`/verify` 是之后持续更新。

### 5. 算 diff

```bash
git -C <repo> log --since <session 开始时间> --format='%h %ad %s' --date=short -- <covers>
git -C <repo> status --short -- <covers>
```

结果写进 `origin.commits` 和 `origin.dirty`。

### 6. 出草稿，等确认

先给用户看：一段五行以内的摘要（goal、AC 通过数/总数、几条 UNKNOWN、几条是推断出来的）+ 完整的 yml。**用户确认之后才写文件。**

```yaml
name: <slug>
status: reconstructed          # 用户确认后保持这个值；只有走过 scope-gate 的才是 confirmed
goal: ...
non_goals: [...]
acceptance_criteria:
  - text: ...
    inferred: false
covers:
  - {repo: <子仓库>, ref: <远端 ref>, paths: [...]}
scope_changes: ["T20 ...: ..."]
risk_budget: ...
origin:
  kind: reconstructed
  sessions: [<sid>, ...]
  transcript_paths: [...]
  intent_quotes: ["回合 1: ...", "回合 4: ..."]
  reconstructed_at: <ISO>
  commits: [...]
  dirty: [...]
ac_results: [...]
```

写入规则：

- 写到 `<repo>/.harness/scope/<slug>.yml`。
- **不覆盖已有 slug**。
- 如果已有 scope 的 `covers` 与这次重叠，不新建，改为把这个 session 追加到那个 scope 的 `origin.sessions`。这一步同样要用户确认。

### 7. 收尾（可选）

问用户要不要接着跑 `harness-spec-review` 做独立收敛判定。backfill 本身不写 `review.status`。

## 硬规则

1. **goal 和 AC 只从人类的话里来**，agent 做了什么只用来判 status。两者混用就是事后诸葛。
2. **「说做了」不等于「做了」**（沿用 session_recap 硬规则 1）。只在正文里声称过、没有对应动作的，判 `UNKNOWN`，不判 `PASS`。
3. **重新验证只能用只读手段**。需要碰集群、云资源或者要写东西才能验证的，判 `UNKNOWN`，并把验证命令列给用户，自己不执行。
4. **不编**。看不出来的就写「看不出」；推断出来的 AC 必须标 `inferred: true`。
5. **一次只写一个文件，先确认再写**。批量补登时也要逐个确认，或者给一份清单让用户一次性勾选。
6. **脱敏**。transcript 里可能出现 token、AK 这类明文（试跑时就碰到一个构建平台的 token 明文出现了 4 次）。任何密钥都不准写进 yml、草稿或者回复里。
7. **意图会漂移**。长 session 里范围会被收窄或改向好几次：goal 取最新的意图，历次变化写进 `scope_changes`（回合号加一句话），旧版本不要混进 AC。
8. **covers 以 commit 为准，不以主线程的 Edit 为准**。代码大多由 subagent 改、git 提交，主线程的 `files_touched` 会严重漏记。要用 session 时间窗内的 commit，并且锚定在远端 ref 上（本地 checkout 和 worktree 可能是旧的）。
9. **live session 只能做快照**：标上 `live_at_reconstruction: true`，之后可以重跑覆盖 `ac_results`。

## 试跑记录

- 2026-10-12：一个真实的 4.3 天 session（50 个人类回合、3 次 compact、大量 subagent）。切出 2 件事，草稿放在该 workspace 的 `.harness/backfill-drafts/`。第 6–9 条硬规则就是这次试跑总结出来的。
