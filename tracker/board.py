"""/api/board 组装：session 按 workspace 归组、挂上 OpenSpec change、算 session↔change 关联。

只读。check / check_pending 由 server 填（这里置 None / False）。
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import config as tracker_config
import openspec

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

_harness = tracker_config._harness

PHASE_ORDER = {"needs_input": 0, "running": 1, "stale": 2, "done": 3, "exited": 4}
SKIP_DIRS = {"node_modules", ".git", "archive", "_archive", "archives", "vendor", ".venv",
             "venv", "__pycache__", "site-packages", "target", "dist", "build", "openspec"}
OPLOG_TAIL_BYTES = 4 * 1024 * 1024
ROOTS_TTL_S = 30
ROOTS_MAX_DEPTH = 7

_CHANGE_PATH_RE = re.compile(r"openspec/changes/([^/\s\"'\\]+)")

_lock = threading.Lock()
_root_cache: dict[str, str] = {}
_oplog_cache: dict[str, tuple[tuple, dict]] = {}
_roots_cache: dict = {"t": 0.0, "key": None, "val": []}


# ------------------------------------------------------------ workspace root

def _git_toplevel(cwd: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=2)
        return r.stdout.strip() or None if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def workspace_root(cwd: str | None) -> str:
    if not cwd:
        return ""
    with _lock:
        hit = _root_cache.get(cwd)
    if hit is not None:
        return hit
    root = None
    try:
        r = _harness.find_project_root(cwd)
        if r:
            root = str(r)
    except Exception:
        pass
    if root is None:
        try:
            p = Path(cwd)
            for cand in [p, *p.parents]:
                if (cand / "openspec").is_dir():
                    root = str(cand)
                    break
        except Exception:
            pass
    if root is None and os.path.isdir(cwd):
        root = _git_toplevel(cwd)
    root = root or cwd
    with _lock:
        _root_cache[cwd] = root
    return root


# ------------------------------------------------------------ roots 扫描

def find_openspec_workspaces(roots) -> list[str]:
    found = []
    for root in roots:
        root = str(root)
        base_depth = root.rstrip(os.sep).count(os.sep)
        for dirpath, dirnames, _ in os.walk(root):
            if "openspec" in dirnames and os.path.isdir(os.path.join(dirpath, "openspec", "changes")):
                found.append(dirpath)
            depth = dirpath.count(os.sep) - base_depth
            if depth >= ROOTS_MAX_DEPTH:
                dirnames[:] = []
            else:
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
    return sorted({os.path.realpath(f) for f in found})


def _scan_roots_cached(roots) -> list[str]:
    key = tuple(str(r) for r in roots)
    now = time.time()
    with _lock:
        c = _roots_cache
        if c["key"] == key and now - c["t"] < ROOTS_TTL_S:
            return c["val"]
    val = find_openspec_workspaces(roots)
    with _lock:
        _roots_cache.update(t=now, key=key, val=val)
    return val


# ------------------------------------------------------------ oplog

def _change_ids_in(*texts) -> set[str]:
    out = set()
    for t in texts:
        if isinstance(t, str) and "openspec/changes/" in t:
            out.update(_CHANGE_PATH_RE.findall(t))
    return out


def oplog_changes(ws: str) -> dict[str, set[str]]:
    """{session_id: {change id}}，只读 oplog 尾部 4MB，按 (path, mtime, size) 缓存。"""
    p = os.path.join(ws, ".harness", "oplog.jsonl")
    try:
        st = os.stat(p)
    except OSError:
        return {}
    key = (st.st_mtime_ns, st.st_size)
    with _lock:
        hit = _oplog_cache.get(p)
    if hit and hit[0] == key:
        return hit[1]
    res: dict[str, set[str]] = {}
    try:
        with open(p, "rb") as f:
            start = max(0, st.st_size - OPLOG_TAIL_BYTES)
            f.seek(start)
            data = f.read()
        lines = data.split(b"\n")
        if start > 0:
            lines = lines[1:]  # 首行可能被截断
        for ln in lines:
            if b"openspec/changes/" not in ln:
                continue
            try:
                rec = json.loads(ln)
            except ValueError:
                continue
            sid = rec.get("session_id")
            ids = _change_ids_in(rec.get("target"), rec.get("command"))
            if sid and ids:
                res.setdefault(sid, set()).update(ids)
    except OSError:
        return {}
    with _lock:
        _oplog_cache[p] = (key, res)
    return res


# ------------------------------------------------------------ 关联

def _tracker_yml_sessions(ws: str, cid: str) -> list[str]:
    f = os.path.join(ws, "openspec", "changes", cid, "tracker.yml")
    if not os.path.isfile(f):
        return []
    try:
        with open(f, encoding="utf-8") as fh:
            d = yaml.safe_load(fh) or {}
        s = d.get("sessions") if isinstance(d, dict) else None
        return [str(x).strip() for x in (s or []) if x is not None and str(x).strip()]
    except Exception:
        return []


def _bound(sid: str, entries: list[str]) -> bool:
    return any(sid == e or (len(e) >= 8 and sid.startswith(e)) for e in entries)


def _mentions(cid: str, *texts) -> bool:
    pat = re.compile(r"(?<![\w-])" + re.escape(cid) + r"(?![\w-])", re.I)
    return any(isinstance(t, str) and pat.search(t) for t in texts)


def _touched_ids(sess: dict, ws: str) -> set[str]:
    out = set()
    for f in sess.get("files_touched") or []:
        if not isinstance(f, str):
            continue
        if os.path.isabs(f) and not f.startswith(ws.rstrip(os.sep) + os.sep):
            continue
        out |= _change_ids_in(f)
    return out


def link(ws: str, ws_sessions: list[dict], changes: list[dict]) -> None:
    """填 session.change_ids / change_source 与 change.session_ids。每个 session 命中最高优先级那一级即停。"""
    if not changes:
        return
    ids = {c["id"] for c in changes}
    yml = {c["id"]: _tracker_yml_sessions(ws, c["id"]) for c in changes}
    oplog = oplog_changes(ws)
    for s in ws_sessions:
        sid = s.get("session_id") or ""
        found = {cid: "tracker.yml" for cid in ids if sid and _bound(sid, yml[cid])}
        if not found:
            touched = (oplog.get(sid, set()) | _touched_ids(s, ws)) & ids
            found = {cid: "touched" for cid in touched}
        if not found:
            found = {cid: "mention" for cid in ids if _mentions(cid, s.get("name"), s.get("spec"))}
        s["change_ids"] = sorted(found)
        s["change_source"] = {k: found[k] for k in sorted(found)}
    for c in changes:
        c["session_ids"] = [s["session_id"] for s in ws_sessions if c["id"] in s["change_ids"]]


# ------------------------------------------------------------ build

def _normalize(s: dict) -> dict:
    s = dict(s)
    s["name"] = s.get("name") or s.get("project_name") or (s.get("spec") or "")[:30] or None
    s.setdefault("turns", 0)
    if s["turns"] is None:
        s["turns"] = 0
    sid, cwd = s.get("session_id"), s.get("cwd")
    if not s.get("resume_cmd") and sid:
        s["resume_cmd"] = (f"cd {shlex.quote(cwd)} && " if cwd else "") + f"claude --resume {sid}"
    s["check"], s["check_pending"] = None, False  # server 负责填
    s["change_ids"], s["change_source"] = [], {}
    return s


def _sort_key(s: dict):
    return (PHASE_ORDER.get(s.get("phase"), 9), -(_ts(s.get("phase_since"))))


def _ts(v) -> float:
    if not v:
        return 0.0
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


_PHASE_PRI = {"needs_input": 0, "running": 1, "stale": 2, "done": 3, "exited": 4}


def dedupe_sessions(sessions) -> list[dict]:
    """同一个 session_id 可能有多个进程文件（例如 --resume 又起了一个进程）。看板上一个 session 只能有一行：
    保留最「活跃」的那条（needs_input > running > stale > done > exited），同级取 phase_since 最新的。"""
    best: dict[str, dict] = {}
    out_no_id: list[dict] = []
    for s in sessions or []:
        sid = s.get("session_id")
        if not sid:
            out_no_id.append(s)
            continue
        cur = best.get(sid)
        if cur is None:
            best[sid] = s
            continue
        a = (_PHASE_PRI.get(s.get("phase"), 9), s.get("phase_since") or "")
        b = (_PHASE_PRI.get(cur.get("phase"), 9), cur.get("phase_since") or "")
        if a[0] < b[0] or (a[0] == b[0] and a[1] > b[1]):
            best[sid] = s
    return list(best.values()) + out_no_id


def build(sessions, ws: str | None = None) -> dict:
    groups: dict[str, list[dict]] = {}
    for s in dedupe_sessions(sessions):
        n = _normalize(s)
        groups.setdefault(workspace_root(n.get("cwd")), []).append(n)

    paths = set(groups)
    try:
        paths.update(_scan_roots_cached(tracker_config.load()["roots"]))
    except Exception:
        pass

    out = []
    for path in sorted(paths):
        name = os.path.basename(path.rstrip(os.sep)) if path else "(no cwd)"
        if ws and name != ws:
            continue
        sess = sorted(groups.get(path, []), key=_sort_key)
        changes = openspec.scan(path) if path else []
        if not sess and not changes:
            continue
        link(path, sess, changes)
        out.append({"name": name, "path": path, "sessions": sess, "changes": changes})
    out.sort(key=lambda w: (not w["sessions"], w["name"]))  # 有 session 的在前

    counts = {p: 0 for p in PHASE_ORDER}
    total = 0
    for w in out:
        for s in w["sessions"]:
            total += 1
            counts[s.get("phase")] = counts.get(s.get("phase"), 0) + 1
    summary = {"workspaces": len(out), "sessions": total, **{k: counts[k] for k in PHASE_ORDER}}
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "summary": summary,
        "workspaces": out,
    }
