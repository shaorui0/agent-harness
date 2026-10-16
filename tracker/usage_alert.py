"""订阅额度告警：每 5 分钟由 LaunchAgent 跑一次（见 install/ai.harness.usage-alert.plist.template）。

每个窗口（5h / 周）每个阈值只告警一次：状态存 ~/.harness/tracker/state/usage_alert.json，
窗口重置（resets_at 变了，或百分比回落到阈值以下）后重新武装。一次跑到 >=90% 只发 90% 那一条。

推送渠道在 ~/.harness/config.yml 的 `tracker:` 段配置（配置在用户目录，不在仓库里）：
  usage_alert_channels: [macos]          # 可选 macos | imessage | ntfy | command，按顺序全发；任一成功即算送达
  usage_imessage_to: "+81..."            # imessage 渠道：收件人（需 Messages 已登录 iMessage）
  usage_ntfy_topic: "<长随机串>"         # ntfy 渠道：topic 等同密码，别用可猜的名字
  usage_notify_command: ["/path/to/script"]   # command 渠道：argv，title 和 body 作为最后两个参数追加

用法：python3 usage_alert.py [--dry-run] [--test]   --test 只发一条测试推送（"Claude usage alert test"）。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import config as tracker_config
import usage

DEFAULT_THRESHOLDS = (70, 90)
DEFAULT_CHANNELS = ["macos"]


def _state_path() -> Path:
    return tracker_config.STATE_DIR / "usage_alert.json"


def load_state() -> dict:
    try:
        d = json.loads(_state_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(st: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


# ------------------------------------------------------------------ 推送

def _applescript_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _send_macos(title: str, body: str, cfg: dict) -> None:
    subprocess.run(["osascript", "-e", f"display notification {_applescript_str(body)} with title {_applescript_str(title)}"],
                   check=True, capture_output=True, timeout=15)


def _send_imessage(title: str, body: str, cfg: dict) -> None:
    to = cfg.get("usage_imessage_to")
    if not to:
        raise RuntimeError("usage_imessage_to 未配置")
    script = ('tell application "Messages"\n'
              '  set s to 1st service whose service type = iMessage\n'
              f'  send {_applescript_str(title + chr(10) + body)} to buddy {_applescript_str(str(to))} of s\n'
              'end tell')
    subprocess.run(["osascript", "-e", script], check=True, capture_output=True, timeout=30)


def _send_ntfy(title: str, body: str, cfg: dict) -> None:
    topic = cfg.get("usage_ntfy_topic")
    if not topic:
        raise RuntimeError("usage_ntfy_topic 未配置")
    payload = json.dumps({"topic": topic, "title": title, "message": body, "priority": 4}).encode("utf-8")
    req = urllib.request.Request("https://ntfy.sh/", data=payload, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        if r.status >= 300:
            raise RuntimeError(f"ntfy HTTP {r.status}")


def _send_command(title: str, body: str, cfg: dict) -> None:
    argv = cfg.get("usage_notify_command")
    if not argv or not isinstance(argv, list):
        raise RuntimeError("usage_notify_command 未配置（需要 argv 列表）")
    subprocess.run([str(a) for a in argv] + [title, body], check=True, capture_output=True, timeout=60)


BACKENDS = {"macos": _send_macos, "imessage": _send_imessage, "ntfy": _send_ntfy, "command": _send_command}


def notify(title: str, body: str, cfg: dict | None = None) -> list[str]:
    """按配置的渠道逐个发。返回成功的渠道名；全部失败返回 []（不抛，调用方据此决定是否记为已告警）。"""
    cfg = cfg if cfg is not None else tracker_config.load()
    ok = []
    for name in cfg.get("usage_alert_channels") or DEFAULT_CHANNELS:
        fn = BACKENDS.get(name)
        if not fn:
            _log(f"未知渠道 {name}")
            continue
        try:
            fn(title, body, cfg)
            ok.append(name)
        except Exception as e:  # noqa: BLE001
            _log(f"渠道 {name} 失败: {type(e).__name__}: {str(e)[:160]}")
    return ok


def _log(msg: str) -> None:
    print(f"{time.strftime('%F %T')} usage_alert: {msg}", flush=True)


# ------------------------------------------------------------------ 判定

def evaluate(windows: list[dict], state: dict, thresholds=DEFAULT_THRESHOLDS) -> tuple[list[dict], dict]:
    """纯函数。返回 (要发的告警列表, 新 state)。不改入参。

    state[window_id] = {"resets_at": int|None, "fired": [70, ...]}
    """
    new = {k: dict(v) for k, v in state.items() if isinstance(v, dict)}
    alerts = []
    ths = sorted(thresholds)
    for w in windows:
        pct = w.get("percent")
        if pct is None:
            continue
        wid, ra = w["id"], w.get("resets_at")
        rec = new.get(wid) or {"resets_at": ra, "fired": []}
        old_ra = rec.get("resets_at")
        if ra is not None and old_ra is not None and abs(ra - old_ra) > 120:
            rec = {"resets_at": ra, "fired": []}        # 新窗口
        rec["resets_at"] = ra if ra is not None else old_ra
        rec["fired"] = [t for t in rec.get("fired", []) if t <= pct]   # 回落后重新武装
        crossed = [t for t in ths if pct >= t]
        todo = [t for t in crossed if t not in rec["fired"]]
        if todo:
            alerts.append({"id": wid, "label": w.get("label", wid), "percent": pct,
                           "threshold": max(todo), "resets_text": w.get("resets_text"), "_crossed": crossed})
        new[wid] = rec
    return alerts, new


def format_alert(a: dict) -> tuple[str, str]:
    title = f"Claude 额度 {a['threshold']}%：{a['label']}"
    body = f"{a['label']} 已用 {a['percent']:g}%"
    if a.get("resets_text"):
        body += f"，重置于 {a['resets_text']}"
    return title, body


def run(dry_run: bool = False, cfg: dict | None = None, res: dict | None = None) -> int:
    cfg = cfg if cfg is not None else tracker_config.load()
    ths = tuple(cfg.get("usage_thresholds") or DEFAULT_THRESHOLDS)
    res = res if res is not None else usage.get(max_age=0, force=True)
    if not res.get("ok"):
        _log(f"取不到官方额度，不告警：{res.get('error')}")
        return 1
    state = load_state()
    alerts, new = evaluate(res["windows"], state, ths)
    for a in alerts:
        title, body = format_alert(a)
        if dry_run:
            _log(f"[dry-run] {title} | {body}")
            continue
        sent = notify(title, body, cfg)
        if sent:
            new[a["id"]]["fired"] = sorted(set(new[a["id"]]["fired"]) | set(a["_crossed"]))
            _log(f"已发送 {title} -> {','.join(sent)}")
        else:
            _log(f"所有渠道都失败，下次重试：{title}")
    if not dry_run:
        save_state(new)
    if not alerts:
        _log("无新告警：" + ", ".join(f"{w['id']}={w['percent']:g}%" for w in res["windows"]))
    return 0


def main(argv: list[str]) -> int:
    if "--test" in argv:
        sent = notify("Claude usage alert test", "Claude usage alert test")
        print("delivered via:", sent or "NONE")
        return 0 if sent else 1
    return run(dry_run="--dry-run" in argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
