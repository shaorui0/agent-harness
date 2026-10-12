#!/usr/bin/env python3
"""harness tracker show —— 把看板按 workspace → session → 任务清单 输出成纯文本 markdown。

给 AI 和终端读，不依赖 ANSI 颜色。数据来源：
  默认        直接调 server.collect_once() + board.build()
  --from-file 读已保存的 board JSON（离线 / 测试）
  --json      不渲染，直接输出 board JSON
  --recap     在 recap 行之后打印 session recap 的 markdown 全文（有缓存才有）

退出码：0 正常；2 指定的 workspace 不存在（stderr 列出可选项）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

PHASE_LABEL = {
    "needs_input": "在等你输入",
    "running": "运行中",
    "stale": "停滞",
    "done": "已完成",
    "exited": "已退出",
}
PHASES = ["needs_input", "running", "done", "stale", "exited"]


def load_board(ws: str | None, from_file: str | None) -> dict:
    if from_file:
        with open(from_file, encoding="utf-8") as f:
            return json.load(f)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import board
    import server
    return board.build(server.collect_once()["sessions"], ws)


def _age(seconds) -> str:
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return ""
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _summary_for(session: dict):
    if session.get("summary"):
        return session["summary"]
    try:  # summary 模块由别处维护，缺失就忽略
        import summary
        got = summary.get(session["session_id"], session.get("transcript_path"))
        return got.get("summary")
    except Exception:
        return None


def _sub_lines(items: list, base: int = 1) -> list[str]:
    lines = []
    for it in items:
        pad = "  " * (base + int(it.get("level") or 0))
        if it.get("done"):
            lines.append(f"{pad}- [x] {it.get('id', '')} {it['text']}".replace("[x]  ", "[x] "))
        else:
            mark = "  ← 正在这条" if it.get("current") else ""
            lines.append(f"{pad}- [ ] {it.get('id', '')} {it['text']}{mark}".replace("[ ]  ", "[ ] "))
    return lines


def _render_items(change: dict) -> list[str]:
    """分组 = 任务（全部完成则折叠成一行），条目 = 子任务（按 level 缩进）。"""
    groups: list[tuple] = []  # (group 名或 None, [items])
    for it in change.get("items") or []:
        g = it.get("group")
        if groups and groups[-1][0] == g and g is not None:
            groups[-1][1].append(it)
        elif g is None and groups and groups[-1][0] is None:
            groups[-1][1].append(it)
        else:
            groups.append((g, [it]))
    lines = []
    for g, its in groups:
        if g is None:  # 无分组：level 0 条目本身就是任务
            lines += _sub_lines(its, 0)
            continue
        n = sum(1 for i in its if i.get("done"))
        ok = n == len(its)
        lines.append(f"- [{'x' if ok else ' '}] {g} ({n}/{len(its)})")
        if not ok:
            lines += _sub_lines(its)
    return lines


def _render_task_tree(tasks: list) -> list[str]:
    lines = []
    for t in tasks:
        subs = t.get("subtasks") or []
        n = sum(1 for x in subs if x.get("done"))
        ok = bool(t.get("done")) and n == len(subs)
        cnt = f" ({n}/{len(subs)})" if subs else ""
        lines.append(f"- [{'x' if ok else ' '}] {t.get('title', '?')}{cnt}")
        if not ok:
            lines += [f"  - [{'x' if x.get('done') else ' '}] {x.get('text', '')}" for x in subs]
    return lines


def _render_summary(sm: dict) -> list[str]:
    head = "Claude 梳理（已过期）：" if sm.get("stale") else "Claude 梳理："
    lines = [head]
    if sm.get("tasks"):
        lines += _render_task_tree(sm["tasks"])
    else:
        lines += [f"- [x] {t}" for t in sm.get("done") or []]
        lines += [f"- [ ] {t}" for t in sm.get("remaining") or []]
    if sm.get("blocked_on"):
        lines.append(f"在等：{sm['blocked_on']}")
    if sm.get("diff_note"):
        lines.append(f"改动：{sm['diff_note']}")
    return lines


def _render_recap(rc: dict, full: bool) -> list[str]:
    ts = str(rc.get("generated_at") or "")
    hhmm = ts[11:16] if len(ts) >= 16 else "?"
    mark = "，已过期" if rc.get("stale") else ""
    out = [f"recap：已生成 {hhmm}{mark}（用 --recap 打印全文）"]
    if full and rc.get("markdown"):
        out.append(rc["markdown"].rstrip())
    return out


def _render_session(s: dict, changes: dict, full_recap: bool = False) -> list[str]:
    cids = [c for c in s.get("change_ids") or [] if c in changes]
    prog = ", ".join(f"{changes[c]['progress']['done']}/{changes[c]['progress']['total']}" for c in cids)
    label = PHASE_LABEL.get(s.get("phase"), s.get("phase", "?"))
    age = _age(s.get("phase_age_s"))
    title = f"### ● {s.get('name') or s['session_id'][:8]} — {label}"
    if age:
        title += f" {age}"
    if prog:
        title += f" · {prog}"
    out = [title]
    sm = _summary_for(s)
    if not cids:
        out.append("无 spec")
    for c in cids:
        ch = changes[c]
        src = (s.get("change_source") or {}).get(c)
        suffix = f"，关联依据 {src}" if src else ""
        out.append(f"change {ch['id']} ({ch['progress']['done']}/{ch['progress']['total']}){suffix}")
        out += _render_items(ch)
    if sm:
        out += _render_summary(sm)
    if s.get("recap"):
        out += _render_recap(s["recap"], full_recap)
    if s.get("resume_cmd"):
        out.append(f"resume: {s['resume_cmd']}")
    return out


def render(board: dict, full_recap: bool = False) -> str:
    wss = board.get("workspaces", [])
    sessions = [s for w in wss for s in w.get("sessions", [])]
    count = {p: sum(1 for s in sessions if s.get("phase") == p) for p in PHASES}
    ts = (board.get("generated_at") or "").replace("T", " ")[:16]
    out = [f"# harness tracker · {ts}".rstrip(" ·"),
           f"{count['needs_input']} 个 session 在等你 · running {count['running']} · "
           f"done {count['done']} · stale {count['stale']} · exited {count['exited']}", ""]
    for w in wss:
        out.append(f"## {w['name']}")
        changes = {c["id"]: c for c in w.get("changes", [])}
        if not w.get("sessions"):
            out.append("（无 session）")
        for s in w.get("sessions", []):
            out += _render_session(s, changes, full_recap)
            out.append("")
        linked = {c for s in w.get("sessions", []) for c in s.get("change_ids") or []}
        orphan = [c for c in w.get("changes", []) if c["id"] not in linked]
        if orphan:
            out.append("未关联 session 的 change：" + "；".join(
                f"{c['id']} ({c['progress']['done']}/{c['progress']['total']})" for c in orphan))
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def filter_ws(board: dict, ws: str) -> dict | None:
    hit = [w for w in board.get("workspaces", []) if w["name"] == ws]
    if not hit:
        return None
    return {**board, "workspaces": hit}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="harness tracker show", description=__doc__.splitlines()[0])
    ap.add_argument("workspace", nargs="?", help="只看这个 workspace（按 name 匹配）")
    ap.add_argument("--json", action="store_true", help="直接输出 board JSON")
    ap.add_argument("--from-file", metavar="PATH", help="读已保存的 board JSON，不采集")
    ap.add_argument("--recap", action="store_true", help="打印 session recap 的 markdown 全文（有缓存才有）")
    a = ap.parse_args(argv)

    board = load_board(a.workspace, a.from_file)
    if a.workspace:
        filtered = filter_ws(board, a.workspace)
        if filtered is None:
            names = ", ".join(w["name"] for w in board.get("workspaces", [])) or "（无）"
            print(f"workspace 不存在：{a.workspace}。可选：{names}", file=sys.stderr)
            return 2
        board = filtered
    if a.json:
        json.dump(board, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        sys.stdout.write(render(board, a.recap))
    return 0


if __name__ == "__main__":
    sys.exit(main())
