"""只读解析 <workspace>/openspec/changes/*/ → Change 列表（契约见 CONTRACT.md「v3 看板契约」）。

不调 openspec CLI（启动慢），直接读 proposal.md / tasks.md。session_ids 由 board.py 填。
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

PROPOSAL_CHARS = 800
MAX_MTIME_FILES = 2000

_TASK_RE = re.compile(r"^(\s*)[-*+]\s+\[([ xX])\]\s+(.*\S)\s*$")
_GROUP_RE = re.compile(r"^##\s+(.*\S)\s*$")
_NUM_RE = re.compile(r"^(\d+[A-Za-z]?(?:\.\d+)*)[.)]?\s+(.*)$")   # 1.1 / 3b.2 都算编号
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def _inline(s: str) -> str:
    """去掉行内 markdown 的强调/代码/链接标记，保留文字。"""
    s = _LINK_RE.sub(r"\1", s)
    s = re.sub(r"(\*\*|__)(.+?)\1", r"\2", s)
    s = s.replace("`", "")
    return s.strip()


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def parse_proposal(text: str) -> tuple[str | None, str]:
    title, body, in_code = None, [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
        if title is None and not in_code and line.startswith("# "):
            title = _inline(line[2:])
            continue
        body.append(line)
    return title, "\n".join(body).strip()[:PROPOSAL_CHARS]


def parse_tasks(text: str) -> list[dict]:
    items, indents, group, in_code = [], [], None, False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        g = _GROUP_RE.match(line)
        if g:
            group = _inline(g.group(1))
            continue
        m = _TASK_RE.match(line)
        if not m:
            continue
        raw = m.group(3).strip()
        raw = re.sub(r"^(\*\*|__)(\d+[A-Za-z]?(?:\.\d+)*[.)]?)\1\s*", r"\2 ", raw)  # **1.1** text
        n = _NUM_RE.match(raw)
        if n:
            iid, text_ = n.group(1), n.group(2)
        else:
            iid, text_ = str(len(items) + 1), raw
        items.append({"id": iid, "text": _inline(text_), "done": m.group(2) != " ",
                      "current": False, "group": group, "level": 0})
        indents.append(_indent_width(m.group(1)))
    _assign_levels(items, indents)
    for i, it in enumerate(items):
        nxt = items[i + 1] if i + 1 < len(items) else None
        is_leaf = not (nxt and nxt["group"] == it["group"] and nxt["level"] > it["level"])
        if is_leaf and not it["done"]:
            it["current"] = True    # 第一个未完成的叶子
            break
    return items


def _indent_width(ws: str) -> int:
    return len(ws.replace("\t", "    "))


def _assign_levels(items: list[dict], indents: list[int]) -> None:
    """按缩进栈算层级：最浅缩进为 0 级，每多一个更深的缩进档 +1（制表符/2/4 空格混用容错）。"""
    if not items:
        return
    stack = [min(indents)]
    for it, ind in zip(items, indents):
        while stack and stack[-1] > ind:
            stack.pop()
        if not stack or stack[-1] < ind:
            stack.append(ind)
        it["level"] = len(stack) - 1


def _latest_mtime(d: Path) -> float:
    best, n = 0.0, 0
    try:
        best = d.stat().st_mtime
    except OSError:
        return 0.0
    for dirpath, _dirs, files in os.walk(d):
        for f in files:
            n += 1
            if n > MAX_MTIME_FILES:
                return best
            try:
                best = max(best, os.stat(os.path.join(dirpath, f)).st_mtime)
            except OSError:
                pass
    return best


def _iso(ts: float) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_scope(ws: Path, cid: str) -> dict | None:
    f = ws / ".harness" / "scope" / f"{cid}.yml"
    if not f.is_file():
        return None
    try:
        d = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        review = d.get("review")
        rs = review.get("status") if isinstance(review, dict) else None
        st = d.get("status")
        return {"status": str(st) if st is not None else None,
                "review": str(rs) if rs is not None else None}
    except Exception:
        return {"status": None, "review": None}


def scan(workspace_path) -> list[dict]:
    ws = Path(workspace_path)
    base = ws / "openspec" / "changes"
    try:
        dirs = sorted(p for p in base.iterdir() if p.is_dir() and p.name != "archive" and not p.name.startswith("."))
    except OSError:
        return []
    out = []
    for d in dirs:
        title, proposal = parse_proposal(_read(d / "proposal.md"))
        items = parse_tasks(_read(d / "tasks.md"))
        out.append({
            "id": d.name,
            "title": title or d.name,
            "path": f"openspec/changes/{d.name}",
            "progress": {"done": sum(1 for i in items if i["done"]), "total": len(items)},
            "items": items,
            "proposal": proposal,
            "updated_at": _iso(_latest_mtime(d)),
            "session_ids": [],
            "scope": load_scope(ws, d.name),
        })
    out.sort(key=lambda c: c["updated_at"] or "", reverse=True)
    return out
