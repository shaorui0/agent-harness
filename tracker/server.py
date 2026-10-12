#!/usr/bin/env python3
"""server.py — harness tracker 的采集后端 + HTTP server（原 claude_monitor）。

实现 collect_once() -> dict，对应 CONTRACT.md 里 GET /api/state 的
{"sessions": [...], "jobs": [...]} 结构（外加一个不对外的 "_events" 字段），
以及 stdlib http.server 实现的路由：GET / , GET /api/state , GET /api/goals。

纯 stdlib，无第三方依赖。所有文件系统 / 子进程读取都用 try/except 隔离，
单个 session 解析失败不影响其他 session。
"""

from __future__ import annotations

import concurrent.futures
import http.server
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlparse
from datetime import datetime, timezone
from pathlib import Path

import config as tracker_config
import specs
import recap
import check

HOME = Path.home()
SESSIONS_DIR = HOME / ".claude" / "sessions"
PROJECTS_DIR = HOME / ".claude" / "projects"
TASKS_DIR = HOME / ".claude" / "tasks"
JOBS_DIR = HOME / ".claude" / "jobs"
TMP_BASE = Path(f"/private/tmp/claude-{os.getuid()}")

STATUS_STALE_SECONDS = 10 * 60     # busy 且 status/transcript 都这么久没动 -> 降级为 unknown（疑似卡死）
IDE_ACTIVE_SECONDS = 60            # IDE 内嵌 session (status 恒 null) 的 transcript mtime 兜底窗口
TAIL_BYTES = 256 * 1024            # 首次 tail 时从文件末尾往前读多少字节

# ---------------------------------------------------------------------------
# 模块级缓存：进程存活期间持续维护，实现「增量 tail」和「事件跃迁检测」。
# ---------------------------------------------------------------------------
_offset_cache: dict[str, int] = {}       # session_id -> transcript 字节偏移量
_derived_cache: dict[str, dict] = {}     # session_id -> 从 transcript 里提炼出的字段（跨轮持久化）
_last_status_cache: dict[str, str] = {}  # session_id -> 上一轮观察到的原始 status（跃迁检测用）
_seen_sessions: set[str] = set()         # 已经采集过至少一轮的 session_id（冷启动不误报事件）

# ---- v2 缓存 ----
EXITED_WINDOW_SECONDS = 6 * 3600   # 进程已死但 transcript 在这个窗口内有更新 -> phase=exited
EXITED_SCAN_TTL = 15               # exited 目录扫描缓存秒数
GIT_TTL = 15                       # git 结果按 (cwd, session) 缓存秒数
GIT_TIMEOUT = 2                    # 单条 git 命令超时
GIT_WAIT_BUDGET = 0.7              # 一次 collect 里最多等 git 刷新这么久（超过就用旧缓存/零值）
HEAD_SCAN_MAX_BYTES = 8 * 1024 * 1024  # 找 spec 时最多从文件头读这么多
_head_cache: dict[str, dict] = {}        # transcript path -> {spec, cwd, first_ts, scanned, done}
_exited_scan_cache: dict = {"ts": 0.0, "items": []}
_git_cache: dict[tuple, dict] = {}       # (cwd, session_id) -> {"ts":..., "diff": {...}}
_git_inflight: set[tuple] = set()
_git_toplevel: dict[str, str | None] = {}
_git_lock = threading.Lock()
_git_pool = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="git")
_last_sessions: dict[str, dict] = {}     # session_id -> 最近一次 collect 的 session dict（/api/check、/api/recap 用）


# =============================================================== utilities


def slug_for(path: str) -> str:
    """cwd 绝对路径 -> ~/.claude/projects 下的目录名（/ 和 _ 等都换成 -）。"""
    return re.sub(r"[^a-zA-Z0-9]", "-", str(path))


def _ms_to_iso(ms) -> str | None:
    if not ms:
        return None
    try:
        return (
            datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def strip_reminders(text: str) -> str:
    text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.S)
    text = re.sub(r"<local-command-[a-z-]+>.*?</local-command-[a-z-]+>", "", text, flags=re.S)
    text = re.sub(r"<command-(name|message|args)>.*?</command-\1>", " ", text, flags=re.S)
    text = re.sub(r"</?pasted_content[^>]*>", "", text)
    return text.strip()


def _meaningful_text(text: str) -> str:
    """去掉输出格式里的纯分隔/段名行（如 `===== 【结论】 =====`），留第一行有内容的。"""
    lines = [ln for ln in text.splitlines() if re.sub(r"[=\-—\s【】#*|]", "", re.sub(r"【[^】]*】", "", ln))]
    return "\n".join(lines) if lines else text


def _clean_text(text: str, n: int = 1500) -> str:
    """last_text：去掉行首 # 与 **，保留换行，超长截断加 …。"""
    lines = []
    for ln in (text or "").splitlines():
        if ln.strip() and not re.sub(r"[=\-—\s【】#*|]", "", re.sub(r"【[^】]*】", "", ln)):
            continue              # 纯分隔 / 段名行
        ln = re.sub(r"^\s*#+\s*", "", ln).replace("**", "").rstrip()
        lines.append(ln)
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return out if len(out) <= n else out[:n].rstrip() + "…"


def _clip(text: str, n: int) -> str:
    text = re.sub(r"[ \t]+", " ", (text or "").strip())
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text if len(text) <= n else text[:n].rstrip() + " …"


def blocks_of(entry: dict) -> list:
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return content or []


def is_human_prompt(entry: dict) -> bool:
    """真·人类输入：排除 sidechain（subagent 内部）、isMeta（hook 注入）、纯 tool_result。"""
    if entry.get("type") != "user" or entry.get("isSidechain"):
        return False
    if entry.get("isMeta"):
        return False
    origin = entry.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return False
    blocks = blocks_of(entry)
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            return False
    text = " ".join(
        b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"
    )
    return bool(strip_reminders(text))


def _human_prompt_text(entry: dict) -> str:
    text = " ".join(
        b.get("text", "") for b in blocks_of(entry) if isinstance(b, dict) and b.get("type") == "text"
    )
    return _clip(strip_reminders(text), 200)


def _summarize_tool_use(block: dict) -> str:
    name = block.get("name", "?")
    inp = block.get("input") or {}
    if name == "Bash":
        cmd = _clip(str(inp.get("command", "")), 300).replace("\n", " ; ")
        return f"Bash: {cmd}"
    if name in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
        path = inp.get("file_path") or inp.get("notebook_path") or "?"
        return f"{name}: {path}"
    if name == "Read":
        return f"Read: {inp.get('file_path', '?')}"
    if name in ("Glob", "Grep"):
        return f"{name}: {_clip(str(inp.get('pattern', '')), 300)}"
    if name in ("Task", "Agent"):
        desc = inp.get("description") or _clip(str(inp.get("prompt", "")), 300)
        return f"Agent[{inp.get('subagent_type', '?')}]: {desc}"
    if name == "TodoWrite":
        return "TodoWrite"
    if name == "Skill":
        return f"Skill: /{inp.get('skill', '?')}"
    desc = inp.get("description")
    if isinstance(desc, str) and desc:
        return f"{name}: {_clip(desc, 300)}"
    for v in inp.values():
        if isinstance(v, str) and v:
            return f"{name}: {_clip(v, 300)}"
    return name


def _tail_read(path: Path, start_offset):
    """增量读取 jsonl 里从 start_offset 到 EOF 的完整行。

    start_offset=None 时做首次「tail 最后 TAIL_BYTES 字节」读取。返回的
    new_offset 永远落在行边界上——末尾可能截断的不完整行不会被消费，留给
    下一次调用（此时它多半已经写完整了）。
    """
    try:
        size = path.stat().st_size
    except OSError:
        return [], (start_offset or 0)

    # 只有「首次读取」或「文件被截断/轮转过」时才需要重新定位到 tail 窗口——
    # 这种情况下 seek 点落在行中间是预期的。一个已经存过的 offset 则保证
    # 是上一轮消费后留下的行边界，不能再套用「丢弃第一行」的逻辑，否则会
    # 把两次 collect_once() 之间新增的第一条记录（往往正是新的人类输入）
    # 平白丢掉。
    freshly_seeked = start_offset is None or start_offset > size
    if freshly_seeked:
        start_offset = max(0, size - TAIL_BYTES)

    try:
        with path.open("rb") as fh:
            fh.seek(start_offset)
            data = fh.read()
    except OSError:
        return [], start_offset

    if not data:
        return [], start_offset

    base = start_offset
    if freshly_seeked and start_offset > 0:
        # 从文件中间 seek 进来的，第一行大概率是残行，丢弃
        nl = data.find(b"\n")
        if nl == -1:
            return [], start_offset
        base += nl + 1
        data = data[nl + 1 :]

    if data.endswith(b"\n"):
        parts = data.split(b"\n")[:-1]
        consumed = len(data)
    else:
        parts = data.split(b"\n")
        consumed = len(data) - len(parts[-1])
        parts = parts[:-1]

    entries = []
    for raw in parts:
        line = raw.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line.decode("utf-8", errors="replace")))
        except json.JSONDecodeError:
            continue

    return entries, base + consumed


# ===================================================== transcript 增量提炼


def _process_transcript(session_id: str, cwd: str, path: Path | None = None) -> dict:
    """增量 tail transcript，更新并返回该 session 的持久化派生字段 + 本轮新事件。"""
    state = _derived_cache.setdefault(
        session_id,
        {
            "last_human_prompt": None,
            "last_human_prompt_uuid": None,
            "last_action": None,
            "last_text": None,   # 最近一段有内容的 assistant 文字（不被后续工具调用覆盖）
            "last_tool": None,   # 最近一次工具调用的单行描述
            "model": None,
            "ctx_tokens_left": 0,
            "git_branch": None,
            "files": {},   # file_path -> 序号（越大越新），上限 200
            "todos": [],   # 最近一次 TodoWrite
        },
    )
    events: list[dict] = []

    if path is None:
        path = PROJECTS_DIR / slug_for(cwd) / f"{session_id}.jsonl"
    if not path.is_file():
        return {"state": state, "events": events}

    prev_offset = _offset_cache.get(session_id)
    entries, new_offset = _tail_read(path, prev_offset)
    _offset_cache[session_id] = new_offset

    for entry in entries:
        try:
            if not isinstance(entry, dict):
                continue
            etype = entry.get("type")

            if entry.get("gitBranch"):
                state["git_branch"] = entry["gitBranch"]

            if entry.get("isSidechain"):
                continue  # subagent 内部对话不参与「人类输入 / 最后动作」判定

            if etype == "user" and is_human_prompt(entry):
                txt = _human_prompt_text(entry)
                if txt:
                    uuid = entry.get("uuid")
                    if session_id in _seen_sessions and uuid != state.get("last_human_prompt_uuid"):
                        events.append(
                            {"session_id": session_id, "type": "new_prompt", "prompt": txt}
                        )
                    state["last_human_prompt"] = txt
                    state["last_human_prompt_uuid"] = uuid

            elif etype == "assistant":
                msg = entry.get("message") or {}
                if msg.get("model"):
                    state["model"] = msg["model"]
                tool_line = None
                text_line = None
                for block in blocks_of(entry):
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        tool_line = _summarize_tool_use(block)
                        _track_tool_use(state, block)
                    elif block.get("type") == "text" and (block.get("text") or "").strip():
                        cleaned = _clean_text(block["text"])
                        if cleaned:
                            text_line = cleaned
                if text_line:
                    state["last_text"] = text_line
                if tool_line:
                    state["last_tool"] = _clip(tool_line, 600)
                    state["last_action"] = state["last_tool"]
                elif text_line:
                    state["last_action"] = _clip(text_line, 600)

            elif etype == "attachment":
                att = entry.get("attachment") or {}
                if att.get("type") == "total_tokens_reminder":
                    m = re.search(r"(\d+)", att.get("text", "") or "")
                    if m:
                        state["ctx_tokens_left"] = int(m.group(1))
        except Exception:
            continue  # 单条记录解析失败不影响其它记录

    return {"state": state, "events": events}


def _track_tool_use(state: dict, block: dict) -> None:
    name = block.get("name")
    inp = block.get("input") or {}
    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        fp = inp.get("file_path") or inp.get("notebook_path")
        if isinstance(fp, str) and fp:
            files = state["files"]
            files.pop(fp, None)
            files[fp] = len(files) and (max(files.values()) + 1) or 1
            if len(files) > 200:
                files.pop(min(files, key=files.get), None)
    elif name == "TodoWrite":
        items = []
        for t in inp.get("todos") or []:
            if isinstance(t, dict) and t.get("content"):
                st = t.get("status")
                items.append({"content": _clip(str(t["content"]), 200),
                              "status": st if st in ("pending", "in_progress", "completed") else "pending"})
        state["todos"] = items


def _head_info(path: Path) -> dict:
    """从文件头增量扫描，一次性抓到 第一条人类输入(spec) / cwd / 第一条时间戳，并缓存。
    transcript 是 tail 增量读的，spec 必须单独从头找；找到后不再读，没找到则从上次 offset 续扫。"""
    key = str(path)
    info = _head_cache.setdefault(
        key, {"spec": None, "cwd": None, "first_ts": None, "scanned": 0, "done": False}
    )
    if info["done"]:
        return info
    try:
        size = path.stat().st_size
        if size <= info["scanned"] or info["scanned"] >= HEAD_SCAN_MAX_BYTES:
            return info
        with path.open("rb") as fh:
            fh.seek(info["scanned"])
            while info["scanned"] < HEAD_SCAN_MAX_BYTES:
                line = fh.readline()
                if not line or not line.endswith(b"\n"):
                    break  # EOF 或半行，下次续
                info["scanned"] += len(line)
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(e, dict):
                    continue
                if not info["cwd"] and e.get("cwd"):
                    info["cwd"] = e["cwd"]
                if not info["first_ts"] and e.get("timestamp"):
                    info["first_ts"] = e["timestamp"]
                if info["spec"] is None and is_human_prompt(e):
                    text = " ".join(
                        b.get("text", "") for b in blocks_of(e)
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                    info["spec"] = _clip(strip_reminders(text), 600)
                if info["spec"] is not None and info["cwd"] and info["first_ts"]:
                    info["done"] = True
                    break
    except OSError:
        pass
    return info


# ============================================================ git diff（带缓存）


def _git(cwd: str, *args: str) -> str | None:
    try:
        r = subprocess.run(
            ["git", "-C", cwd, *args], capture_output=True, text=True, timeout=GIT_TIMEOUT,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return r.stdout if r.returncode == 0 else None


_EMPTY_DIFF = {"files_changed": 0, "insertions": 0, "deletions": 0, "commits_since_start": 0, "dirty": False}


def _compute_diff(cwd: str, files: list[str], started_at: str | None) -> dict:
    out = dict(_EMPTY_DIFF)
    if not cwd or not os.path.isdir(cwd):
        return out
    top = _git_toplevel.get(cwd, "")
    if top == "":
        top = (_git(cwd, "rev-parse", "--show-toplevel") or "").strip() or None
        _git_toplevel[cwd] = top
    if not top:
        return out
    inside = [f for f in files if f.startswith(top.rstrip("/") + "/")]
    if inside:
        num = _git(top, "diff", "--numstat", "HEAD", "--", *inside)
        for line in (num or "").splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            out["files_changed"] += 1
            out["insertions"] += int(parts[0]) if parts[0].isdigit() else 0
            out["deletions"] += int(parts[1]) if parts[1].isdigit() else 0
        out["dirty"] = out["files_changed"] > 0
    if started_at:
        # 只数碰过本 session 所改文件的 commit：同仓库多个 session 时，全仓 log 会让每行都显示同一个数。
        log = _git(top, "log", f"--since={started_at}", "--oneline", "--", *inside) if inside else ""
        out["commits_since_start"] = len((log or "").splitlines())
    return out


def _git_refresh(key: tuple, files: list[str], started_at: str | None) -> None:
    try:
        d = _compute_diff(key[0], files, started_at)
        with _git_lock:
            _git_cache[key] = {"ts": time.time(), "diff": d}
    except Exception:
        with _git_lock:
            _git_cache[key] = {"ts": time.time(), "diff": dict(_EMPTY_DIFF)}
    finally:
        with _git_lock:
            _git_inflight.discard(key)


def _git_request(key: tuple, files: list[str], started_at: str | None):
    """缓存新鲜就不动；否则提交后台刷新。返回 future（仅当本次新提交时）。"""
    now = time.time()
    with _git_lock:
        c = _git_cache.get(key)
        if (c and now - c["ts"] < GIT_TTL) or key in _git_inflight:
            return None
        _git_inflight.add(key)
    return _git_pool.submit(_git_refresh, key, files, started_at)


# ================================================= 进程判活 / iTerm 定位


def _all_proc_start_times() -> dict[int, str]:
    """一次性拿到所有存活进程的 lstart（asctime 格式，UTC），避免每个 session 单开一次 ps。

    强制 LC_ALL=C + TZ=UTC 是关键坑：session json 里的 procStart 是用
    UTC + C-locale 的 asctime 顺序（'%a %b %d %H:%M:%S %Y'）写的，但本机
    locale 是 en_AU，默认 `ps -o lstart=` 既会把日期顺序换成 'Www dd Mmm'，
    又会用本地时区（这里是 JST，相差 9 小时）——不strip locale/TZ 直接
    比对字符串必然全部不匹配。
    """
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid,lstart"],
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
        )
    except (subprocess.SubprocessError, OSError):
        return {}
    table: dict[int, str] = {}
    if out.returncode != 0:
        return table
    for line in out.stdout.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        pid_str, lstart = parts
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        table[pid] = re.sub(r"\s+", " ", lstart.strip())
    return table


def _proc_alive(pid, proc_start: str, proc_table: dict[int, str]) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    lstart = proc_table.get(pid)
    if not lstart:
        return False  # ps 里找不到这个 pid -> 进程已死
    expected = re.sub(r"\s+", " ", str(proc_start or "").strip())
    return bool(expected) and lstart == expected  # 对不上 -> pid 被复用


def _iterm_location(pid) -> str | None:
    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        out = subprocess.run(
            ["ps", "eww", str(pid)], capture_output=True, text=True, timeout=3
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if out.returncode != 0:
        return None
    m = re.search(r"ITERM_SESSION_ID=(\S+)", out.stdout)
    if not m:
        return None
    return m.group(1).split(":")[0] or None


# ======================================================= subagents / todo


def _subagent_counts(cwd: str, session_id: str) -> tuple[int, int]:
    done = 0
    inflight = 0
    slug = slug_for(cwd)
    try:
        subagents_dir = PROJECTS_DIR / slug / session_id / "subagents"
        if subagents_dir.is_dir():
            done = sum(1 for _ in subagents_dir.glob("agent-*.meta.json"))
    except OSError:
        pass
    try:
        tasks_dir = TMP_BASE / slug / session_id / "tasks"
        if tasks_dir.is_dir():
            for entry in tasks_dir.iterdir():
                if entry.is_symlink():
                    inflight += 1
    except OSError:
        pass
    return done, inflight


def _todo_counts(session_id: str) -> tuple[int, int]:
    done = 0
    total = 0
    d = TASKS_DIR / session_id
    if not d.is_dir():
        return 0, 0
    try:
        files = list(d.glob("*.json"))  # glob 天然排掉 .lock / .highwatermark
    except OSError:
        return 0, 0
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        total += 1
        if data.get("status") == "completed":
            done += 1
    return done, total


def _todo_items(session_id: str, fallback: list) -> list[dict]:
    """优先 ~/.claude/tasks/<sid>/*.json（与 _todo_counts 同源），没有则用 transcript 里最近一次 TodoWrite。"""
    items: list[dict] = []
    d = TASKS_DIR / session_id
    if d.is_dir():
        try:
            for f in sorted(d.glob("*.json"), key=lambda x: (len(x.stem), x.stem)):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                content = data.get("subject") or data.get("content") or data.get("activeForm")
                if not content:
                    continue
                st = data.get("status")
                items.append({"content": _clip(str(content), 200),
                              "status": st if st in ("pending", "in_progress", "completed") else "pending"})
        except OSError:
            pass
    return (items or list(fallback or []))[:20]


# ================================================== exited session 发现


def _discover_exited(live_ids: set[str], dead_meta: dict[str, dict], now: float) -> list[dict]:
    """进程已死但 transcript 6h 内有更新的 session。

    ~/.claude/sessions/*.json 在进程退出后不一定被清（也不保证有），所以不依赖它：
    直接扫 ~/.claude/projects/*/*.jsonl 的 mtime（只 stat，不读内容），目录扫描缓存 15s。
    cwd 从 jsonl 头部读（slug 是有损编码，不能反推），读一次永久缓存。
    """
    c = _exited_scan_cache
    if now - c["ts"] >= EXITED_SCAN_TTL:
        items = []
        try:
            for proj in PROJECTS_DIR.iterdir():
                if not proj.is_dir():
                    continue
                try:
                    for f in proj.glob("*.jsonl"):
                        try:
                            mt = f.stat().st_mtime
                        except OSError:
                            continue
                        if now - mt <= EXITED_WINDOW_SECONDS:
                            items.append({"path": f, "sid": f.stem, "mtime": mt})
                except OSError:
                    continue
        except OSError:
            pass
        items.sort(key=lambda x: -x["mtime"])
        c["items"], c["ts"] = items[:60], now
    out = []
    for it in c["items"]:
        if it["sid"] in live_ids:
            continue
        # 缓存期内 mtime 可能已变旧，重新 stat 一次便宜且准确
        try:
            mt = it["path"].stat().st_mtime
        except OSError:
            continue
        if now - mt > EXITED_WINDOW_SECONDS:
            continue
        out.append({**it, "mtime": mt, "meta": dead_meta.get(it["sid"])})
    return out


def _phase_of(status: str | None) -> str:
    return {"busy": "running", "shell": "running", "waiting": "needs_input", "idle": "done"}.get(status or "", "stale")


def _v2_fields(derived: dict, head: dict, session_id, cwd, started_iso, phase, phase_since_ms_or_s, now,
               transcript_path) -> dict:
    """拼 v2 per-session 字段（不含 diff，diff 之后批量异步填）。phase_since_ms_or_s 为 epoch 秒或 None。"""
    files = derived.get("files") or {}
    ordered = sorted(files, key=files.get, reverse=True)[:30]
    since = phase_since_ms_or_s
    return {
        "phase": phase,
        "phase_since": (datetime.fromtimestamp(since, tz=timezone.utc).isoformat().replace("+00:00", "Z")
                        if since else None),
        "phase_age_s": max(0, int(now - since)) if since else 0,
        "spec": head.get("spec"),
        "files_touched": ordered,
        "diff": dict(_EMPTY_DIFF),
        "todo_items": _todo_items(session_id, derived.get("todos")) if session_id else [],
        "transcript_path": str(transcript_path) if transcript_path else None,
    }


def _v3_fields(sd: dict) -> None:
    """v3 看板字段：name 兜底、turns、resume_cmd（started_at 已有）。"""
    if not sd.get("name"):
        sd["name"] = sd.get("project_name") or (sd.get("spec") or "")[:30] or None
    tp = sd.get("transcript_path")
    sd["turns"] = _count_turns(Path(tp)) if tp else 0
    cwd, sid = sd.get("cwd"), sd.get("session_id")
    sd["resume_cmd"] = f"cd {shlex.quote(cwd)} && claude --resume {sid}" if cwd and sid else None


_turns_cache: dict[str, tuple[int, int]] = {}   # transcript path -> (已扫字节, 人类回合数)


def _count_turns(path: Path) -> int:
    """人类回合数：从文件头增量扫描（只解析含 "type":"user" 的行），结果按偏移缓存。"""
    key = str(path)
    off, count = _turns_cache.get(key, (0, 0))
    try:
        size = path.stat().st_size
        if size < off:
            off, count = 0, 0
        if size == off:
            return count
        with path.open("rb") as fh:
            fh.seek(off)
            while True:
                line = fh.readline()
                if not line or not line.endswith(b"\n"):
                    break
                off += len(line)
                if b'"type":"user"' not in line and b'"type": "user"' not in line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(e, dict) and not e.get("isSidechain") and is_human_prompt(e):
                    count += 1
    except OSError:
        return count
    _turns_cache[key] = (off, count)
    return count


# ==================================================================== jobs


def _collect_jobs() -> list[dict]:
    jobs = []
    if not JOBS_DIR.is_dir():
        return jobs
    try:
        entries = list(JOBS_DIR.iterdir())
    except OSError:
        return jobs
    for d in entries:
        try:
            if not d.is_dir():
                continue
            state_path = d / "state.json"
            if not state_path.is_file():
                continue
            data = json.loads(state_path.read_text(encoding="utf-8"))
            if data.get("state") == "done":
                continue
            jobs.append(
                {
                    "daemon_short": d.name,
                    "name": data.get("name"),
                    "state": data.get("state"),
                    "detail": data.get("detail"),
                    "tokens": data.get("tokens") or 0,
                    "updated_at": data.get("updatedAt"),
                }
            )
        except Exception:
            continue
    return jobs


# ============================================================ collect_once


def collect_once() -> dict:
    """对应 /api/state 的 {"sessions": [...], "jobs": [...]} 结构，
    外加一个不对外的 "_events" 字段（新人类输入 / busy->waiting|idle 状态跃迁）。
    """
    now = time.time()
    sessions: list[dict] = []
    events: list[dict] = []

    try:
        session_files = sorted(SESSIONS_DIR.glob("*.json"))
    except OSError:
        session_files = []

    proc_table = _all_proc_start_times()
    live_ids: set[str] = set()
    dead_meta: dict[str, dict] = {}
    diff_jobs: list[tuple] = []  # (session dict, key, files, started_iso)

    for f in session_files:
        try:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue

            pid = data.get("pid")
            proc_start = data.get("procStart")
            if not _proc_alive(pid, proc_start, proc_table):
                # 进程已死：不作为 live，但留作 exited 候选的元数据
                if data.get("sessionId"):
                    dead_meta[data["sessionId"]] = data
                continue

            session_id = data.get("sessionId")
            cwd = data.get("cwd") or ""
            project_name = os.path.basename(cwd.rstrip("/")) if cwd else None

            raw_status = data.get("status")
            status_updated_at_ms = data.get("statusUpdatedAt")
            status = raw_status

            transcript_path = None
            if session_id and cwd:
                transcript_path = PROJECTS_DIR / slug_for(cwd) / f"{session_id}.jsonl"

            if status is None:
                # IDE 内嵌 session：status 恒为 null，用 transcript mtime 兜底
                # （只用于展示，不追求精确）。
                status = "unknown"
                try:
                    if transcript_path is not None and transcript_path.is_file():
                        mtime = transcript_path.stat().st_mtime
                        if now - mtime < IDE_ACTIVE_SECONDS:
                            status = "busy"
                except OSError:
                    pass
            elif raw_status == "busy" and status_updated_at_ms:
                # 只有 busy 会「失联」：idle/waiting 本来就是在等人，几天不动是正常态。
                # busy 太久没更新且 transcript 也不再写入 -> 疑似卡死，降级为 unknown。
                age = now - (status_updated_at_ms / 1000)
                if age > STATUS_STALE_SECONDS:
                    try:
                        silent = transcript_path is None or now - transcript_path.stat().st_mtime > STATUS_STALE_SECONDS
                    except OSError:
                        silent = True
                    if silent:
                        status = "unknown"

            # busy -> waiting/idle 状态跃迁事件（用原始 status，冷启动首轮不误报）
            if session_id:
                prev_raw = _last_status_cache.get(session_id)
                if (
                    session_id in _seen_sessions
                    and prev_raw == "busy"
                    and raw_status in ("waiting", "idle")
                ):
                    events.append(
                        {
                            "session_id": session_id,
                            "type": "status_transition",
                            "prev_status": prev_raw,
                            "cur_status": raw_status,
                        }
                    )
                _last_status_cache[session_id] = raw_status

            derived = {
                "last_human_prompt": None,
                "last_action": None,
                "last_text": None,
                "last_tool": None,
                "model": None,
                "ctx_tokens_left": 0,
                "git_branch": None,
            }
            if session_id and cwd:
                try:
                    result = _process_transcript(session_id, cwd)
                    derived = result["state"]
                    events.extend(result["events"])
                except Exception:
                    pass

            subagents_done, subagents_inflight = 0, 0
            if cwd and session_id:
                try:
                    subagents_done, subagents_inflight = _subagent_counts(cwd, session_id)
                except Exception:
                    pass

            todo_done, todo_total = 0, 0
            if session_id:
                try:
                    todo_done, todo_total = _todo_counts(session_id)
                except Exception:
                    pass

            try:
                iterm_location = _iterm_location(pid)
            except Exception:
                iterm_location = None

            live_ids.add(session_id)
            started_iso = _ms_to_iso(data.get("startedAt"))
            head = {}
            t_mtime = None
            if transcript_path is not None:
                try:
                    head = _head_info(transcript_path)
                    t_mtime = transcript_path.stat().st_mtime
                except OSError:
                    pass
            phase = _phase_of(status)
            su_ms = status_updated_at_ms
            phase_since = (su_ms / 1000) if su_ms else t_mtime
            v2 = _v2_fields(derived, head, session_id, cwd, started_iso, phase, phase_since, now,
                            transcript_path if (transcript_path and t_mtime) else None)

            sess_dict = (
                {
                    "session_id": session_id,
                    "pid": pid,
                    "cwd": cwd or None,
                    "project_name": project_name,
                    "status": status,
                    "waiting_for": data.get("waitingFor"),
                    "name": data.get("name"),
                    "kind": data.get("kind"),
                    "model": derived.get("model"),
                    "started_at": _ms_to_iso(data.get("startedAt")),
                    "status_updated_at": _ms_to_iso(status_updated_at_ms),
                    "last_human_prompt": derived.get("last_human_prompt"),
                    "last_action": derived.get("last_action"),
                    "last_text": derived.get("last_text"),
                    "last_tool": derived.get("last_tool"),
                    "ctx_tokens_left": derived.get("ctx_tokens_left") or 0,
                    "subagents_inflight": subagents_inflight,
                    "subagents_done": subagents_done,
                    "todo_done": todo_done,
                    "todo_total": todo_total,
                    "iterm_location": iterm_location,
                    "git_branch": derived.get("git_branch"),
                    **v2,
                }
            )
            sessions.append(sess_dict)
            diff_jobs.append((sess_dict, (cwd, session_id), derived.get("files") or {}, started_iso))
            if session_id:
                _seen_sessions.add(session_id)
        except Exception:
            continue  # 单个 session 解析失败不能影响其它 session

    # ---- exited sessions ----
    try:
        exited = _discover_exited(live_ids, dead_meta, now)
    except Exception:
        exited = []
    for it in exited:
        try:
            sid, path, meta = it["sid"], it["path"], it["meta"] or {}
            head = _head_info(path)
            cwd = meta.get("cwd") or head.get("cwd") or ""
            derived = _process_transcript(sid, cwd, path)["state"]  # events 丢弃：events 对 exited 无意义
            started_iso = _ms_to_iso(meta.get("startedAt")) or head.get("first_ts")
            v2 = _v2_fields(derived, head, sid, cwd, started_iso, "exited", it["mtime"], now, path)
            sess_dict = {
                "session_id": sid,
                "pid": meta.get("pid") or 0,
                "cwd": cwd or None,
                "project_name": os.path.basename(cwd.rstrip("/")) if cwd else None,
                "status": "unknown",
                "waiting_for": None,
                "name": meta.get("name"),
                "kind": meta.get("kind") or "interactive",
                "model": derived.get("model"),
                "started_at": started_iso,
                "status_updated_at": None,
                "last_human_prompt": derived.get("last_human_prompt"),
                "last_action": derived.get("last_action"),
                    "last_text": derived.get("last_text"),
                    "last_tool": derived.get("last_tool"),
                "ctx_tokens_left": derived.get("ctx_tokens_left") or 0,
                "subagents_inflight": 0,
                "subagents_done": 0,
                "todo_done": sum(1 for t in v2["todo_items"] if t["status"] == "completed"),
                "todo_total": len(v2["todo_items"]),
                "iterm_location": None,
                "git_branch": derived.get("git_branch"),
                **v2,
            }
            sessions.append(sess_dict)
            if cwd:
                diff_jobs.append((sess_dict, (cwd, sid), derived.get("files") or {}, started_iso))
        except Exception:
            continue

    # ---- git diff：缓存命中直接用；过期的丢线程池，最多等 GIT_WAIT_BUDGET ----
    try:
        waits = []
        for sess_dict, key, files_map, started_iso in diff_jobs:
            if not key[0]:
                continue
            files = sorted(files_map, key=files_map.get, reverse=True)[:30]
            fut = _git_request(key, files, started_iso)
            with _git_lock:
                has_cache = key in _git_cache
            if fut is not None and not has_cache:
                waits.append(fut)
        if waits:
            concurrent.futures.wait(waits, timeout=GIT_WAIT_BUDGET)
        with _git_lock:
            for sess_dict, key, _f, _s in diff_jobs:
                c = _git_cache.get(key)
                if c:
                    sess_dict["diff"] = dict(c["diff"])
    except Exception:
        pass

    try:
        jobs = _collect_jobs()
    except Exception:
        jobs = []

    # 过滤旁路 Haiku session（固定 cwd，见 config.SIDECAR_CWD）。
    # 旧版本没隔离 cwd，靠首条 prompt 的固定开头兜底识别。
    sidecar_root = str(tracker_config.SIDECAR_CWD)
    sidecar_ids = {
        sd.get("session_id") for sd in sessions
        if (sidecar_root and (sd.get("cwd") or "").startswith(sidecar_root))
        or (sd.get("spec") or "").startswith("你在做旁路")
    }
    if sidecar_ids:
        sessions = [sd for sd in sessions if sd.get("session_id") not in sidecar_ids]
        events = [ev for ev in events if ev.get("session_id") not in sidecar_ids]

    for sd in sessions:
        try:
            _v3_fields(sd)
        except Exception:
            pass

    counts = {p: 0 for p in ("running", "needs_input", "done", "stale", "exited")}
    oldest = 0
    for sd in sessions:
        counts[sd["phase"]] = counts.get(sd["phase"], 0) + 1
        if sd["phase"] == "needs_input":
            oldest = max(oldest, sd["phase_age_s"])
    summary = {
        "total": len(sessions),
        "running": counts["running"],
        "needs_input": counts["needs_input"],
        "done": counts["done"],
        "stale": counts["stale"],
        "exited": counts["exited"],
        "oldest_needs_input_s": oldest,
        "generated_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
    }

    _last_sessions.clear()
    _last_sessions.update({sd["session_id"]: sd for sd in sessions if sd.get("session_id")})

    return {"sessions": sessions, "jobs": jobs, "summary": summary, "_events": events}


# =============================================================== HTTP server

HOST = "127.0.0.1"
PORT = int(tracker_config.load()["port"])
_MODULE_DIR = Path(__file__).parent
_STATIC_DIR = _MODULE_DIR / "static"
_INDEX_HTML = _STATIC_DIR / "index.html"


_cloudcli_cache: dict = {"ts": 0.0, "up": False}


def _cloudcli_links() -> dict:
    """看板里的 CloudCLI 入口：地址来自配置；是否在线探测一下（1s 超时，缓存 10s）；插件是否已装看目录。"""
    url = str(tracker_config.load().get("cloudcli_url") or "").rstrip("/")
    if not url:
        return {}
    now = time.time()
    if now - _cloudcli_cache["ts"] > 10:
        up = False
        try:
            import urllib.request  # noqa: PLC0415
            with urllib.request.urlopen(url + "/", timeout=1) as r:
                up = 200 <= r.status < 400
        except Exception:  # noqa: BLE001 — 连不上就是没起来
            up = False
        _cloudcli_cache.update(ts=now, up=up)
    plugin = (Path.home() / ".claude-code-ui" / "plugins" / "harness-tracker" / "manifest.json").exists()
    return {"cloudcli": {"url": url, "up": _cloudcli_cache["up"], "plugin_installed": plugin}}


def _build_board(ws: str | None) -> dict:
    """调 board.build，并补上每个 session 的 check / check_pending、recap / recap_pending。board.py 不存在时抛 ImportError。"""
    import board  # noqa: PLC0415 — 延迟导入：board.py 缺失时只影响 /api/board
    data = collect_once()
    sessions = data.get("sessions", [])
    result = board.build(sessions, ws)
    for w in result.get("workspaces", []):
        for sess in w.get("sessions", []):
            sid = sess.get("session_id")
            r = check.get(sid, sess.get("transcript_path"))
            sess["check"] = r["check"]
            sess["check_pending"] = r["pending"]
            rr = recap.get(sid, sess.get("transcript_path"))
            sess["recap"] = rr["recap"]
            sess["recap_pending"] = rr["pending"]
    result["links"] = _cloudcli_links()
    return result


def _board_response(ws: str | None) -> tuple[int, dict]:
    try:
        return 200, _build_board(ws)
    except ImportError:
        return 501, {"error": "board module not available"}


def _check_request(sid: str) -> tuple[int, dict]:
    sess = _last_sessions.get(sid)
    if sess is None:
        collect_once()
        sess = _last_sessions.get(sid)
    if sess is None:
        return 404, {"error": "session not found"}
    changes: list[dict] = []
    try:
        import board  # noqa: PLC0415
        for w in board.build(list(_last_sessions.values()), None).get("workspaces", []):
            for bs in w.get("sessions", []):
                if bs.get("session_id") == sid:
                    ids = set(bs.get("change_ids") or [])
                    changes = [{"id": c["id"], "items": [{"id": i.get("id"), "done": bool(i.get("done")),
                                                          "text": i.get("text")} for i in c.get("items") or []]}
                               for c in w.get("changes", []) if c.get("id") in ids]
    except Exception:  # noqa: BLE001 — board 出错按「无关联」处理，不调 LLM
        changes = []
    result = check.request(sid, sess, changes)
    if result == "no_change":
        return 404, {"error": "no change linked"}
    if result == "cooldown":
        return 200, {"pending": False}
    return 202, {"pending": True}


def _recap_request(sid: str) -> tuple[int, dict]:
    sess = _last_sessions.get(sid)
    if sess is None:
        collect_once()
        sess = _last_sessions.get(sid)
    if sess is None:
        return 404, {"error": "session not found"}
    result = recap.request(sid, sess)
    if result == "cooldown":
        return 200, {"pending": False}
    return 202, {"pending": True}


class MonitorHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send_bytes(body, "application/json; charset=utf-8", status)

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming convention)
        try:
            path = self.path.split("?", 1)[0]

            if path in ("/", "/index.html"):
                try:
                    body = _INDEX_HTML.read_bytes()
                except OSError:
                    self._send_json({"error": "index.html not found"}, status=404)
                    return
                self._send_bytes(body, "text/html; charset=utf-8")
                return

            if path in ("/deck", "/deck.html", "/design", "/design.html"):
                name = "deck.html" if path.startswith("/deck") else "design.html"
                self._send_bytes((_STATIC_DIR / name).read_bytes(), "text/html; charset=utf-8")
                return

            if path == "/data/snapshot.js":
                try:
                    body = tracker_config.SNAPSHOT.read_bytes()
                except OSError:
                    body = b"window.SNAPSHOT = null;\n"
                self._send_bytes(body, "application/javascript; charset=utf-8")
                return

            if path == "/api/state":
                data = collect_once()
                data.pop("_events", None)
                self._send_json(data)
                return

            if path == "/api/board":
                ws = (parse_qs(urlparse(self.path).query).get("ws") or [""])[0] or None
                status, body = _board_response(ws)
                self._send_json(body, status=status)
                return

            if path == "/api/check":
                sid = (parse_qs(urlparse(self.path).query).get("session_id") or [""])[0]
                tpath = (_last_sessions.get(sid) or {}).get("transcript_path")
                self._send_json(check.get(sid, tpath))
                return

            if path == "/api/recap":
                sid = (parse_qs(urlparse(self.path).query).get("session_id") or [""])[0]
                tpath = (_last_sessions.get(sid) or {}).get("transcript_path")
                self._send_json(recap.get(sid, tpath))
                return

            self._send_json({"error": "not found"}, status=404)
        except Exception as e:  # noqa: BLE001 — 单个请求出错不能拖垮 server
            try:
                self._send_json({"error": str(e)}, status=500)
            except Exception:
                pass

    def do_POST(self) -> None:  # noqa: N802
        try:
            path = self.path.split("?", 1)[0]
            if path not in ("/api/check", "/api/recap"):
                self._send_json({"error": "not found"}, status=404)
                return
            try:
                n = min(int(self.headers.get("Content-Length") or 0), 65536)
                body = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, json.JSONDecodeError):
                self._send_json({"error": "bad json"}, status=400)
                return
            sid = body.get("session_id") if isinstance(body, dict) else None
            if not sid or not isinstance(sid, str):
                self._send_json({"error": "session_id required"}, status=400)
                return
            status, resp = (_recap_request if path == "/api/recap" else _check_request)(sid)
            self._send_json(resp, status=status)
        except Exception as e:  # noqa: BLE001
            try:
                self._send_json({"error": str(e)}, status=500)
            except Exception:
                pass

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        sys.stderr.write(
            "%s - - [%s] %s\n"
            % (self.address_string(), self.log_date_time_string(), format % args)
        )


def main() -> None:
    specs.start_background(int(tracker_config.load()["scan_interval_s"]))
    server = http.server.ThreadingHTTPServer((HOST, PORT), MonitorHandler)
    print(f"harness tracker listening on http://{HOST}:{PORT}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--dump":
        # 调试用：只跑一次采集，打印 JSON，不起 HTTP server。
        print(json.dumps(collect_once(), indent=2, ensure_ascii=False))
    else:
        main()
