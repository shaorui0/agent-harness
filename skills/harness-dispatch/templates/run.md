# dispatch: __SLUG__

created: __ISO8601__
updated: __ISO8601__
status: running        # running | converged | stopped_at_limit | stopped_no_progress | blocked_on_human
source: __来源__        # conversation / <file:line> / user-paste
limits: waves __n__/4  agents __n__/24

## DoD（唯一停机判据，已确认）

- [ ] D1 __可核查断言__ | verify: __命令 / 文件断言 / human-read__
- [ ] D2 __...__

## Non-goals

- __这次明确不做的__

## Scope

- write_roots: __本次允许子 agent 写的路径全集__
- forbidden: __显式禁区__
- mutation: escrow（子 agent 不自行执行线上写命令）

## Stop

- 全部 D 打勾 → converged
- waves > 4 或 agents > 24 → stopped_at_limit
- 连续 2 波无新勾且无新事实 → stopped_no_progress
- 出现 class=decide 或 human-read verify → blocked_on_human

## Tasks

| id | from（原文一行，禁止改写） | class | needs | produces | owned_paths | wave | state |
|---|---|---|---|---|---|---|---|
| t1 |  | build |  |  |  | 1 | todo |

state: todo | dispatched | done | blocked | deferred

## Wave log

### Wave 1 — __ISO8601__

- 派出: __a1..a5，各自 task id__
- 新 ✓: __D2（复核: 跑了什么，看到什么）__
- 新事实: __n 条 → findings.md__
- 接受的 NEXT: __指向 D<n> / 推翻 findings 第 k 条__
- deferred: __条目 + 为什么不进__
- schema_violation: __哪一路，已重派到 wave 2__
