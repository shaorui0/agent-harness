"""recap.py 的单元测试：不调真实 claude，调用点用 monkeypatch 替换。"""
import os
import tempfile
import time
import unittest
from pathlib import Path

import config
import recap

MD = "## 一句话\nsomething\n## 续跑指令\n```\nresume here\n```"


class RecapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig = (config.STATE_DIR, config.SIDECAR_CWD, recap._run_extract, recap._run_claude, recap._settings)
        config.STATE_DIR = Path(self.tmp.name) / "state"
        config.SIDECAR_CWD = config.STATE_DIR / "sidecar"
        self.skill = Path(self.tmp.name) / "SKILL.md"
        self.skill.write_text("SKILL BODY UNIQUE")
        self.prompts = []
        recap._run_extract = lambda tp, cwd: "SKELETON UNIQUE"
        recap._run_claude = lambda prompt, model: (self.prompts.append((prompt, model)) or MD)
        recap._settings = lambda: (str(self.skill), "sonnet")
        recap._reset_for_tests()
        self.addCleanup(self._restore)
        self.tp = Path(self.tmp.name) / "t.jsonl"
        self.tp.write_text("{}\n")
        self.sess = {"transcript_path": str(self.tp), "cwd": self.tmp.name, "spec": "first prompt",
                     "diff": {"files_changed": 2}}

    def _restore(self):
        (config.STATE_DIR, config.SIDECAR_CWD, recap._run_extract, recap._run_claude, recap._settings) = self._orig
        recap._reset_for_tests()

    def test_generate_and_cache_roundtrip(self):
        self.assertEqual(recap.request("s1", self.sess, sync=True), "started")
        r = recap.get("s1", str(self.tp))
        self.assertEqual(r["recap"]["markdown"], MD)
        self.assertEqual(r["recap"]["model"], "sonnet")
        self.assertFalse(r["recap"]["skill_missing"])
        self.assertFalse(r["recap"]["stale"])
        self.assertFalse(r["pending"])
        self.assertIsNone(r["error"])
        recap._store("s1", {**r["recap"], "markdown": "newer"})
        recap._reset_for_tests()
        self.assertEqual(recap.get("s1")["recap"]["markdown"], "newer")
        self.assertEqual(len((config.STATE_DIR / "recaps.jsonl").read_text().strip().splitlines()), 2)

    def test_stale(self):
        recap.request("s1", self.sess, sync=True)
        far = time.time() + 120
        os.utime(self.tp, (far, far))
        self.assertTrue(recap.get("s1", str(self.tp))["recap"]["stale"])
        near = time.time() + 10
        os.utime(self.tp, (near, near))
        self.assertFalse(recap.get("s1", str(self.tp))["recap"]["stale"])

    def test_cooldown_and_inflight(self):
        self.assertEqual(recap.request("s1", self.sess, sync=True), "started")
        self.assertEqual(recap.request("s1", self.sess, sync=True), "cooldown")
        self.assertEqual(len(self.prompts), 1)
        recap._last_done["s1"] = time.time() - 61
        self.assertEqual(recap.request("s1", self.sess, sync=True), "started")
        recap._inflight.add("s2")
        self.assertEqual(recap.request("s2", self.sess), "pending")
        self.assertTrue(recap.get("s2")["pending"])
        self.assertTrue(recap.is_pending("s2"))

    def test_failure_recorded_not_raised(self):
        def boom(prompt, model):
            raise RuntimeError("claude down")
        recap._run_claude = boom
        recap.request("s1", self.sess, sync=True)
        r = recap.get("s1")
        self.assertIsNone(r["recap"])
        self.assertIn("claude down", r["error"])
        self.assertFalse(r["pending"])

    def test_skill_missing_falls_back(self):
        recap._settings = lambda: (str(Path(self.tmp.name) / "nope.md"), "haiku")
        recap.request("s1", self.sess, sync=True)
        r = recap.get("s1")["recap"]
        self.assertTrue(r["skill_missing"])
        self.assertEqual(r["model"], "haiku")
        self.assertIn("七段", self.prompts[0][0])
        self.assertEqual(self.prompts[0][1], "haiku")

    def test_fenced_output_stripped(self):
        self.assertEqual(recap.clean_output("```markdown\n## A\nx\n```\n"), "## A\nx")
        self.assertEqual(recap.clean_output("  ## A\n  "), "## A")
        # 内部代码块、多个代码块不被误剥
        self.assertEqual(recap.clean_output(MD), MD)
        multi = "```\na\n```\ntext\n```\nb\n```"
        self.assertEqual(recap.clean_output(multi), multi)
        recap._run_claude = lambda p, m: "```\n## 一句话\nok\n```"
        recap.request("s1", self.sess, sync=True)
        self.assertEqual(recap.get("s1")["recap"]["markdown"], "## 一句话\nok")

    def test_prompt_contains_skill_and_skeleton(self):
        recap.request("s1", self.sess, sync=True)
        p = self.prompts[0][0]
        for needle in ("SKILL BODY UNIQUE", "SKELETON UNIQUE", "first prompt", "files_changed=2", "不要调用任何工具"):
            self.assertIn(needle, p)

    def test_skeleton_trimmed(self):
        big = "H" * 3000 + "M" * 50000 + "T" * 30000
        out = recap.trim_skeleton(big)
        self.assertTrue(out.startswith("H" * 3000))
        self.assertTrue(out.endswith("T" * 30000))
        self.assertNotIn("M" * 100, out)
        self.assertEqual(recap.trim_skeleton("short"), "short")

    def test_claude_command_has_setting_sources(self):
        captured = {}
        import subprocess

        class P:
            returncode = 0; stdout = "x"; stderr = ""

        def fake_run(cmd, **kw):
            captured["cmd"], captured["kw"] = cmd, kw
            return P()
        orig = subprocess.run
        subprocess.run = fake_run
        self.addCleanup(lambda: setattr(subprocess, "run", orig))
        recap._run_claude = self._orig[3]
        self.assertEqual(recap._run_claude("PROMPT", "sonnet"), "x")
        cmd = captured["cmd"]
        self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "project")
        self.assertEqual(cmd[cmd.index("--model") + 1], "sonnet")
        self.assertIn("--no-session-persistence", cmd)
        self.assertEqual(cmd[-1], "PROMPT")
        self.assertEqual(captured["kw"]["timeout"], 180)


if __name__ == "__main__":
    unittest.main()
