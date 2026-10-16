"""订阅额度（Claude Code plan usage）：5 小时窗口 + 每周上限，供 /api/usage 和 usage_alert.py 共用。

数据源（按权威度）：
1. 官方：`claude -p "/usage"`。CLI 自己调 Anthropic 的 usage 接口，打印 `Current session: 20% used · resets Oct 16 at 9:50pm (Asia/Tokyo)`。
   不需要读 OAuth token，不消耗模型额度（本地斜杠命令），约 4s。加 --no-session-persistence，不留 transcript。
2. 兜底：官方取不到时，累加 ~/.claude/projects/**/*.jsonl 里 message.usage 的 token，只报 token 数，**不给百分比**
   （百分比的分母是 Anthropic 内部的，本地算不出）。

缓存在 ~/.harness/tracker/state/usage.json（只含百分比和时间，没有任何 token / 账号信息），server 和 alert 共用，避免重复调 claude。
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import config as tracker_config

CLAUDE_TIMEOUT = 60
CACHE_TTL_S = 120
PROJECTS_DIR = Path.home() / ".claude" / "projects"

_LINE = re.compile(r"^\s*(?P<label>Current [^:]+?):\s*(?P<pct>\d+(?:\.\d+)?)%\s*used(?:\s*[·•\-]\s*resets\s+(?P<resets>.+?))?\s*$")
_MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
_RESET = re.compile(
    r"^(?:(?P<mon>[A-Za-z]{3})[a-z]*\.?\s+(?P<day>\d{1,2})\s+at\s+)?"
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)\s*(?:\((?P<tz>[^)]+)\))?$", re.I)


def window_id(label: str) -> str:
    l = label.lower()
    if "session" in l:
        return "five_hour"
    if "week" in l and "all models" in l:
        return "seven_day"
    return re.sub(r"[^a-z0-9]+", "_", l.replace("current", "")).strip("_") or "other"


def parse_resets(text: str | None, now: datetime | None = None) -> int | None:
    """'Oct 16 at 9:50pm (Asia/Tokyo)' / '9:50pm (Asia/Tokyo)' -> epoch 秒；解析不了返回 None。"""
    if not text:
        return None
    m = _RESET.match(text.strip())
    if not m:
        return None
    try:
        tz = ZoneInfo(m.group("tz")) if m.group("tz") else datetime.now().astimezone().tzinfo
    except Exception:  # noqa: BLE001
        return None
    now = (now or datetime.now(tz)).astimezone(tz)
    hour = int(m.group("h")) % 12 + (12 if m.group("ap").lower() == "pm" else 0)
    minute = int(m.group("m") or 0)
    if m.group("mon"):
        mon = _MONTHS.get(m.group("mon").lower())
        if not mon:
            return None
        cand = datetime(now.year, mon, int(m.group("day")), hour, minute, tzinfo=tz)
        if cand < now - timedelta(days=30):   # 跨年：12 月看到 1 月
            cand = cand.replace(year=now.year + 1)
    else:
        cand = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if cand < now:
            cand += timedelta(days=1)
    return int(cand.timestamp())


def parse_usage_text(text: str, now: datetime | None = None) -> list[dict]:
    out = []
    for line in (text or "").splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        out.append({
            "id": window_id(m.group("label")),
            "label": m.group("label"),
            "percent": float(m.group("pct")),
            "resets_text": m.group("resets"),
            "resets_at": parse_resets(m.group("resets"), now),
        })
    return out


# ------------------------------------------------------------------ 官方来源

def _run_claude_usage() -> str:
    tracker_config.SIDECAR_CWD.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["claude", "-p", "/usage", "--no-session-persistence"],
        capture_output=True, text=True, timeout=CLAUDE_TIMEOUT,
        cwd=str(tracker_config.SIDECAR_CWD), env=tracker_config.claude_env(),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude 退出码 {proc.returncode}: {(proc.stderr or '')[:200]}")
    return proc.stdout or ""


# ------------------------------------------------------------------ 兜底：本地 transcript 累加

def _sum_tokens(horizons: dict[str, float], now: float, projects_dir: Path | None = None) -> dict[str, int]:
    """horizons: {name: 秒数}。按 message.id 去重（同一条消息会被多次写入 transcript）。"""
    root = projects_dir or PROJECTS_DIR
    longest = max(horizons.values())
    totals = {k: 0 for k in horizons}
    seen: set[str] = set()
    for p in root.glob("**/*.jsonl"):
        try:
            if now - p.stat().st_mtime > longest:
                continue
            with p.open(encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"usage"' not in line:
                        continue
                    try:
                        e = json.loads(line)
                        msg = e.get("message") or {}
                        u = msg.get("usage")
                        ts = datetime.fromisoformat(str(e.get("timestamp", "")).replace("Z", "+00:00")).timestamp()
                    except (ValueError, AttributeError):
                        continue
                    if not isinstance(u, dict):
                        continue
                    mid = msg.get("id") or e.get("uuid")
                    if mid in seen:
                        continue
                    seen.add(mid)
                    n = sum(int(u.get(k) or 0) for k in
                            ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
                    for name, h in horizons.items():
                        if now - ts <= h:
                            totals[name] += n
        except OSError:
            continue
    return totals


def local_tokens(now: float | None = None, projects_dir: Path | None = None) -> dict:
    now = now or time.time()
    t = _sum_tokens({"five_hour": 5 * 3600, "seven_day": 7 * 86400}, now, projects_dir)
    return {"five_hour_tokens": t["five_hour"], "seven_day_tokens": t["seven_day"]}


# ------------------------------------------------------------------ 对外接口

def fetch(now: datetime | None = None) -> dict:
    """实时取一次，不读缓存。永不抛：失败时 ok=False + error + 本地 token 兜底。"""
    ts = int(time.time())
    try:
        windows = parse_usage_text(_run_claude_usage(), now)
        if not windows:
            raise RuntimeError("/usage 输出里没解析到任何额度行（格式变了？）")
        return {"ok": True, "source": "claude -p /usage", "fetched_at": ts, "windows": windows}
    except Exception as e:  # noqa: BLE001
        res = {"ok": False, "source": "transcripts", "fetched_at": ts, "windows": [],
               "error": f"{type(e).__name__}: {e}"[:300]}
        try:
            res["local"] = local_tokens()
        except Exception:  # noqa: BLE001
            pass
        return res


def _cache_path() -> Path:
    return tracker_config.STATE_DIR / "usage.json"


def _store(res: dict) -> None:
    p = _cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def _load() -> dict | None:
    try:
        d = json.loads(_cache_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def get(max_age: int = CACHE_TTL_S, force: bool = False) -> dict:
    """缓存新鲜就用缓存；否则取一次并写缓存。失败的结果不覆盖上一次成功的缓存（但会带 stale 标记返回）。"""
    cached = _load()
    if not force and cached and cached.get("ok") and time.time() - cached.get("fetched_at", 0) < max_age:
        return cached
    res = fetch()
    if res["ok"]:
        _store(res)
        return res
    if cached and cached.get("ok"):
        return {**cached, "stale": True, "error": res.get("error")}
    return res
