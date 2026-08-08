#!/usr/bin/env python3
"""
PreToolUse hook — the autonomy envelope.

Reads the project's `autonomy` block and decides three ways:

  deny   命中 deny_patterns / k8s deny_contexts / 写到 write_roots 之外
  allow  命中 allow_prefixes 或允许的 k8s context 且没有任何 deny 命中
  (none) 其余一律交回正常权限流程

The point is that a declared safe zone should not be re-confirmed on every call,
and everything outside it should be stopped rather than merely prompted.

Patterns are regular expressions matched against the command string. An invalid
regex degrades to a literal substring match.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import emit, load_config, read_event, safe_main  # noqa: E402


def matches(pattern, text):
    try:
        return re.search(pattern, text) is not None
    except re.error:
        return pattern in text


def strip_intent(command):
    """The `# INTENT:` convention prepends a comment line to mutating commands.
    Prefix matching should see the real command."""
    lines = [l for l in command.splitlines() if l.strip() and not l.strip().startswith("#")]
    return lines[0].strip() if lines else command.strip()


def decide(payload, reason):
    emit({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": payload,
            "permissionDecisionReason": reason,
        }
    })


def check_bash(command, auto):
    for pattern in auto.get("deny_patterns") or []:
        if matches(str(pattern), command):
            decide("deny", f"harness envelope: 命中禁止规则 `{pattern}`。这个动作在项目声明的爆炸半径之外。")

    k8s = auto.get("k8s") or {}
    for ctx in k8s.get("deny_contexts") or []:
        if re.search(rf"(^|[\s=/'\"]){re.escape(str(ctx))}([\s'\"]|$)", command):
            decide("deny", f"harness envelope: 命令里出现了禁止的 k8s context `{ctx}`。")

    if not auto.get("auto_allow_inside", True):
        emit()

    head = strip_intent(command)
    for prefix in auto.get("allow_prefixes") or []:
        if head.startswith(str(prefix)):
            decide("allow", f"harness envelope: `{prefix}` 在项目声明的安全区内，直接执行。")

    allow_ctx = k8s.get("allow_contexts") or []
    if allow_ctx:
        for ctx in allow_ctx:
            if str(ctx) in command:
                for prefix in auto.get("allow_prefixes") or []:
                    if head.startswith(str(prefix)):
                        decide("allow", f"harness envelope: `{ctx}` 在允许的 context 内。")
    emit()


def check_write(file_path, auto, root):
    roots = auto.get("write_roots") or []
    if not roots or root is None or not file_path:
        emit()
    try:
        target = Path(file_path).resolve()
    except Exception:
        emit()
    for r in roots:
        try:
            allowed = (root / str(r)).resolve()
        except Exception:
            continue
        if target == allowed or allowed in target.parents:
            emit()
    decide(
        "deny",
        f"harness envelope: `{file_path}` 在允许的写入范围之外（write_roots: {', '.join(str(r) for r in roots)}）。",
    )


def main():
    event = read_event()
    cwd = event.get("cwd") or "."
    cfg, root = load_config(cwd)
    auto = cfg.get("autonomy") or {}
    tool = event.get("tool_name")
    tool_input = event.get("tool_input") or {}

    if tool == "Bash":
        command = tool_input.get("command") or ""
        if command:
            check_bash(command, auto)
    elif tool in ("Edit", "Write", "NotebookEdit"):
        check_write(tool_input.get("file_path"), auto, root)
    emit()


if __name__ == "__main__":
    safe_main(main)
