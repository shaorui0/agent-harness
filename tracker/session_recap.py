#!/usr/bin/env python3
"""session_recap.py — 从 Claude Code 的 session transcript 里提取「骨架」。

用途：session 停了很久回来，不想重读整个对话。这个脚本把 transcript
(~/.claude/projects/<slug>/<session-id>.jsonl) 压成一个可读的骨架：
每个人类回合说了什么 → agent 做了哪些动作（改了哪些文件、跑了哪些命令）
→ 最后的 todo 状态 → 停在哪里。

它只做**提取**，不做总结。总结交给 agent（见 rules/skills/session_recap.md）。

用法:
    python3 tools/session_recap.py --list                # 列出当前项目最近的 session
    python3 tools/session_recap.py --list --all-projects # 跨项目列出
    python3 tools/session_recap.py latest                # 提取最近活动的 session
    python3 tools/session_recap.py <session-id>          # 提取指定 session
    python3 tools/session_recap.py <path/to/x.jsonl>     # 直接给文件路径
    python3 tools/session_recap.py latest --full         # 不截断正文
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"

# 正文截断长度（--full 时全部放开）
CAP_USER = 700
CAP_ASSISTANT = 400
CAP_ASSISTANT_TAIL = 1200  # 最后几个回合给更多篇幅
TAIL_TURNS = 3
CAP_BASH = 160
CAP_TOOLDESC = 120

NOISE_TOOLS = {"Read", "Glob", "Grep", "ToolSearch", "Monitor", "TodoWrite"}
# 优先保留的「改变了世界」的动作
MUTATING_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "Task", "Agent", "Skill"}


# ---------------------------------------------------------------- utilities


def slug_for(path: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", str(Path(path).resolve()))


def strip_reminders(text: str) -> str:
    text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.S)
    text = re.sub(r"<local-command-[a-z-]+>.*?</local-command-[a-z-]+>", "", text, flags=re.S)
    text = re.sub(r"<command-(name|message|args)>.*?</command-\1>", " ", text, flags=re.S)
    return text.strip()


def clip(text: str, n: int, full: bool = False) -> str:
    text = re.sub(r"[ \t]+", " ", (text or "").strip())
    text = re.sub(r"\n{3,}", "\n\n", text)
    if full or len(text) <= n:
        return text
    return text[:n].rstrip() + " …"


def ts_of(entry: dict):
    raw = entry.get("timestamp")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def fmt_ts(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "?"


def fmt_gap(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}min"
    if seconds < 86400 * 2:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def norm_path(path: str, cwd: str | None) -> str:
    """统一成绝对路径，好把 file-history-delta 的相对路径和工具入参的绝对路径合并。"""
    if not path:
        return path
    if not path.startswith("/") and cwd:
        return str(Path(cwd) / path)
    return path


def display_path(path: str, cwd: str | None) -> str:
    if cwd and path.startswith(cwd.rstrip("/") + "/"):
        return path[len(cwd.rstrip("/")) + 1:]
    return path


def blocks_of(entry: dict) -> list:
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return content or []


def read_entries(path: Path):
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def is_human_prompt(entry: dict) -> bool:
    """真正由人敲进去的一轮（排除 tool_result、hook 注入、task 通知）。"""
    if entry.get("type") != "user" or entry.get("isSidechain"):
        return False
    if entry.get("isMeta"):
        return False
    origin = entry.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return False
    for block in blocks_of(entry):
        if block.get("type") == "tool_result":
            return False
    return bool(strip_reminders(" ".join(
        b.get("text", "") for b in blocks_of(entry) if b.get("type") == "text"
    )))


# ---------------------------------------------------------------- discovery


def project_dirs(all_projects: bool, cwd: str):
    if all_projects:
        return sorted([p for p in PROJECTS_DIR.iterdir() if p.is_dir()])
    target = PROJECTS_DIR / slug_for(cwd)
    return [target] if target.is_dir() else []


def scan_sessions(dirs) -> list[dict]:
    out = []
    for d in dirs:
        for f in d.glob("*.jsonl"):
            try:
                stat = f.stat()
            except OSError:
                continue
            out.append({"path": f, "mtime": stat.st_mtime, "size": stat.st_size,
                        "project": d.name, "id": f.stem})
    out.sort(key=lambda s: s["mtime"], reverse=True)
    return out


def session_headline(path: Path, max_lines: int = 4000) -> dict:
    """便宜地扫一遍拿标题/首个提问/回合数。"""
    info = {"title": None, "first_prompt": None, "turns": 0, "branch": None,
            "cwd": None, "start": None, "end": None}
    for i, entry in enumerate(read_entries(path)):
        if i > max_lines:
            break
        etype = entry.get("type")
        if etype == "ai-title" and entry.get("aiTitle"):
            info["title"] = entry["aiTitle"]
        elif etype == "summary" and entry.get("summary"):
            info["title"] = info["title"] or entry["summary"]
        if entry.get("cwd"):
            info["cwd"] = entry["cwd"]
        if entry.get("gitBranch"):
            info["branch"] = entry["gitBranch"]
        dt = ts_of(entry)
        if dt:
            info["start"] = info["start"] or dt
            info["end"] = dt
        if is_human_prompt(entry):
            info["turns"] += 1
            if info["first_prompt"] is None:
                info["first_prompt"] = clip(strip_reminders(" ".join(
                    b.get("text", "") for b in blocks_of(entry) if b.get("type") == "text")), 140)
    return info


def resolve_session(token: str, cwd: str) -> Path:
    p = Path(token)
    if p.suffix == ".jsonl" and p.is_file():
        return p
    dirs = project_dirs(False, cwd) or project_dirs(True, cwd)
    if token in ("latest", ".", "current"):
        sessions = scan_sessions(dirs)
        if not sessions:
            sys.exit(f"no transcripts under {PROJECTS_DIR} for cwd={cwd}")
        return sessions[0]["path"]
    for d in list(dirs) + sorted(PROJECTS_DIR.iterdir()):
        cand = d / f"{token}.jsonl"
        if cand.is_file():
            return cand
    sys.exit(f"session not found: {token}")


# ---------------------------------------------------------------- extraction


def summarize_tool(block: dict, full: bool) -> tuple[str, dict]:
    """(一行人类可读的动作, 结构化附加信息)"""
    name = block.get("name", "?")
    inp = block.get("input") or {}
    extra = {}
    if name == "Bash":
        cmd = clip(inp.get("command", ""), CAP_BASH, full).replace("\n", " ; ")
        return f"$ {cmd}", extra
    if name in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
        path = inp.get("file_path") or inp.get("notebook_path") or "?"
        extra["file"] = path
        return f"{name} {path}", extra
    if name == "Read":
        return f"Read {inp.get('file_path', '?')}", extra
    if name in ("Glob", "Grep"):
        return f"{name} {clip(str(inp.get('pattern', '')), 60, full)}", extra
    if name in ("Task", "Agent"):
        desc = inp.get("description") or clip(str(inp.get("prompt", "")), CAP_TOOLDESC, full)
        return f"Agent[{inp.get('subagent_type', '?')}] {desc}", extra
    if name == "TodoWrite":
        extra["todos"] = inp.get("todos") or []
        return "TodoWrite", extra
    if name == "TaskCreate":
        extra["task_create"] = inp
        return f"TaskCreate «{clip(inp.get('subject', ''), 90, full)}»", extra
    if name == "TaskUpdate":
        extra["task_update"] = inp
        return f"TaskUpdate #{inp.get('taskId')} → {inp.get('status', '?')}", extra
    if name == "ExitPlanMode":
        extra["plan"] = inp.get("plan", "")
        return "ExitPlanMode（提交计划待批准）", extra
    if name == "Skill":
        return f"Skill /{inp.get('skill', '?')} {inp.get('args', '')}".strip(), extra
    if name.startswith("mcp__"):
        return f"{name} {clip(json.dumps(inp, ensure_ascii=False), CAP_TOOLDESC, full)}", extra
    return f"{name} {clip(json.dumps(inp, ensure_ascii=False), CAP_TOOLDESC, full)}", extra


def build_turns(path: Path, full: bool) -> dict:
    turns: list[dict] = []
    meta = {"cwd": None, "branch": None, "title": None, "version": None,
            "start": None, "end": None, "id": path.stem, "path": str(path),
            "compactions": 0, "interrupts": 0}
    last_todos = None
    files_touched: dict[str, int] = {}
    tasks: dict[str, dict] = {}          # taskId -> {subject, status}
    pending_create: dict[str, dict] = {} # tool_use_id -> TaskCreate input
    last_plan = None
    cur = None

    def new_turn(entry, text):
        return {"ts": ts_of(entry), "prompt": text, "actions": [], "tools": {},
                "texts": [], "todos": None, "interrupted": False, "notes": []}

    for entry in read_entries(path):
        etype = entry.get("type")
        if entry.get("cwd") and not meta["cwd"]:
            meta["cwd"] = entry["cwd"]  # 只认第一次的 cwd：Bash 里的 cd 会污染后续记录
        if entry.get("gitBranch"):
            meta["branch"] = entry["gitBranch"]
        if entry.get("version"):
            meta["version"] = entry["version"]
        if etype == "ai-title" and entry.get("aiTitle"):
            meta["title"] = entry["aiTitle"]
        if etype == "file-history-delta" and entry.get("trackingPath"):
            fp = norm_path(entry["trackingPath"], meta["cwd"])
            files_touched[fp] = files_touched.get(fp, 0) + 1
        dt = ts_of(entry)
        if dt:
            meta["start"] = meta["start"] or dt
            meta["end"] = dt

        if entry.get("isSidechain"):
            continue  # subagent 的内部对话，跳过

        if etype == "summary" or entry.get("isCompactSummary"):
            meta["compactions"] += 1
            if cur:
                cur["notes"].append("↯ 此处发生过 context 压缩（compact）")
            continue

        if etype == "user":
            for block in blocks_of(entry):
                if block.get("type") != "tool_result":
                    continue
                created = pending_create.pop(block.get("tool_use_id"), None)
                if not created:
                    continue
                body = block.get("content")
                if isinstance(body, list):
                    body = " ".join(b.get("text", "") for b in body if isinstance(b, dict))
                m = re.search(r"Task #(\d+)", str(body or ""))
                tid = m.group(1) if m else str(len(tasks) + 1)
                tasks[tid] = {"subject": created.get("subject", ""),
                              "desc": created.get("description", ""), "status": "pending"}
            raw = " ".join(b.get("text", "") for b in blocks_of(entry) if b.get("type") == "text")
            if "[Request interrupted by user" in raw:
                meta["interrupts"] += 1
                if cur:
                    cur["interrupted"] = True
            if is_human_prompt(entry):
                cur = new_turn(entry, clip(strip_reminders(raw), CAP_USER, full))
                turns.append(cur)
            continue

        if etype == "assistant" and cur is not None:
            for block in blocks_of(entry):
                btype = block.get("type")
                if btype == "text":
                    txt = clip(block.get("text", ""), 10_000, True)
                    if txt:
                        cur["texts"].append(txt)
                elif btype == "tool_use":
                    name = block.get("name", "?")
                    cur["tools"][name] = cur["tools"].get(name, 0) + 1
                    line, extra = summarize_tool(block, full)
                    if name not in NOISE_TOOLS:
                        cur["actions"].append((name, line))
                    if "file" in extra:
                        fp = norm_path(extra["file"], meta["cwd"])
                        files_touched[fp] = files_touched.get(fp, 0) + 1
                    if "todos" in extra:
                        last_todos = extra["todos"]
                        cur["todos"] = extra["todos"]
                    if "task_create" in extra:
                        pending_create[block.get("id")] = extra["task_create"]
                    if "task_update" in extra:
                        upd = extra["task_update"]
                        task = tasks.setdefault(str(upd.get("taskId")),
                                                {"subject": "(未知任务)", "desc": "", "status": "pending"})
                        if upd.get("status"):
                            task["status"] = upd["status"]
                        if upd.get("subject"):
                            task["subject"] = upd["subject"]
                    if "plan" in extra and extra["plan"]:
                        last_plan = extra["plan"]

    meta["files"] = files_touched
    meta["last_todos"] = last_todos
    meta["tasks"] = tasks
    meta["last_plan"] = last_plan
    return {"meta": meta, "turns": turns}


# ---------------------------------------------------------------- rendering


def compress_actions(actions: list, full: bool, cwd: str | None = None) -> list[str]:
    """合并重复动作，非 full 模式下优先保留 mutation，Bash 只留少量代表。"""
    merged: list[list] = []
    seen: dict[str, list] = {}
    prefix = (cwd.rstrip("/") + "/") if cwd else None
    for name, line in actions:
        if prefix:
            line = line.replace(prefix, "")
        if line in seen:
            seen[line][2] += 1
            continue
        seen[line] = [name, line, 1]
        merged.append(seen[line])
    render_line = lambda m: m[1] + (f"  ×{m[2]}" if m[2] > 1 else "")
    if full or len(merged) <= 10:
        return [render_line(m) for m in merged]

    mut = [m for m in merged if m[0] in MUTATING_TOOLS]
    other = [m for m in merged if m[0] not in MUTATING_TOOLS]
    keep_mut = mut if len(mut) <= 10 else mut[:5] + [["", "…", 0]] + mut[-5:]
    keep_other = other[:3] + ([["", "…", 0]] + other[-2:] if len(other) > 5 else other[3:])
    dropped = len(merged) - len([m for m in keep_mut + keep_other if m[1] != "…"])
    lines = [render_line(m) if m[1] != "…" else "…" for m in keep_mut + keep_other]
    if dropped > 0:
        lines.append(f"…（另有 {dropped} 条动作已省略，--full 查看）")
    return [l for l in lines if l != "…"]


def render(data: dict, full: bool, max_turns: int) -> str:
    meta, turns = data["meta"], data["turns"]
    out: list[str] = []
    w = out.append

    span = ""
    if meta["start"] and meta["end"]:
        span = f"（跨度 {fmt_gap((meta['end'] - meta['start']).total_seconds())}）"
    w("# SESSION SKELETON")
    w("")
    w(f"- session: `{meta['id']}`")
    w(f"- title: {meta.get('title') or '(无)'}")
    w(f"- cwd: {meta.get('cwd')}  |  branch: {meta.get('branch')}")
    w(f"- 首次活动: {fmt_ts(meta['start'])}  |  最后活动: {fmt_ts(meta['end'])} {span}")
    if meta["end"]:
        idle = (datetime.now(timezone.utc) - meta["end"].astimezone(timezone.utc)).total_seconds()
        w(f"- 距今闲置: {fmt_gap(idle)}")
    w(f"- 人类回合数: {len(turns)}  |  compact 次数: {meta['compactions']}  |  被打断次数: {meta['interrupts']}")
    w("")

    shown = turns
    skipped = 0
    if not full and len(turns) > max_turns:
        head, tail = turns[:3], turns[-(max_turns - 3):]
        skipped = len(turns) - len(head) - len(tail)
        shown = head + [None] + tail

    w("## TIMELINE（按人类回合切分）")
    w("")
    idx = 0
    prev_end = None
    for turn in shown:
        if turn is None:
            w(f"…… 中间省略 {skipped} 个回合（用 --full 查看全部）……")
            w("")
            continue
        idx = turns.index(turn) + 1
        gap = ""
        if prev_end and turn["ts"]:
            delta = (turn["ts"] - prev_end).total_seconds()
            if delta > 3600:
                gap = f"  ⏸ 距上一回合 {fmt_gap(delta)}"
        prev_end = turn["ts"] or prev_end
        w(f"### T{idx} · {fmt_ts(turn['ts'])}{gap}")
        w(f"**人类**: {turn['prompt']}")
        if turn["interrupted"]:
            w("> ⚠️ 这一回合被用户中途打断")
        for note in turn["notes"]:
            w(f"> {note}")
        if turn["tools"]:
            counts = ", ".join(f"{k}×{v}" for k, v in sorted(
                turn["tools"].items(), key=lambda kv: -kv[1]))
            w(f"**工具**: {counts}")
        acts = compress_actions(turn["actions"], full, meta.get("cwd"))
        if acts:
            w("**动作**:")
            for a in acts:
                w(f"- {a}")
        if turn["texts"]:
            is_tail = idx > len(turns) - TAIL_TURNS
            cap = CAP_ASSISTANT_TAIL if is_tail else CAP_ASSISTANT
            tail_text = turn["texts"][-1]
            if re.match(r"\s*(API Error|Request timed out|.*rate limit)", tail_text, re.I):
                w(f"> ⚠️ 这一回合以错误结束（非正常收尾）：{clip(tail_text, 200, False)}")
            else:
                w(f"**Claude 结论**: {clip(tail_text, cap, full)}")
        w("")

    mark_of = lambda s: {"completed": "x", "in_progress": "~", "pending": " ",
                         "cancelled": "-"}.get(s, s or "?")

    if meta.get("tasks"):
        w("## 任务板最终状态（TaskCreate/TaskUpdate 重建）")
        w("")
        for tid, task in sorted(meta["tasks"].items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 999):
            w(f"- [{mark_of(task['status'])}] #{tid} {task['subject']}")
            if task["status"] != "completed" and task.get("desc"):
                w(f"      ↳ {clip(task['desc'], 200, full)}")
        w("")

    if meta["last_todos"]:
        w("## 最后一次 TODO 状态")
        w("")
        for todo in meta["last_todos"]:
            w(f"- [{mark_of(todo.get('status', '?'))}] "
              f"{todo.get('content') or todo.get('activeForm') or todo}")
        w("")

    if meta.get("last_plan"):
        w("## 最后一次提交的计划（ExitPlanMode）")
        w("")
        w(clip(meta["last_plan"], 2000, full))
        w("")

    if meta["files"]:
        w(f"## 本 session 改过 / 生成过的文件（共 {len(meta['files'])} 个，路径相对 cwd）")
        w("")
        for fp, n in sorted(meta["files"].items(), key=lambda kv: -kv[1])[:25]:
            w(f"- {display_path(fp, meta.get('cwd'))}  ({n}×)")
        w("")

    return "\n".join(out)


def render_list(sessions: list[dict], limit: int, show_project: bool) -> str:
    out = ["# 最近的 SESSION", ""]
    for s in sessions[:limit]:
        info = session_headline(s["path"])
        age = fmt_gap(datetime.now().timestamp() - s["mtime"])
        line = [f"## {info['title'] or '(未命名)'}"]
        line.append(f"- id: `{s['id']}`")
        if show_project:
            line.append(f"- project: {s['project']}")
        line.append(f"- 最后活动: {datetime.fromtimestamp(s['mtime']):%Y-%m-%d %H:%M}（{age} 前）"
                    f" | 人类回合: {info['turns']} | 大小: {s['size'] / 1024:.0f}KB")
        if info["first_prompt"]:
            line.append(f"- 开场: {info['first_prompt']}")
        out.extend(line + [""])
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session", nargs="?", default="latest",
                    help="session id / jsonl 路径 / latest（默认）")
    ap.add_argument("--list", action="store_true", help="列出最近的 session 而不是提取")
    ap.add_argument("--all-projects", action="store_true", help="--list 时跨所有项目")
    ap.add_argument("-n", "--limit", type=int, default=10, help="--list 显示条数")
    ap.add_argument("--cwd", default=os.getcwd(), help="按哪个工作目录定位项目（默认当前目录）")
    ap.add_argument("--full", action="store_true", help="不截断正文、不省略回合")
    ap.add_argument("--max-turns", type=int, default=25, help="非 --full 时最多展示的回合数")
    args = ap.parse_args()

    if args.list:
        dirs = project_dirs(args.all_projects, args.cwd)
        if not dirs:
            sys.exit(f"no project dir for cwd={args.cwd} under {PROJECTS_DIR}")
        print(render_list(scan_sessions(dirs), args.limit, args.all_projects))
        return

    path = resolve_session(args.session, args.cwd)
    print(render(build_turns(path, args.full), args.full, args.max_turns))


if __name__ == "__main__":
    main()
