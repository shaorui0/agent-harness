"""手动 Session Recap：只在 POST /api/recap 时生成，没有任何自动触发路径。

流程：session_recap.py 提取骨架 -> 读 session_recap skill 文本 -> 拼 prompt ->
隔离 cwd 里跑 `claude -p --model <recap_model> --no-session-persistence --setting-sources project` ->
输出当 markdown 原样存入 recaps.jsonl。读取时现算 stale。纯 stdlib。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import config as tracker_config

# --setting-sources project：不加载用户级 settings（用户级 hook 会注入输出格式契约，模型就不按 skill 输出了）
_CLAUDE_BASE = ["claude", "-p", "--no-session-persistence", "--setting-sources", "project"]
_RECAP_SCRIPT = Path(__file__).resolve().parent / "session_recap.py"
EXTRACT_TIMEOUT = 20
CLAUDE_TIMEOUT = 180
SKELETON_MAX = 40000
HEAD_CHARS = 3000
TAIL_CHARS = 30000
COOLDOWN_S = 60
STALE_AFTER_S = 60
DEFAULT_SKILL = "~/.claude/skills/session_recap/SKILL.md"

FALLBACK_SKILL = """# Session Recap（内置最小说明）
把 session 骨架写成一份七段 briefing（markdown，每段一个 `## ` 标题）：
一句话 / 已解决 / 当前讨论到哪 / 未完成 / 关键决定与文件 / 风险与待确认 / 续跑指令（放进代码块）。
硬规则：
- 「说要做」不等于「做完」；没有动作证据的不算已完成。
- 不许编造；看不出来就写「不确定」。
- 不要大段贴原文，用自己的话概括。
"""

PROMPT_HEAD = (
    "你正在执行下面这个 skill（session_recap）。skill 里需要你跑 session_recap.py 的步骤已经由系统完成，"
    "骨架在后面。严格按 skill 的 Step 3 写 briefing 并遵守硬规则。"
    "只输出 briefing 的 markdown，不要寒暄，不要调用任何工具。"
)

_lock = threading.Lock()
_inflight: set[str] = set()
_last_done: dict[str, float] = {}
_errors: dict[str, str] = {}
_cache: dict[str, dict] = {}
_loaded = False


def _jsonl_path() -> Path:
    return tracker_config.STATE_DIR / "recaps.jsonl"


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


def _run_claude(prompt: str, model: str) -> str:
    tracker_config.SIDECAR_CWD.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        _CLAUDE_BASE[:2] + ["--model", model] + _CLAUDE_BASE[2:] + [prompt],
        capture_output=True, text=True, timeout=CLAUDE_TIMEOUT, cwd=str(tracker_config.SIDECAR_CWD),
        env=tracker_config.claude_env(),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude 退出码 {proc.returncode}: {(proc.stderr or '')[:200]}")
    return proc.stdout or ""


# ------------------------------------------------------------------ 拼装

def _settings() -> tuple[str, str]:
    try:
        cfg = tracker_config.load()
    except Exception:  # noqa: BLE001 — 配置读失败用缺省
        cfg = tracker_config.DEFAULTS
    return str(cfg.get("recap_skill") or DEFAULT_SKILL), str(cfg.get("recap_model") or "sonnet")


def load_skill(path: str) -> tuple[str, bool]:
    """返回 (skill 文本, skill_missing)。读不到走内置最小说明。"""
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8")
        if text.strip():
            return text, False
    except OSError:
        pass
    return FALLBACK_SKILL, True


def trim_skeleton(skeleton: str) -> str:
    if len(skeleton) > SKELETON_MAX:
        return skeleton[:HEAD_CHARS] + "\n...（中间省略）...\n" + skeleton[-TAIL_CHARS:]
    return skeleton


def build_prompt(skill_text: str, skeleton: str, session: dict) -> str:
    d = session.get("diff") or {}
    diff = (f"files_changed={d.get('files_changed', 0)} insertions={d.get('insertions', 0)} "
            f"deletions={d.get('deletions', 0)} commits_since_start={d.get('commits_since_start', 0)} "
            f"dirty={d.get('dirty', False)}")
    spec = (session.get("spec") or "").strip() or "（无）"
    return (f"{PROMPT_HEAD}\n\n## skill 全文\n{skill_text}\n\n## session 骨架\n{trim_skeleton(skeleton)}\n\n"
            f"## session 的 spec（第一条 prompt）\n{spec}\n\n## diff 统计\n{diff}\n")


_FENCE_RE = re.compile(r"\A```[A-Za-z0-9_-]*[ \t]*\n(.*)\n```\Z", re.S)


def clean_output(text: str) -> str:
    """strip 首尾空白；若整体被一对 ``` 包住则剥掉。内部的代码块保持不变。"""
    out = (text or "").strip()
    m = _FENCE_RE.match(out)
    # 只剥「整体」围栏：内部不能再出现独立的 ``` 行（否则是多个代码块，不是外层包裹）
    if m and not re.search(r"^```", m.group(1), re.M):
        out = m.group(1).strip()
    return out


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
                    if isinstance(rec, dict) and rec.get("session_id") and isinstance(rec.get("recap"), dict):
                        _cache[rec["session_id"]] = rec["recap"]
        except OSError:
            pass


def _reset_for_tests() -> None:
    global _loaded
    with _lock:
        _cache.clear(); _errors.clear(); _last_done.clear(); _inflight.clear()
        _loaded = False


def _store(session_id: str, recap: dict) -> None:
    with _lock:
        _cache[session_id] = recap
    try:
        _jsonl_path().parent.mkdir(parents=True, exist_ok=True)
        with _jsonl_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"session_id": session_id, "recap": recap}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _with_stale(recap: dict, transcript_path: str | None) -> dict:
    out = dict(recap)
    stale = False
    src = _iso_ts(recap.get("source_mtime"))
    if transcript_path and src is not None:
        try:
            stale = Path(transcript_path).stat().st_mtime - src > STALE_AFTER_S
        except OSError:
            pass
    out["stale"] = stale
    return out


# ------------------------------------------------------------------ 对外接口

def get(session_id: str, transcript_path: str | None = None) -> dict:
    """GET /api/recap 的响应体：{"recap": ..|None, "pending": bool, "error": str|None}。"""
    _ensure_loaded()
    with _lock:
        cached = _cache.get(session_id)
        pending = session_id in _inflight
        error = _errors.get(session_id)
    return {"recap": _with_stale(cached, transcript_path) if cached else None,
            "pending": pending, "error": error}


def is_pending(session_id: str) -> bool:
    with _lock:
        return session_id in _inflight


def _generate(session_id: str, session: dict) -> None:
    try:
        tpath = session.get("transcript_path")
        if not tpath:
            raise RuntimeError("没有 transcript")
        source_mtime = Path(tpath).stat().st_mtime
        skill_path, model = _settings()
        skill_text, missing = load_skill(skill_path)
        skeleton = _run_extract(tpath, session.get("cwd"))
        prompt = build_prompt(skill_text, skeleton, session)
        markdown = clean_output(_run_claude(prompt, model))
        if not markdown:
            raise RuntimeError("claude 输出为空")
        _store(session_id, {
            "markdown": markdown,
            "generated_at": _now_iso(),
            "source_mtime": _ts_iso(source_mtime),
            "model": model,
            "skill_missing": missing,
        })
        with _lock:
            _errors.pop(session_id, None)
    except Exception as e:  # noqa: BLE001 — 失败只记录，不抛
        with _lock:
            _errors[session_id] = f"{type(e).__name__}: {e}"[:300]
    finally:
        with _lock:
            _inflight.discard(session_id)
            _last_done[session_id] = time.time()


def request(session_id: str, session: dict, *, sync: bool = False) -> str:
    """返回 "started" | "pending"（已在生成）| "cooldown"（60s 内刚生成过）。sync 仅供测试。"""
    _ensure_loaded()
    with _lock:
        if session_id in _inflight:
            return "pending"
        if time.time() - _last_done.get(session_id, 0) < COOLDOWN_S:
            return "cooldown"
        _inflight.add(session_id)
    args = (session_id, dict(session))
    if sync:
        _generate(*args)
    else:
        threading.Thread(target=_generate, args=args, name="recap", daemon=True).start()
    return "started"
