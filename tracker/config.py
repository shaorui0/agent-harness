"""tracker 的路径与配置：代码在仓库里，运行时数据一律落在 ~/.harness/tracker/，仓库里不留任何数据。

配置读 ~/.harness/config.yml 的 `tracker:` 段（解析器复用 hooks/_harness.py），缺省值见 DEFAULTS。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "hooks"))
import _harness  # noqa: E402

DEFAULTS = {
    "roots": ["~/work"],      # 扫 .harness/scope/ 的根目录
    "port": 8787,             # 只绑 127.0.0.1
    "scan_interval_s": 300,   # 后台重扫 scope 的间隔
    "recap_skill": "~/.claude/skills/session_recap/SKILL.md",   # 「Session Recap」框执行的 skill 文本
    "recap_model": "sonnet",
    "check_model": "sonnet",     # 「核对勾选」用的模型
    "usage_thresholds": [70, 90],    # 订阅额度告警/进度条刻度（%）
    "usage_alert_channels": ["macos"],   # macos | imessage | ntfy | command，见 usage_alert.py
    "cloudcli_url": "http://localhost:5173",   # CloudCLI 的访问地址，看板里的入口和「在 CloudCLI 打开」用它
}

HOME = Path(os.environ.get("HARNESS_TRACKER_HOME", Path.home() / ".harness" / "tracker"))
STATE_DIR = HOME / "state"            # recap / goal 记录（goals.jsonl）
SIDECAR_CWD = STATE_DIR / "sidecar"   # 旁路 Haiku 调用的固定 cwd，server 按它过滤掉自身
SNAPSHOT = HOME / "snapshot.js"       # specs.py 的产物，/data/snapshot.js 原样返回


def load() -> dict:
    cfg = dict(DEFAULTS)
    user = _harness.read_yaml_file(_harness.GLOBAL_CONFIG).get("tracker") or {}
    cfg.update({k: v for k, v in user.items() if v is not None})
    if os.environ.get("HARNESS_TRACKER_PORT"):
        cfg["port"] = int(os.environ["HARNESS_TRACKER_PORT"])
    cfg["roots"] = [Path(r).expanduser() for r in cfg["roots"]]
    return cfg


# 这些变量把「当前进程」标识成某个 Claude Code session 的子进程（session id、消息 socket/token 等）。
# 看板如果是在某个 session 的 Bash 里启动的，会继承它们；旁路的 `claude -p` 再继承下去，就会被当成那个 session 的子 session。
_SESSION_ENV = {
    "CLAUDECODE", "CLAUDE_PID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH",
}


def claude_env() -> dict:
    """给旁路 `claude -p` 用的环境：去掉 session 身份变量，保留认证相关变量。"""
    return {k: v for k, v in os.environ.items() if k not in _SESSION_ENV}
