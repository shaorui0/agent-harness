"""summary.py 的单元测试：不调真实 claude，调用点用 monkeypatch 替换。"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import config
import summary


class SummaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig = (config.STATE_DIR, config.SIDECAR_CWD, summary._run_extract, summary._run_claude)
        config.STATE_DIR = Path(self.tmp.name) / "state"
        config.SIDECAR_CWD = config.STATE_DIR / "sidecar"
        summary._run_extract = lambda tp, cwd: "skeleton text"
        summary._run_claude = lambda prompt: '{"done":["wrote a.sh"],"remaining":["test it"],"blocked_on":null,"diff_note":"touched a.sh"}'
        summary._reset_for_tests()
        self.addCleanup(self._restore)
        self.tp = Path(self.tmp.name) / "t.jsonl"
        self.tp.write_text("{}\n")
        self.sess = {"transcript_path": str(self.tp), "cwd": self.tmp.name, "diff": {"files_changed": 1}}

    def _restore(self):
        config.STATE_DIR, config.SIDECAR_CWD, summary._run_extract, summary._run_claude = self._orig
        summary._reset_for_tests()

    def test_parse_json_fenced(self):
        self.assertEqual(summary.parse_json_obj('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(summary.parse_json_obj('ok: {"a": {"b": 2}} done'), {"a": {"b": 2}})
        with self.assertRaises(ValueError):
            summary.parse_json_obj("no json")
        with self.assertRaises(ValueError):
            summary.parse_json_obj("[1, 2]")

    def test_normalize(self):
        n = summary.normalize({"done": ["x", " "], "remaining": "bad", "blocked_on": "null"}, 100.0)
        self.assertEqual(n["done"], ["x"])
        self.assertEqual(n["remaining"], [])
        self.assertIsNone(n["blocked_on"])
        self.assertEqual(n["model"], "haiku")

    def test_generate_and_cache_roundtrip(self):
        self.assertEqual(summary.request("s1", self.sess, [], sync=True), "started")
        r = summary.get("s1", str(self.tp))
        self.assertEqual(r["summary"]["done"], ["wrote a.sh"])
        self.assertFalse(r["summary"]["stale"])
        self.assertFalse(r["pending"])
        self.assertIsNone(r["error"])
        # 重载后从 jsonl 读回；同 session 多条取最后一条
        summary._store("s1", {**r["summary"], "done": ["newer"]})
        summary._reset_for_tests()
        self.assertEqual(summary.get("s1")["summary"]["done"], ["newer"])
        lines = (config.STATE_DIR / "summaries.jsonl").read_text().strip().splitlines()
        self.assertEqual(len(lines), 2)

    def test_stale(self):
        summary.request("s1", self.sess, [], sync=True)
        future = time.time() + 120
        os.utime(self.tp, (future, future))
        self.assertTrue(summary.get("s1", str(self.tp))["summary"]["stale"])
        near = time.time() + 10
        os.utime(self.tp, (near, near))
        self.assertFalse(summary.get("s1", str(self.tp))["summary"]["stale"])

    def test_cooldown_and_inflight(self):
        calls = []
        summary._run_claude = lambda p: calls.append(p) or '{"done":[]}'
        self.assertEqual(summary.request("s1", self.sess, [], sync=True), "started")
        self.assertEqual(summary.request("s1", self.sess, [], sync=True), "cooldown")
        self.assertEqual(len(calls), 1)
        summary._last_done["s1"] = time.time() - 61
        self.assertEqual(summary.request("s1", self.sess, [], sync=True), "started")
        summary._inflight.add("s2")
        self.assertEqual(summary.request("s2", self.sess, []), "pending")
        self.assertTrue(summary.get("s2")["pending"])

    def test_failure_recorded_not_raised(self):
        def boom(prompt):
            raise RuntimeError("claude down")
        summary._run_claude = boom
        summary.request("s1", self.sess, [], sync=True)
        r = summary.get("s1")
        self.assertIsNone(r["summary"])
        self.assertIn("claude down", r["error"])
        self.assertFalse(r["pending"])
        summary._run_claude = lambda p: "not json"
        summary._last_done.clear()
        summary.request("s1", self.sess, [], sync=True)
        self.assertIn("ValueError", summary.get("s1")["error"])

    def test_prompt_contains_inputs(self):
        p = summary.build_prompt("SKELETON {braces}", {"diff": {"files_changed": 3}},
                                 [{"id": "chg-a", "tasks_text": "- [ ] 1.1 do thing"}])
        for needle in ("SKELETON {braces}", "chg-a", "1.1 do thing", "files_changed=3"):
            self.assertIn(needle, p)


if __name__ == "__main__":
    unittest.main()
