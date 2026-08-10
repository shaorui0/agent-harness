#!/usr/bin/env python3
"""
UserPromptSubmit hook — per-turn output-format reinforcement.

SessionStart injects the full contract once, at turn 0. Long sessions drift —
the model stops following it, or the SessionStart context gets compacted away.
This hook re-asserts a one-line pointer before every turn so the format holds
without re-injecting the whole skeleton/table every time.

Source of truth for the actual contract is skills/harness-output-format/SKILL.md;
this hook only ever prints a short pointer at it, never the full spec.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import emit, emit_context, load_config, read_event, safe_main  # noqa: E402

REMINDER = (
    "【格式】实质性回复用 结论/证据/问题/下一步 骨架：bullet 走并列项，表格走对照，"
    "自然段走因果链；删过渡句、铺垫句、总结句。纯对话/简单确认/单点问答不受此约束。"
    "完整规范见 skill `harness-output-format`。"
)


def main():
    event = read_event()
    cwd = event.get("cwd") or "."
    cfg, _root = load_config(cwd)
    out = cfg.get("output") or {}
    if out.get("contract") != "structured":
        emit()
    emit_context("UserPromptSubmit", REMINDER)


if __name__ == "__main__":
    safe_main(main)
