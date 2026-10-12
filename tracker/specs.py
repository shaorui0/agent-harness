#!/usr/bin/env python3
"""扫描所有 workspace 的 .harness/scope/*.yml → tracker 的只读快照（~/.harness/tracker/snapshot.js）。

只读：不改任何 scope，不调 LLM。drift 完全由 git 算出（确定性），不信任 agent 自述。

用法：
    python3 specs.py              # 按 ~/.harness/config.yml 的 tracker.roots 扫，写快照
    python3 specs.py --validate   # 只做格式校验，有问题时退出码非零（harness doctor 用）
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import config as tracker_config

try:
    import yaml  # PyYAML：scope 是人写的完整 YAML，_harness 的极简解析器覆盖不了
except ImportError:  # pragma: no cover
    yaml = None

SKIP_DIRS = {"node_modules", ".git", "archive", "_archive", "archives", "vendor", ".venv"}
SPEC_STATUSES = {"draft", "confirmed", "reconstructed"}
REVIEW_STATUSES = {"CLEARED", "NOT_CLEARED", "DONE_WITH_CONCERNS"}
REQUIRED = ("name", "goal", "acceptance_criteria", "covers")


def find_scope_dirs(root: Path) -> list[Path]:
    found = []
    for dirpath, dirnames, _ in os.walk(root):
        if ".harness" in dirnames and (Path(dirpath) / ".harness" / "scope").is_dir():
            found.append(Path(dirpath) / ".harness" / "scope")
        # 剪枝：.harness 自己、依赖目录、隐藏目录都不往下走（rglob 会先钻进去再过滤，慢一个数量级）
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
    return sorted(found)


def git(repo: Path, *args: str) -> str:
    try:
        r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def iso(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return (v if v.tzinfo else v.replace(tzinfo=timezone.utc)).isoformat()
    return str(v)


def cover_groups(workspace: Path, covers: list) -> list[tuple[Path, str | None, list[str]]]:
    """covers 两种写法：
    - 字符串：相对 workspace 的路径
    - {repo, ref, paths}：嵌套的独立仓库，可以锚定到远端 ref（本地 checkout 可能是旧的）
    """
    plain, groups = [], []
    for c in covers or []:
        if isinstance(c, str):
            plain.append(c)
        elif isinstance(c, dict) and c.get("paths"):
            groups.append((workspace / c.get("repo", "."), c.get("ref"), [str(p) for p in c["paths"]]))
    if plain:
        groups.insert(0, (workspace, None, plain))
    return groups


def drift(workspace: Path, covers: list, since: str | None) -> dict:
    """review 之后 covers 上又发生了多少 commit —— 有就说明上次的验收结论可能已经过期。"""
    if not covers or not since:
        return {"commits_after_review": None, "recent": [], "last_commit": None}
    lines, last = [], None
    for repo, ref, paths in cover_groups(workspace, covers):
        rev = [ref] if ref else []
        lines += [ln for ln in git(repo, "log", *rev, f"--since={since}", "--format=%h %cs %s", "--", *paths).splitlines() if ln]
        last = last or git(repo, "log", *rev, "-1", "--format=%h %cs %s", "--", *paths) or None
    return {"commits_after_review": len(lines), "recent": lines[:5], "last_commit": last}


def problems(d, path: Path) -> list[str]:
    """格式问题清单。空列表 = 合格。"""
    if not isinstance(d, dict):
        return ["顶层不是 mapping"]
    out = [f"缺字段 {k}" for k in REQUIRED if not d.get(k)]
    if d.get("name") and d["name"] != path.stem:
        out.append(f"name={d['name']} 与文件名 {path.stem} 不一致")
    if d.get("status") and d["status"] not in SPEC_STATUSES:
        out.append(f"status={d['status']} 不在 {sorted(SPEC_STATUSES)}")
    rs = (d.get("review") or {}).get("status") if isinstance(d.get("review"), dict) else None
    if rs and rs not in REVIEW_STATUSES:
        out.append(f"review.status={rs} 不在 {sorted(REVIEW_STATUSES)}")
    return out


def _ac_text(x) -> str:
    return str(x.get("text", x) if isinstance(x, dict) else x).strip()


def load_scope(path: Path, workspace: Path) -> dict:
    d = yaml.safe_load(path.read_text()) or {}
    review = d.get("review") or {}
    covers = d.get("covers") or []
    reviewed_at = iso(review.get("reviewed_at"))
    since = reviewed_at or iso(d.get("confirmed_at"))
    return {
        "name": d.get("name") or path.stem,
        "file": str(path),
        "repo": workspace.name,
        "repo_path": str(workspace),
        "spec_status": d.get("status"),
        "goal": (d.get("goal") or "").strip(),
        "non_goals": d.get("non_goals") or [],
        "acceptance_criteria": [_ac_text(x) for x in d.get("acceptance_criteria") or []],
        "ac_results": d.get("ac_results") or [],
        "covers": covers,
        "risk_budget": d.get("risk_budget"),
        "confirmed_at": iso(d.get("confirmed_at")),
        "origin": d.get("origin"),
        "review": {
            "status": review.get("status"),
            "rounds": review.get("rounds"),
            "reviewed_at": reviewed_at,
            "scores": review.get("scores") or {},
            "why": (review.get("why_not_cleared") or review.get("concerns") or review.get("summary") or ""),
        },
        "drift": drift(workspace, covers, since),
        "warnings": problems(d, path),
        "next_actions": d.get("next_actions") or review.get("next_actions") or [],
    }


def iter_scope_files(roots):
    for root in roots:
        for sdir in find_scope_dirs(Path(root).expanduser()):
            for f in sorted(sdir.glob("*.yml")):
                yield f, sdir.parent.parent


def build(roots) -> dict:
    scopes, errors = [], []
    for f, ws in iter_scope_files(roots):
        try:
            scopes.append(load_scope(f, ws))
        except Exception as e:  # 单个 scope 坏了不影响整体快照
            errors.append({"file": str(f), "error": str(e)[:200]})
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "roots": [str(r) for r in roots],
        "scopes": scopes,
        "errors": errors,
    }


def write(snap: dict) -> Path:
    out = tracker_config.SNAPSHOT
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text("window.SNAPSHOT = " + json.dumps(snap, ensure_ascii=False, indent=1, default=str) + ";\n")
    tmp.replace(out)
    return out


def validate(roots) -> list[tuple[str, str]]:
    """[(file, problem)]：YAML 解析失败或字段不合格。"""
    bad = []
    for f, _ in iter_scope_files(roots):
        try:
            d = yaml.safe_load(f.read_text())
        except Exception as e:
            bad.append((str(f), "YAML 解析失败：" + str(e).splitlines()[0][:160]))
            continue
        bad += [(str(f), p) for p in problems(d, f)]
    return bad


def start_background(interval_s: int) -> None:
    """server 用：后台定时重扫，请求路径上不跑 git。"""
    def loop():
        while True:
            try:
                write(build(tracker_config.load()["roots"]))
            except Exception as e:  # noqa: BLE001
                sys.stderr.write(f"specs scan failed: {e}\n")
            time.sleep(interval_s)
    threading.Thread(target=loop, name="specs-scan", daemon=True).start()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--validate", action="store_true")
    args = ap.parse_args()
    if yaml is None:
        print("需要 PyYAML：python3 -m pip install pyyaml", file=sys.stderr)
        return 2
    roots = tracker_config.load()["roots"]
    if args.validate:
        bad = validate(roots)
        for f, p in bad:
            print(f"{f}: {p}")
        return 1 if bad else 0
    snap = build(roots)
    out = write(snap)
    print(f"{len(snap['scopes'])} scopes, {len(snap['errors'])} errors -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
