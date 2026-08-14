#!/usr/bin/env python3
"""Pure-function tests for harness-envelope.py's scope gate + write_roots decision.

No test framework, no new dependency — run directly:
    python3 hooks/test_envelope.py

Covers the paths listed in docs/scope-and-spec-gate-design.md's Test Review,
plus the 2026-08-14 revert (protected_paths no longer falls back to
write_roots or the whole repo — see that doc's revert entry):
  - protected_paths explicitly configured + no confirmed scope -> deny
  - protected_paths explicitly configured + confirmed scope covering the path -> no restriction
  - protected_paths unconfigured, even with write_roots set -> no restriction (the fallback is gone)
  - confirmed scope's `covers` must actually include the target path, not just any protected path
  - path outside protected_paths -> unaffected
  - write_roots regression (deny outside / allow inside), unchanged by the refactor
plus two extra cases (draft-status scope, disabled gate) caught while writing this.
"""

import importlib.util
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


envelope = _load("harness_envelope", "harness-envelope.py")


def _project(tmp, scope_files=None, write_roots=None, protected_paths=None, enabled=True):
    root = Path(tmp)
    (root / ".harness" / "scope").mkdir(parents=True, exist_ok=True)
    for name, body in (scope_files or {}).items():
        (root / ".harness" / "scope" / f"{name}.yml").write_text(body, encoding="utf-8")
    cfg = {
        "autonomy": {"write_roots": write_roots or []},
        "scope_gate": {"enabled": enabled, "protected_paths": protected_paths or []},
    }
    return root, cfg


def test_no_confirmed_scope_denies():
    with tempfile.TemporaryDirectory() as tmp:
        root, cfg = _project(tmp, write_roots=["src"], protected_paths=["src"])
        (root / "src").mkdir()
        target = root / "src" / "app.py"
        result = envelope.scope_gate_decision(str(target), cfg, root)
        assert result is not None and result[0] == "deny", f"expected deny, got {result}"


def test_confirmed_scope_covering_path_allows():
    with tempfile.TemporaryDirectory() as tmp:
        scope_yml = "status: confirmed\ncovers:\n  - src\n"
        root, cfg = _project(tmp, scope_files={"my-scope": scope_yml}, write_roots=["src"], protected_paths=["src"])
        (root / "src").mkdir()
        target = root / "src" / "app.py"
        result = envelope.scope_gate_decision(str(target), cfg, root)
        assert result is None, f"expected None (no restriction), got {result}"


def test_draft_scope_does_not_satisfy_gate():
    with tempfile.TemporaryDirectory() as tmp:
        scope_yml = "status: draft\ncovers:\n  - src\n"
        root, cfg = _project(tmp, scope_files={"my-scope": scope_yml}, write_roots=["src"], protected_paths=["src"])
        (root / "src").mkdir()
        target = root / "src" / "app.py"
        result = envelope.scope_gate_decision(str(target), cfg, root)
        assert result is not None and result[0] == "deny", f"draft scope must not satisfy the gate, got {result}"


def test_unconfigured_protected_paths_never_restricts():
    """2026-08-14 revert: protected_paths no longer falls back to write_roots
    or the whole repo when left unset. A project with write_roots configured
    but no explicit protected_paths must write freely — no fallback deny."""
    with tempfile.TemporaryDirectory() as tmp:
        root, cfg = _project(tmp, write_roots=["src"])
        (root / "src").mkdir()
        target = root / "src" / "app.py"
        result = envelope.scope_gate_decision(str(target), cfg, root)
        assert result is None, f"unconfigured protected_paths must not restrict, got {result}"


def test_confirmed_scope_must_cover_the_target_not_just_any_protected_path():
    """A confirmed scope covering `docs` must not satisfy the gate for a
    write under `charts`, even though both are listed in protected_paths."""
    with tempfile.TemporaryDirectory() as tmp:
        scope_yml = "status: confirmed\ncovers:\n  - docs\n"
        root, cfg = _project(
            tmp, scope_files={"my-scope": scope_yml}, protected_paths=["docs", "charts"]
        )
        (root / "charts").mkdir()
        target = root / "charts" / "values.yaml"
        result = envelope.scope_gate_decision(str(target), cfg, root)
        assert result is not None and result[0] == "deny", f"expected deny (covers doesn't include charts), got {result}"


def test_path_outside_protected_paths_unaffected():
    with tempfile.TemporaryDirectory() as tmp:
        root, cfg = _project(tmp, write_roots=["src"], protected_paths=["src"])
        (root / "other").mkdir()
        target = root / "other" / "notes.md"
        result = envelope.scope_gate_decision(str(target), cfg, root)
        assert result is None, f"path outside protected_paths should be unaffected, got {result}"


def test_disabled_gate_never_restricts():
    with tempfile.TemporaryDirectory() as tmp:
        root, cfg = _project(tmp, write_roots=["src"], enabled=False)
        (root / "src").mkdir()
        target = root / "src" / "app.py"
        result = envelope.scope_gate_decision(str(target), cfg, root)
        assert result is None, f"disabled gate must never restrict, got {result}"


def test_write_roots_regression_deny_outside():
    with tempfile.TemporaryDirectory() as tmp:
        root, cfg = _project(tmp, write_roots=["src"])
        (root / "other").mkdir()
        target = root / "other" / "notes.md"
        result = envelope.write_roots_decision(str(target), cfg, root)
        assert result is not None and result[0] == "deny", f"expected deny outside write_roots, got {result}"


def test_write_roots_regression_allow_inside():
    with tempfile.TemporaryDirectory() as tmp:
        root, cfg = _project(tmp, write_roots=["src"])
        (root / "src").mkdir()
        target = root / "src" / "app.py"
        result = envelope.write_roots_decision(str(target), cfg, root)
        assert result is None, f"expected None (no restriction) inside write_roots, got {result}"


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main():
    failures = []
    for t in TESTS:
        try:
            t()
            print(f"ok   {t.__name__}")
        except AssertionError as e:
            failures.append(t.__name__)
            print(f"FAIL {t.__name__}: {e}")
    if failures:
        print(f"\n{len(failures)}/{len(TESTS)} failed")
        sys.exit(1)
    print(f"\n{len(TESTS)}/{len(TESTS)} passed")


if __name__ == "__main__":
    main()
