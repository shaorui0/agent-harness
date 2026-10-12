import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout

import show

HERE = os.path.dirname(os.path.abspath(__file__))
MOCK = os.path.join(HERE, "static", "dev", "mock_board.json")


def run(*args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = show.main(list(args))
    return code, out.getvalue(), err.getvalue()


class ShowTest(unittest.TestCase):
    def test_header_and_current_marker(self):
        code, out, _ = run("--from-file", MOCK)
        self.assertEqual(code, 0)
        self.assertIn("# harness tracker · 2026-10-12 10:00", out)
        self.assertIn("1 个 session 在等你 · running 1 · done 1 · stale 1 · exited 1", out)
        self.assertIn("### ● db-backup — 在等你输入 3m · 7/9", out)
        self.assertEqual(out.count("← 正在这条"), 2)  # 每个 change 一条
        self.assertIn("摘要（已过期）：", out)
        self.assertIn("resume: cd ", out)

    def test_fold_done_items(self):
        _, out, _ = run("--from-file", MOCK, "infra")
        # backup-two-modes: 7 项已完成 > 6，折叠成 6 项 + 最近完成的 1 条
        self.assertIn("- [x] … 已完成 6 项", out)
        # autoscaler-cooldown: 2 项已完成，不折叠
        self.assertNotIn("已完成 1 项", out)

    def test_no_spec_and_empty_changes_workspace(self):
        _, out, _ = run("--from-file", MOCK, "webapp")
        self.assertIn("## webapp", out)
        self.assertEqual(out.count("无 spec"), 2)
        self.assertNotIn("## infra", out)

    def test_ws_filter(self):
        _, out, _ = run("--from-file", MOCK, "infra")
        self.assertIn("## infra", out)
        self.assertNotIn("## webapp", out)

    def test_unknown_ws(self):
        code, out, err = run("--from-file", MOCK, "nope")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("infra", err)
        self.assertIn("webapp", err)

    def test_json(self):
        code, out, _ = run("--from-file", MOCK, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)["workspaces"]), 2)


if __name__ == "__main__":
    unittest.main()
