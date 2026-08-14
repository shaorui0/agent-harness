#!/usr/bin/env python3
"""
PreToolUse hook — the autonomy envelope + scope gate.

Reads the project's `autonomy` block and decides three ways:

  deny   命中 deny_patterns / k8s deny_contexts / 写到 write_roots 之外
  allow  命中 allow_prefixes 或允许的 k8s context 且没有任何 deny 命中
  (none) 其余一律交回正常权限流程

The point is that a declared safe zone should not be re-confirmed on every call,
and everything outside it should be stopped rather than merely prompted.

Patterns are regular expressions matched against the command string. An invalid
regex degrades to a literal substring match.

Scope gate (see docs/scope-and-spec-gate-design.md): a second, orthogonal write
restriction, checked independently of the write_roots boundary above. It never
grants `allow` — it only ever adds a `deny` on top of whatever the write_roots
check decides. Writes inside `scope_gate`'s protected paths require a
`status: confirmed` scope artifact under `.harness/scope/` whose `covers` list
includes that path. This is deliberately NOT session- or branch-inferred — the
artifact explicitly declares what it covers, the hook only checks path
prefixes, the same shape as `write_roots` itself.

Opt-in only: `protected_paths` defaults to empty and is never inferred from
`write_roots` or the project root. A local, git-tracked edit is reversible by
construction — git is the safety net, not a pre-declared scope. This gate
exists for the rare case a project wants extra friction on specific paths
(e.g. files that are themselves part of the harness's own bootstrapping), not
as a default tax on every write. See docs/scope-and-spec-gate-design.md's
2026-08-14 revert entry for why the earlier "fall back to the whole repo"
default was removed.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import emit, load_config, read_event, read_yaml_file, safe_main  # noqa: E402


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


def _resolve_under(root, prefix, target):
    """True if `target` is `root/prefix` or lives under it."""
    try:
        allowed = (root / str(prefix)).resolve()
    except Exception:
        return False
    return target == allowed or allowed in target.parents


def write_roots_decision(file_path, cfg, root):
    """Pure decision for the write_roots boundary: ('deny', reason) or None.
    None means "no opinion" (either no roots configured, or the path is
    inside them) — it never actively grants allow."""
    auto = cfg.get("autonomy") or {}
    roots = auto.get("write_roots") or []
    if not roots or root is None or not file_path:
        return None
    try:
        target = Path(file_path).resolve()
    except Exception:
        return None
    if any(_resolve_under(root, r, target) for r in roots):
        return None
    return (
        "deny",
        f"harness envelope: `{file_path}` 在允许的写入范围之外（write_roots: {', '.join(str(r) for r in roots)}）。",
    )


def _effective_protected_paths(cfg):
    """scope_gate.protected_paths, taken as-is. No fallback to write_roots or
    the whole project root — the gate is opt-in, not a default-deny surface.
    See docs/scope-and-spec-gate-design.md's 2026-08-14 revert entry."""
    gate = cfg.get("scope_gate") or {}
    return gate.get("protected_paths") or []


def _confirmed_scope_covers(root, target):
    """Scan `.harness/scope/*.yml` for a `status: confirmed` artifact whose
    `covers` list includes a prefix `target` falls under."""
    scope_dir = root / ".harness" / "scope"
    if not scope_dir.is_dir():
        return False
    for path in scope_dir.glob("*.yml"):
        data = read_yaml_file(path)
        if data.get("status") != "confirmed":
            continue
        for prefix in data.get("covers") or []:
            if _resolve_under(root, prefix, target):
                return True
    return False


def scope_gate_decision(file_path, cfg, root):
    """Pure decision for the scope gate: ('deny', reason) or None. Never
    'allow' — this only ever adds a restriction on top of write_roots, it
    does not grant permission write_roots would otherwise withhold."""
    gate = cfg.get("scope_gate") or {}
    if not gate.get("enabled", True) or root is None or not file_path:
        return None
    try:
        target = Path(file_path).resolve()
    except Exception:
        return None
    protected = _effective_protected_paths(cfg)
    if not any(_resolve_under(root, p, target) for p in protected):
        return None
    if _confirmed_scope_covers(root, target):
        return None
    return (
        "deny",
        f"harness scope gate: `{file_path}` 在受保护范围内（protected_paths: "
        f"{', '.join(str(p) for p in protected)}），但没有找到覆盖它的 `status: confirmed` "
        f"scope artifact。先跑 `/harness-scope-gate` 把这个改动的边界问清楚。",
    )


def check_write(file_path, cfg, root):
    for decision_fn in (write_roots_decision, scope_gate_decision):
        result = decision_fn(file_path, cfg, root)
        if result is not None:
            decide(*result)
    emit()


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
        check_write(tool_input.get("file_path"), cfg, root)
    emit()


if __name__ == "__main__":
    safe_main(main)
