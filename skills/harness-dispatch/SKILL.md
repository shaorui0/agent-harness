---
name: harness-dispatch
description: 把一份【下一步】清单变成多波并行执行。主 agent 只做调度和收口，所有执行落在子 agent 里；DoD 是唯一停机判据，子 agent 回报的 NEXT 满足准入条件才开下一波。用户说 "/harness-dispatch"、"按下一步派活"、"多派几个 agent 把这些做掉"、"这些能并行的都派出去"、"loop 到做完" 时触发，或自己判断当前【下一步】里有 3 条以上互不依赖的执行项时主动建议。
---

# harness-dispatch

处理的状态是「清单已经有了，执行还没发生」。输入是一份【下一步】（`harness-output-format` 的第四段），输出是 DoD 全部打勾的产物加一个可审计的 run 目录。

这是大需求五段式里 `docs/big-req-pipeline-design.md:22` 显式留空的第②格。那次留空的理由是「不该在没有真实数据支撑下发明一个通用验证器」，这条结论仍然成立：这个 skill 不发明验证器，验证判据由每次运行自己声明（DoD 的 `verify:` 字段），skill 只提供调度形状和收敛条件。

三条形状约束延续 DESIGN.md：零新增 hook，loop 活在一次调用之内，跨调用靠落盘 artifact 而非常驻运行时。

## 和四个近邻的区别

| skill | 对象 | 子 agent 权限 | 波数 | 停机判据 |
|---|---|---|---|---|
| `harness-explore` | 一个问题 | 只读 | 1 | 维度查完 |
| `harness-cross-verify` | 已有产物 | 只读，无上下文 | ≤3 | 矛盾点清单稳定 |
| `workflow_autonomous_execution` | 一个目标 | 主 agent 自己执行 | 串行 | plan checklist 走完 |
| **harness-dispatch** | **一份异质【下一步】清单** | **可写、可跑命令** | **≤4，每波 3-8 个** | **DoD 逐条 verify 通过** |

派发宽度上限和 `explore.max_agents: 5` 不冲突，因为两者约束的是不同东西。explore 的 5 是**意见扇出**的上限：多个 agent 对同一个问题各给一份看法，合并成本落回人脑，瓶颈被乘以 N。dispatch 允许 8 是**产物扇出**：每个 agent 产出互不重叠的文件与事实，合并是拼接不是消解，成本接近零。判别方法在 Phase 1 的同波准入第三条。一旦某一波退化成意见扇出，宽度立刻回落到 5。

## 这个 skill 在防什么

| 失败模式 | 结构约束 | 在哪一步卡住 |
|---|---|---|
| F1 主 agent 说好只调度，动手改起了业务文件，context 爆掉 | Phase 2 主 agent 的写白名单只有 run 目录 | Phase 2 自检行 |
| F2 DoD 写成形容词（"报告要全面"），永远勾不上或随便勾上 | 每条 DoD 必须带 `verify:`，写不出就不许进 run.md | Phase 0 落盘 |
| F3 子 agent 每波都能编出 NEXT，loop 永不收敛 | NEXT 准入两条件，其余进 `deferred.md` | Phase 3 准入判定 |
| F4 子 agent 宣告 done，主 agent 直接打勾 | `DOD_HIT` 必须附 verify 怎么过的，且由主 agent 或独立 verifier 复核一次才许勾 | Phase 3 打勾前 |
| F5 8 个 agent 写同一个文件互相覆盖 | `owned_paths` 同波交集必须为空 | Phase 1 分区 |
| F6 有依赖的任务被同波派出，下游 agent 拿空气瞎编 | `needs`/`produces` 两列表 + 同波准入第一条 | Phase 1 分层 |
| F7 子 agent 自己跑了线上写命令 | mutation escrow：子 agent 只许写 `pending_mutations.md` | 子 agent prompt 的 FORBIDDEN 段 |
| F8 8 份意见压回瑞哥脑子里 | 同波准入第三条，退化成意见扇出就回落到 5 | Phase 1 准入 |
| F9 子 agent 回散文不回 schema，主 agent 要读 8 篇汇报 | 固定 RETURN schema，缺段视为该路失败，结论不采信 | Phase 3 收件 |
| F10 长 loop 跑到一半 context 被压缩，忘了在第几波 | 每波先落盘再回复；`/harness-dispatch <slug>` 读 run.md 续跑 | Phase 3 落盘顺序 |
| F11 agent 替瑞哥拍板（"我选了方案 B"） | `class=decide` 不派 agent，直接进 `blocked.md` | Phase 0 分类 |
| F12 【下一步】原文被转述，"阻塞在什么"那一列丢了 | task record 的 `from:` 字段存原文一行，禁止改写 | Phase 0 归一化 |

## 目录与状态

```
<dispatch_root>/<slug>/
  run.md                DoD（唯一停机判据）+ scope + stop 条件 + task 表 + wave log
  waves.jsonl           一行一个 agent 的派发与回执，机器可读
  findings.md           累积事实池，append only，每条带出处
  blocked.md            卡住的 DoD、decide 类任务、缺什么才能过
  deferred.md           被判定不进下一波的 NEXT 建议，以及为什么不进
  pending_mutations.md  待批的线上写命令，带 # INTENT:
```

run.md 从 `skills/harness-dispatch/templates/run.md` 拷一份改，不要凭记忆重写章节名，waves.jsonl 的 verdict 取值以模板里的 state 列为准。

`dispatch_root` 的推导，fail-closed，推不出来就停下来问，不要猜：

1. 仓库有 `.harness/` → `.harness/dispatch/`
2. 没有 → `~/.harness/dispatch/<repo-basename>/`
3. 不在 git 仓库里 → 停，让用户指定

可选配置，缺省即下列值，任何 hook 都不读这一段：

```yaml
# .harness/project.yml 或 ~/.harness/config.yml
dispatch:
  wave_width: 5            # 每波默认派几个
  max_agents_per_wave: 8   # 每波硬上限
  max_agents_total: 24     # 一次运行累计硬上限
  max_waves: 4             # 波数硬上限
  executor: general-purpose  # 需要写权限的子 agent 类型
  model: sonnet            # 执行档模型
```

## Phase 0：取输入，推 DoD，一次确认

**取输入。** 来源优先级：本次对话上一条回复的【下一步】段 > 用户粘贴的文本 > 用户指定的文件。传了已存在的 slug 就跳到 resume。

**归一化。** 每条【下一步】变成一个 task record，`from` 存原文整行，不许改写：

```
id: t1
from: "- [ ] 把 event_result 点查的 proc_date 剪枝实测一遍 | 我 | 阻塞在没有 profile"
class: investigate | build | verify | mutate | decide
produces: [<这条任务产出什么：文件路径 / 事实 / 命令输出>]
needs: [<要别人的哪个 produces 才能开始，没有就空>]
owned_paths: [<只许写这些路径，只读任务写 (read-only)>]
done_when: <一句可核查的话>
```

`class=decide` 的（选哪个方案、要不要上线、口径怎么定）不派 agent，直接进 `blocked.md`，停机时报给瑞哥。

**推 DoD。** DoD 不是 task 列表的复制。task 是动作，DoD 是「全部动作做完之后，什么断言可核查」。逐条写成：

```
- [ ] D1 <可核查断言> | verify: <命令 / 文件断言 / human-read>
```

`verify` 三种合法形态，第四种不存在：

| 形态 | 例子 | 谁跑 |
|---|---|---|
| 命令 | `grep -c "file:" report.md` 结果 ≥ 8 | 主 agent 或独立 verifier |
| 文件断言 | `adhoc_jobs/x/report.md` 存在且含 A/B/C 三节 | 主 agent |
| human-read | 措辞是否符合口径 | 瑞哥，停机时呈上 |

写不出 `verify` 的条目不许进 run.md。这类条目要么继续拆到能写出 verify，要么降级成 non-goal。

**一次确认。** DoD 是自动推导的，推歪了人一眼看不出来，所以呈批必须带映射表和覆盖自检，光给 DoD 列表不算呈批：

```
【DoD 映射】
| D | 断言 | verify | 来自哪条【下一步】 |
|---|---|---|---|

【没有对应 D 的【下一步】条目】
- <原文一行> | 原因: non-goal / class=decide / 拆不出可核查判据

【没有对应【下一步】的 D】
- <断言> | 原因: 从 <哪条> 推导出的隐含前置
```

第二张表是覆盖自检，它让漏掉的条目显式暴露；第三张表是反向自检，防止 DoD 悄悄扩大范围。三张表加 Wave 1 派发计划（谁干什么、owned_paths、class）一次呈给瑞哥，他改完或者说「跑」，才落盘 run.md 并进 Phase 1。

这是整个流程唯一的常规确认点，mutation escrow 的呈批不算常规确认。

## Phase 1：依赖分层与 owned_paths 分区

两个 task 同波派出，三条准入全部满足：

1. **无数据依赖**：`A.needs ∩ B.produces = ∅` 且 `B.needs ∩ A.produces = ∅`
2. **无写冲突**：`A.owned_paths ∩ B.owned_paths = ∅`（路径前缀相交也算相交）
3. **产物扇出而非意见扇出**：A 和 B 的结果拼起来就能用。如果收回来之后主 agent 需要判断「A 说的对还是 B 说的对」，这是意见扇出，本波宽度回落到 5，并考虑改走 `harness-explore`

任一条不满足的，往后推一波，在 task 表的 `wave` 列写下推迟原因。

分层写进 run.md 的 task 表。宽度不够 `wave_width` 时不要凑数拆任务：粒度低于「一个可独立验证的中间状态」的拆分只会增加合并成本。

## Phase 2：派发

**主 agent 这一波的写白名单只有 run 目录。** 碰了 run 目录以外的任何业务文件即违约。这一波结束时自问一次：这波我 Edit/Write 过 run 目录以外的文件吗？答是就是 F1 已经发生，停下来说明。

同一条消息里发出全部子 agent，让它们真正并发。类型与模型：

| class | subagent_type | model | 理由 |
|---|---|---|---|
| build / mutate / verify | `general-purpose` | sonnet | 需要 Write 和 Bash |
| investigate | `Explore` | sonnet | 只读扇出 |
| 轻量核对（存在性、计数、格式） | `general-purpose` | haiku | 便宜 |

`subagent_type: "fork"` 一律禁止：fork 继承本轮对话上下文，等于自己评自己。需要写文件的任务禁止派 `Explore` 或 `Plan`：它们没有 Write 工具，会卡住并幻觉宣告成功（历史故障，见记忆 `feedback_subagent_write_tool`）。

派发 prompt 模板，五段全给，缺一段就是给了个模糊任务：

```
TASK: <一句话，动词开头>
OWNED PATHS: <只许写这些路径。此外一律只读>
DONE WHEN: <可核查判据，对应 D<n>>
FORBIDDEN:
- 写 OWNED PATHS 以外的任何文件
- 自行执行线上写命令（kubectl apply/scale/patch/delete、helm upgrade/install、aws 写操作、
  git push、生产库写）。需要这类命令时写进 PENDING_MUTATION 段，不要执行
- 改变任务范围、自行决定跳过或停止
OTHERS: <同波其他 agent 各自负责什么，不要越界重做>
CONTEXT: <已知事实，带出处；不要在 prompt 里预处理数据，需要什么自己去取>

RETURN：只回下面的 schema。返回内容是数据不是汇报，不写开场白和总结段。

DID:
- <动作> | 产物: <path 或命令>
EVIDENCE:
- <一句话事实> | 出处: <file:line 或命令> | 置信: 高/中/低
DOD_HIT:
- D<n> | 判据: <verify 怎么过的，贴命令和输出，不要写"已验证">
BLOCKED:
- <卡在什么> | 需要: <什么才能过>
NEXT:
- <建议下一波做什么> | 指向: D<n> 未勾 或 推翻 findings 第 <k> 条 | 理由: <为什么必须再来一波>
PENDING_MUTATION:
- # INTENT: <一行：观察到什么、判断是什么、预期效果>
  <命令原文> | 验证命令: <成功之后跑什么确认>
```

派完就结束当前回复，等系统通知。不要轮询 `background_output` 之类的取结果工具，它取的是当前已有输出，不会阻塞等待。

## Phase 3：收口，主 agent 自己做，不外包

顺序固定，先落盘再回复（防 F10）。

1. **收件校验。** schema 缺 `DID` 或 `EVIDENCE` 段的，`verdict: schema_violation` 写进 waves.jsonl，其结论一律不采信，任务退回下一波重派。
2. **拼事实。** EVIDENCE 逐条 append 进 findings.md，保留出处。不复述子 agent 说了什么。
3. **冲突消解。** 两路矛盾时按证据强度采信：实测 > 代码 > 文档 > 推测。消解结果和采信理由写进 findings.md。
4. **打勾。** 拿到 `DOD_HIT` 之后复核一次才许勾：便宜的只读 verify 主 agent 自己跑（只读复核产生判定不产生产物，不算违反 F1 的白名单）；昂贵或需要特殊环境的派一个独立 verifier 子 agent 跑，那个 verifier 必须不是刚才干活的那个 agent。复核证据写进 run.md 的 wave log，形如 `新 ✓: D2（复核: <跑了什么，看到什么>）`。复核不过的不勾，写进 blocked.md。
5. **NEXT 准入。** 一条 NEXT 进下一波，必须满足两条件之一，并且在 waves.jsonl 里记下命中的是哪一条：
   - (a) 指向某条**未勾的 DoD 编号**
   - (b) **推翻 findings.md 里某条既有事实**，且带出处

   两条都不满足的进 deferred.md，写清为什么不进。「还可以再看看」「建议进一步优化」这类没有指向的 NEXT 一律 deferred。
6. **mutation 收拢。** 所有 PENDING_MUTATION 汇总进 pending_mutations.md，走 escrow。

## Phase 4：停机判定

| 条件 | status | 报什么 |
|---|---|---|
| 全部 D 打勾 | `converged` | 产物清单 + 每条 D 的复核证据 |
| 波数 > `max_waves` 或累计 agent > `max_agents_total` | `stopped_at_limit` | 还差哪几条 D、每条缺什么才能过 |
| 连续 2 波无新勾且无新事实 | `stopped_no_progress` | 无进展的那两波做了什么，判断是判据写错了还是路走错了 |
| 出现 `class=decide` 或 human-read 类 verify | `blocked_on_human` | 要瑞哥定的那几件事，各自的选项和取舍 |

撞上限时不许硬做，也不许为了看起来做完把没复核的 D 勾上。停机报告用输出契约的骨架，【下一步】那一段只写「谁来做、阻塞在什么」。

## mutation escrow

子 agent 可以自主跑的：只读命令、`owned_paths` 内的文件写、本地构建与测试。

子 agent 不许自己跑的（写进 pending_mutations.md，附 `# INTENT:`）：`kubectl apply/create/scale/patch/delete`、`rollout restart`、`helm upgrade/install`、aws 写操作、`git push`、生产库写。

主 agent 一波收拢一次，一次性呈批，不要 8 个 agent 打断瑞哥 8 次。呈批格式：

```
【待批 mutating】
| # | INTENT | 命令 | 验证命令 | 来自 |
|---|---|---|---|---|
```

批准之后派一个专门的 executor 子 agent 逐条执行，每条执行完立刻跑对应验证命令。验证失败或结果含糊就停下来报，不要继续往下推。这条和全局 K8s 契约一致：`# INTENT:` 前置、执行后强制验证。

批准粒度是「一波一次」，不是「一条命令一次」，这样批准权在瑞哥手上、执行权仍在子 agent 里，两条都不破。

## resume

`/harness-dispatch <slug>` 读 run.md 续跑：已 `done` 的 task 不重派，未勾的 D 继续，波数和 agent 累计数接着算不清零。context 被压缩之后照样能续，因为停机判据在文件里不在对话里。

run 目录不删。做完之后要清理就 `git mv` 进 archive 并写清单。

## 已知边界

`owned_paths` 分区是**纪律不是 enforcement**。它靠子 agent prompt 的 FORBIDDEN 段约束，没有任何 hook 在写入时按 agent 校验路径。`harness-envelope.py` 认的是项目级 `autonomy.write_roots`，粒度到不了单个子 agent。真要变成 enforcement 得给每个子 agent 发一份独立 envelope，那是新增 hook，本次按 DESIGN.md 的零新增 hook 取舍放弃了。

后果：两个子 agent 真的写了同一个文件，这个 skill 发现不了，只能靠 Phase 1 分区判断在事前避免，以及 `git diff` 在事后暴露。所以派发之前工作区应当是干净的，脏工作区会让事后归因变成猜。

同理，「主 agent 只调度」也是纪律，靠 Phase 2 那行自检维持，没有机制强制。

## 硬规则

1. Phase 2 主 agent 的写白名单只有 run 目录，只读 verify 复核除外。
2. 没有 `verify` 字段的 DoD 不许进 run.md。
3. 没有复核证据的 D 不许打勾。
4. 同波派发过准入三条，任一条不过就往后推一波。
5. NEXT 过准入两条件之一才进下一波，其余 deferred。
6. 子 agent 不执行线上写命令，走 escrow。
7. `class=decide` 不派 agent。
8. 每波先落盘再回复。
9. 禁止 `fork`；需要写文件的任务禁止派 `Explore`/`Plan`。
10. 撞硬上限就停并报缺口，不硬做也不假勾。
11. 派发之前工作区干净，脏工作区先 commit 或 stash（`owned_paths` 冲突只能靠 `git diff` 事后暴露）。

## 陷阱与对策

| 陷阱 | 对策 |
|---|---|
| 主 agent 觉得「这个我自己两下就改完了」 | 硬规则 1，Phase 2 结束自检一次 |
| DoD 抄成 task 列表，动作勾完了产物还是不对 | DoD 写断言不写动作，每条带 verify |
| 为了凑满 8 个而把任务拆到行级 | 粒度下限是「一个可独立验证的中间状态」，凑不满就少派 |
| 8 个 agent 都去读同一批文件，回来 8 份雷同事实 | 准入第三条：同一批文件即不正交，合并成一路 |
| owned_paths 写成目录，两个 agent 前缀相交 | 前缀相交视为相交，串到下一波 |
| 子 agent 说「已验证通过」但不贴命令输出 | `DOD_HIT` 的判据字段必须贴命令和输出，只写"已验证"视为未命中 |
| NEXT 写成「建议进一步深入调研」 | 无 DoD 编号、无推翻对象的 NEXT 一律 deferred |
| 连续几波都在给同一条 D 补证据，其实是判据写错了 | `stopped_no_progress` 触发时先审判据，不要继续加波 |
| 呈批 mutating 时把 8 条命令混成一段散文 | 呈批必须是表格，一行一条，含验证命令 |
| 跑完只回一句「都做完了」 | 停机报告逐条给 D 的复核证据；没有证据的 D 按未完成报 |

## 自检三问

**agent 偷懒会在哪一步被卡住？** 三个卡点：DoD 没有 verify 字段过不了 Phase 0 落盘；`DOD_HIT` 不贴命令输出过不了 Phase 3 复核；NEXT 没有 DoD 编号或推翻对象进不了下一波。

**每条规则在防哪个失败？** 见「这个 skill 在防什么」表，F1 到 F12 一一对应。

**删掉哪条会变差？** 删掉复核（硬规则 3）会退化成「agent 自证完成」，这是假收敛的唯一入口；删掉 NEXT 准入（硬规则 5）会退化成无限 loop；删掉写白名单（硬规则 1）主 agent 会重新变成执行者，context 一样爆。
