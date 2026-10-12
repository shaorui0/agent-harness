"""手动核对：只在 POST /api/check 时生成，没有任何自动触发路径。

流程：actions.py 生成整个 session 的动作索引（失败回退 session_recap.py 骨架）+ 关联 change 的任务清单（id | [x]/[ ] | text）+ diff 统计 -> 拼 prompt ->
隔离 cwd 里跑 `claude -p --model <tracker.check_model，缺省 sonnet> --no-session-persistence --setting-sources project`
-> 解析 JSON -> normalize（丢弃非法 id / verdict）-> 追加写 checks.jsonl。
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

import actions
import config as tracker_config

DEFAULT_MODEL = "sonnet"   # 判断「勾选是否有证据」需要比 haiku 更强的模型；可用 tracker.check_model 改


def _model() -> str:
    try:
        return str(tracker_config.load().get("check_model") or DEFAULT_MODEL)
    except Exception:  # noqa: BLE001 — 配置读不出来就用缺省
        return DEFAULT_MODEL


# --setting-sources project：不加载用户级 settings。用户级 hooks 会往每次调用里注入「输出格式契约」，
# 模型就不再只输出 JSON。旁路调用不需要任何 hook。
_CLAUDE_ARGS = ["--no-session-persistence", "--setting-sources", "project"]
_RECAP_SCRIPT = Path(__file__).resolve().parent / "session_recap.py"
EXTRACT_TIMEOUT = 20
CLAUDE_TIMEOUT = 150
HEAD_CHARS = 1500
TAIL_CHARS = 12000
COOLDOWN_S = 60          # 生成完（含失败）后这么久内不重复触发
STALE_AFTER_S = 60       # transcript 比 source_mtime 晚这么久 -> stale
TASKS_MAX_CHARS = 6000
INDEX_MAX_CHARS = 60000

VERDICTS = ("ok", "unsupported", "unticked_done")
EVIDENCE_MAX = 80
OFF_SPEC_MAX = 8
OFF_SPEC_TEXT_MAX = 120
BLOCKED_MAX = 200

PROMPT_TEMPLATE = """你在做旁路「勾选核对」，不参与任务执行。只输出一个 JSON 对象，不要解释，不要代码围栏，不要 JSON 之外的任何文字。

目标：对照下面 session 的实际记录，核对每个 change 的 tasks.md 勾选情况是否有证据。

格式：
{"changes": [{"id": "<change id>", "items": [{"id": "<任务 id>", "verdict": "ok|unsupported|unticked_done", "evidence": "..."}]}],
 "off_spec": ["..."], "blocked_on": "..." 或 null}

规则：
1. 对每个已勾选（[x]）的条目给 ok 或 unsupported：ok = 记录里有对应动作或产物作证据；unsupported = 已勾选但记录里看不到证据（「说做了」不算证据）。
2. 对每个未勾选（[ ]）的条目：只有记录里有明确动作且结果显示已完成时才给 unticked_done，其余未勾选条目不要列出。
3. evidence 一句话、不超过 80 字，必须指向动作（命令 / 文件 / 提交 / 测试结果）。
4. off_spec：session 里实际做了、但 tasks.md 里没有任何条目对应的事，每条一句，最多 8 条。
5. blocked_on：此刻在等人或外部什么，没有写 null。
6. 条目 id 必须原样使用下面清单里的 id；清单里没有的 id 不要写。看不出来的不要编。

## 任务清单（id | 勾选 | 内容）
{tasks}

## diff 统计
{diff}

## session 动作索引
下面是整个 session 的动作索引（改变状态的动作 + 每回合结论），不是全文；`unsupported` 只能在索引里找不到对应动作时才给，找到了动作就给 ok 并把动作写进 evidence。
{skeleton}
"""

_lock = threading.Lock()
_inflight: set[str] = set()
_last_done: dict[str, float] = {}     # session_id -> 上次生成结束（time.time）
_errors: dict[str, str] = {}
_cache: dict[str, dict] = {}
_loaded = False


def _jsonl_path() -> Path:
    return tracker_config.STATE_DIR / "checks.jsonl"


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

def _run_recap(transcript_path: str, cwd: str | None) -> str:
    """回退路径：session_recap 骨架（头尾截断）。"""
    proc = subprocess.run(
        [sys.executable, str(_RECAP_SCRIPT), str(Path(transcript_path).resolve())],
        capture_output=True, text=True, timeout=EXTRACT_TIMEOUT,
        cwd=cwd if cwd and Path(cwd).is_dir() else None,
    )
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        raise RuntimeError("session_recap 提取失败")
    out = proc.stdout
    if len(out) > HEAD_CHARS + TAIL_CHARS:
        out = out[:HEAD_CHARS] + "\n...（中间省略）...\n" + out[-TAIL_CHARS:]
    return out


def _run_extract(transcript_path: str, cwd: str | None) -> str:
    try:
        return actions.build_index(transcript_path, INDEX_MAX_CHARS)
    except Exception:  # noqa: BLE001 — 索引构建失败就退回骨架
        return _run_recap(transcript_path, cwd)


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


def valid_ids_of(changes: list[dict]) -> dict[str, set[str]]:
    """{change id: {任务 id}}，来自 board 的 change.items。"""
    return {str(c.get("id")): {str(i.get("id")) for i in (c.get("items") or []) if i.get("id") is not None}
            for c in changes or [] if c.get("id")}


def _clip(v, n: int) -> str:
    return str(v).strip()[:n] if v is not None else ""


def normalize(obj: dict, valid_ids: dict, source_mtime: float | None) -> dict:
    """valid_ids: {change id: 任务 id 集合}。丢弃清单外的 change / 条目 id、非法 verdict；截断 evidence / off_spec。"""
    changes_out = []
    raw_changes = obj.get("changes")
    for ch in raw_changes if isinstance(raw_changes, list) else []:
        if not isinstance(ch, dict):
            continue
        cid = str(ch.get("id") or "").strip()
        if cid not in valid_ids:
            continue
        allowed = {str(x) for x in valid_ids[cid]}
        seen, items = set(), []
        raw_items = ch.get("items")
        for it in raw_items if isinstance(raw_items, list) else []:
            if not isinstance(it, dict):
                continue
            iid, verdict = str(it.get("id") or "").strip(), it.get("verdict")
            if iid not in allowed or iid in seen or verdict not in VERDICTS:
                continue
            seen.add(iid)
            items.append({"id": iid, "verdict": verdict, "evidence": _clip(it.get("evidence"), EVIDENCE_MAX)})
        changes_out.append({"id": cid, "items": items})
    raw_off = obj.get("off_spec")
    off_spec = []
    for x in raw_off if isinstance(raw_off, list) else []:
        s = _clip(x, OFF_SPEC_TEXT_MAX)
        if s:
            off_spec.append(s)
        if len(off_spec) >= OFF_SPEC_MAX:
            break
    blocked = obj.get("blocked_on")
    blocked = _clip(blocked, BLOCKED_MAX) if blocked not in (None, "", "null") else None
    return {
        "changes": changes_out,
        "off_spec": off_spec,
        "blocked_on": blocked or None,
        "generated_at": _now_iso(),
        "source_mtime": _ts_iso(source_mtime) if source_mtime else None,
        "model": _model(),
    }


def _tasks_block(changes: list[dict]) -> str:
    parts = []
    for ch in changes or []:
        lines = [f"{i.get('id', '')} | {'[x]' if i.get('done') else '[ ]'} | {str(i.get('text') or '').strip()}"
                 for i in ch.get("items") or []]
        if lines:
            parts.append(f"### {ch.get('id', '')}\n" + "\n".join(lines)[:TASKS_MAX_CHARS])
    return "\n\n".join(parts) or "（无关联 change）"


def build_prompt(skeleton: str, session: dict, changes: list[dict]) -> str:
    d = session.get("diff") or {}
    diff = (f"files_changed={d.get('files_changed', 0)} insertions={d.get('insertions', 0)} "
            f"deletions={d.get('deletions', 0)} commits_since_start={d.get('commits_since_start', 0)} "
            f"dirty={d.get('dirty', False)}")
    # 用 replace 而不是 format：骨架里有大量花括号
    return (PROMPT_TEMPLATE.replace("{tasks}", _tasks_block(changes)).replace("{diff}", diff)
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
                    if isinstance(rec, dict) and rec.get("session_id") and isinstance(rec.get("check"), dict):
                        _cache[rec["session_id"]] = rec["check"]   # 后写的覆盖前写的
        except OSError:
            pass


def _reset_for_tests() -> None:
    global _loaded
    with _lock:
        _cache.clear(); _errors.clear(); _last_done.clear(); _inflight.clear()
        _loaded = False


def _store(session_id: str, check: dict) -> None:
    with _lock:
        _cache[session_id] = check
    try:
        _jsonl_path().parent.mkdir(parents=True, exist_ok=True)
        with _jsonl_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"session_id": session_id, "check": check}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _with_stale(check: dict, transcript_path: str | None) -> dict:
    out = dict(check)
    stale = False
    src = _iso_ts(check.get("source_mtime"))
    if transcript_path and src is not None:
        try:
            stale = Path(transcript_path).stat().st_mtime - src > STALE_AFTER_S
        except OSError:
            pass
    out["stale"] = stale
    return out


# ------------------------------------------------------------------ 对外接口

def get(session_id: str, transcript_path: str | None = None) -> dict:
    """返回 GET /api/check 的响应体：{"check": ..|None, "pending": bool, "error": str|None}。"""
    _ensure_loaded()
    with _lock:
        cached = _cache.get(session_id)
        pending = session_id in _inflight
        error = _errors.get(session_id)
    return {
        "check": _with_stale(cached, transcript_path) if cached else None,
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
        if not changes:
            raise RuntimeError("没有关联 change")
        source_mtime = Path(tpath).stat().st_mtime
        skeleton = _run_extract(tpath, session.get("cwd"))
        prompt = build_prompt(skeleton, session, changes)
        obj = parse_json_obj(_run_claude(prompt))
        _store(session_id, normalize(obj, valid_ids_of(changes), source_mtime))
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
    """返回 "started" | "pending"（已在生成）| "cooldown"（60s 内刚生成过）| "no_change"（无关联 change，不调 LLM）。
    changes: [{"id", "items": [{"id", "done", "text"}]}]。sync 仅供测试。"""
    _ensure_loaded()
    if not changes:
        return "no_change"
    with _lock:
        if session_id in _inflight:
            return "pending"
        if time.time() - _last_done.get(session_id, 0) < COOLDOWN_S:
            return "cooldown"
        _inflight.add(session_id)
    args = (session_id, dict(session), list(changes))
    if sync:
        _generate(*args)
    else:
        threading.Thread(target=_generate, args=args, name="check", daemon=True).start()
    return "started"
