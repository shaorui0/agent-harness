#!/usr/bin/env python3
"""
SessionStart + UserPromptSubmit hook — OpenSpec awareness (nudge only).

Deterministic part (this hook): list in-flight OpenSpec changes at session
start; once per session, if the session has done a lot of writing without being
linked to any change, say so. The judgement "should this be a change?" stays
with the model and the human — the hook never blocks, never edits the prompt.

Opt-in: only active when <root>/openspec/changes/ exists, or project.yml has
`openspec.nudge: true`. `openspec.nudge: false` turns it off. Optional knobs:
`openspec.nudge_min_writes` (default 10), `openspec.nudge_min_files` (default 3).

Standalone: stdlib only (+ _harness), no dependency on tracker/. Never calls a
model, never touches the network, writes only the per-session marker under
~/.harness/run/. Any failure degrades to silent exit 0.

Dry run (prints what would happen, injects nothing, writes no marker):
    python3 harness-openspec-aware.py --dry-run [--root DIR] [--oplog PATH]
        [--since YYYY-MM-DD] [--min-writes N] [--min-files M]
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _harness  # noqa: E402
from _harness import (  # noqa: E402
    emit, emit_context, find_project_root, load_config, read_event, read_yaml_file,
    safe_main,
)

TAG = "openspec-nudge"
TAIL_BYTES = 1024 * 1024
MAX_LISTED = 5
DEFAULT_MIN_WRITES = 10
DEFAULT_MIN_FILES = 3

HEADER = "本 workspace 在做的 OpenSpec change（要继续其中之一就沿用它，不要重复新建）："
NUDGE = (
    "【harness·OpenSpec】这个 session 已写了 {n} 次、涉及 {m} 个文件，但没有关联任何 OpenSpec change。"
    "如果这是要真改东西、≥5 步、可能跨 session 的任务：先 `/opsx:new <对象-动作>`，"
    "用 tasks.md 拆步，再按它做；如果只是调研或一两步的小改动，忽略这条——本 session 不会再提醒。"
)

_TASK_RE = re.compile(r"^\s*[-*]\s+\[( |x|X)\]")


# ---------------------------------------------------------------- scope

def resolve_root(cwd):
    """Project root, else nearest ancestor containing openspec/changes."""
    root = find_project_root(cwd)
    if root is not None:
        return root
    try:
        p = Path(cwd).resolve()
    except Exception:
        return None
    for cand in [p, *p.parents]:
        if (cand / "openspec" / "changes").is_dir():
            return cand
    return None


def settings_for(root, cfg=None):
    """Returns (active, min_writes, min_files)."""
    if cfg is None:
        cfg = {}
        gc = _harness.GLOBAL_CONFIG
        if gc.is_file():
            cfg = read_yaml_file(gc)
        pj = root / ".harness" / "project.yml"
        if pj.is_file():
            cfg = _harness._deep_merge(cfg, read_yaml_file(pj))
    o = cfg.get("openspec")
    o = o if isinstance(o, dict) else {}
    nudge = o.get("nudge")
    has_dir = (root / "openspec" / "changes").is_dir()
    active = nudge is not False and (has_dir or nudge is True)
    return active, _as_int(o.get("nudge_min_writes"), DEFAULT_MIN_WRITES), \
        _as_int(o.get("nudge_min_files"), DEFAULT_MIN_FILES)


def _as_int(v, default):
    try:
        if isinstance(v, bool):
            return default
        n = int(v)
        return n if n >= 0 else default
    except Exception:
        return default


# ---------------------------------------------------------------- changes

def count_tasks(text):
    done = total = 0
    in_code = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        m = _TASK_RE.match(line)
        if m:
            total += 1
            if m.group(1) != " ":
                done += 1
    return done, total


def _read(p):
    try:
        return Path(p).read_text(encoding="utf-8")
    except Exception:
        return ""


def _newest_mtime(d):
    best = 0.0
    try:
        for e in os.scandir(d):
            try:
                best = max(best, e.stat().st_mtime)
            except Exception:
                pass
    except Exception:
        pass
    try:
        best = max(best, os.stat(d).st_mtime)
    except Exception:
        pass
    return best


def _title(d, fallback):
    in_code = False
    for line in _read(Path(d) / "proposal.md").splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
            continue
        if not in_code and line.startswith("# "):
            return line[2:].strip() or fallback
    return fallback


def list_changes(root):
    """Active (not archive) changes: [(mtime, id, done, total, title)]."""
    base = Path(root) / "openspec" / "changes"
    out = []
    try:
        entries = sorted(base.iterdir())
    except Exception:
        return out
    for d in entries:
        if not d.is_dir() or d.name.startswith(".") or d.name == "archive":
            continue
        done, total = count_tasks(_read(d / "tasks.md"))
        out.append((_newest_mtime(d), d.name, done, total, _title(d, d.name)))
    out.sort(key=lambda r: r[0], reverse=True)
    return out


def session_start_text(root):
    rows = [r for r in list_changes(root) if not (r[3] > 0 and r[2] >= r[3])]
    if not rows:
        return ""
    lines = [HEADER]
    for _m, cid, done, total, title in rows[:MAX_LISTED]:
        lines.append(f"{cid}  {done}/{total}  {title}")
    if len(rows) > MAX_LISTED:
        lines.append(f"…另有 {len(rows) - MAX_LISTED} 个")
    return "\n".join(lines)


# ---------------------------------------------------------------- linkage

def _sid_match(entry, sid):
    e = str(entry).strip()
    if not e or not sid:
        return False
    return e == sid or (len(e) >= 8 and sid.startswith(e)) or \
        (len(sid) >= 8 and e.startswith(sid[:8]))


def tracker_linked(root, sid):
    """Name of the change whose tracker.yml lists this session, else None."""
    base = Path(root) / "openspec" / "changes"
    try:
        dirs = sorted(base.iterdir())
    except Exception:
        return None
    for d in dirs:
        tf = d / "tracker.yml"
        if not d.is_dir() or not tf.is_file():
            continue
        sessions = read_yaml_file(tf).get("sessions")
        if isinstance(sessions, str):
            sessions = [sessions]
        if isinstance(sessions, list) and any(_sid_match(s, sid) for s in sessions):
            return d.name
    return None


def _in_changes(target, rec_cwd, root):
    if not isinstance(target, str) or not target:
        return False
    try:
        p = Path(target)
        if not p.is_absolute():
            p = Path(rec_cwd or root) / p
        p = p.resolve()
        base = (Path(root) / "openspec" / "changes").resolve()
        return base in p.parents
    except Exception:
        return False


# ---------------------------------------------------------------- oplog

def iter_oplog(path, tail_bytes=TAIL_BYTES):
    """Yield records. With tail_bytes, read only the last N bytes and drop the
    possibly-truncated first line. Bad lines are skipped."""
    try:
        with open(path, "rb") as fh:
            truncated = False
            if tail_bytes:
                size = fh.seek(0, os.SEEK_END)
                if size > tail_bytes:
                    fh.seek(size - tail_bytes)
                    truncated = True
                else:
                    fh.seek(0)
            data = fh.read()
    except Exception:
        return
    lines = data.split(b"\n")
    if truncated and lines:
        lines = lines[1:]
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            continue
        if isinstance(rec, dict):
            yield rec


def session_stats(records, root):
    """{sid: {writes, targets:set, change_writes, first_ts}}"""
    stats = {}
    for r in records:
        sid = r.get("session_id")
        if not isinstance(sid, str) or not sid:
            continue
        s = stats.setdefault(sid, {"writes": 0, "targets": set(), "change_writes": 0,
                                   "first_ts": str(r.get("ts") or "")})
        s["writes"] += 1
        t = r.get("target")
        if isinstance(t, str) and t:
            s["targets"].add(t)
            if _in_changes(t, r.get("cwd"), root):
                s["change_writes"] += 1
    return stats


def evaluate(stat, root, sid, min_writes, min_files):
    """Returns (would_nudge, linked_reason_or_None)."""
    linked = tracker_linked(root, sid)
    if linked:
        return False, f"tracker:{linked}"
    if stat and stat["change_writes"] > 0:
        return False, "oplog:wrote-change-dir"
    if not stat:
        return False, None
    return (stat["writes"] >= min_writes and len(stat["targets"]) >= min_files), None


# ---------------------------------------------------------------- marker

def marker_path(sid, run_dir=None):
    run_dir = Path(run_dir) if run_dir else _harness.RUN_DIR
    return run_dir / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', sid)}.{TAG}"


def marker_exists(sid, run_dir=None):
    try:
        return marker_path(sid, run_dir).exists()
    except Exception:
        return True  # can't tell -> stay quiet


def write_marker(sid, run_dir=None):
    try:
        p = marker_path(sid, run_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("1", encoding="utf-8")
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- hook

def build_output(event, run_dir=None):
    """Returns (event_name, text) or None. Pure apart from the marker write."""
    name = event.get("hook_event_name")
    if name not in ("SessionStart", "UserPromptSubmit"):
        return None
    cwd = event.get("cwd") or "."
    root = resolve_root(cwd)
    if root is None:
        return None
    cfg, _r = load_config(cwd)
    if _r is None:
        cfg = None  # ancestor-only root: settings_for loads global config itself
    active, min_writes, min_files = settings_for(root, cfg)
    if not active:
        return None

    if name == "SessionStart":
        text = session_start_text(root)
        return ("SessionStart", text) if text else None

    sid = event.get("session_id")
    if not isinstance(sid, str) or not sid or marker_exists(sid, run_dir):
        return None
    stats = session_stats(iter_oplog(root / ".harness" / "oplog.jsonl"), root)
    stat = stats.get(sid)
    would, _why = evaluate(stat, root, sid, min_writes, min_files)
    if not would:
        return None
    if not write_marker(sid, run_dir):
        return None
    return ("UserPromptSubmit",
            NUDGE.format(n=stat["writes"], m=len(stat["targets"])))


def main():
    event = read_event()
    if not isinstance(event, dict):
        emit()
    res = build_output(event)
    if res:
        emit_context(res[0], res[1])
    emit()


# ---------------------------------------------------------------- dry run

def dry_run(argv):
    ap = argparse.ArgumentParser(prog="harness-openspec-aware.py --dry-run")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--root", default=".")
    ap.add_argument("--oplog")
    ap.add_argument("--since")
    ap.add_argument("--min-writes", type=int)
    ap.add_argument("--min-files", type=int)
    a = ap.parse_args(argv)

    root = Path(a.root).resolve()
    active, mw, mf = settings_for(root)
    mw = a.min_writes if a.min_writes is not None else mw
    mf = a.min_files if a.min_files is not None else mf
    oplog = Path(a.oplog) if a.oplog else root / ".harness" / "oplog.jsonl"

    print(f"root 生效（opt-in）: {'是' if active else '否（会静默）'}  "
          f"min_writes={mw}  min_files={mf}")
    recs = [r for r in iter_oplog(oplog, tail_bytes=0)
            if not a.since or str(r.get("ts") or "")[:10] >= a.since]
    stats = session_stats(recs, root)
    order = sorted(stats, key=lambda k: stats[k]["first_ts"])
    n_nudge = 0
    for sid in order:
        s = stats[sid]
        would, why = evaluate(s, root, sid, mw, mf)
        n_nudge += would
        link = f"已关联（{why}）" if why else "未关联"
        print(f"{sid[:8]}  写 {s['writes']:>4} 次  文件 {len(s['targets']):>3} 个  "
              f"{link}  {'会提醒' if would else '不提醒'}")
    print(f"共 {len(order)} 个 session，会提醒 {n_nudge} 个")
    print("--- SessionStart 会注入 ---")
    print(session_start_text(root) or "（无，静默）")
    sys.exit(0)


if __name__ == "__main__":
    if "--dry-run" in sys.argv[1:]:
        try:
            dry_run(sys.argv[1:])
        except SystemExit:
            raise
        except Exception as exc:  # dry run is interactive: show, don't hide
            print(f"dry-run 失败: {exc}")
            sys.exit(0)
    safe_main(main)
