# 开工前澄清 + 收敛点判定 —— 机制设计（Q1/Q2/Q3 已拍板，待架构评审）

来源：`/harness-explore` 三路调查（现状盘点 / Claude Code 机制盘点 / 方法论盘点）收敛结果。
这份文档是思考的落地，**不是实现计划**——按 DESIGN.md 的取舍，写代码前要先过一次 `plan-eng-review` 式评审。

## 背景

用户想要两件事：

1. 中大型项目开工前，强制把边界清晰的目标问题问完，才能进入分 spec 阶段。
2. 这个流程的终点/收敛点（spec 完成/收尾判定），也要有强制或半强制的校验。

三路调查的结论：这两件事现在都没有对应机制。`harness-onboard` 的澄清是一次性项目级配置，
`harness-compact` 只管文档周期收敛，都不是"单个任务/spec 是否做完"的判断层。

## 核心约束：真正能强制的是什么

DESIGN.md 已经定了两条原则，这两条原则直接决定了这套新机制的形状：

- **原则一**：需要判断的事只能做成 skill，不能指望 hook 去判断"这是不是一个大任务"或"这份 spec 写得够不够好"。
- **原则二**：hook 里不调模型——所以任何语义判断（问得够不够、spec 写得好不好）必须发生在 skill 里，hook 只能检查 skill 落盘的结果。

由此得到一个诚实的边界，比 `/harness-explore` 那次收敛更进一步：

> **"要不要触发澄清"这件事本身不能被硬件强制，只能靠 model 的判断（配合 prompt 层提醒）主动触发。
> 能被硬件强制的，是"一旦声明了 scope，写操作是否卡在澄清完成之前"。**

也就是：强制力不在"入口检测"，在"入口检测之后的闸门"。这跟现有 `harness-envelope.py` 的 `write_roots` 检查是同一种形状——不判断"这个写操作该不该发生"，只判断"落在声明的范围内没有"。

## 组件一：scope-gate（开工前澄清）

**触发**（判断，走 skill，不走 hook）：
新 skill `harness-scope-gate`。model 判断当前任务规模够大时主动调用；`harness-prompt-contract.py` 可以加一行 advisory 提醒（不 block，只是提高被调用的概率，跟现有 UserPromptSubmit 契约提醒同一套路）。

**产出 artifact**（判断的落盘结果）：

```yaml
# .harness/scope/<slug>.yml
name: <slug>
status: draft | confirmed
goal: ...
non_goals: [...]
acceptance_criteria: [...]
risk_budget: ...
confirmed_at: <timestamp>
```

用 `AskUserQuestion` 逐题问，一题一题推进（参考 office-hours 的强制逼问设计），至少覆盖四项：目标产出、明确不做什么、验收标准、边界/风险预算。全部回答完才把 `status` 从 `draft` 改成 `confirmed`。

**强制点**（hook 检查 artifact，不判断语义）：

`project.yml` 新增 `scope_gate.protected_paths`（路径前缀列表，默认空）。
`harness-envelope.py` 的 `check_write` 加一步：目标文件命中 `protected_paths` 时，如果找不到对应 `status: confirmed` 的 scope artifact，`deny`，原因文案指向"先跑 `/harness-scope-gate`"。

**边界**：`protected_paths` 全局默认开启（见"已拍板"Q3），不需要项目手动配置才生效——跟 `write_roots` 的"未配置=不限制"方向相反，这是有意选择：新装的机制默认从严，而不是默认放行。对已经 onboard 过的存量项目，这是一次破坏性变更（见下方架构评审 Q2 的决议：不做自动迁移）。

## 组件二：spec-review-gate（收敛点判定）

**触发**：scope artifact 从 `draft` → `confirmed` 之后，或用户显式跑 `/harness-spec-review`。

**判断逻辑**（走 skill，复用已验证过的模式）：
借 office-hours / plan-ceo-review 共用的 Spec Review Loop —— 独立、无上下文的 reviewer subagent 按 5 维度（Completeness/Consistency/Clarity/Scope/Feasibility）打分，最多 3 轮，**连续 2 轮无新意见就停**（收敛守卫，不是无限重试）；未解决项写进 artifact 当作 concern，不是卡死循环。这个有限重试的设计也参考了 autoplan 的 Pre-Gate Verification（最多重试 2 次，仍缺失就带警告继续）。

**产出/更新** 同一个 artifact，加一段：

```yaml
review:
  status: CLEARED | NOT_CLEARED | DONE_WITH_CONCERNS
  rounds: <n>
  concerns: [...]
```

**强制点**（扩展现有 Stop hook，复用已有的一次性 guard 机制）：

`harness-stop-contract.py` 加一步：stop 前检查当前 session 关联的 scope artifact 是否存在且 `review.status` 已经是 `CLEARED` 或 `DONE_WITH_CONCERNS`。不是就用现有 `guard_once` 拦一次（跟 continue-guard 共享"一个 session 只推一次"的防死循环设计），提醒跑 `/harness-spec-review`。

## 与现有组件的关系

| 现有组件 | 当前角色 | 加了这套设计之后 |
|---|---|---|
| `harness-envelope.py`（PreToolUse） | 命令/路径 allow/deny | 加一步 scope artifact 存在性检查（新逻辑，不改现有 allow/deny） |
| `harness-stop-contract.py`（Stop） | 防止过度发问（continue-guard） | 加一步"未收敛不许停"（第二个一次性 guard） |
| `harness-onboard` | 项目级一次性边界配置 | `project.yml` 新增 `scope_gate.protected_paths` 字段 |
| （不存在） | — | 新 skill：`harness-scope-gate`、`harness-spec-review` |
| `harness-compact` | 文档级周期收敛 | 不变，两者层级不同：这个管"项目现在该说什么"，新机制管"这个 spec 做完没有" |

## 已拍板

- **Q1（装哪）**：装进 `agent-harness` 源仓库。所有接入项目默认可用，不是单项目试点。
- **Q2（复用方式）**：直接复用 office-hours / plan-eng-review 的 AskUserQuestion + Spec Review Loop 模式（这两个 skill 在 `context-infrastructure/archives/skills/gstack/` 下，是引入的第三方 skill，"直接复用"意味着新 skill 里照搬这套逼问/收敛结构，不重新发明）。
- **Q3（protected_paths 默认值）**：默认生效，方向对齐 envelope 现有"默认从严"原则。具体取值：`scope_gate.protected_paths` 未显式配置时，**继承 `autonomy.write_roots`**（项目已经声明的写入范围）；两者都没配置的项目（open write）默认取整个仓库根目录 `.`。这样不需要用户为 scope-gate 单独维护一份新列表，且默认行为跟"没配置就整个仓库处于严格边界内"的现有心智一致。

## 架构评审（plan-eng-review 式，逐问拍板）

### Scope Challenge

现有可复用的组件：`harness-envelope.py` 的 allow/deny 判定形状、`harness-stop-contract.py` 的 `guard_once` 一次性拦截机制——两者都直接复用，不新建 hook 类型。改动面：`project.yml` 加字段 + 2 个 hook 各加一段逻辑 + 2 个新 skill，共约 5-6 个文件，低于"8 文件即警惕过度设计"的阈值，规模合理。

### 三个悬而未决点，已拍板

1. **scope 归属判定**：命中 `protected_paths` 但没有已知 confirmed scope 时——**强制显式声明，不做 cwd/branch 推断**。找不到就 `deny`，报错指向 `/harness-scope-gate`。理由：跟现有 envelope 的字符串匹配判定同一种形状，不引入语义推断，符合"hook 不做判断"的原则。
2. **存量项目的破坏性变更**：`protected_paths` 全局默认开启后，所有已 onboard 项目第一次触发写操作就会被拒绝（它们从未有 scope artifact）——**接受这个破坏性变更，不做自动迁移**。用户手动跑一次 `/harness-scope-gate` 补上。**必须做的事**：升级说明（commit message / README 变更记录）里显式标注"BREAKING"，否则用户会把第一次 deny 当成 bug 而不是预期行为。
3. **Stop hook 两个 guard 的优先级**：新增的"spec 未收敛不许停"检查 **优先于** 现有 continue-guard 判断。理由：spec 收敛是硬性验收规则，不该被"这轮是不是在问 permission"这种风格性判断挡住或延后。

### Code Quality 发现（无需决策，直接改）

- **`harness-envelope.py:78-96` 的 `check_write` 必须重构，不能直接叠加新逻辑。** 现状：函数内部靠散布的 `emit()` 调用做提前返回（`emit()` 内部是 `sys.exit(0)`，见 `_harness.py:204-207`），比如"未配置 `write_roots` 就直接放行退出"。新的 scope 检查是一条正交规则，如果照原样插入，会被这些提前 `emit()` 短路掉，导致"两条规则都要卡住"的意图在代码里失效。**修法**：把 `check_write`（write_roots 判断）和新的 `check_scope`（scope artifact 判断）都改成纯函数，返回 `"deny" | "allow" | None`，外层统一判定：任一个返回 `deny` 就 deny，否则 `emit()` 放行。这是唯一合理写法，不是需要拍板的决策，实现时直接这么改。（confidence 8/10，已读代码确认）

### Test Review

现状：仓库里**没有任何测试文件**（`find` 全仓库确认），所有 6 个 hook 目前是零覆盖。这次新增的两段逻辑是安全关键代码（判断错了，后果是"锁死所有写操作"或"完全不生效"两个极端），不能沿用"没测试"的现状。

```
新增代码路径覆盖
===========================
[+] harness-envelope.py: check_scope()
    ├── [GAP] 命中 protected_paths + 无 confirmed scope → deny
    ├── [GAP] 命中 protected_paths + confirmed scope → allow
    ├── [GAP] 不命中 protected_paths → 不受影响（现有 write_roots 判断独立生效）
    └── [GAP] write_roots 判断本身的回归测试（重构 check_write 后必须保证原有行为不变）

[+] harness-stop-contract.py: 新收敛检查
    ├── [GAP] spec 未收敛 + 文本触发 continue-guard → 收敛检查先拦（优先级验证）
    ├── [GAP] spec 已 CLEARED/DONE_WITH_CONCERNS + 正常停 → 不拦
    └── [GAP] 项目没有任何 scope artifact（scope_gate 概念不适用的场景）→ 不拦，不报错
─────────────────────────────────
COVERAGE: 0/7 已覆盖
─────────────────────────────────
```

**测试要求（写代码时一并完成，不是可选项）**：仓库没有测试框架依赖，用纯 `python3` + `assert` 写（比如 `hooks/test_envelope.py`，直接 import 被测函数跑一遍场景，`python3 hooks/test_envelope.py` 退出码为 0 即通过），不引入 pytest 等新依赖。至少覆盖上面 7 条。

### Failure Mode 检查

`check_write`/`check_scope` 重构如果判断反了（比如把 `deny` 和 `allow` 分支写反），故障是**静默的**——hook 永远走 `safe_main` 的 try/except，任何异常都会被吞掉退化成放行（见 `_harness.py:239-247`）。这意味着"新逻辑完全没生效"和"新逻辑生效但判断反了"从外部观察是同一个现象：写操作照样通过。**这是一个 critical gap**：没有测试的情况下，这层新增的"强制"可能悄悄变成摆设，而不会有任何报错提示用户。这进一步坐实了上面 Test Review 的要求不是"最好有"，是"没有就等于没做"。

### NOT in scope

- 自动给存量项目迁移 scope artifact（已拍板：不做）
- `harness-spec-review` 的 reviewer 打分维度权重调优（照搬 office-hours 的 5 维度，不重新设计）
- Codex/outside-voice 式的第二模型交叉验证（这套设计本身没有引入模型判断到 hook 里，不需要跨模型校验这个环节）

### What already exists

- `harness-envelope.py` 的 allow/deny 判定框架、`_harness.py` 的 `guard_once`/`load_config`/`emit` —— 全部直接复用，新代码只加判断逻辑，不加新的运行时基础设施。

## 下一步

- [x] Q1/Q2/Q3（方向性）拍板
- [x] 架构评审：scope 归属判定 / 存量项目迁移 / hook 优先级 三个决策点已拍板
- [x] 实现：`project.yml` schema 变更（`scope_gate.protected_paths`，全局默认开启，`hooks/_harness.py` DEFAULTS）
- [x] 实现：`harness-envelope.py` 重构 `check_write` + 新增 `check_scope`（纯函数写法，`write_roots_decision`/`scope_gate_decision`）
- [x] 实现：`harness-stop-contract.py` 新增收敛检查（`pending_spec_reviews`，跑在 continue-guard 之前）
- [x] 实现：新 skill `harness-scope-gate`、`harness-spec-review`
- [x] 实现：`hooks/test_envelope.py`（7 例）+ `hooks/test_stop_contract.py`（3 例），10/10 通过
- [x] 升级说明标注 BREAKING（README.md 已加，`bin/harness doctor`/`install` 已跑）
- [x] 在 `cre6630-infra`（真实已 onboard 项目）验证：5 个场景全部符合预期，验证用的临时 scope artifact 已清理，仓库无残留

## 验证记录（2026-08-13，对照真实 `cre6630-infra`）

用真实的 `.harness/project.yml`（`write_roots: []`，从未有 `scope/`）跑 `harness-envelope.py`/`harness-stop-contract.py`：

| 场景 | 输入 | 结果 |
|---|---|---|
| 无 confirmed scope，写仓库内任意文件 | `docs/pool-system/00-INDEX.md` | `deny`（protected_paths 回退到 `.`，符合 BREAKING 预期） |
| Stop hook，无任何 scope artifact | `last_assistant_message: "done"` | 不拦，退出码 0 |
| 补一个 `confirmed` scope，`covers: [docs]`，写 `docs/` 下文件 | 同上文件 | 无限制，放行 |
| 同一个 scope，写 `covers` 之外的路径 | `e2e_test/foo.py` | 仍然 `deny` |
| Stop hook，scope confirmed 但 `review.status` 未收敛 | 同上 | 拦一次，提示跑 `/harness-spec-review` |

顺带发现（非本次改动引入，是 envelope 一直如此的既有行为）：`load_config` 按**当前 session 的 cwd** 找项目根，不是按目标文件路径。从 agent-harness 的 session 直接 `Write` 到 `cre6630-infra` 下的文件不会触发它的 envelope（`root=None`）——跨项目远程写入从来不在这层保护范围内，这次验证因此改用子进程 + 显式 `cwd` 模拟真实的"在那个项目里工作"场景，而不是从当前 session 直接操作。
