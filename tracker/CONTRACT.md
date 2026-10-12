# tracker API 契约（Sessions 部分，原 claude_monitor）

三个实现 agent（fleet collector / goal tracker / 前端）都必须先读这份契约再写代码，字段名不能自行改动。

## GET /api/state

```json
{
  "sessions": [
    {
      "session_id": "string",
      "pid": 12345,
      "cwd": "/abs/path",
      "project_name": "cwd basename",
      "status": "busy | waiting | idle | unknown",
      "waiting_for": "string|null",
      "name": "string",
      "kind": "interactive | bg",
      "model": "string|null",
      "started_at": "ISO8601 string|null",
      "status_updated_at": "ISO8601 string|null",
      "last_human_prompt": "string|null (已 strip system-reminder，截断展示由前端做)",
      "last_action": "string|null (最后一个 tool_use 的一行摘要，或最后 assistant 文本片段)",
      "ctx_tokens_left": 12345,
      "subagents_inflight": 0,
      "subagents_done": 0,
      "todo_done": 0,
      "todo_total": 0,
      "iterm_location": "string|null (如 'w5t1p0' 原始 ITERM_SESSION_ID 或已解析的 'win5 tab1')",
      "git_branch": "string|null"
    }
  ],
  "jobs": [
    {
      "daemon_short": "string",
      "name": "string",
      "state": "string (非 done 的都要出现，如 working/idle)",
      "detail": "string",
      "tokens": 0,
      "updated_at": "ISO8601 string"
    }
  ]
}
```

字段缺失时用 `null`（字符串类）或 `0`（数字类），不要省略 key，前端不用做 `field in obj` 判断。

## GET /api/goals

```json
{
  "goals": {
    "<session_id>": {
      "goal": "string|null",
      "convergence_point": "string|null",
      "is_continuation": true,
      "verdict": "open | converged | diverged | abandoned | null",
      "reason": "string|null",
      "status_badge": "progress | converged | diverged | abandoned | extract_failed",
      "updated_at": "ISO8601 string"
    }
  }
}
```

`status_badge` 是前端直接拿去选 emoji 的字段（进行中🟡 / 已收敛✅ / 偏离⚠️ / 放弃⛔ / 提取失败❓），后端算好，前端不做判断逻辑。

## goal_tracker.py 对外接口（server.py 调用）

```python
def on_new_human_prompt(session_id: str, prompt_text: str) -> None: ...
def on_status_transition(session_id: str, prev_status: str, cur_status: str, recent_activity: str) -> None: ...
def get_state() -> dict: ...  # 直接是 /api/goals 的 {"goals": {...}} 结构
```

三个函数都不能阻塞调用方超过几毫秒——内部必须扔进线程池异步跑，`get_state()` 只读当前内存态。

## collect_once()（server.py 采集部分对外接口，供集成用）

```python
def collect_once() -> dict: ...  # 直接是 /api/state 的 {"sessions": [...], "jobs": [...]} 结构
```

同时 collector 每轮要能对比出「新的人类输入」和「busy→waiting/idle 状态跃迁」两类事件（哪怕先简单存成一个模块级或返回值里的 `events` 列表也行），供集成 agent 接到 goal_tracker 的两个 on_* 函数上。

---

# v2 契约增量（2026-10-12）

设计原则：SRE 视角只回答四个问题——**跑完没有 / 当初要做什么(spec) / 现在什么状态 / 改了什么(diff)**。过程（每个 tool call）封装掉，不展示。v1 字段全部保留，下面只列新增。

## /api/state 新增

### 顶层 `summary`

```json
"summary": {
  "total": 0,
  "running": 0,          // phase == running
  "needs_input": 0,      // phase == needs_input
  "done": 0,             // phase == done
  "stale": 0,            // phase == stale
  "exited": 0,           // phase == exited
  "oldest_needs_input_s": 0,   // 等人最久的那个已经等了多少秒，0 = 没有
  "generated_at": "ISO8601"
}
```

### 每个 session 新增字段

```json
{
  "phase": "running | needs_input | done | stale | exited",
  "phase_since": "ISO8601|null",
  "phase_age_s": 0,
  "spec": "string|null",
  "files_touched": ["abs/or/rel path"],
  "diff": {
    "files_changed": 0,
    "insertions": 0,
    "deletions": 0,
    "commits_since_start": 0,
    "dirty": false
  },
  "todo_items": [{"content": "string", "status": "pending|in_progress|completed"}],
  "transcript_path": "string|null"
}
```

- `phase` 映射：busy→running；waiting→needs_input；idle→done（这一轮跑完了，等下一条指令）；unknown→stale；进程已死但 transcript 在 6h 内有更新→exited（这类 session 也要出现在 sessions 里，pid 可为 0 之外的原值，status 填 "unknown"）。
- `spec`：本 session **第一条**人类输入（strip reminder 后，截到 600 字）。`last_human_prompt` 仍是最近一条。
- `files_touched`：transcript 里 Edit / Write / MultiEdit / NotebookEdit tool_use 的 file_path 去重，按最近修改排序，最多 30 条。
- `diff`：只对 `files_touched` 里落在 cwd git 仓库内的文件跑 `git diff --numstat HEAD -- <files>` 汇总；`commits_since_start` = `git log --since=<started_at> --oneline | wc -l`；`dirty` = 上述 diff 非空。cwd 不是 git 仓库 → 全 0/false。git 调用按 cwd 缓存 15 秒、超时 2 秒，100 个 session 不能把一次请求拖过 1 秒。
- `todo_items`：最多 20 条，来源与 todo_counts 相同。

## GET /api/recap?session_id=<sid>

返回 `{"recap": <RecapObj>|null, "pending": bool}`。若无缓存或缓存早于 transcript mtime，server 调 `goal_tracker.request_recap(session_id, transcript_path, cwd)` 异步触发，立刻返回 `pending: true`。前端点开详情时调用，2 秒轮询直到 pending=false。

## /api/goals 每个 session 新增 `recap`

```json
"recap": {
  "done": ["已完成的事，≤5 条，一行一条"],
  "now": "当前停在哪 / 正在做什么，一句",
  "next": "下一步该谁做什么，一句",
  "blocked_on": "string|null（在等人决策什么）",
  "updated_at": "ISO8601",
  "source_mtime": 0.0
} | null
```

## goal_tracker.py 新增接口

```python
def request_recap(session_id: str, transcript_path: str, cwd: str) -> None: ...  # 非阻塞，入线程池
def get_recap(session_id: str) -> dict | None: ...   # 读内存态；含 source_mtime 供 server 判断是否过期
```

- recap 生成：用 `tools/session_recap.py`（仓库根下）抽取 transcript 骨架（非 --full），再喂 Haiku 出上面 JSON。
- busy→waiting/idle 跃迁时 goal_tracker 也自动刷新一次 recap（同一 session 60 秒内最多一次）。
- 持久化：append 到 `state/goals.jsonl`，记录 type="recap"，重放时恢复。

# Specs 部分（2026-10-12 并入 agent-harness）

## GET /data/snapshot.js

返回 `specs.py` 生成的 `~/.harness/tracker/snapshot.js` 原样内容：`window.SNAPSHOT = {...};`。还没扫描过时返回 `window.SNAPSHOT = null;`。
server 启动时会起一个后台线程，按 `tracker.scan_interval_s`（默认 300s）重扫，请求路径上不跑 git。

```json
{
  "generated_at": "ISO",
  "roots": ["~/work"],
  "scopes": [{
    "name": "slug", "file": "abs path", "repo": "workspace 名", "repo_path": "abs",
    "spec_status": "draft | confirmed | reconstructed",
    "goal": "...", "non_goals": [], "acceptance_criteria": ["text"], "ac_results": [],
    "covers": ["path" | {"repo": "...", "ref": "...", "paths": []}],
    "review": {"status": "CLEARED | NOT_CLEARED | DONE_WITH_CONCERNS | null", "reviewed_at": "ISO", "rounds": 1, "scores": {}, "why": "..."},
    "drift": {"commits_after_review": 3, "recent": ["sha date subject"], "last_commit": "..."},
    "origin": null, "warnings": ["字段问题"], "next_actions": []
  }],
  "errors": [{"file": "abs path", "error": "YAML 解析失败原因"}]
}
```

## GET /deck

思路讲解 slides（`static/deck.html`）。第 10 页读取 `/data/snapshot.js` 显示真实数据。

## 计划中（未实现）

`GET /api/tasks` 返回 `{summary, tasks, unscoped, errors}`，join 规则见 `docs/tracker-design.md` §4。
