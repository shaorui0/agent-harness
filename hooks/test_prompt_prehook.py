#!/usr/bin/env python3
"""Tests for harness-prompt-prehook.py.

Run: python3 hooks/test_prompt_prehook.py   (or python3 -m unittest hooks.test_prompt_prehook -v)

Hook runs as a real subprocess with an isolated HOME, so the developer's own
~/.harness/config.yml and env never leak in. Axiom data is the synthetic
fixture in hooks/fixtures_prehook/.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK = HERE / "harness-prompt-prehook.py"
FIXTURE = HERE / "fixtures_prehook" / "axioms_INDEX.md"
MAX_CHARS = 600


def run(prompt, env_axioms=None, home=None, cwd=None, raw=None):
    env = {k: v for k, v in os.environ.items() if k != "HARNESS_PREHOOK_AXIOMS"}
    env["HOME"] = home or tempfile.mkdtemp()
    if env_axioms:
        env["HARNESS_PREHOOK_AXIOMS"] = str(env_axioms)
    payload = raw if raw is not None else json.dumps({"prompt": prompt, "cwd": cwd or env["HOME"]})
    t = time.perf_counter()
    p = subprocess.run([sys.executable, str(HOOK)], input=payload, capture_output=True,
                       text=True, timeout=5, env=env)
    return p, time.perf_counter() - t


def ctx(prompt, **kw):
    p, _ = run(prompt, **kw)
    assert p.returncode == 0
    return json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"] if p.stdout.strip() else ""


class AxiomTriggers(unittest.TestCase):
    def test_verify(self):
        c = ctx("这个方案怎么验证它真的可靠？", env_axioms=FIXTURE)
        self.assertIn("V02", c)
        self.assertIn("可验证性", c)

    def test_selection(self):
        c = ctx("帮我对比一下这两个框架，技术选型怎么定", env_axioms=FIXTURE)
        self.assertIn("A06", c)
        self.assertIn("T08", c)

    def test_resume(self):
        self.assertIn("A16", ctx("我想改一下简历里这个项目的写法", env_axioms=FIXTURE))


class ConfigResolution(unittest.TestCase):
    PROMPT = "这个方案怎么验证它真的可靠？"

    def test_default_none_has_no_axiom_section(self):
        self.assertEqual(ctx(self.PROMPT), "")
        c = ctx("搞个新工具给我用用看", )
        self.assertNotIn("公理", c)
        self.assertIn("构建类请求未写清", c)

    def test_global_config(self):
        home = tempfile.mkdtemp()
        (Path(home) / ".harness").mkdir()
        (Path(home) / ".harness" / "config.yml").write_text(f"prehook:\n  axioms_index: {FIXTURE}\n")
        self.assertIn("V02", ctx(self.PROMPT, home=home))

    def test_project_yml_relative_path_and_overrides_global(self):
        home = tempfile.mkdtemp()
        (Path(home) / ".harness").mkdir()
        (Path(home) / ".harness" / "config.yml").write_text("prehook:\n  axioms_index: /nonexistent/x.md\n")
        proj = Path(tempfile.mkdtemp()) / "repo"
        (proj / ".harness").mkdir(parents=True)
        (proj / "idx.md").write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
        (proj / ".harness" / "project.yml").write_text("name: t\nprehook:\n  axioms_index: idx.md\n")
        self.assertIn("V02", ctx(self.PROMPT, home=home, cwd=str(proj)))

    def test_env_overrides_project(self):
        home = tempfile.mkdtemp()
        proj = Path(tempfile.mkdtemp()) / "repo"
        (proj / ".harness").mkdir(parents=True)
        (proj / ".harness" / "project.yml").write_text("name: t\nprehook:\n  axioms_index: /nonexistent/x.md\n")
        self.assertIn("V02", ctx(self.PROMPT, env_axioms=FIXTURE, home=home, cwd=str(proj)))

    def test_missing_file_degrades_silently(self):
        self.assertEqual(ctx(self.PROMPT, env_axioms="/nonexistent/idx.md"), "")


class VagueBuild(unittest.TestCase):
    def test_vague_gets_hints(self):
        c = ctx("搞个prehook，每次发prompt都帮我优化一下")
        for k in ("产出", "验证", "不碰"):
            self.assertIn(k, c)

    def test_specific_build_no_hints(self):
        c = ctx("实现一个解析器，验收：pytest 全部通过，只改 tools/foo 目录")
        self.assertNotIn("构建类请求未写清", c)


class NoNoise(unittest.TestCase):
    def test_short_and_paths(self):
        for p in ["go", "跑", "ok", "好的", "继续", "/tmp/work/a/b.md", "~/work/x.py",
                  "https://example.com/a?b=1", "/clear", "", "   "]:
            with self.subTest(p=p):
                self.assertEqual(run(p, env_axioms=FIXTURE)[0].stdout, "")


class Protocol(unittest.TestCase):
    P = "搞个新工具，顺便考虑一下技术选型和验证"

    def test_schema_and_size(self):
        p, _ = run(self.P, env_axioms=FIXTURE)
        d = json.loads(p.stdout)
        self.assertEqual(list(d), ["hookSpecificOutput"])
        h = d["hookSpecificOutput"]
        self.assertEqual(h["hookEventName"], "UserPromptSubmit")
        self.assertLessEqual(len(h["additionalContext"]), MAX_CHARS)
        self.assertRegex(h["additionalContext"], r"[一-鿿]")

    def test_bad_input_exit0(self):
        for raw in ["", "not json", "[]", '{"prompt": 123}']:
            self.assertEqual(run(None, raw=raw)[0].returncode, 0)

    def test_fast(self):
        _, dt = run(self.P, env_axioms=FIXTURE)
        self.assertLess(dt, 0.2)

    def test_no_network_or_llm_imports(self):
        src = HOOK.read_text()
        for bad in ("socket", "urllib", "requests", "http.client", "anthropic", "subprocess"):
            self.assertNotIn("import " + bad, src)


if __name__ == "__main__":
    unittest.main()
