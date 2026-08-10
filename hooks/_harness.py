"""
_harness.py — shared runtime for all harness hooks.

Design constraints (see DESIGN.md):
  - Hooks sit on the critical path. Never call a model, never do network IO.
  - Never fail the turn. Every entry point wraps in try/except and exits 0.
  - Silent no-op when the cwd has no .harness/ and no global config.

Config resolution order (later overrides earlier):
  1. built-in DEFAULTS
  2. ~/.harness/config.yml            (global, applies in any workspace)
  3. <project_root>/.harness/project.yml
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
GLOBAL_DIR = HOME / ".harness"
GLOBAL_CONFIG = GLOBAL_DIR / "config.yml"
RUN_DIR = GLOBAL_DIR / "run"

DEFAULTS = {
    "name": None,
    "description": None,
    "state": {"file": ".harness/STATE.md", "max_chars": 8000},
    "context_files": [],
    "journal": {"dir": ".harness/journal", "max_message_chars": 4000, "tail": 5},
    "archive": {"dir": ".harness/archive"},
    "autonomy": {
        "auto_allow_inside": True,
        "allow_prefixes": [],
        "deny_patterns": [],
        "write_roots": [],
        "k8s": {"allow_contexts": [], "deny_contexts": []},
    },
    "explore": {"default_agents": 3, "max_agents": 5},
    "output": {"contract": "structured", "nudge_once": True, "auto_continue": True},
    "memory": {"enabled": True, "include_ancestors": True, "max_chars": 2500},
}

PROJECTS_DIR = HOME / ".claude" / "projects"


# ---------------------------------------------------------------- yaml loading

def _scalar(raw):
    raw = raw.strip()
    if raw == "":
        return None
    if len(raw) >= 2 and raw[0] in "\"'" and raw[-1] == raw[0]:
        return raw[1:-1]
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        return [_scalar(p) for p in inner.split(",")] if inner else []
    low = raw.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "~", "none"):
        return None
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    if re.fullmatch(r"-?\d+\.\d+", raw):
        return float(raw)
    return raw


def _tokenize(text):
    out = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        line = raw
        m = re.search(r"\s+#", line)
        if m and line.count('"') % 2 == 0 and line.count("'") % 2 == 0:
            line = line[: m.start()]
        if not line.strip():
            continue
        out.append((len(line) - len(line.lstrip()), line.strip()))
    return out


def _parse_block(lines, i, indent):
    if i < len(lines) and lines[i][1].startswith("- "):
        items = []
        while i < len(lines) and lines[i][0] == indent and lines[i][1].startswith("- "):
            body = lines[i][1][2:].strip()
            i += 1
            if body == "" and i < len(lines) and lines[i][0] > indent:
                sub, i = _parse_block(lines, i, lines[i][0])
                items.append(sub)
            else:
                items.append(_scalar(body))
        return items, i

    node = {}
    while i < len(lines) and lines[i][0] == indent:
        content = lines[i][1]
        if content.startswith("- "):
            break
        key, sep, rest = content.partition(":")
        if not sep:
            i += 1
            continue
        key, rest = key.strip(), rest.strip()
        i += 1
        if rest:
            node[key] = _scalar(rest)
        elif i < len(lines) and lines[i][0] > indent:
            node[key], i = _parse_block(lines, i, lines[i][0])
        elif i < len(lines) and lines[i][0] == indent and lines[i][1].startswith("- "):
            node[key], i = _parse_block(lines, i, indent)
        else:
            node[key] = None
    return node, i


def _mini_yaml(text):
    lines = _tokenize(text)
    if not lines:
        return {}
    value, _ = _parse_block(lines, 0, lines[0][0])
    return value if isinstance(value, dict) else {}


def _load_yaml(text):
    """Parse YAML. Falls back to a minimal indent parser covering the harness
    schema subset so hooks keep working under a python3 without PyYAML."""
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(text)
        return data if isinstance(data, dict) else {}
    except ImportError:
        return _mini_yaml(text)
    except Exception:
        return _mini_yaml(text)


# ---------------------------------------------------------------- config

def _deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        elif v is not None:
            out[k] = v
    return out


def find_project_root(start):
    """Walk up from `start` looking for a project marker.

    The marker is `.harness/project.yml`, not merely a `.harness/` directory:
    ~/.harness is the global config dir, so a bare-directory test would make
    every path under $HOME resolve to $HOME as its project root.
    """
    try:
        p = Path(start).resolve()
    except Exception:
        return None
    for cand in [p, *p.parents]:
        if cand == GLOBAL_DIR.parent and (cand / ".harness") == GLOBAL_DIR:
            continue
        if (cand / ".harness" / "project.yml").is_file():
            return cand
    return None


def read_yaml_file(path):
    try:
        return _load_yaml(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_config(cwd):
    """Returns (config_dict, project_root_or_None)."""
    cfg = json.loads(json.dumps(DEFAULTS))
    if GLOBAL_CONFIG.is_file():
        cfg = _deep_merge(cfg, read_yaml_file(GLOBAL_CONFIG))
    root = find_project_root(cwd)
    if root:
        cfg = _deep_merge(cfg, read_yaml_file(root / ".harness" / "project.yml"))
    return cfg, root


# ---------------------------------------------------------------- hook plumbing

def read_event():
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def emit(payload=None):
    if payload:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.exit(0)


def emit_context(event_name, text, system_message=None):
    if not text:
        emit()
    payload = {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": text,
        }
    }
    if system_message:
        payload["systemMessage"] = system_message
    emit(payload)


def guard_once(session_id, tag):
    """Returns True the first time it is called for (session_id, tag)."""
    if not session_id:
        return False
    try:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        marker = RUN_DIR / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', session_id)}.{tag}"
        if marker.exists():
            return False
        marker.write_text("1", encoding="utf-8")
        return True
    except Exception:
        return False


def safe_main(fn):
    """Run a hook body; any failure degrades to a silent no-op."""
    try:
        fn()
    except SystemExit:
        raise
    except Exception:
        pass
    sys.exit(0)


# ---------------------------------------------------------------- helpers

def git_info(cwd):
    def run(args):
        try:
            return subprocess.run(
                args, cwd=cwd, capture_output=True, text=True, timeout=3
            ).stdout.strip()
        except Exception:
            return ""

    status = run(["git", "status", "--porcelain"])
    return {
        "branch": run(["git", "rev-parse", "--abbrev-ref", "HEAD"]) or None,
        "head": run(["git", "rev-parse", "--short", "HEAD"]) or None,
        "dirty": len([l for l in status.splitlines() if l.strip()]) if status else 0,
    }


def project_slug(path):
    """Claude Code keys its auto-memory by cwd: every non-alphanumeric character
    of the absolute path becomes a dash."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(Path(path)))


def sibling_memory_indexes(cwd, root, include_ancestors=True):
    """Claude Code loads the MEMORY.md of the *cwd's* project dir only. A repo
    entered from a subdirectory therefore cannot see the memories written while
    sitting at the repo root, and vice versa. Collect the indexes it will NOT
    load so SessionStart can surface them.

    Returns [(dir_path, index_text, memory_count)], nearest ancestor first.
    """
    if not PROJECTS_DIR.is_dir():
        return []
    try:
        here = Path(cwd).resolve()
    except Exception:
        return []
    loaded = project_slug(here)

    candidates = []
    if root is not None:
        try:
            candidates.append(Path(root).resolve())
        except Exception:
            pass
    if include_ancestors:
        stop = Path(root).resolve() if root is not None else None
        for parent in here.parents:
            candidates.append(parent)
            if stop is not None and parent == stop:
                break
            if parent == HOME or parent == Path("/"):
                break

    out, seen = [], set()
    for cand in candidates:
        slug = project_slug(cand)
        if slug == loaded or slug in seen:
            continue
        seen.add(slug)
        mdir = PROJECTS_DIR / slug / "memory"
        index = mdir / "MEMORY.md"
        if not index.is_file():
            continue
        try:
            text = index.read_text(encoding="utf-8")
        except Exception:
            continue
        count = len([p for p in mdir.glob("*.md") if p.name != "MEMORY.md"])
        out.append((mdir, text, count))
    return out


def message_text(msg):
    if isinstance(msg, str):
        return msg
    if isinstance(msg, dict):
        return msg.get("text") or ""
    return ""


def truncate(text, limit):
    if not text or len(text) <= limit:
        return text or ""
    return text[:limit] + f"\n…[truncated, {len(text) - limit} more chars]"


def load_skill_body(name):
    """Read a sibling skill's SKILL.md body with its YAML frontmatter stripped.

    Resolved relative to this file (not cwd), so it works identically whether
    called from the real path in this repo or through the ~/.claude/hooks
    symlink. Returns "" on any failure — callers must have a fallback, since
    hooks must never fail the turn over a missing/broken skill file.
    """
    path = Path(__file__).resolve().parent.parent / "skills" / name / "SKILL.md"
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return ""
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            text = text[end + 4:]
    return text.strip()
