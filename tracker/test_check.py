"""check.py 的单元测试：不调真实 claude，调用点用 monkeypatch 替换。"""
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import check
import config


class ClaudeEnvTest(unittest.TestCase):
    def test_session_identity_vars_removed_auth_kept(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "x", "CLAUDE_CODE_MESSAGING_TOKEN": "y",
                                          "CLAUDECODE": "1", "ANTHROPIC_API_KEY": "k"}):
            env = config.claude_env()
        for k in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDECODE"):
            self.assertNotIn(k, env)
        self.assertEqual(env["ANTHROPIC_API_KEY"], "k")


CHANGES = [{"id": "chg-a", "items": [
    {"id": "1.1", "done": True, "text": "write script"},
    {"id": "1.2", "done": False, "text": "run tests"},
    {"id": "2.1", "done": True, "text": "update docs"},
]}]
GOOD = ('{"changes":[{"id":"chg-a","items":[{"id":"1.1","verdict":"ok","evidence":"pytest passed"},'
        '{"id":"1.2","verdict":"unticked_done","evidence":"ran tests"},{"id":"2.1","verdict":"unsupported"}]}],'
        '"off_spec":["tweaked CI"],"blocked_on":null}')


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig = (config.STATE_DIR, config.SIDECAR_CWD, check._run_extract, check._run_claude)
        config.STATE_DIR = Path(self.tmp.name) / "state"
        config.SIDECAR_CWD = config.STATE_DIR / "sidecar"
        check._run_extract = lambda tp, cwd: "skeleton text"
        check._run_claude = lambda prompt: GOOD
        check._reset_for_tests()
        self.addCleanup(self._restore)
        self.tp = Path(self.tmp.name) / "t.jsonl"
        self.tp.write_text("{}\n")
        self.sess = {"transcript_path": str(self.tp), "cwd": self.tmp.name, "diff": {"files_changed": 1}}

    def _restore(self):
        config.STATE_DIR, config.SIDECAR_CWD, check._run_extract, check._run_claude = self._orig
        check._reset_for_tests()

    # ---- normalize
    def test_parse_json_fenced(self):
        self.assertEqual(check.parse_json_obj('```json\n{"a": 1}\n```'), {"a": 1})
        with self.assertRaises(ValueError):
            check.parse_json_obj("no json")
        with self.assertRaises(ValueError):
            check.parse_json_obj("[1]")

    def test_normalize_drops_invalid(self):
        valid = check.valid_ids_of(CHANGES)
        raw = {"changes": [
            {"id": "chg-a", "items": [
                {"id": "1.1", "verdict": "ok", "evidence": "e"},
                {"id": "9.9", "verdict": "ok"},            # id 不在清单
                {"id": "1.2", "verdict": "great"},          # verdict 非法
                {"id": "1.1", "verdict": "unsupported"},    # 重复
                "junk", {"verdict": "ok"},
            ]},
            {"id": "other", "items": [{"id": "1.1", "verdict": "ok"}]},   # change 不在清单
            "junk",
        ], "off_spec": "bad", "blocked_on": "null"}
        n = check.normalize(raw, valid, 100.0)
        self.assertEqual(n["changes"], [{"id": "chg-a", "items": [{"id": "1.1", "verdict": "ok", "evidence": "e"}]}])
        self.assertEqual(n["off_spec"], [])
        self.assertIsNone(n["blocked_on"])
        self.assertEqual(n["model"], check._model())

    def test_normalize_truncation(self):
        raw = {"changes": [{"id": "chg-a", "items": [{"id": "1.1", "verdict": "ok", "evidence": "e" * 300}]}],
               "off_spec": ["o" * 300] * 12 + [" "], "blocked_on": " waiting "}
        n = check.normalize(raw, check.valid_ids_of(CHANGES), 1.0)
        self.assertEqual(len(n["changes"][0]["items"][0]["evidence"]), 80)
        self.assertEqual(len(n["off_spec"]), 8)
        self.assertEqual(len(n["off_spec"][0]), 120)
        self.assertEqual(n["blocked_on"], "waiting")

    # ---- generate / cache / stale
    def test_generate_and_cache_roundtrip(self):
        self.assertEqual(check.request("s1", self.sess, CHANGES, sync=True), "started")
        r = check.get("s1", str(self.tp))
        self.assertEqual([i["verdict"] for i in r["check"]["changes"][0]["items"]],
                         ["ok", "unticked_done", "unsupported"])
        self.assertEqual(r["check"]["off_spec"], ["tweaked CI"])
        self.assertFalse(r["check"]["stale"])
        self.assertFalse(r["pending"])
        self.assertIsNone(r["error"])
        check._store("s1", {**r["check"], "off_spec": ["newer"]})
        check._reset_for_tests()
        self.assertEqual(check.get("s1")["check"]["off_spec"], ["newer"])
        lines = (config.STATE_DIR / "checks.jsonl").read_text().strip().splitlines()
        self.assertEqual(len(lines), 2)

    def test_stale(self):
        check.request("s1", self.sess, CHANGES, sync=True)
        future = time.time() + 120
        os.utime(self.tp, (future, future))
        self.assertTrue(check.get("s1", str(self.tp))["check"]["stale"])
        near = time.time() + 10
        os.utime(self.tp, (near, near))
        self.assertFalse(check.get("s1", str(self.tp))["check"]["stale"])

    def test_cooldown_and_inflight(self):
        calls = []
        check._run_claude = lambda p: calls.append(p) or '{"changes":[]}'
        self.assertEqual(check.request("s1", self.sess, CHANGES, sync=True), "started")
        self.assertEqual(check.request("s1", self.sess, CHANGES, sync=True), "cooldown")
        self.assertEqual(len(calls), 1)
        check._last_done["s1"] = time.time() - 61
        self.assertEqual(check.request("s1", self.sess, CHANGES, sync=True), "started")
        check._inflight.add("s2")
        self.assertEqual(check.request("s2", self.sess, CHANGES), "pending")
        self.assertTrue(check.get("s2")["pending"])
        self.assertTrue(check.is_pending("s2"))
        self.assertEqual(len(calls), 2)

    def test_no_change_never_calls_llm(self):
        calls = []
        check._run_claude = lambda p: calls.append(p) or GOOD
        self.assertEqual(check.request("s1", self.sess, [], sync=True), "no_change")
        self.assertEqual(check.request("s1", self.sess, None, sync=True), "no_change")
        self.assertEqual(calls, [])
        self.assertFalse(check.is_pending("s1"))
        self.assertIsNone(check.get("s1")["check"])

    def test_failure_recorded_not_raised(self):
        def boom(prompt):
            raise RuntimeError("claude down")
        check._run_claude = boom
        check.request("s1", self.sess, CHANGES, sync=True)
        r = check.get("s1")
        self.assertIsNone(r["check"])
        self.assertIn("claude down", r["error"])
        self.assertFalse(r["pending"])
        check._run_claude = lambda p: "not json"
        check._last_done.clear()
        check.request("s1", self.sess, CHANGES, sync=True)
        self.assertIn("ValueError", check.get("s1")["error"])

    # ---- prompt / claude 调用
    def test_prompt_contains_task_list_and_skeleton(self):
        p = check.build_prompt("SKELETON {braces}", {"diff": {"files_changed": 3}}, CHANGES)
        self.assertIn("只输出一个 JSON 对象", p)
        for needle in ("SKELETON {braces}", "chg-a", "1.1 | [x] | write script", "1.2 | [ ] | run tests",
                       "2.1 | [x] | update docs", "files_changed=3"):
            self.assertIn(needle, p)

    def test_prompt_skeleton_truncated(self):
        p = check.build_prompt("H" * 1500 + "M" * 50000 + "T" * 12000, {}, CHANGES)
        self.assertIn("中间省略", p)
        self.assertNotIn("M" * 100, p)

    def test_run_claude_command(self):
        seen = {}

        def fake_run(cmd, **kw):
            seen.update(cmd=cmd, **kw)
            return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")
        with mock.patch.object(subprocess, "run", fake_run):
            check._run_claude = self._orig[3]
            check._run_claude("PROMPT")
        cmd = seen["cmd"]
        self.assertEqual(cmd[:2], ["claude", "-p"])
        self.assertEqual(cmd[cmd.index("--model") + 1], check._model())
        i = cmd.index("--setting-sources")
        self.assertEqual(cmd[i + 1], "project")
        self.assertIn("--no-session-persistence", cmd)
        self.assertEqual(cmd[-1], "PROMPT")
        self.assertEqual(seen["cwd"], str(config.SIDECAR_CWD))
        self.assertNotIn("CLAUDECODE", seen["env"])

    def test_default_model(self):
        self.assertEqual(config.DEFAULTS["check_model"], "sonnet")
        self.assertNotIn("summary_model", config.DEFAULTS)


if __name__ == "__main__":
    unittest.main()
