---
name: harness-openspec
description: 用 OpenSpec 管「要动代码/配置、≥5 步、可能跨 session」的单个 change 的任务分解与进度（proposal→specs/design→tasks→★任务评审→apply loop(多 subagent 并行)→verify→证据落盘→archive）。只做执行层，不替代 .harness/scope 的边界澄清，也不替代 14-backlog.md 的全局缺口表。用户说 "/harness-openspec"、"用 openspec 管这个"、"这个 change 拆一下任务"、"openspec 和 scope gate 怎么配合" 时触发；或自己判断当前任务符合下面「该用」的三条判据时主动建议（建议，不自动开）。
---

# harness-openspec

> 若同目录存在 `LOCAL.md`（私有补充，已 gitignore），先读它：里面是本文占位符（`<公司仓>`、`<项目 backlog 文件>` 等）的真实值。

OpenSpec（`@fission-ai/openspec`，全局已装）在这套 harness 里**只占一个格子：一个 change 的任务清单 + 逐条进度**。
边界澄清、收敛判定、全局缺口、当前事实这四件事已经各有归属，**不要用 openspec 再做一遍**。

## 1. 职责边界（先看这张表，再决定要不要开）

| 要解决的问题 | 归谁 | openspec 的位置 |
|---|---|---|
| 开工前把边界问清楚（goal / non_goals / acceptance / **covers**） | `.harness/scope/<slug>.yml` + `/harness-scope-gate` | ❌ 不替代。`covers` 是 PreToolUse (`harness-envelope.py`) **唯一**认的字段，openspec 没有等价物 |
| 做完之后的收敛判定（CLEARED / NOT_CLEARED） | `/harness-spec-review <slug>` | 🟡 `/opsx:verify` 是 change 内自检，**不是**收敛判定，两者都跑 |
| 全项目缺口的单一入口（`A*`/`B*`/…/`Q*`） | `<项目 backlog 文件>` | ❌ 不替代。openspec 的 change 列表不是第二张 backlog |
| 当前有效事实 / 已被推翻的旧结论 | `CLAUDE.md` + `.harness/STATE.md` + `docs/` | ❌ 不替代。`openspec/specs/` 在本仓基本用不上（见 §4 的 `--skip-specs`） |
| **一个 change 的任务分解 + 逐条完成状态 + 跨 session 续接** | **（此前没有）** | ✅ **这是开它的唯一理由** |

## 2. 该用 / 不该用

**该用**——三条同时成立：
1. 要真改东西（代码 / chart values / Job / 脚本），不是调查或写文档；
2. 任务数 ≥5，且有先后依赖，光靠 acceptance_criteria 列不清；
3. 大概率跨 session 或跨天，需要"回来能知道做到第几条"。

典型合格对象：某个服务的功能改动、多组件 patch、部署配置的结构性改动、多阶段上线。

**不该用**：
- oncall / 线上排查 / 一次性只读探针 → 直接干，结束后按 CLAUDE.md §「Case 沉淀」写 `cases/`；
- 纯文档修订、口径勘误 → 直接改，别过四道 artifact 门；
- 单步或两步的改动 → 一个 scope artifact 就够，开 openspec 是纯开销；
- 想拿它当"我这个项目还欠什么"的总表 → 那是 `<项目 backlog 文件>`，**不要分叉**。

## 3. 目录与 git 约定（本仓已配好，别改）

| 路径 | 状态 |
|---|---|
| `<公司仓>/openspec/{specs,changes,changes/archive}/` | 被该仓的 `.git/info/exclude` 挡住，**不进公司仓** |
| `<公司仓>/.claude/{skills/openspec-*,commands/opsx/*}` | 被该仓 `.gitignore` 的 `.claude/` 挡住 |
| 本 skill | `~/.claude/skills/harness-openspec/`（全局，跟 `harness-*` 一家） |

设计取舍：openspec 产物是**本地流程状态**，和 `.harness/` 同性质（`project.yml` 写着"对公司仓
零 tracked change"）。代价是**它不持久**——所以有 §6 硬规则 1。

## 4. 标准流程

openspec 自带 10 个 skill + 10 个 `/opsx:*` 命令，**别重写它们**，直接调：

```
/opsx:new <slug>        # 建 change 骨架（openspec/changes/<slug>/.openspec.yaml）
/opsx:continue          # 生成下一个 artifact，按门序自动选
/opsx:apply             # 按 tasks.md 实施，逐条打勾
/opsx:verify            # 归档前自检：实现 vs artifact 是否一致
/opsx:archive           # 收尾，change → changes/archive/
/opsx:explore           # 还没想清楚时先进探索模式，不要急着建 change
/opsx:ff <slug>         # 一把生成全部 artifact（只在需求已经很清楚时用）
```

CLI（脚本里/要精确控制时用）：

```bash
openspec list                      # 当前 change
openspec list --specs              # 已有 spec
openspec status --change <slug>    # 四门进度，跨 session 回来第一条就跑它
openspec view                      # 交互式 dashboard
openspec validate --all --strict
openspec archive <slug> --skip-specs -y
```

**门序是 `proposal → {design, specs} → tasks`**，`tasks` 被前两个 block 住。
🔴 **本仓九成 change 应该 `--skip-specs` 归档**：infra / tooling / 迁移脚本类改动没有"capability
requirement"可写，硬凑 `openspec/specs/<capability>/spec.md` 只会造出第二个真相源，直接和 §1
最后两行冲突。`specs/` 只在明确要给某个服务定长期行为契约时才写（候选：某个服务的
恰好一次语义、某个调度器的状态机）。

## 5. 和现有 harness 的接线

```
需求模糊  → /opsx:explore（或 /harness-req-split）
   ↓
边界澄清  → /harness-scope-gate  → .harness/scope/<slug>.yml (status: confirmed, covers: [...])
   ↓                                  ↑ PreToolUse 只认这个                      ← Spec
任务分解  → /opsx:new <同一个 slug>  → proposal.md 里引用 scope artifact，不要把边界再问一遍
   ↓
★任务评审 → §5a  独立 reviewer（Fork）审 tasks.md → review.md   ← Hook（freeze 门）
   ↓  不过 → 回任务分解；有 Q → HITL 问瑞哥；过 → 才许 apply
实施 loop → §5c  Loop + 多 subagent（Fork）逐条做、逐条留证、逐条打勾   ← Loop
   ↓  每条 [x] 必须有 evidence/ 下的原始输出（§5b）
自检      → /opsx:verify
   ↓
结论回写  → docs/ + <项目 backlog 文件> + CLAUDE.md（§6 硬规则 1）
   ↓
收敛判定  → /harness-spec-review <slug>
   ↓
归档      → /opsx:archive <slug> --skip-specs（evidence/ 随 change 一起进 archive）
```

**slug 必须三处同名**：`.harness/scope/<slug>.yml`、`openspec/changes/<slug>/`、`/harness-spec-review <slug>`。
不同名等于同一件事在两套系统里各有一个身份，就是本仓最贵的那类 bug。

四原语对位（`bestpractice_agentic_control_primitives`）：

| 原语 | 在本流程里 | 落点 |
|---|---|---|
| Spec | scope artifact + proposal/design/tasks，tasks 里每条带 `verify:` | §5a 评审后冻结 |
| Loop | apply 阶段：观察 tasks.md → 派下一批 → 验证 → 打勾，直到无 `[ ]` | §5c |
| Hook | 任务评审门（apply 前）、HITL 门（风险/不确定）、证据门（打勾前） | §5a / §5b / §5c |
| Fork | reviewer、并行执行 subagent、独立 verifier，各自干净上下文 | §5a / §5c |

### 5a. 任务评审门（tasks 生成后、apply 前，必过）

tasks.md 写完**不许直接 apply**。派一个**无上下文的独立 reviewer subagent**（Fork：评审必须不共享生产者上下文，
只给它 `proposal.md`/`design.md`/`tasks.md`/scope artifact 的路径），产出
`openspec/changes/<slug>/review.md`，逐项检查：

| 检查项 | 不过的表现 |
|---|---|
| 覆盖 | scope 的 acceptance / proposal 的目标，有哪条没有任何 task 对应 |
| 够不够 | 缺回滚、缺验证、缺前置准备、缺收尾回写（§6 硬规则 1）的 task |
| 清晰度 | 一条 task 两个人会做出两种东西；动词模糊（"优化""处理"） |
| 可验证 | 没有 `verify:`，或 verify 是形容词而非可执行命令/可观测事实 |
| 依赖与并行 | 缺 `needs`；同批 task 的 `owns` 路径有交集 |
| 风险分级 | 触线上/集群/不可逆的 task 没标 🟢/🟡/🔴，没走 `# INTENT:` + 人工 review |
| 粒度 | 一条 task 跨 session 才能做完，或细到无意义 |

review.md 固定结构：`VERDICT: PASS | REVISE | BLOCKED`、`GAPS`（缺的 task）、`UNCLEAR`（不清楚的，写成
Q1/Q2…）、`RISKY`（需要人拍板的）。处理规则：
- `REVISE` → 主 agent 改 tasks.md 再送审，**最多 3 轮**，超了转 BLOCKED；
- `UNCLEAR`/`RISKY` 里属于瑞哥的决策 → **停下来问**，不替他选；
- `PASS` 才冻结 tasks.md 进入 apply。冻结后新增/改 task 要在 tasks.md 末尾 `## Changes after freeze` 留一行原因。

### 5b. 验证证据必须落盘（Evidence，瑞哥 review 的依据）

所有验证动作的结果都存进 change 目录，不留在对话里：

```
openspec/changes/<slug>/
  review.md                 §5a 评审结论（每轮追加，不覆盖）
  evidence/
    INDEX.md                一行一条：task-id | 验证内容 | 命令 | 结论(PASS/FAIL) | 文件
    <task-id>-<短名>.log    命令 + 时间戳 + 原始输出（原样，不转述、不截成"看起来正常"）
    <task-id>-<短名>.json   结构化结果/查询返回/截图路径（如有）
  loop.md                   §5c 每轮 loop 的记录（派了谁、结果、下一轮）
```

- `.log` 头三行固定：`# task: <id>`、`# cmd: <完整命令>`、`# at: <ISO 时间>`，其后是原始输出；
- FAIL 的证据也要存（失败→修→重跑，两份都留，文件名加 `-try1`/`-try2`）；
- tasks.md 里每条打勾必须能点到证据：`- [x] 2.3 … | evidence: evidence/2.3-xxx.log`；
- 🔴 **没有证据文件不许打勾**。subagent 回报 "done" 不算证据，要主 agent 或独立 verifier 打开 `.log` 复核；
- 敏感信息（token、密码、客户数据）落盘前脱敏——evidence 是给人看的，但不进公司仓；
- archive 时 evidence/ 整个随 change 进 `changes/archive/`，不要在归档前清理。

### 5c. 执行 = Loop + 多 subagent（apply 阶段）

review 通过后进入**真正的 loop**，不是"做一条停一下问"。DoD（唯一停机判据）：
**tasks.md 里没有 `- [ ]`，且每个 `[x]` 都有 evidence。** 机检：

```bash
grep -c '^\s*- \[ \]' openspec/changes/<slug>/tasks.md   # 必须为 0
```

loop 一轮（每轮在 `loop.md` 追加一段）：

1. **观察**：读 tasks.md，列出未勾项；
2. **分批**：选出 `needs` 已满足、`owns` 路径互不相交的 task 作为本批（有 ≥2 条就**必须**并行派多个 subagent，每条/每组一个，`run_in_background`，宽度 3–8；有依赖的下一批再派）；
3. **派发**：每个 subagent 的 prompt 带齐：task 原文、`verify:` 命令、`owns` 路径、证据落盘位置与格式（§5b）、FORBIDDEN（不许碰 owns 之外的文件、不许跑线上写命令——写命令写进 `pending_mutations.md` 等批）。详细的波次/准入/回执 schema 沿用 `harness-dispatch`，不要重写；
4. **收件**：subagent 回 `DONE | FAILED | BLOCKED` + 证据路径；缺证据视为失败；
5. **验证**：主 agent 打开 evidence 复核（任务多/风险高时派一个独立 verifier subagent 复核）；过了才勾 `[x]` 并更新 `evidence/INDEX.md`；
6. **回到 1**，直到 DoD 满足。FAILED 自动重试最多 2 次，仍失败转 BLOCKED。

**什么时候才许停下来问人（loop 的唯一出口之外的停机条件）：**
- 遇到**不确定**：task 有歧义、两种做法结果不同、证据结论模棱两可；
- 遇到**风险**：🔴 级动作、线上/集群写操作、不可逆、花钱、碰 covers 之外的路径；
- 预算耗尽：总 agent 数 > 24 或 轮数 > 6。
除此之外**不要**中途汇报求确认——该做的自己做下去。被 BLOCKED 的 task 先跳过、继续跑其他不依赖它的 task，最后一次性汇报
阻塞清单（沿用 `harness-dispatch` 的 `blocked.md` 语义，写进 `loop.md`）。

**证据门脚本**（每次打勾后、以及宣告 DoD 满足前必跑）：

```bash
python3 -I ~/.claude/skills/harness-openspec/scripts/check_evidence.py <slug> --root <项目根>
# exit 0 = 全勾且全有证据；1 = 有 [x] 缺证据/文件空/缺 # cmd: # at: 头/缺 INDEX.md；2 = 证据都对但仍有 [ ]
```

exit 1 时该条打勾无效，退回改成 `[ ]` 重做。脚本只查"证据存在且格式合规"，内容是否真能证明仍由主 agent/verifier 打开 `.log` 判断。

收尾汇报只给：DoD 是否满足、未勾项及原因、`evidence/INDEX.md` 路径、待批事项。

## 6. 硬规则

1. 🔴 **归档前必须把结论回写到 tracked 的文件**——`docs/`、`<项目 backlog 文件>`、
   `CLAUDE.md` 之一。`openspec/` 不进 git，只写在那里等于没写。**没回写不许 archive。**
2. 🔴 **不新增编号命名空间**。引用缺口一律用 backlog 文件里的原编号、沿用已有的发现/问题编号。change 用 kebab-case slug，**永远不给 change 编号**
   （多套编号相撞的代价很高）。
3. 🔴 **openspec 不管集群操作的安全门**。`# INTENT:` 注释、🟢/🟡/🔴 分级、分阶段 + 人工 review、
   长跑任务必须 in-cluster Job，这些是 CLAUDE.md §7 的规则，`tasks.md` 里也照写照做，
   打勾不代表跳过 review。
4. 一次只开**一个** change。`openspec list` 出现两个以上未归档的，先收掉一个再开新的。
5. 完全 opt-in：不挂 hook、不做强制、不在 session 开头自动检查。判断不该用就别开。（**开了之后**，§5a 评审门、§5b 证据、§5c loop 都是硬流程，不是 opt-in。）
5a. 🔴 **没过 §5a 评审不许 apply；没有 evidence 不许打勾；loop 中除"不确定/有风险/预算耗尽"外不许停下来问。**
6. 想清楚再建 change：需求还模糊时用 `/opsx:explore`，别先 `/opsx:new` 再对着空模板憋 proposal。

## 7. 已知坑

- **装了不等于会用**：曾有项目 init 过完整 openspec，几个月后 specs/changes/archive 三个目录全空、整套未进 git。**如果两周内一个 change 都没跑完，就是不合用，直接停，别养着**。
- **强制会立刻反噬**：scope_gate 默认从严那两天攒了 15 个 confirmed
  artifact、11 个从未收敛，"多数是为了解锁写权限临时补的"。所以硬规则 5 是 opt-in 不是保守。
- `openspec status` 不接位置参数，必须 `--change <slug>`（`openspec status <slug>` 直接报错）。
- `openspec init` 不动 `CLAUDE.md`，也不覆盖已有 `.claude/settings.local.json`（v1.1.1 实测）。
  但它会往 `.claude/` 铺 20 个文件；重装/升级前先确认那两处 gitignore 还挡得住。
- `openspec archive` 默认要求更新 specs，本仓请一律带 `--skip-specs`（见 §4）。
