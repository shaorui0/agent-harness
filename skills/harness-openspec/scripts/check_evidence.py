#!/usr/bin/env python3
"""Evidence gate for an OpenSpec change (harness-openspec §5b/§5c).

Usage: check_evidence.py <slug> [--root DIR]      (also accepts a change dir path)
Checks, for openspec/changes/<slug>/tasks.md:
  - every `- [x]` line carries `evidence: <path>` (path relative to the change dir)
  - each evidence file exists, is non-empty, and has `# cmd:` + `# at:` headers (.log)
  - evidence/INDEX.md exists when any task is checked
  - reports remaining `- [ ]` count (DoD = 0)
Exit 0 = all checked tasks evidenced AND no unchecked; 1 = evidence problem; 2 = unchecked remain only.
"""
import argparse, re, sys
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("slug")
ap.add_argument("--root", default=".")
a = ap.parse_args()

p = Path(a.slug)
change = p if (p / "tasks.md").is_file() else Path(a.root) / "openspec" / "changes" / a.slug
tasks = change / "tasks.md"
if not tasks.is_file():
    sys.exit(f"tasks.md not found: {tasks}")

done = todo = 0
errs = []
for n, line in enumerate(tasks.read_text().splitlines(), 1):
    m = re.match(r"^\s*[-*]\s+\[( |x|X)\]\s*(.*)", line)
    if not m:
        continue
    if m.group(1) == " ":
        todo += 1
        continue
    done += 1
    refs = re.findall(r"evidence:\s*([^\s|,;]+)", m.group(2))
    if not refs:
        errs.append(f"L{n}: [x] without `evidence:` -> {m.group(2)[:60]}")
        continue
    for r in refs:
        f = change / r
        if not f.is_file() or f.stat().st_size == 0:
            errs.append(f"L{n}: evidence missing/empty: {r}")
        elif f.suffix == ".log":
            head = f.read_text(errors="replace")[:600]
            if "# cmd:" not in head or "# at:" not in head:
                errs.append(f"L{n}: {r} lacks `# cmd:`/`# at:` header")
if done and not (change / "evidence" / "INDEX.md").is_file():
    errs.append("evidence/INDEX.md missing")

print(f"change={change.name} checked={done} unchecked={todo} problems={len(errs)}")
for e in errs:
    print("  FAIL", e)
sys.exit(1 if errs else (2 if todo else 0))
