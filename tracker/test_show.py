import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

import show


def run(*args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = show.main(list(args))
    return code, out.getvalue(), err.getvalue()


def it(id_, text, done, group=None, level=0, current=False):
    return {"id": id_, "text": text, "done": done, "group": group, "level": level, "current": current}


def change(cid, items):
    d = sum(1 for i in items if i["done"])
    return {"id": cid, "title": cid, "progress": {"done": d, "total": len(items)}, "items": items}


CH = change("demo-change", [
    it("1.1", "写脚本", True, "1. 准备"), it("1.2", "跑脚本", True, "1. 准备"),
    it("2.1", "改配置", True, "2. 实施"),
    it("2.2", "上线", False, "2. 实施", current=True),
    it("2.2.1", "灰度", False, "2. 实施", level=1),
])


def board(sessions, changes=None, name="w"):
    return {"generated_at": "2026-10-12T10:00:00",
            "workspaces": [{"name": name, "changes": changes or [], "sessions": sessions}]}


def sess(**kw):
    return {"session_id": "s1", "name": "demo", "phase": "running", "change_ids": [], **kw}


class TreeTest(unittest.TestCase):
    def test_header(self):
        b = board([sess(phase="needs_input", phase_age_s=180, change_ids=["demo-change"])], [CH])
        out = show.render(b)
        self.assertIn("# harness tracker · 2026-10-12 10:00", out)
        self.assertIn("1 个 session 在等你 · running 0", out)
        self.assertIn("### ● demo — 在等你输入 3m · 3/5", out)

    def test_openspec_tree(self):
        out = show.render(board([sess(change_ids=["demo-change"])], [CH]))
        self.assertIn("- [x] 1. 准备 (2/2)\n", out)
        self.assertNotIn("写脚本", out)  # 全部完成的分组折叠
        self.assertIn("- [ ] 2. 实施 (1/3)", out)
        self.assertIn("  - [x] 2.1 改配置", out)
        self.assertIn("  - [ ] 2.2 上线  ← 正在这条", out)
        self.assertIn("    - [ ] 2.2.1 灰度", out)
        self.assertEqual(out.count("← 正在这条"), 1)

    def test_no_group_items(self):
        ch = change("flat", [it("1", "甲", True), it("2", "乙", False, current=True), it("2.1", "乙子", False, level=1)])
        out = show.render(board([sess(change_ids=["flat"])], [ch]))
        self.assertIn("- [x] 1 甲", out)
        self.assertIn("  - [ ] 2.1 乙子", out)

    def test_check_marks(self):
        chk = {"changes": [{"id": "demo-change", "items": [
            {"id": "1.1", "verdict": "ok", "evidence": "pytest 通过"},
            {"id": "2.1", "verdict": "unsupported", "evidence": ""},
            {"id": "2.2", "verdict": "unticked_done", "evidence": "部署脚本已执行"}]}],
            "off_spec": ["修了 CI 缓存", "改了 README"], "blocked_on": "等审批",
            "generated_at": "2026-10-12T10:05:00", "stale": True}
        out = show.render(board([sess(change_ids=["demo-change"], check=chk)], [CH]))
        self.assertIn("- [x] 1. 准备 (2/2)\n", out)
        self.assertNotIn("✓", out)
        self.assertNotIn("pytest 通过", out)  # ok 不输出
        self.assertIn("  - [x] 2.1 改配置  ⚠ 无证据", out)
        self.assertIn("  - [ ] 2.2 上线  ← 正在这条  ⏳ 似乎已做完\n      证据：部署脚本已执行", out)
        self.assertIn("在等：等审批", out)
        self.assertIn("规格外：修了 CI 缓存；改了 README", out)
        self.assertIn("核对：10:05，已过期", out)

    def test_flagged_done_group_expands(self):
        chk = {"changes": [{"id": "demo-change", "items": [{"id": "1.2", "verdict": "unsupported", "evidence": ""}]}]}
        out = show.render(board([sess(change_ids=["demo-change"], check=chk)], [CH]))
        self.assertIn("- [x] 1. 准备 (2/2)\n  - [x] 1.1 写脚本\n  - [x] 1.2 跑脚本  ⚠ 无证据", out)

    def test_exec_lines(self):
        ch = {**CH, "session_ids": ["s1", "s2"]}
        s1 = sess(change_ids=["demo-change"], phase="needs_input", phase_age_s=120)
        s2 = sess(session_id="s2", name="other", change_ids=["demo-change"], phase_age_s=7200)
        out = show.render(board([s1, s2], [ch]))
        self.assertIn("执行：demo 在等你输入 2m\n执行：other 运行中 2h", out)

    def test_no_check_no_marks(self):
        out = show.render(board([sess(change_ids=["demo-change"])], [CH]))
        self.assertNotIn("在等：", out)
        self.assertNotIn("规格外", out)
        self.assertIn("执行：demo 运行中", out)

    def test_no_process_fields(self):
        out = show.render(board([sess(last_action="正在跑某事", summary={"tasks": [], "blocked_on": "旧", "now": "改文件"})]))
        self.assertNotIn("Claude 梳理", out)
        self.assertNotIn("旧", out)
        self.assertNotIn("现在：", out)
        self.assertNotIn("正在跑某事", out)
        self.assertIn("无 spec", out)


class CliTest(unittest.TestCase):
    def _file(self, b):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(b, f)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_recap_line_and_flag(self):
        rc = {"markdown": "## 续跑\nGO", "generated_at": "2026-10-12T09:35:00", "stale": True}
        p = self._file(board([sess(recap=rc)]))
        _, plain, _ = run("--from-file", p)
        _, full, _ = run("--from-file", p, "--recap")
        self.assertIn("recap：已生成 09:35，已过期（用 --recap 打印全文）", plain)
        self.assertNotIn("GO", plain)
        self.assertIn("## 续跑\nGO", full)

    def test_ws_filter_and_unknown(self):
        p = self._file(board([sess()]))
        self.assertEqual(run("--from-file", p, "w")[0], 0)
        code, out, err = run("--from-file", p, "nope")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("w", err)

    def test_json(self):
        code, out, _ = run("--from-file", self._file(board([sess()])), "--json")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)["workspaces"]), 1)

    def test_orphan_change(self):
        out = show.render(board([sess()], [CH]))
        self.assertIn("未关联 session 的 change：demo-change (3/5)", out)


if __name__ == "__main__":
    unittest.main()
