#!/usr/bin/env python3
"""Tests for harness-openspec-aware.py (unittest; fake workspace + fake oplog).

    cd hooks && python3 -m unittest test_openspec_aware

Never touches the real HOME: subprocess runs get an isolated HOME, in-process
calls pass an injected run_dir for the once-per-session marker.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK = HERE / "harness-openspec-aware.py"

sys.path.insert(0, str(HERE))
_spec = importlib.util.spec_from_file_location("harness_openspec_aware", HOOK)
aware = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(aware)

SID = "abcdef12-0000-0000-0000-000000000000"


class Fixture:
    def __init__(self, tmp, changes=True, project_yml="name: t\n"):
        self.tmp = Path(tmp)
        self.root = self.tmp / "ws"
        (self.root / ".harness").mkdir(parents=True)
        if project_yml is not None:
            (self.root / ".harness" / "project.yml").write_text(project_yml, encoding="utf-8")
        if changes:
            (self.root / "openspec" / "changes").mkdir(parents=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.run_dir = self.home / ".harness" / "run"

    def change(self, name, tasks="", title=None, tracker=None, mtime=None):
        d = self.root / "openspec" / "changes" / name
        d.mkdir(parents=True, exist_ok=True)
        if title:
            (d / "proposal.md").write_text(f"# {title}\n\nbody\n", encoding="utf-8")
        (d / "tasks.md").write_text(tasks, encoding="utf-8")
        if tracker is not None:
            (d / "tracker.yml").write_text(tracker, encoding="utf-8")
        if mtime:
            for f in d.iterdir():
                os.utime(f, (mtime, mtime))
            os.utime(d, (mtime, mtime))
        return d

    def oplog(self, records, raw_prefix=""):
        p = self.root / ".harness" / "oplog.jsonl"
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(raw_prefix)
            for r in records:
                fh.write(json.dumps(r) + "\n")
        return p

    def writes(self, n, files, sid=SID, prefix="src"):
        return [{"session_id": sid, "tool": "Edit", "action": "edit", "agent": None,
                 "ts": "2026-10-01T10:00:00+09:00", "cwd": str(self.root),
                 "target": f"{self.root}/{prefix}/f{i % files}.py"} for i in range(n)]

    def event(self, name, sid=SID, cwd=None):
        return {"hook_event_name": name, "session_id": sid, "cwd": str(cwd or self.root)}

    def run(self, event_or_raw):
        raw = event_or_raw if isinstance(event_or_raw, str) else json.dumps(event_or_raw)
        env = dict(os.environ, HOME=str(self.home))
        return subprocess.run([sys.executable, str(HOOK)], input=raw,
                              capture_output=True, text=True, env=env, timeout=10)


def tasks(done, total):
    return "## 1. x\n\n" + "\n".join(
        f"- [{'x' if i < done else ' '}] 1.{i + 1} step" for i in range(total)) + "\n"


class ScopeTests(unittest.TestCase):
    def test_no_openspec_dir_is_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Fixture(tmp, changes=False)
            f.oplog(f.writes(50, 10))
            for name in ("SessionStart", "UserPromptSubmit"):
                self.assertIsNone(aware.build_output(f.event(name), f.run_dir))

    def test_nudge_true_enables_without_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Fixture(tmp, changes=False, project_yml="name: t\nopenspec:\n  nudge: true\n")
            f.oplog(f.writes(12, 4))
            out = aware.build_output(f.event("UserPromptSubmit"), f.run_dir)
            self.assertIsNotNone(out)

    def test_nudge_false_disables(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Fixture(tmp, project_yml="name: t\nopenspec:\n  nudge: false\n")
            f.change("a", tasks(1, 3), title="A")
            f.oplog(f.writes(50, 10))
            self.assertIsNone(aware.build_output(f.event("SessionStart"), f.run_dir))
            self.assertIsNone(aware.build_output(f.event("UserPromptSubmit"), f.run_dir))

    def test_ancestor_with_changes_dir_without_project_yml(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Fixture(tmp, project_yml=None)
            f.change("a", tasks(1, 3), title="A")
            sub = f.root / "deep" / "er"
            sub.mkdir(parents=True)
            out = aware.build_output(f.event("SessionStart", cwd=sub), f.run_dir)
            self.assertIsNotNone(out)
            self.assertIn("a  1/3  A", out[1])


class SessionStartTests(unittest.TestCase):
    def test_format_order_limit_and_completed_hidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Fixture(tmp)
            for i in range(7):
                f.change(f"c{i}", tasks(1, 3), title=f"Title {i}", mtime=1_700_000_000 + i * 100)
            f.change("finished", tasks(3, 3), title="Done", mtime=1_800_000_000)
            (f.root / "openspec" / "changes" / "archive" / "old").mkdir(parents=True)
            _n, text = aware.build_output(f.event("SessionStart"), f.run_dir)
            lines = text.splitlines()
            self.assertEqual(lines[0], aware.HEADER)
            self.assertEqual(len(lines), 1 + 5 + 1)
            self.assertEqual(lines[1], "c6  1/3  Title 6")  # newest first
            self.assertEqual(lines[5], "c2  1/3  Title 2")
            self.assertEqual(lines[6], "…另有 2 个")
            self.assertNotIn("finished", text)
            self.assertNotIn("archive", text)

    def test_title_falls_back_to_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Fixture(tmp)
            f.change("no-proposal", tasks(0, 2))
            _n, text = aware.build_output(f.event("SessionStart"), f.run_dir)
            self.assertIn("no-proposal  0/2  no-proposal", text)

    def test_no_listable_change_is_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Fixture(tmp)
            self.assertIsNone(aware.build_output(f.event("SessionStart"), f.run_dir))
            f.change("finished", tasks(2, 2), title="Done")
            self.assertIsNone(aware.build_output(f.event("SessionStart"), f.run_dir))


class NudgeTests(unittest.TestCase):
    def nudge(self, f, sid=SID):
        return aware.build_output(f.event("UserPromptSubmit", sid=sid), f.run_dir)

    def test_threshold_boundaries(self):
        cases = [(10, 3, True), (9, 3, False), (10, 2, False), (30, 1, False)]
        for n, files, expect in cases:
            with self.subTest(n=n, files=files), tempfile.TemporaryDirectory() as tmp:
                f = Fixture(tmp)
                f.oplog(f.writes(n, files))
                self.assertEqual(self.nudge(f) is not None, expect)

    def test_configurable_thresholds(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Fixture(tmp, project_yml=(
                "name: t\nopenspec:\n  nudge_min_writes: 4\n  nudge_min_files: 2\n"))
            f.oplog(f.writes(4, 2))
            out = self.nudge(f)
            self.assertIsNotNone(out)
            self.assertIn("已写了 4 次、涉及 2 个文件", out[1])

    def test_other_sessions_do_not_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Fixture(tmp)
            f.oplog(f.writes(50, 10, sid="someone-else"))
            self.assertIsNone(self.nudge(f))

    def test_bound_by_tracker_full_or_short_id(self):
        for entry in (SID, SID[:8]):
            with self.subTest(entry=entry), tempfile.TemporaryDirectory() as tmp:
                f = Fixture(tmp)
                f.change("c", tasks(0, 3), title="C", tracker=f"sessions: [{entry}]\n")
                f.oplog(f.writes(20, 5))
                self.assertIsNone(self.nudge(f))

    def test_tracker_block_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Fixture(tmp)
            f.change("c", tasks(0, 3), tracker=f"sessions:\n  - other\n  - {SID[:8]}\n")
            f.oplog(f.writes(20, 5))
            self.assertIsNone(self.nudge(f))

    def test_wrote_change_dir_counts_as_linked(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Fixture(tmp)
            recs = f.writes(20, 5)
            recs.append({"session_id": SID, "ts": "2026-10-01T10:00:00+09:00",
                         "cwd": str(f.root),
                         "target": "openspec/changes/new-thing/proposal.md"})  # relative
            f.oplog(recs)
            self.assertIsNone(self.nudge(f))

    def test_sibling_path_is_not_change_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Fixture(tmp)
            recs = f.writes(20, 5, prefix="openspec/changes-notes")
            f.oplog(recs)
            self.assertIsNotNone(self.nudge(f))

    def test_truncated_tail_and_garbage_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Fixture(tmp)
            filler = [{"session_id": "old", "target": "x" * 200, "ts": "t"}
                      for _ in range(aware.TAIL_BYTES // 200)]
            recs = filler + f.writes(12, 4)
            p = f.oplog(recs, raw_prefix="not json\n")
            with open(p, "a", encoding="utf-8") as fh:
                fh.write("{broken json\n")
            self.assertGreater(p.stat().st_size, aware.TAIL_BYTES)
            out = self.nudge(f)
            self.assertIsNotNone(out)
            self.assertIn("已写了 12 次", out[1])

    def test_only_once_per_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Fixture(tmp)
            f.oplog(f.writes(12, 4))
            self.assertIsNotNone(self.nudge(f))
            self.assertIsNone(self.nudge(f))
            self.assertTrue(aware.marker_path(SID, f.run_dir).exists())

    def test_missing_oplog_or_session_id_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Fixture(tmp)
            self.assertIsNone(self.nudge(f))
            f.oplog(f.writes(12, 4))
            self.assertIsNone(aware.build_output(
                {"hook_event_name": "UserPromptSubmit", "cwd": str(f.root)}, f.run_dir))


class ProcessTests(unittest.TestCase):
    def test_output_shape_matches_other_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Fixture(tmp)
            f.change("c", tasks(1, 3), title="C")
            f.oplog(f.writes(12, 4))
            p = f.run(f.event("SessionStart"))
            self.assertEqual(p.returncode, 0)
            self.assertEqual(set(json.loads(p.stdout)), {"hookSpecificOutput"})
            hso = json.loads(p.stdout)["hookSpecificOutput"]
            self.assertEqual(hso["hookEventName"], "SessionStart")
            self.assertIn("c  1/3  C", hso["additionalContext"])

            p = f.run(f.event("UserPromptSubmit"))
            out = json.loads(p.stdout)
            self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
            self.assertNotIn("decision", out)
            self.assertNotIn("block", p.stdout.lower().replace("blocked", ""))
            self.assertTrue((f.run_dir / f"{SID}.{aware.TAG}").exists())
            # second prompt: silent
            p = f.run(f.event("UserPromptSubmit"))
            self.assertEqual((p.returncode, p.stdout), (0, ""))

    def test_bad_input_is_silent_exit_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Fixture(tmp)
            for raw in ("", "   ", "{not json", "[]", "null", "{}",
                        json.dumps({"hook_event_name": "UserPromptSubmit"}),
                        json.dumps({"hook_event_name": "SessionStart", "cwd": 5}),
                        json.dumps({"hook_event_name": "Stop", "cwd": str(f.root)})):
                with self.subTest(raw=raw):
                    p = f.run(raw)
                    self.assertEqual((p.returncode, p.stdout), (0, ""))

    def test_dry_run_writes_no_marker_and_no_hook_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Fixture(tmp)
            f.change("c", tasks(1, 3), title="C")
            f.oplog(f.writes(12, 4))
            env = dict(os.environ, HOME=str(f.home))
            p = subprocess.run([sys.executable, str(HOOK), "--dry-run", "--root", str(f.root),
                                "--since", "2026-10-01"],
                               capture_output=True, text=True, env=env, timeout=10)
            self.assertEqual(p.returncode, 0)
            self.assertNotIn("hookSpecificOutput", p.stdout)
            self.assertIn("共 1 个 session，会提醒 1 个", p.stdout)
            self.assertIn(SID[:8], p.stdout)
            self.assertIn("c  1/3  C", p.stdout)
            self.assertFalse(f.run_dir.exists())


if __name__ == "__main__":
    unittest.main()
