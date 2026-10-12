"""手动摘要：只在 POST /api/summary 时生成，没有任何自动触发路径。

流程：session_recap.py 提取骨架 + 关联 change 的 tasks.md + diff 统计 -> 拼 prompt ->
隔离 cwd 里跑 `claude -p --model <summary_model，缺省 sonnet> --no-session-persistence` -> 解析 JSON -> 追加写 summaries.jsonl。
读取时现算 stale（transcript mtime 比生成时的 source_mtime 晚 60 秒以上）。纯 stdlib。
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import config as tracker_config

DEFAULT_MODEL = "sonnet"   # 任务梳理要判断「做完没有」，需要比 haiku 更强的模型；可用 tracker.summary_model 改


def _model() -> str:
    try:
        return str(tracker_config.load().get("summary_model") or DEFAULT_MODEL)
    except Exception:  # noqa: BLE001 — 配置读不出来就用缺省
        return DEFAULT_MODEL


# --setting-sources project：不加载用户级 settings。用户级 hooks 会往每次调用里注入「输出格式契约」，
# 模型就不再只输出 JSON（实测：摘要全部解析失败）。旁路调用不需要任何 hook。
_CLAUDE_ARGS = ["--no-session-persistence", "--setting-sources", "project"]
_RECAP_SCRIPT = Path(__file__).resolve().parent / "session_recap.py"
EXTRACT_TIMEOUT = 20
CLAUDE_TIMEOUT = 150
HEAD_CHARS = 1500
TAIL_CHARS = 12000
COOLDOWN_S = 60          # 生成完（含失败）后这么久内不重复触发
STALE_AFTER_S = 60       # transcript 比 source_mtime 晚这么久 -> stale
TASKS_MAX_CHARS = 4000

TASKS_MAX = 15
SUBTASKS_MAX = 12
TEXT_MAX = 120

PROMPT_TEMPLATE = """你在做旁路 session 摘要，不参与任务执行。只输出一个 JSON 对象，不要解释，不要代码围栏，不要 JSON 之外的任何文字。

目标：让人一眼看到「这个 session 里完成了哪些任务、每个任务的子任务完成了哪些」。不关心过程。

格式：
{"tasks": [{"title": "...", "done": true, "subtasks": [{"text": "...", "done": true}]}],
 "blocked_on": "..." 或 null, "diff_note": "..."}

规则：
- tasks：人类在这个 session 里提出的、有独立交付意义的事。按人类意图分，不按工具调用分；一个任务通常对应人类的一个诉求，不要把每次读文件、每条命令都拆成任务。
- subtasks：该任务下的具体步骤，每条一句。
- done：必须有 transcript 里的动作证据（实际执行的命令、写入的文件、得到的结果）。「说要做」「计划做」不算完成；看不出来就 false。task.done 为 true 当且仅当它的所有 subtask 都 done；没有 subtask 时由你判断。
- 如果下面有关联的任务清单（tasks.md）：任务 title 尽量对齐其中的分组标题；tasks.md 里没有、但 session 里实际做了或提出了的任务，也要补上。
- blocked_on：在等谁或什么（用户确认、外部结果等），没有就 null。
- diff_note：一句话说明改了哪些地方（可参考下面的 diff 统计）。
- 看不出来的不要编；没有就给空数组或 null。最多 15 个任务，每个任务最多 12 个子任务，每句不超过 120 字。

## 关联的任务清单（tasks.md）
{tasks}

## diff 统计
{diff}

## session 骨架
{skeleton}
"""

_lock = threading.Lock()
_inflight: set[str] = set()
_last_done: dict[str, float] = {}     # session_id -> 上次生成结束（monotonic 无关，用 time.time）
_errors: dict[str, str] = {}
_cache: dict[str, dict] = {}
_loaded = False


def _jsonl_path() -> Path:
    return tracker_config.STATE_DIR / "summaries.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _ts_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _iso_ts(s) -> float | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


# ------------------------------------------------------------------ 外部调用点（测试里 monkeypatch）

def _run_extract(transcript_path: str, cwd: str | None) -> str:
    proc = subprocess.run(
        [sys.executable, str(_RECAP_SCRIPT), str(Path(transcript_path).resolve())],
        capture_output=True, text=True, timeout=EXTRACT_TIMEOUT,
        cwd=cwd if cwd and Path(cwd).is_dir() else None,
    )
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        raise RuntimeError("session_recap 提取失败")
    return proc.stdout


def _run_claude(prompt: str) -> str:
    tracker_config.SIDECAR_CWD.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["claude", "-p", "--model", _model()] + _CLAUDE_ARGS + [prompt], capture_output=True, text=True,
        timeout=CLAUDE_TIMEOUT, cwd=str(tracker_config.SIDECAR_CWD),
        env=tracker_config.claude_env(),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude 退出码 {proc.returncode}: {(proc.stderr or '')[:200]}")
    return proc.stdout or ""


# ------------------------------------------------------------------ 解析 / 拼装

def parse_json_obj(text: str) -> dict:
    """容错：可能被 ``` 包裹或带前后文字，取第一个 { 到最后一个 }。"""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        raise ValueError("输出里没有 JSON 对象")
    obj = json.loads(text[start:end + 1])
    if not isinstance(obj, dict):
        raise ValueError("JSON 不是对象")
    return obj


def _str_list(v) -> list[str]:
    if not isinstance(v, list):
        return []
    return [str(x).strip() for x in v if str(x).strip()]


def _clip(v) -> str:
    return str(v).strip()[:TEXT_MAX] if v is not None else ""


def _parse_tasks(v) -> list[dict] | None:
    """解析模型给的 tasks；不是列表 -> None（视为没给）。字段缺失/类型不对的条目丢弃或修正。"""
    if not isinstance(v, list):
        return None
    out = []
    for t in v:
        if not isinstance(t, dict):
            continue
        title = _clip(t.get("title"))
        if not title:
            continue
        subs = []
        raw_subs = t.get("subtasks")
        for st in raw_subs if isinstance(raw_subs, list) else []:
            if isinstance(st, dict):
                text, d = _clip(st.get("text")), st.get("done")
            elif isinstance(st, str):
                text, d = _clip(st), False
            else:
                continue
            if text:
                subs.append({"text": text, "done": d is True})
            if len(subs) >= SUBTASKS_MAX:
                break
        done = all(x["done"] for x in subs) if subs else t.get("done") is True
        out.append({"title": title, "done": done, "subtasks": subs})
        if len(out) >= TASKS_MAX:
            break
    return out


def _compat_lists(tasks: list[dict]) -> tuple[list[str], list[str]]:
    done, remaining = [], []
    for t in tasks:
        items = [(s["text"], s["done"]) for s in t["subtasks"]] or [(t["title"], t["done"])]
        for text, d in items:
            (done if d else remaining).append(text)
    return done, remaining


def normalize(obj: dict, source_mtime: float | None) -> dict:
    blocked = obj.get("blocked_on")
    blocked = str(blocked).strip() if blocked not in (None, "", "null") else None
    now = obj.get("now")
    now = str(now).strip() if now not in (None, "", "null") else None
    tasks = _parse_tasks(obj.get("tasks"))
    if tasks is not None:
        done, remaining = _compat_lists(tasks)
    else:   # 模型没给 tasks：沿用旧结构
        done, remaining = _str_list(obj.get("done")), _str_list(obj.get("remaining"))
    return {
        "tasks": tasks,
        "done": done,
        "remaining": remaining,
        "now": now or None,
        "blocked_on": blocked or None,
        "diff_note": str(obj.get("diff_note") or "").strip(),
        "generated_at": _now_iso(),
        "source_mtime": _ts_iso(source_mtime) if source_mtime else None,
        "model": _model(),
    }


def build_prompt(skeleton: str, session: dict, changes: list[dict]) -> str:
    if len(skeleton) > HEAD_CHARS + TAIL_CHARS:
        skeleton = skeleton[:HEAD_CHARS] + "\n...（中间省略）...\n" + skeleton[-TAIL_CHARS:]
    parts = []
    for ch in changes or []:
        text = (ch.get("tasks_text") or "").strip()
        if text:
            parts.append(f"### {ch.get('id', '')}\n{text[:TASKS_MAX_CHARS]}")
    tasks = "\n\n".join(parts) or "（无关联 change）"
    d = session.get("diff") or {}
    diff = (f"files_changed={d.get('files_changed', 0)} insertions={d.get('insertions', 0)} "
            f"deletions={d.get('deletions', 0)} commits_since_start={d.get('commits_since_start', 0)} "
            f"dirty={d.get('dirty', False)}")
    # 用 replace 而不是 format：骨架里有大量花括号
    return (PROMPT_TEMPLATE.replace("{tasks}", tasks).replace("{diff}", diff)
            .replace("{skeleton}", skeleton))


# ------------------------------------------------------------------ 缓存

def _ensure_loaded() -> None:
    global _loaded
    with _lock:
        if _loaded:
            return
        _loaded = True
        try:
            with _jsonl_path().open(encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(rec, dict) and rec.get("session_id") and isinstance(rec.get("summary"), dict):
                        _cache[rec["session_id"]] = rec["summary"]   # 后写的覆盖前写的
        except OSError:
            pass


def _reset_for_tests() -> None:
    global _loaded
    with _lock:
        _cache.clear(); _errors.clear(); _last_done.clear(); _inflight.clear()
        _loaded = False


def _store(session_id: str, summary: dict) -> None:
    with _lock:
        _cache[session_id] = summary
    try:
        _jsonl_path().parent.mkdir(parents=True, exist_ok=True)
        with _jsonl_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"session_id": session_id, "summary": summary}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _with_stale(summary: dict, transcript_path: str | None) -> dict:
    out = dict(summary)
    out.setdefault("now", None)     # 旧缓存没有 now
    out.setdefault("tasks", None)   # 旧缓存没有 tasks，前端回退到 done/remaining
    stale = False
    src = _iso_ts(summary.get("source_mtime"))
    if transcript_path and src is not None:
        try:
            stale = Path(transcript_path).stat().st_mtime - src > STALE_AFTER_S
        except OSError:
            pass
    out["stale"] = stale
    return out


# ------------------------------------------------------------------ 对外接口

def get(session_id: str, transcript_path: str | None = None) -> dict:
    """返回 GET /api/summary 的响应体：{"summary": ..|None, "pending": bool, "error": str|None}。"""
    _ensure_loaded()
    with _lock:
        cached = _cache.get(session_id)
        pending = session_id in _inflight
        error = _errors.get(session_id)
    return {
        "summary": _with_stale(cached, transcript_path) if cached else None,
        "pending": pending,
        "error": error,
    }


def is_pending(session_id: str) -> bool:
    with _lock:
        return session_id in _inflight


def _generate(session_id: str, session: dict, changes: list[dict]) -> None:
    try:
        tpath = session.get("transcript_path")
        if not tpath:
            raise RuntimeError("没有 transcript")
        source_mtime = Path(tpath).stat().st_mtime
        skeleton = _run_extract(tpath, session.get("cwd"))
        prompt = build_prompt(skeleton, session, changes)
        obj = parse_json_obj(_run_claude(prompt))
        _store(session_id, normalize(obj, source_mtime))
        with _lock:
            _errors.pop(session_id, None)
    except Exception as e:  # noqa: BLE001 — 失败只记录，不抛
        with _lock:
            _errors[session_id] = f"{type(e).__name__}: {e}"[:300]
    finally:
        with _lock:
            _inflight.discard(session_id)
            _last_done[session_id] = time.time()


def request(session_id: str, session: dict, changes: list[dict] | None = None, *, sync: bool = False) -> str:
    """返回 "started" | "pending"（已在生成）| "cooldown"（60s 内刚生成过）。sync 仅供测试。"""
    _ensure_loaded()
    with _lock:
        if session_id in _inflight:
            return "pending"
        if time.time() - _last_done.get(session_id, 0) < COOLDOWN_S:
            return "cooldown"
        _inflight.add(session_id)
    args = (session_id, dict(session), list(changes or []))
    if sync:
        _generate(*args)
    else:
        threading.Thread(target=_generate, args=args, name="summary", daemon=True).start()
    return "started"
