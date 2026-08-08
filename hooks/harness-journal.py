#!/usr/bin/env python3
"""
Stop hook — the zero-friction write path (WAL).

Appends one record per turn. Makes no judgment about importance, does no
summarisation, calls no model. Whatever selection and compaction is needed
happens later in /harness-compact.

Record shape (one JSON object per line):
  {"ts", "session_id", "cwd", "project", "git": {...}, "chars", "text"}

Writes to <project>/.harness/journal/YYYY-MM-DD.jsonl when a project is found,
otherwise to ~/.harness/journal/YYYY-MM-DD.jsonl so nothing is lost in
unconfigured workspaces.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import (  # noqa: E402
    GLOBAL_DIR, emit, git_info, load_config, message_text, read_event,
    safe_main, truncate,
)


def journal_path(cfg, root):
    day = datetime.now().strftime("%Y-%m-%d")
    if root is not None:
        base = root / (cfg.get("journal", {}).get("dir") or ".harness/journal")
    else:
        base = GLOBAL_DIR / "journal"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{day}.jsonl"


def main():
    event = read_event()
    # Subagent turns arrive on SubagentStop; keep the main-turn WAL clean.
    if event.get("hook_event_name") not in (None, "Stop"):
        emit()

    cwd = event.get("cwd") or "."
    cfg, root = load_config(cwd)
    text = message_text(event.get("last_assistant_message"))
    if not text.strip():
        emit()

    limit = int((cfg.get("journal") or {}).get("max_message_chars") or 4000)
    record = {
        "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "session_id": event.get("session_id"),
        "cwd": cwd,
        "project": cfg.get("name") or (root.name if root else None),
        "git": git_info(cwd),
        "chars": len(text),
        "text": truncate(text, limit),
    }

    path = journal_path(cfg, root)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    emit()


if __name__ == "__main__":
    safe_main(main)
