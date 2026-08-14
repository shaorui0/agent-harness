#!/usr/bin/env python3
"""Integration tests for harness-stop-contract.py.

No test framework — run directly: python3 hooks/test_stop_contract.py

Runs the hook as a real subprocess (it reads stdin JSON and calls sys.exit,
same as Claude Code invokes it) with an isolated HOME per test so guard_once
markers never leak between tests or into the real ~/.harness/run.

The Stop hook judges only this turn's own reply. It used to also scan
`.harness/scope/*.yml` and block on any unconverged artifact; that guard was
removed (see docs/scope-and-spec-gate-design.md 修订记录), so the first test
below is a regression test pinning the *absence* of that scan — an unconverged
scope must not interrupt a session that never touched it.

Covered:
  - confirmed-but-unconverged scope -> no block (no cross-session scanning)
  - reply that asks permission -> continue guard blocks, scope state irrelevant
  - no scope artifacts at all -> no block, no error
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK = HERE / "harness-stop-contract.py"


def _project(tmp, scope_files=None):
    root = Path(tmp) / "project"
    scope_dir = root / ".harness" / "scope"
    scope_dir.mkdir(parents=True, exist_ok=True)
    (root / ".harness" / "project.yml").write_text("name: test\n", encoding="utf-8")
    for name, body in (scope_files or {}).items():
        (scope_dir / f"{name}.yml").write_text(body, encoding="utf-8")
    return root


def run_hook(tmp, root, last_message, session="s1"):
    home = Path(tmp) / "home"
    home.mkdir(exist_ok=True)
    event = {
        "hook_event_name": "Stop",
        "cwd": str(root),
        "session_id": session,
        "last_assistant_message": last_message,
    }
    import os
    env = dict(os.environ, HOME=str(home))
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(event), capture_output=True, text=True, env=env, timeout=10,
    )
    return proc


def test_unconverged_spec_does_not_block():
    """Regression: no scope scanning at Stop. A confirmed artifact that never
    converged belongs to whoever opened it — it must not end this turn."""
    with tempfile.TemporaryDirectory() as tmp:
        root = _project(tmp, scope_files={"my-scope": "status: confirmed\n"})
        proc = run_hook(tmp, root, "done")
        assert proc.returncode == 0, f"hook must never fail the turn, rc={proc.returncode} stderr={proc.stderr}"
        assert proc.stdout.strip() == "", f"expected no block payload, got {proc.stdout!r}"


def test_permission_asking_reply_hits_continue_guard():
    with tempfile.TemporaryDirectory() as tmp:
        root = _project(tmp, scope_files={"my-scope": "status: confirmed\n"})
        text = "这部分我先做到这里，要不要我继续往下把剩下的做完，还是先在这里停一下等你确认一下？"
        proc = run_hook(tmp, root, text)
        assert proc.stdout.strip(), "expected a block payload on stdout, got nothing"
        payload = json.loads(proc.stdout)
        assert payload.get("decision") == "block", f"expected block, got {payload}"
        assert "continue guard" in payload.get("reason", ""), payload


def test_no_scope_artifacts_no_block_no_error():
    with tempfile.TemporaryDirectory() as tmp:
        root = _project(tmp)  # no scope files at all
        proc = run_hook(tmp, root, "完成")  # short, unrelated to permission-asking
        assert proc.returncode == 0, f"hook must never fail the turn, got rc={proc.returncode} stderr={proc.stderr}"
        assert proc.stdout.strip() == "", f"expected no block payload, got {proc.stdout!r}"


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main():
    failures = []
    for t in TESTS:
        try:
            t()
            print(f"ok   {t.__name__}")
        except AssertionError as e:
            failures.append(t.__name__)
            print(f"FAIL {t.__name__}: {e}")
    if failures:
        print(f"\n{len(failures)}/{len(TESTS)} failed")
        sys.exit(1)
    print(f"\n{len(TESTS)}/{len(TESTS)} passed")


if __name__ == "__main__":
    main()
