# tracker 设计

> 状态：2026-10-12。已经决定的项写成「已定」，还没拍板的列在 §9。使用说明见 [`tracker/README.md`](../tracker/README.md)。

## 1. 一句话

tracker 是 `.harness/` 的只读旁路，一个进程对应一个页面。**Task（spec）是主视图，Session（运行态）是附属视图**，中间靠一个 join 连接。

它的前身有两个，现已合并为本目录这一份实现：一个 session 监控面板（claude_monitor），以及一个 scope 扫描脚本（spec_tracker）。

## 2. 实测：把两边 join 起来是什么样（2026-10-12，一个真实 workspace）

| 事实 | 数字 |
|---|---|
| 能解析的 scope | 12 个，另有 4 个 YAML 解析失败 |
| 当前可见的 session | 23 个 |
| 用「主线程改过的文件 ∩ covers」能绑到 scope 的 session | **0 / 23** |
| 12 个 scope 最近一次相关活动 | 都在 3 天前或更早 |
| `covers` 写成宽目录的 scope | 4 个 |
| 6 个已通过 review 的 scope 中，review 后代码又改过的 | 4 个（其中一部分是宽 cover 带来的假 drift） |

这组数字推出三条设计结论：

1. **当天的工作全部没有 spec**。所以除了 spec 列表，还需要一个「没挂 spec 的工作」视图。
2. **主线程的文件记录严重漏记**。代码大多是 subagent 改完直接 commit 的：一个 session 主线程只记到 12 个文件，实际有 51 个提交。所以绑定要用 oplog（它也记录 subagent 的写操作）和 commit，不能只看 transcript 里的 Edit。
3. **drift 的准确度取决于 cover 写得多细**。宽目录 cover 是 Problem Framing 的缺陷，应当作为提示标出来，而不是直接算成 drift。

## 3. 数据模型

```
Task      ← .harness/scope/*.yml               desired：goal / AC / covers / review
  ├─ Evidence ← ac_results                      observed：每条 AC 的 PASS/FAIL/UNKNOWN + commit
  └─ Drift    ← git log [ref] --since reviewed_at -- covers
Session   ← ~/.claude/projects/*.jsonl + pid     运行态：phase / 首条 prompt / diff / recap
Binding   Session → Task | unscoped（带 source）
```

## 4. Binding 规则

按优先级依次尝试，命中一个就停：

| 优先级 | 来源 | 现在能用吗 | 置信度 |
|---|---|---|---|
| 1 | `oplog.jsonl` 中该 session_id 的 `target` 落在某个 scope 的 `covers` 内 | ✅ oplog 已经记录了 session_id 和 subagent 的写操作 | 高 |
| 2 | session 读写过 `.harness/scope/<slug>.yml`，或者 prompt 里提到了 slug | ✅ | 高 |
| 3 | 将来由 `/task bind` 显式写入的 task 字段 | ⚪ | 确定 |
| 4 | 以上都没命中，按 workspace 分组归入 `unscoped` | ✅ | — |

## 5. 视图（v3：树形，不再分 tab）

- **左栏**：workspace → session。排序 needs_input > running > stale > done > exited，同级新的在前。「无 spec」只是 session 上的一个标记，不是单独的视图。
- **右栏**（选中 session）：头部 phase（phase、diff、resume 命令）/ 任务进度（关联 change 的 `tasks.md`，树形：分组=任务、条目=子任务）/ Claude 梳理的任务（手动按钮生成，tasks → subtasks 完成情况）/ Session Recap（手动按钮执行 session_recap skill，七段 briefing 含续跑指令）。
- 入口：CloudCLI 的 Tracker tab（主）、独立总览页 `127.0.0.1:8787`、`harness tracker show`（AI/终端）。

## 6. 部件去留

| 部件 | 处理 |
|---|---|
| session 采集（phase、files、diff、exited 发现） | 保留，作为采集层 |
| `/api/recap`（点开时按需调 Haiku） | 由手动摘要（`POST /api/summary`）取代 |
| 目标自动提炼（每条新 prompt、每次状态跳变都调一次 Haiku） | 已删除（移入 `_archive/`）：spec 本身就是目标，不用让 LLM 去猜 |
| 前端每 2s 轮询 | 计划改成 30s，加手动刷新按钮 |
| `specs.py` | ✅ 已完成：后台每 5 分钟重扫，不在请求路径上执行；支持 `{repo, ref, paths}` 写法；提供 `--validate` |
| `/api/board`（`board.py`）、OpenSpec 解析（`openspec.py`）、树形 UI | 已实现，取代原 `join`、`/api/tasks`、Tasks / Unscoped tab |
| Framing 质量提示：AC 不可执行、cover 太宽、YAML 坏了 | 部分完成：YAML 解析失败和缺字段已经进了 `warnings` 和 `doctor` |

## 7. 补登：把 Unscoped 变成 Task

```
Unscoped 一行 ──复制命令──▶ /harness-spec-backfill <sid>
  session_recap.py 提取 → 按人类意图切分 → 只从人类原话重建 spec
  → AC 判定（rechecked > transcript_claim > UNKNOWN）→ 草稿放 .harness/backfill-drafts/
  → 人确认 → .harness/scope/<slug>.yml（status: reconstructed）→ 下次扫描进入 Tasks
```

- `reconstructed` 和 `confirmed` 分开显示、分开统计：事后补登的 spec 不能冒充事前写好的 spec。
- `ac_results` 和将来的 `/verify` 共用同一套 schema。
- 第一次试跑（一个跨 4.3 天、有 50 个人类回合的 session）暴露了 4 个问题，已经写进 skill 的硬规则：transcript 里有明文密钥；意图前后改了 5 次；文件记录漏记，covers 要以 commit 为准；session 还在跑，补登结果只能当快照。

## 8. 为什么放在 agent-harness 里（已定）

agent-harness 负责**写**：hook、skill，以及 `.harness/` 的格式。tracker 负责**读**。两者放在同一个仓库有三点好处：

1. **格式和解析方一起演进**。scope 的字段改了，tracker 跟着改；`doctor` 也能顺带校验格式。
2. **符合原则一**。tracker 是 pull 入口：只读，不挂事件，不做判断。
3. **复用 `bin/harness`**。安装、体检、启动都走同一个入口。

本仓库是公开的，所以：

- **仓库里只有代码和 mock**。真实快照、recap 记录、补登草稿都留在各自的 workspace 或 `~/.harness/tracker/`。
- 以后提交前要检查，不能带进工作数据，比如真实路径、集群名、bucket 名、session 原话。

## 9. 待拍板

| # | 决策 | 建议 |
|---|---|---|
| D7 | 覆盖率怎么统计 | 分三段：confirmed / reconstructed / 无 spec |

已定：

- D2：删掉自动提炼，加手动「让 claude 梳理任务」按钮（按需调 sonnet）。
- D3：不再分 tab，用 workspace → session 树，「无 spec」只是标记。
- D4：宽 cover 只提示，drift 降为提示，不作为判断依据。
- D5：不做 `/task`、`/verify`，用 OpenSpec 自带的 `/opsx:verify`；hook 和状态栏先不改。

- D1 / D8：tracker 放在 `agent-harness/tracker/`，只保留这一份实现。
- D6：backfill skill 放在 `agent-harness/skills/`。

## 10. 明确不做

- 不建中心数据库或索引，扫描加缓存够用。
- tracker 不写任何 scope。
- 不改 hooks 和状态栏（D5）。
