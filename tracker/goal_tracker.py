"""goal_tracker.py — 旁路目标/收敛点提取子模块。

用 Haiku 模型旁路（不影响被监控 session 本身）识别每个 Claude Code session 当前
这一轮对话的目标，以及一个可判断的收敛点。对外只暴露三个函数，见 CONTRACT.md：

    on_new_human_prompt(session_id, prompt_text) -> None
    on_status_transition(session_id, prev_status, cur_status, recent_activity) -> None
    get_state() -> dict   # {"goals": {session_id: {...}}}，即 /api/goals 的结构

纯 stdlib 实现，不起 HTTP server，不碰 ~/.claude/ 下的 fleet 数据。
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import config as tracker_config
from typing import Any, Optional

# --------------------------------------------------------------------------
# 配置 / 常量
# --------------------------------------------------------------------------

_MODULE_DIR = Path(__file__).parent
_STATE_DIR = tracker_config.STATE_DIR
_STATE_FILE = _STATE_DIR / "goals.jsonl"

_CLAUDE_TIMEOUT_SECONDS = 25

# 旁路 Haiku 调用本身也是一个 Claude Code session。不隔离的话 server 会把它采集进 fleet，
# 它的「新人类输入」又触发一次目标提取 -> 又一个 session，形成自激循环。
# 两道隔离：--no-session-persistence 不落 transcript；固定 cwd 让 server 能按目录过滤掉。
SIDECAR_CWD = tracker_config.SIDECAR_CWD
_HAIKU_CMD = ["claude", "-p", "--model", "haiku", "--no-session-persistence"]


def _run_haiku(prompt: str, timeout: int) -> subprocess.CompletedProcess:
    SIDECAR_CWD.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        _HAIKU_CMD + [prompt],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(SIDECAR_CWD),
    )

# verdict -> status_badge 映射（CONTRACT.md 里 /api/goals 的 status_badge 字段）
_VERDICT_TO_BADGE = {
    "converged": "converged",
    "open": "progress",
    "diverged": "diverged",
    "abandoned": "abandoned",
}
_DEFAULT_BADGE = "progress"

# 触发收敛判断的状态集合（从忙碌转为空闲/等待）
_TRANSITION_TRIGGER_STATUSES = ("waiting", "idle")

# 提示词模板：原样使用，只替换花括号变量。注意模板里 JSON 示例本身也带花括号，
# 所以用 str.replace 逐个替换占位符，而不是 str.format（避免和字面量花括号冲突，
# 也避免用户输入里出现 { } 时把 format 炸掉）。
_EXTRACT_PROMPT_TEMPLATE = """你在做旁路目标提取，不参与任务执行，只输出 JSON，不要解释。

{prev_context}
用户刚发给一个 Claude Code agent 的新输入：
{prompt_text}

输出:
{"goal": "这一轮目标，一句话", "convergence_point": "什么条件达成时这个目标算收敛，要具体可判断", "is_continuation": true/false}"""

_VERDICT_PROMPT_TEMPLATE = """你在做旁路收敛判断，只输出 JSON，不要解释。

目标: {goal}
收敛点: {convergence_point}
agent 近期动作摘要:
{recent_activity}
session 当前状态: {cur_status}

输出:
{"verdict": "converged|open|diverged|abandoned", "reason": "一句话"}"""

_RECAP_SCRIPT = _MODULE_DIR / "session_recap.py"
_RECAP_EXTRACT_TIMEOUT = 20
_RECAP_HEAD_CHARS = 1500
_RECAP_TAIL_CHARS = 12000
_RECAP_MIN_INTERVAL_S = 60
_RECAP_CLAUDE_TIMEOUT = 45

_RECAP_PROMPT_TEMPLATE = """你在做旁路 session 断点摘要，不参与任务执行，只输出 JSON，不要解释，不要代码围栏。

下面是一个 Claude Code session 的骨架提取（人类回合、agent 动作、todo、停在哪里）：
-----
{skeleton}
-----

输出严格 JSON:
{"done": ["..."], "now": "...", "next": "...", "blocked_on": null}

要求:
- done: 最多 5 条，每条一行，写已达成的【结果】而不是过程（"修了 X 的 bug" 而不是 "读了文件、跑了测试"）；没有就给 []
- now: 一句话，当前停在哪 / 正在做什么
- next: 一句话，下一步该谁做什么
- blocked_on: 人类还欠 agent 什么决策或输入，一句话；不需要人介入则为 null
- 全部中文，尽量简短"""

# --------------------------------------------------------------------------
# 模块级状态
# --------------------------------------------------------------------------

_executor = ThreadPoolExecutor(max_workers=3)
_lock = threading.Lock()

_state: dict[str, dict[str, Any]] = {}

_recaps: dict[str, dict[str, Any]] = {}
_recap_inflight: set[str] = set()
_recap_last_start: dict[str, float] = {}

_loaded = False
_load_lock = threading.Lock()


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_json_obj(text: str) -> dict:
    """从 `claude -p` 的 stdout 里抠出第一个 `{` 到最后一个 `}` 之间的内容并 json.loads。

    Claude CLI 的 -p 输出经常带 markdown 代码围栏（```json ... ```）或前后解释文字，
    实测样例：

        ```json
        {
          "goal": "...",
          "convergence_point": "...",
          "is_continuation": false
        }
        ```
    """
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"no JSON object found in output: {text!r}")
    snippet = text[start : end + 1]
    return json.loads(snippet)


def _append_jsonl(record: dict) -> None:
    line = json.dumps(record, ensure_ascii=False)
    with _lock:
        with _STATE_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def _call_claude_haiku(prompt: str) -> dict:
    """调用本机 `claude -p ... --model haiku`，解析出 JSON dict。失败一律抛异常。"""
    result = _run_haiku(prompt, _CLAUDE_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise RuntimeError(
            f"claude cli exited {result.returncode}: {result.stderr.strip()[:500]}"
        )
    return _extract_json_obj(result.stdout)


# --------------------------------------------------------------------------
# 状态加载 / 重放
# --------------------------------------------------------------------------


def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    with _load_lock:
        if _loaded:
            return
        _load_state_from_disk()
        _loaded = True


def _load_state_from_disk() -> None:
    """读取整个 goals.jsonl，按 session_id 重放：
    - 保留每个 session 最后一条 extract 事件的 goal/convergence_point/is_continuation
    - 保留每个 session 最后一条 verdict 事件的 verdict/reason
    - 合并成内存态，updated_at 取两者里较新的时间戳
    """
    if not _STATE_FILE.exists():
        return

    last_extract: dict[str, tuple] = {}
    last_verdict: dict[str, tuple] = {}
    last_recap: dict[str, dict] = {}

    try:
        with _STATE_FILE.open("r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sid = rec.get("session_id")
                if not sid:
                    continue
                ts = rec.get("ts") or ""
                rtype = rec.get("type")
                if rtype == "extract":
                    last_extract[sid] = (
                        ts,
                        rec.get("goal"),
                        rec.get("convergence_point"),
                        rec.get("is_continuation"),
                    )
                elif rtype == "verdict":
                    last_verdict[sid] = (ts, rec.get("verdict"), rec.get("reason"))
                elif rtype == "recap":
                    rc = rec.get("recap")
                    if isinstance(rc, dict):
                        last_recap[sid] = rc
    except OSError:
        return

    with _lock:
        _recaps.update(last_recap)

    session_ids = set(last_extract) | set(last_verdict)
    with _lock:
        for sid in session_ids:
            ext = last_extract.get(sid)
            ver = last_verdict.get(sid)

            goal = convergence_point = is_continuation = None
            verdict = reason = None
            ts_candidates = []

            if ext:
                ts_candidates.append(ext[0])
                goal, convergence_point, is_continuation = ext[1], ext[2], ext[3]
            if ver:
                ts_candidates.append(ver[0])
                verdict, reason = ver[1], ver[2]

            updated_at = max(ts_candidates) if ts_candidates else _now_iso()
            status_badge = _VERDICT_TO_BADGE.get(verdict, _DEFAULT_BADGE)

            _state[sid] = {
                "goal": goal,
                "convergence_point": convergence_point,
                "is_continuation": is_continuation,
                "verdict": verdict,
                "reason": reason,
                "status_badge": status_badge,
                "updated_at": updated_at,
            }


# --------------------------------------------------------------------------
# 线程池任务：目标提取
# --------------------------------------------------------------------------


def _build_extract_prompt(session_id: str, prompt_text: str) -> str:
    with _lock:
        prev_entry = _state.get(session_id)

    prev_context = ""
    if prev_entry is not None and prev_entry.get("verdict") != "converged":
        goal = prev_entry.get("goal")
        conv = prev_entry.get("convergence_point")
        if goal and conv:
            prev_context = f"上一个目标: {goal} / 收敛点: {conv}\n"

    full_prompt = _EXTRACT_PROMPT_TEMPLATE.replace("{prev_context}", prev_context)
    full_prompt = full_prompt.replace("{prompt_text}", prompt_text)
    return full_prompt


def _do_extract(session_id: str, prompt_text: str) -> None:
    full_prompt = _build_extract_prompt(session_id, prompt_text)

    try:
        data = _call_claude_haiku(full_prompt)
        goal = data.get("goal")
        convergence_point = data.get("convergence_point")
        is_continuation = data.get("is_continuation")

        now = _now_iso()
        with _lock:
            _state[session_id] = {
                "goal": goal,
                "convergence_point": convergence_point,
                "is_continuation": is_continuation,
                "verdict": "open",
                "reason": None,
                "status_badge": "progress",
                "updated_at": now,
            }
        _append_jsonl(
            {
                "ts": now,
                "session_id": session_id,
                "type": "extract",
                "goal": goal,
                "convergence_point": convergence_point,
                "is_continuation": is_continuation,
            }
        )
    except Exception:
        # 超时 / JSON 解析失败 / subprocess 报错：catch 住不抛异常，
        # 只把 status_badge 标成 extract_failed，其他字段保留原值不动。
        now = _now_iso()
        with _lock:
            entry = _state.get(session_id)
            if entry is None:
                entry = {
                    "goal": None,
                    "convergence_point": None,
                    "is_continuation": None,
                    "verdict": None,
                    "reason": None,
                    "status_badge": "extract_failed",
                    "updated_at": now,
                }
            else:
                entry = dict(entry)
                entry["status_badge"] = "extract_failed"
                entry["updated_at"] = now
            _state[session_id] = entry


# --------------------------------------------------------------------------
# 线程池任务：收敛判断
# --------------------------------------------------------------------------


def _build_verdict_prompt(
    goal: Optional[str],
    convergence_point: Optional[str],
    recent_activity: str,
    cur_status: str,
) -> str:
    full_prompt = _VERDICT_PROMPT_TEMPLATE.replace("{goal}", goal or "")
    full_prompt = full_prompt.replace("{convergence_point}", convergence_point or "")
    full_prompt = full_prompt.replace("{recent_activity}", recent_activity or "")
    full_prompt = full_prompt.replace("{cur_status}", cur_status or "")
    return full_prompt


def _do_verdict(
    session_id: str,
    goal: Optional[str],
    convergence_point: Optional[str],
    cur_status: str,
    recent_activity: str,
) -> None:
    full_prompt = _build_verdict_prompt(goal, convergence_point, recent_activity, cur_status)

    try:
        data = _call_claude_haiku(full_prompt)
        verdict = data.get("verdict")
        reason = data.get("reason")
        status_badge = _VERDICT_TO_BADGE.get(verdict, _DEFAULT_BADGE)

        now = _now_iso()
        with _lock:
            entry = _state.get(session_id)
            if entry is None:
                # session 在等待期间被清掉了，没有合理的落点，直接放弃这次更新
                return
            entry = dict(entry)
            entry["verdict"] = verdict
            entry["reason"] = reason
            entry["status_badge"] = status_badge
            entry["updated_at"] = now
            _state[session_id] = entry

        _append_jsonl(
            {
                "ts": now,
                "session_id": session_id,
                "type": "verdict",
                "verdict": verdict,
                "reason": reason,
            }
        )
    except Exception:
        # 失败：catch 住，不覆盖现有状态
        pass


# --------------------------------------------------------------------------
# 线程池任务：session recap
# --------------------------------------------------------------------------


def _normalize_recap(data: dict, source_mtime: float) -> dict:
    done = data.get("done")
    if not isinstance(done, list):
        done = []
    done = [str(x).strip() for x in done if str(x).strip()][:5]
    blocked = data.get("blocked_on")
    if blocked is not None:
        blocked = str(blocked).strip() or None
        if blocked and blocked.lower() in ("null", "none", "无"):
            blocked = None
    return {
        "done": done,
        "now": str(data.get("now") or "").strip(),
        "next": str(data.get("next") or "").strip(),
        "blocked_on": blocked,
        "updated_at": _now_iso(),
        "source_mtime": source_mtime,
    }


def _do_recap(session_id: str, transcript_path: str, cwd: str) -> None:
    try:
        try:
            source_mtime = Path(transcript_path).stat().st_mtime
        except OSError:
            return
        proc = subprocess.run(
            [sys.executable, str(_RECAP_SCRIPT), str(Path(transcript_path).resolve())],
            capture_output=True,
            text=True,
            timeout=_RECAP_EXTRACT_TIMEOUT,
            cwd=cwd if cwd and Path(cwd).is_dir() else None,
        )
        out = proc.stdout or ""
        if proc.returncode != 0 or not out.strip():
            return
        if len(out) > _RECAP_HEAD_CHARS + _RECAP_TAIL_CHARS:
            out = (
                out[:_RECAP_HEAD_CHARS]
                + "\n...（中间省略）...\n"
                + out[-_RECAP_TAIL_CHARS:]
            )
        prompt = _RECAP_PROMPT_TEMPLATE.replace("{skeleton}", out)
        result = _run_haiku(prompt, _RECAP_CLAUDE_TIMEOUT)
        if result.returncode != 0:
            return
        recap = _normalize_recap(_extract_json_obj(result.stdout), source_mtime)
        with _lock:
            _recaps[session_id] = recap
        _append_jsonl(
            {"ts": recap["updated_at"], "session_id": session_id, "type": "recap", "recap": recap}
        )
    except Exception:
        pass
    finally:
        with _lock:
            _recap_inflight.discard(session_id)


# --------------------------------------------------------------------------
# 对外接口（CONTRACT.md）
# --------------------------------------------------------------------------


def on_new_human_prompt(session_id: str, prompt_text: str) -> None:
    """新的人类输入到达时调用。不阻塞：提交到线程池异步做目标提取。"""
    _ensure_loaded()
    _executor.submit(_do_extract, session_id, prompt_text)


def request_recap(session_id: str, transcript_path: str, cwd: str) -> None:
    """非阻塞：异步生成 session recap。同一 session 已在生成中则忽略。"""
    if not session_id or not transcript_path:
        return
    _ensure_loaded()
    with _lock:
        if session_id in _recap_inflight:
            return
        _recap_inflight.add(session_id)
        _recap_last_start[session_id] = time.time()
    try:
        _executor.submit(_do_recap, session_id, transcript_path, cwd)
    except Exception:
        with _lock:
            _recap_inflight.discard(session_id)


def recap_status(session_id: str) -> dict:
    """recap 是否在生成中、最近一次开始生成的时间（失败也算），供 server 判断 pending / 冷却。"""
    with _lock:
        return {
            "inflight": session_id in _recap_inflight,
            "last_start": _recap_last_start.get(session_id, 0.0),
        }


def get_recap(session_id: str) -> Optional[dict]:
    _ensure_loaded()
    with _lock:
        r = _recaps.get(session_id)
        return copy.deepcopy(r) if r else None


def on_status_transition(
    session_id: str,
    prev_status: str,
    cur_status: str,
    recent_activity: str,
    transcript_path: Optional[str] = None,
    cwd: Optional[str] = None,
) -> None:
    """状态发生跃迁时调用。只在跃迁到 waiting/idle 且已有已知 goal 时才提交收敛判断。

    若提供 transcript_path，则同时（每 session 60s 限流）刷新 recap。"""
    if cur_status not in _TRANSITION_TRIGGER_STATUSES:
        return

    _ensure_loaded()
    if transcript_path and prev_status == "busy":
        with _lock:
            recent = time.time() - _recap_last_start.get(session_id, 0) < _RECAP_MIN_INTERVAL_S
        if not recent:
            request_recap(session_id, transcript_path, cwd or "")
    with _lock:
        entry = _state.get(session_id)

    if entry is None or entry.get("goal") is None:
        # 还没提取过目标，没有可判断的收敛点
        return

    _executor.submit(
        _do_verdict,
        session_id,
        entry.get("goal"),
        entry.get("convergence_point"),
        cur_status,
        recent_activity,
    )


def get_state() -> dict:
    """直接返回内存态，包成 /api/goals 的结构。"""
    _ensure_loaded()
    with _lock:
        goals = copy.deepcopy(_state)
        recaps = copy.deepcopy(_recaps)
    for sid, rc in recaps.items():
        if sid not in goals:
            goals[sid] = {
                "goal": None,
                "convergence_point": None,
                "is_continuation": None,
                "verdict": None,
                "reason": None,
                "status_badge": _DEFAULT_BADGE,
                "updated_at": rc.get("updated_at"),
            }
    for sid, g in goals.items():
        g["recap"] = recaps.get(sid)
    return {"goals": goals}


# --------------------------------------------------------------------------
# 独立测试
# --------------------------------------------------------------------------

if __name__ == "__main__":
    import shutil
    import sys
    import time

    def _dump(label: str) -> None:
        print(f"\n=== {label} ===")
        print(json.dumps(get_state(), ensure_ascii=False, indent=2))

    if shutil.which("claude") is None:
        print("FAIL: 本机 PATH 里没有找到 `claude` 命令，无法测试。", file=sys.stderr)
        sys.exit(1)

    session_id = "test-session-1"

    print("调用 on_new_human_prompt(...)，等待异步线程池完成提取 ...")
    on_new_human_prompt(session_id, "帮我看看这个 bug 怎么修")

    entry = None
    for _ in range(15):
        time.sleep(1)
        entry = get_state()["goals"].get(session_id)
        if entry is not None and entry.get("status_badge") in ("progress", "extract_failed"):
            break

    if entry is None:
        print("FAIL: 等待 15s 后仍未拿到任何状态，可能线程池任务还没跑完或调用挂住了。")
        sys.exit(1)

    if entry.get("status_badge") == "extract_failed":
        print("FAIL: 目标提取失败（extract_failed）。内存态：")
        _dump("extract 失败后的状态")
        sys.exit(1)

    print("OK: 目标提取成功。")
    _dump("extract 成功后的状态")

    print("\n调用 on_status_transition(...)，模拟 busy -> waiting，等待收敛判断 ...")
    on_status_transition(
        session_id,
        "busy",
        "waiting",
        "已经读了 3 个相关文件，定位到问题在 auth.py 第 42 行的空指针检查缺失，"
        "写好了修复补丁并运行了测试，测试通过",
    )

    verdict_entry = None
    for _ in range(15):
        time.sleep(1)
        verdict_entry = get_state()["goals"].get(session_id)
        if verdict_entry is not None and verdict_entry.get("reason") is not None:
            break

    _dump("verdict 判断后的最终状态")

    if verdict_entry is not None and verdict_entry.get("reason") is not None:
        print("\nOK: 收敛判断也成功拿到了 Haiku 的返回。")
    else:
        print("\nFAIL: 等待 15s 后收敛判断仍未完成或失败（reason 仍为 null）。")
