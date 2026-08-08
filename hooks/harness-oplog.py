#!/usr/bin/env python3
"""
PostToolUse hook — 变更留痕（oplog）。

journal 记的是「说了什么」，oplog 记的是「动了什么」。任何写文件、移文件、
改集群的动作都在这里留一条，带 `# INTENT:` 注释里的意图和当时的 git ref。

只记变更，只读命令一律跳过，否则 oplog 会被 grep/ls 淹掉。

落点：<project>/.harness/oplog.jsonl，无项目时 ~/.harness/oplog.jsonl
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import (  # noqa: E402
    GLOBAL_DIR, emit, git_info, load_config, read_event, safe_main, truncate,
)

# 命中即记录。写得宽一点：漏记比多记贵。
MUTATING = re.compile(
    r"""(?x)
    \b(?:
        rm | mv | cp | mkdir | rmdir | touch | ln | chmod | chown | truncate |
        tee | dd | sed \s+ -i | patch |
        git \s+ (?:mv|rm|add|commit|checkout|restore|reset|revert|stash|apply|
                    push|merge|rebase|cherry-pick|tag|branch \s+ -[dD]) |
        helm \s+ (?:upgrade|install|uninstall|rollback|delete) |
        kubectl \s+ (?:apply|create|delete|scale|patch|replace|edit|annotate|label|
                        rollout|cordon|drain|taint|set) |
        kwest[a-z]+ \s+ (?:apply|create|delete|scale|patch|replace|edit|annotate|
                            label|rollout|cordon|drain|taint|set) |
        docker \s+ (?:build|push|rm|rmi) |
        aws \s+ \S+ \s+ (?:create|delete|update|put|modify|terminate|stop|start)
    )\b
    | >{1,2}\s*\S            # shell 重定向写文件
    """
)

READ_ONLY_HINT = re.compile(r"^\s*(?:git\s+(?:status|log|diff|show|branch)\b|ls\b|cat\b|rg\b|grep\b)")


def intent_of(command):
    for line in command.splitlines():
        s = line.strip()
        if s.lower().startswith("# intent:"):
            return s.split(":", 1)[1].strip()
    return None


def target_of(command):
    """粗略取动作对象，便于 grep。取不准就留空，不猜。"""
    m = re.search(r"\b(?:git\s+mv|mv|cp)\s+(\S+)\s+(\S+)", command)
    if m:
        return f"{m.group(1)} -> {m.group(2)}"
    m = re.search(r"\b(?:rm|touch|mkdir)\s+(?:-\S+\s+)*(\S+)", command)
    if m:
        return m.group(1)
    return None


def oplog_path(cfg, root):
    base = root / ".harness" if root is not None else GLOBAL_DIR
    base.mkdir(parents=True, exist_ok=True)
    return base / "oplog.jsonl"


def main():
    event = read_event()
    tool = event.get("tool_name")
    tool_input = event.get("tool_input") or {}
    cwd = event.get("cwd") or "."

    record = None
    if tool in ("Edit", "Write", "NotebookEdit"):
        record = {
            "action": {"Edit": "edit", "Write": "write", "NotebookEdit": "edit"}[tool],
            "target": tool_input.get("file_path"),
            "command": None,
            "intent": None,
        }
    elif tool == "Bash":
        command = tool_input.get("command") or ""
        if not command or READ_ONLY_HINT.match(command) or not MUTATING.search(command):
            emit()
        record = {
            "action": "exec",
            "target": target_of(command),
            "command": truncate(command, 600),
            "intent": intent_of(command),
        }
    else:
        emit()

    cfg, root = load_config(cwd)
    record.update({
        "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "session_id": event.get("session_id"),
        "agent": event.get("agent_type"),
        "cwd": cwd,
        "project": cfg.get("name") or (root.name if root else None),
        "git": git_info(cwd),
        "tool": tool,
    })

    with open(oplog_path(cfg, root), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    emit()


if __name__ == "__main__":
    safe_main(main)
