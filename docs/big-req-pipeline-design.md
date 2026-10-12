# 大需求执行五段式 — 机制设计

来源：`context-infrastructure` 一次调研（`contexts/survey_sessions/ai_agent_infra_fast_validation_pipeline_survey_20260813.md`）收敛出的通用五段式，加一轮 Plan agent 对组件粒度的二次校验。

## 背景

用户想把"大需求怎么落地"的五段式（①需求拆解 ②开发迭代 ③集群/规模验证 ④管线闭环 ⑤交叉/并行验证）固化成可安装的 harness 组件，而不是一篇方法论文档。

## 核心约束：DESIGN.md 两条原则怎么约束这次设计

**原则一**（触发靠事件，不靠判断）直接否掉了"做一个跨五段的 orchestrator"这个最直觉的方案。现有 skill 全部符合"一次调用内完成，靠外部落盘 artifact 传递状态，不维持跨 session 执行位置"这个隐藏约束；五段式里的③（灰度验证天然跨小时/天）和④（online monitor 天然跨天跨周）做不到这一点。硬要串成一个 orchestrator，只有两条路：发明一整套新的长跑运行时（凭空设计，缺乏支撑），或者做成需要"知道现在第几步、该不该往下推"的 hook——这正是 `pending_spec_reviews` 那次被删除的机制的形状（扫全局状态、拦错人、污染上下文）。

所以：**阶段推进权始终留在人/model 手里，靠 slug 引用同一批文件串联，不靠代码控制流**。这跟现有 `harness-scope-gate` 报告里"改完之后跑一次 /harness-spec-review（没有 hook 会催，得自己记得）"是同一条纪律，五段式延续，不是例外。

**原则二**（写入不做判断，判断延迟到压缩）体现在 pipeline artifact 的设计上：它是"指针 + 没地方放的新状态"的集合，不复制①⑤已经落盘的内容——复制一份就要面对"两处状态谁是准的"这个新增的一致性负担，本身就是一种隐性判断。

## 组件逐个：触发 / artifact / 为什么不做成 hook

| 阶段 | 组件 | 触发方式 | artifact | 为什么不挂 hook |
|---|---|---|---|---|
| ① 需求拆解 | `harness-req-split`（新 skill） | 纯 pull，用户/model 主动调用 | `.harness/scope/groups/<slug>.yml` + 各 `.harness/scope/<child>.yml`（新增 `scenarios`/`group` 可选字段） | "该不该先拆解"是判断，判断不挂事件 |
| ② 开发迭代 | 无新机制 | — | 无——结束判定直接借用 `harness-spec-review` 的 Completeness 维度 | 本地反馈循环高度依赖各项目自己的工具链，标准化就是在没有真实数据支撑下发明一个通用验证器 |
| ③ 集群/规模验证 | `harness-scale-check`（新 skill，薄先例） | 纯 pull | `.harness/pipeline/<slug>.yml` 的 `scale_verification` 段 | 业界/本仓库都没有可复用先例（只有 k8s-gate.sh 一次性代码），checklist 型 skill 已经是能接受的最小机制形态；"该不该扩大规模/扩大到哪一档"仍是判断，不挂 hook |
| ④ 管线闭环 | `.harness/pipeline/<slug>.yml`（新 artifact schema） | 由①创建骨架，②③⑤各自写自己的段 | 见下方 schema | 纯 artifact，没有对应的"触发"概念 |
| ⑤ 交叉/并行验证 | `harness-cross-verify`（新 skill） | 纯 pull | 写回 `.harness/pipeline/<slug>.yml` 的 `cross_validation` 段 | "维度算不算正交""矛盾该怎么解读"是判断；"cross-verify 没跑不许推进"这类强制点跟 `pending_spec_reviews` 是同一类错误，不做 |

**零新增 hook。** 三个候选触发点（"该不该先拆解""pipeline某阶段没做完不许停""cross-verify没跑不许merge"）全部落在"判断"而非"事件"一侧。

## Pipeline artifact schema

```yaml
# .harness/pipeline/<slug>.yml
name: <slug>
status: draft | active | gated | shipped | rolled_back
requirement_group: .harness/scope/groups/<slug>.yml   # 指针，不复制内容
created_at:
updated_at:

stages:
  decomposition:                # ① 指针
    children: [<child-slug-1>, <child-slug-2>]
  dev_iteration:                 # ② 自由记录，无判定逻辑
    status: not_started | in_progress | done
    notes: >
  scale_verification:            # ③ harness-scale-check 写
    status: not_started | in_progress | passed | failed
    evidence: []
    notes: >
  offline_gate:                  # ④a 指针，映射各 child 的 review.status
    per_child:
      <child-slug-1>: CLEARED
  online_monitor:                 # ④b 自由记录
    status: not_configured | monitoring | drift_detected
    dashboards: []
    last_check_at:
  feedback_loop:                  # ④c 记录闭环发生过
    fed_back_cases: []
  cross_validation:               # ⑤ harness-cross-verify 写
    contradictions: []
    reviewer_rounds:

gate_decision: blocked | pass | pass_with_concerns   # 人工/model 写，不是算出来的
```

不进 `SessionStart` 注入（跟 `scope/*.yml` 一样，按需查阅不是每 session 相关状态）。想让某个大需求常驻上下文，用现有扩展点：`project.yml` 的 `context_files` 加一行指向具体 `pipeline/<slug>.yml`，不新增机制。

对 `harness-spec-review` 唯一的触碰：写回 artifact 之后，如果存在同名 `pipeline/<slug>.yml` 就可选同步 `offline_gate.per_child`，找不到就静默跳过，不改它的判断逻辑本身。

## 已拍板

- **组件粒度**：不做跨阶段 orchestrator。做 2 个新薄 skill（`harness-req-split`、`harness-cross-verify`）+ 1 个薄先例 skill（`harness-scale-check`）+ 1 个共享 artifact schema，阶段间靠 slug 约定串联。
- **③的处理方式**：用户明确选择"现在就建一个轻量 checklist skill"，而非"先留字段、等真实数据攒够再抽象"的更保守选项。已在 `harness-scale-check/SKILL.md` 顶部显式标注"薄先例、可能被推倒重做"，对齐这个仓库自己的诚实修订文化（参照下方"NOT in scope"里的态度）。
- **文件布局**：不新增 `templates/*.yml`（现有 scope schema 也是写在 SKILL.md 正文里，延续惯例）；不改 `install/hooks-manifest.json`（新增的是 skill 目录，`bin/harness install` 的 `for skill in sorted(SKILLS_SRC.iterdir())` 循环自动纳入）。
- **`scope/groups/` 子目录**：①的"组"文件放子目录而非跟普通 scope 平铺，避开 `harness-envelope.py`/`harness-spec-review` 现有的非递归 `glob("*.yml")`，不需要改这两处现有代码。

## NOT in scope

- 自动迁移存量 scope artifact 到 pipeline（比如 `cre6630-infra` 现存 15 个 scope、11 个未 review 的历史遗留）。这些 artifact 之间原本没有记录"哪几个属于同一个大需求"，事后靠脚本猜分组关系，本质是用自动化去做一次没有真实依据的语义判断——正是这次设计反复要避免的事。想追溯性挂进某个 pipeline，直接手工把 slug 填进 `pipeline.stages.decomposition.children` 即可，这是开放的手工逃生口,不是强制脚本。
- 跨阶段 orchestrator / 长跑 workflow 引擎——理由见"核心约束"一节。
- ②开发迭代的标准化验证器——各项目工具链差异太大，标准化本身就是没有数据支撑的过度设计。
- 任何读取"跨会话/全局 pipeline 状态"的 hook——不管是"提醒该拆解了"还是"提醒该交叉验证了"，都跟 `pending_spec_reviews` 是同一类错误。

## 如果这次设计将来被推翻，最可能先从哪开始

`harness-scale-check` 是本次唯一违背"先攒数据再建机制"原则、被用户主动要求提前建的组件（其余四段都是先有真实先例或至少能复用现有成熟形状才落地）。如果几轮真实使用后发现分档粒度不对、或者"多实例方差检查"步骤根本没人用，应该从这里开始修订，参照 `docs/scope-and-spec-gate-design.md` 的修订记录格式,在这份文档里补一节"修订记录"，不要静默改掉就算了。
