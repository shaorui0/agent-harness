"""actions.py 的单元测试：用 tempfile 构造小 jsonl，不碰真实 transcript。"""
import json
import tempfile
import unittest
from pathlib import Path

import actions

_n = [0]


def _ts():
    _n[0] += 1
    return f"2026-01-02T03:{_n[0] % 60:02d}:00Z"


def human(text):
    return {"type": "user", "timestamp": _ts(), "cwd": "/w", "message": {"role": "user", "content": text}}


def asst(*blocks, side=False):
    e = {"type": "assistant", "timestamp": _ts(), "message": {"role": "assistant", "content": list(blocks)}}
    if side:
        e["isSidechain"] = True
    return e


def tu(i, name, **inp):
    return {"type": "tool_use", "id": i, "name": name, "input": inp}


def txt(s):
    return {"type": "text", "text": s}


def result(i, body, err=False):
    return {"type": "user", "timestamp": _ts(), "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": i, "content": body, "is_error": err}]}}


class ActionsTest(unittest.TestCase):
    def build(self, rows, **kw):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        p = Path(d.name) / "s.jsonl"
        p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        return actions.build_index(str(p), **kw)

    def rows(self):
        return [
            human("first request please"),
            asst(tu("a", "Bash", command="ls -la && git status")),
            result("a", "total 0"),
            asst(tu("b", "Bash", command="cat x.txt | grep foo | head -3")),
            asst(tu("c", "Bash", command="kubectl get pods -n demo")),
            asst(tu("d", "Write", file_path="/w/src/new.py")),
            asst(tu("e", "Edit", file_path="/w/src/new.py")),
            asst(tu("f", "Edit", file_path="/w/src/new.py")),
            asst(tu("g", "Edit", file_path="/w/src/new.py")),
            asst(tu("h", "Bash", command="python3 -m pytest -q\n# multi line")),
            result("h", "...\n5 passed in 0.1s\n"),
            asst(tu("i", "Bash", command="git commit -m 'x'")),
            asst(tu("j", "Bash", command="echo hi > out.txt")),
            asst(tu("k", "Agent", subagent_type="Explore", description="look around the repo")),
            asst(tu("l", "SendMessage", to="worker", summary="please continue")),
            asst(tu("m", "Bash", command="python3 call.py -H 'Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abcdefghijk.sig' "
                                         "https://x.test AWS_SECRET_ACCESS_KEY=abc123 password=hunter2 AKIAABCDEFGHIJKLMNOP")),
            asst(tu("n", "Bash", command="echo secret", ), side=True),
            asst(tu("o", "Write", file_path="/w/side.py"), txt("SIDE conclusion"), side=True),
            asst(txt("## **Intermediate** words")),
            asst(txt("---\n# Final **summary** of work\n---")),
            human("second request"),
            asst(tu("p", "Bash", command="rm -rf build")),
            asst(txt("cleaned")),
        ]

    def test_readonly_skipped_write_kept(self):
        out = self.build(self.rows())
        for bad in ("ls -la", "cat x.txt", "kubectl get", "git status"):
            self.assertNotIn(bad, out)
        for good in ("Write src/new.py", "Bash: python3 -m pytest -q # multi line", "Bash: git commit",
                     "Bash: echo hi > out.txt", "Bash: rm -rf build",
                     "Agent[Explore]: look around the repo", "SendMessage → worker: please continue"):
            self.assertIn(good, out)

    def test_repeated_edits_merged(self):
        out = self.build(self.rows())
        self.assertIn("Edit src/new.py ×3", out)
        self.assertEqual(out.count("Edit src/new.py"), 1)

    def test_secrets_redacted(self):
        out = self.build(self.rows())
        for secret in ("eyJhbGci", "abc123", "hunter2", "AKIAABCDEFGHIJKLMNOP"):
            self.assertNotIn(secret, out)
        self.assertIn("<redacted>", out)

    def test_sidechain_absent(self):
        out = self.build(self.rows())
        self.assertNotIn("side.py", out)
        self.assertNotIn("SIDE", out)

    def test_titles_conclusions_and_result_hint(self):
        out = self.build(self.rows())
        self.assertIn("T1 01-02 ", out)
        self.assertIn("人类：first request please", out)
        self.assertIn("T2 ", out)
        self.assertIn("→ Final summary of work", out)
        self.assertNotIn("Intermediate", out)
        self.assertNotIn("**", out)
        self.assertIn("→ cleaned", out)
        self.assertIn("  ↳ 5 passed", out)

    def test_budget_keeps_titles_and_conclusions(self):
        rows = []
        for t in range(6):
            rows.append(human(f"request number {t}"))
            for k in range(40):
                rows.append(asst(tu(f"x{t}_{k}", "Write", file_path=f"/w/f_{t}_{k}.py")))
            rows.append(asst(txt(f"conclusion {t}")))
        full = self.build(rows)
        small = self.build(rows, max_chars=2500)
        self.assertGreater(len(full), 2500)
        self.assertLessEqual(len(small), 2500)
        for t in range(6):
            self.assertIn(f"request number {t}", small)
            self.assertIn(f"→ conclusion {t}", small)
        self.assertIn("… 省略", small)
        self.assertIn("f_0_0.py", small)     # 靠近开头保留
        self.assertIn("f_5_39.py", small)    # 靠近结尾保留
        self.assertNotIn("f_3_20.py", small)

    def test_redact_and_readonly_helpers(self):
        self.assertEqual(actions.redact("token=abc"), "token=<redacted>")
        self.assertTrue(actions.is_readonly_bash("git log --oneline | head"))
        self.assertTrue(actions.is_readonly_bash("ls 2>/dev/null"))
        self.assertFalse(actions.is_readonly_bash("sed -i s/a/b/ f"))
        self.assertFalse(actions.is_readonly_bash("kubectl apply -f x.yaml"))
        self.assertFalse(actions.is_readonly_bash("cd d && make test"))

    def test_empty(self):
        self.assertIn("没有人类回合", self.build([asst(txt("x"))]))


if __name__ == "__main__":
    unittest.main()
