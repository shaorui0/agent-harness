import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import board
import openspec

SID_A = "aaaa1111-0000-4000-8000-000000000001"
SID_B = "bbbb2222-0000-4000-8000-000000000002"
SID_C = "cccc3333-0000-4000-8000-000000000003"
SID_D = "dddd4444-0000-4000-8000-000000000004"

TASKS = """# Tasks

## 1. Scope
- [x] 1.1 Pick **two** modes
- [X] 1.2 Move `data` into its own dir
  - [ ] 1.2.1 nested sub item with [link](http://x)
## 2. 验证
- [ ] 2.1 跨集群拷贝
- [ ] plain item without number
1. [ ] 3. should still parse as ordered list? no
- not a task
"""


def write(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class ItemIdTest(unittest.TestCase):
    def test_letter_suffixed_ids_are_kept(self):
        import openspec
        items = openspec.parse_tasks("## 3b. x\n- [x] 3b.1 first\n- [ ] 3b.2 second\n- [ ] 4.1 third\n")
        ids = [i["id"] for i in (items[1] if isinstance(items, tuple) else items)] if not isinstance(items, dict) else [i["id"] for i in items["items"]]
        self.assertEqual(ids, ["3b.1", "3b.2", "4.1"])


class DedupeTest(unittest.TestCase):
    def test_same_session_id_keeps_most_active(self):
        a = {"session_id": "s1", "phase": "done", "phase_since": "2026-01-01T10:00:00Z"}
        b = {"session_id": "s1", "phase": "running", "phase_since": "2026-01-01T09:00:00Z"}
        c = {"session_id": "s1", "phase": "running", "phase_since": "2026-01-01T11:00:00Z"}
        d = {"session_id": "s2", "phase": "exited", "phase_since": "2026-01-01T08:00:00Z"}
        e = {"phase": "done"}
        out = board.dedupe_sessions([a, b, c, d, e])
        self.assertEqual(len(out), 3)
        kept = {x.get("session_id"): x for x in out if x.get("session_id")}
        self.assertIs(kept["s1"], c)
        self.assertIs(kept["s2"], d)


class BoardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name).resolve() / "demo-ws"
        ch = self.ws / "openspec" / "changes"
        write(ch / "alpha-change" / "proposal.md", "# Alpha title\n\nBody text here.\n")
        write(ch / "alpha-change" / "tasks.md", TASKS)
        write(ch / "alpha-change" / "tracker.yml", f"sessions: [{SID_A[:8]}]\n")
        write(ch / "beta-change" / "tasks.md", "- [ ] first\n- [x] second\n")
        write(ch / "archive" / "old-change" / "tasks.md", "- [x] done\n")
        write(self.ws / ".harness" / "project.yml", "name: demo\n")
        write(self.ws / ".harness" / "scope" / "alpha-change.yml",
              "status: confirmed\nreview:\n  status: CLEARED\n")
        write(self.ws / ".harness" / "scope" / "beta-change.yml", "status: [unclosed\n")
        oplog = [
            {"session_id": SID_B, "action": "write", "target": "openspec/changes/beta-change/tasks.md"},
            {"session_id": SID_C, "action": "bash", "target": None, "command": "ls src"},
        ]
        write(self.ws / ".harness" / "oplog.jsonl", "\n".join(json.dumps(r) for r in oplog) + "\n")
        os.environ["HARNESS_TRACKER_HOME"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def sessions(self):
        cwd = str(self.ws / "src")
        base = {"cwd": cwd, "phase": "done", "phase_since": "2026-10-12T00:00:00Z"}
        return [
            {**base, "session_id": SID_A, "name": "one"},
            {**base, "session_id": SID_B, "name": "two", "phase": "running"},
            {**base, "session_id": SID_C, "name": "three", "spec": "continue alpha-change work"},
            {**base, "session_id": SID_D, "name": None, "project_name": "src", "phase": "needs_input",
             "files_touched": [str(self.ws / "openspec/changes/beta-change/design.md")]},
        ]

    def test_scan(self):
        cs = {c["id"]: c for c in openspec.scan(self.ws)}
        self.assertEqual(set(cs), {"alpha-change", "beta-change"})
        a = cs["alpha-change"]
        self.assertEqual(a["title"], "Alpha title")
        self.assertEqual(a["proposal"], "Body text here.")
        self.assertEqual(a["path"], "openspec/changes/alpha-change")
        ids = [i["id"] for i in a["items"]]
        self.assertEqual(ids[:5], ["1.1", "1.2", "1.2.1", "2.1", "5"])
        self.assertEqual(a["items"][0]["text"], "Pick two modes")
        self.assertEqual(a["items"][2]["text"], "nested sub item with link")
        self.assertEqual(a["items"][0]["group"], "1. Scope")
        self.assertEqual(a["items"][3]["group"], "2. 验证")
        self.assertEqual([i["level"] for i in a["items"]][:4], [0, 0, 1, 0])
        cur = [i["id"] for i in a["items"] if i["current"]]
        self.assertEqual(cur, ["1.2.1"])
        self.assertEqual(a["progress"], {"done": 2, "total": len(a["items"])})
        self.assertEqual(a["scope"], {"status": "confirmed", "review": "CLEARED"})
        b = cs["beta-change"]
        self.assertEqual(b["title"], "beta-change")
        self.assertEqual(b["scope"], {"status": None, "review": None})
        self.assertTrue(b["updated_at"].endswith("Z"))

    def test_levels_and_leaf_current(self):
        txt = ("## G\n- [x] 1.1 a\n\t- [x] 1.1.1 tab child\n- [ ] 1.2 parent\n"
               "  - [x] 1.2.1 two-space child\n    - [ ] 1.2.1.1 deeper\n  - [ ] 1.2.2 sibling\n- [ ] 1.3 last\n")
        items = openspec.parse_tasks(txt)
        self.assertEqual([(i["id"], i["level"]) for i in items],
                         [("1.1", 0), ("1.1.1", 1), ("1.2", 0), ("1.2.1", 1), ("1.2.1.1", 2),
                          ("1.2.2", 1), ("1.3", 0)])
        self.assertEqual([i["id"] for i in items if i["current"]], ["1.2.1.1"])
        # 父项未完成但有子项：current 取叶子而不是父项；progress 仍按所有条目
        items = openspec.parse_tasks("- [ ] 1 parent\n    - [ ] 1.1 child\n")
        self.assertEqual([i["id"] for i in items if i["current"]], ["1.1"])
        # 整体缩进的文件：最浅为 0 级
        items = openspec.parse_tasks("  - [ ] a\n    - [ ] b\n")
        self.assertEqual([i["level"] for i in items], [0, 1])

    def test_link_priorities(self):
        res = board.build(self.sessions())
        w = [x for x in res["workspaces"] if x["path"] == str(self.ws)][0]
        by = {s["session_id"]: s for s in w["sessions"]}
        self.assertEqual(by[SID_A]["change_source"], {"alpha-change": "tracker.yml"})
        self.assertEqual(by[SID_B]["change_source"], {"beta-change": "touched"})
        self.assertEqual(by[SID_C]["change_source"], {"alpha-change": "mention"})
        self.assertEqual(by[SID_D]["change_source"], {"beta-change": "touched"})
        self.assertEqual(by[SID_D]["name"], "src")
        self.assertIn(f"claude --resume {SID_A}", by[SID_A]["resume_cmd"])
        ch = {c["id"]: c for c in w["changes"]}
        self.assertEqual(sorted(ch["alpha-change"]["session_ids"]), sorted([SID_A, SID_C]))
        self.assertEqual(sorted(ch["beta-change"]["session_ids"]), sorted([SID_B, SID_D]))

    def test_sort_summary_filter(self):
        res = board.build(self.sessions())
        w = [x for x in res["workspaces"] if x["path"] == str(self.ws)][0]
        self.assertEqual([s["phase"] for s in w["sessions"]], ["needs_input", "running", "done", "done"])
        self.assertEqual(res["summary"]["sessions"], 4)
        self.assertEqual(res["summary"]["needs_input"], 1)
        self.assertIsNone(w["sessions"][0]["check"])
        self.assertFalse(w["sessions"][0]["check_pending"])
        self.assertEqual(board.build(self.sessions(), ws="nope")["workspaces"], [])
        self.assertEqual(len(board.build(self.sessions(), ws="demo-ws")["workspaces"]), 1)

    def test_changes_without_sessions_via_roots(self):
        found = board.find_openspec_workspaces([Path(self.tmp.name)])
        self.assertEqual(found, [str(self.ws)])

    def test_oplog_tail_truncation(self):
        p = self.ws / ".harness" / "oplog.jsonl"
        old = board.OPLOG_TAIL_BYTES
        try:
            line = json.dumps({"session_id": SID_B, "target": "openspec/changes/alpha-change/x"}) + "\n"
            write(p, line * 50)
            board.OPLOG_TAIL_BYTES = len(line) * 3 + 5
            board._oplog_cache.clear()
            self.assertEqual(board.oplog_changes(str(self.ws)), {SID_B: {"alpha-change"}})
        finally:
            board.OPLOG_TAIL_BYTES = old


if __name__ == "__main__":
    unittest.main()
