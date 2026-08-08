#!/usr/bin/env python3
"""
SessionStart hook — the zero-friction read path.

Walks up from cwd for a .harness/ directory. If found, injects the compacted
project state, the journal tail, the autonomy envelope and the output contract
into the start of the session. If not found, injects only the global operating
contract, so the mechanism still applies in any workspace.

Never reads the WAL (journal) beyond its tail, so session-start context stays
bounded no matter how long the project runs.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import (  # noqa: E402
    emit_context, load_config, read_event, safe_main, sibling_memory_indexes,
    truncate,
)

CONTRACT = """## 作业契约（harness）

**输出结构化，段落之间用分隔线隔开。** 实质性回复用这个骨架，不写长篇散文：

```
========= 【结论】 ==========================
一到三句

========= 【证据】 ==========================
- 事实 | 出处（file:line 或命令）

========= 【问题】 ==========================
- 问题1：

========= 【下一步】 ========================
- xxx
```
分隔线用等号补齐到大致等宽，段名放在 `【】` 里。纯对话、简单确认、单点问答不受此约束。

**并行看合并成本。** 调查任务先拆正交维度，维度之间结果不重叠才并行派 subagent（默认 %(agents)d 个，上限 %(max_agents)d）。
同一个问题要多个意见时串行，不要并行，因为合并成本会落回人脑。

**报告只报差异和决策。** 过程写进文件，回复里只留结论、证据和待决项。

**删除走归档。** 任何清理动作用 `git mv` 进 archive 并写清单，不用 `rm`。"""

NO_PROJECT_HINT = """
这个目录没有 `.harness/` 配置，当前只加载了全局契约。
需要给它装上项目级状态与边界时运行 `/harness-onboard`。"""


def render_envelope(auto):
    allow = auto.get("allow_prefixes") or []
    deny = auto.get("deny_patterns") or []
    k8s = auto.get("k8s") or {}
    k_allow = k8s.get("allow_contexts") or []
    k_deny = k8s.get("deny_contexts") or []
    roots = auto.get("write_roots") or []
    if not any([allow, deny, k_allow, k_deny, roots]):
        return ""

    lines = ["## 自主边界（envelope）", ""]
    if allow:
        lines.append("直接执行，不要停下来问：")
        lines += [f"- `{p}`" for p in allow]
        lines.append("")
    if k_allow:
        lines.append(f"允许操作的 k8s context：{', '.join(str(c) for c in k_allow)}")
    if k_deny:
        lines.append(f"**禁止**触碰的 k8s context：{', '.join(str(c) for c in k_deny)}")
    if k_allow or k_deny:
        lines.append("")
    if deny:
        lines.append("硬禁止（hook 会拦截）：")
        lines += [f"- `{p}`" for p in deny]
        lines.append("")
    if roots:
        lines.append(f"写操作限定在：{', '.join(str(r) for r in roots)}")
        lines.append("")
    lines.append(
        "边界内的动作直接做完，不要逐步征求同意。边界外的动作停下来，"
        "回复第一行写 `【需要决策】` 并说明选项与取舍。"
    )
    return "\n".join(lines)


def journal_tail(root, cfg):
    jdir = root / (cfg.get("journal", {}).get("dir") or ".harness/journal")
    n = int(cfg.get("journal", {}).get("tail") or 5)
    if not jdir.is_dir():
        return ""
    files = sorted([p for p in jdir.glob("*.jsonl") if p.is_file()])
    records = []
    for path in reversed(files):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except Exception:
                continue
            if len(records) >= n:
                break
        if len(records) >= n:
            break
    if not records:
        return ""

    out = ["## 最近的 session 记录（WAL 尾部，非权威口径）", ""]
    for rec in records:
        ts = (rec.get("ts") or "")[:16].replace("T", " ")
        git = rec.get("git") or {}
        ref = f"{git.get('branch') or '?'}@{git.get('head') or '?'}"
        head_line = (rec.get("text") or "").strip().splitlines()
        summary = head_line[0][:110] if head_line else ""
        out.append(f"- {ts} [{ref}] {summary}")
    out.append("")
    out.append("完整时间线在 journal 里，需要判断哪些结论已过期时运行 `/harness-compact`。")
    return "\n".join(out)


def render_memory(cwd, root, cfg):
    """Claude Code auto-loads only the MEMORY.md keyed to the exact cwd. Surface
    the sibling indexes it cannot see, and state the precedence."""
    mem = cfg.get("memory") or {}
    if not mem.get("enabled", True):
        return ""
    others = sibling_memory_indexes(
        cwd, root, include_ancestors=mem.get("include_ancestors", True)
    )
    if not others:
        return ""

    lines = ["## 跨 session 记忆", ""]
    lines.append(
        "自动记忆按 cwd 分目录存放，本轮只加载了当前 cwd 那一份。"
        "下面是同一个项目下**没有被加载**的记忆索引，按需读文件正文："
    )
    lines.append("")
    for mdir, text, count in others:
        lines.append(f"### `{mdir}`（{count} 条）")
        lines.append("")
        body = "\n".join(
            l for l in text.splitlines()
            if l.strip().startswith("-") or l.strip().startswith("#")
        )
        lines.append(truncate(body.strip(), int(mem.get("max_chars") or 2500)))
        lines.append("")
    lines.append(
        "**权威顺序**：`STATE.md` > journal > 自动记忆。"
        "STATE.md 是带出处、经过收敛的当前状态；自动记忆记的是写下那一刻的事实，"
        "可能已经过期，引用前先核实文件、函数、参数是否还在。冲突时以 STATE.md 为准，"
        "并把冲突记进开放问题，等 `/harness-compact` 裁决。"
    )
    return "\n".join(lines)


def build(cfg, root, cwd="."):
    contract = CONTRACT % {
        "agents": int((cfg.get("explore") or {}).get("default_agents") or 3),
        "max_agents": int((cfg.get("explore") or {}).get("max_agents") or 5),
    }
    if root is None:
        mem = render_memory(cwd, root, cfg)
        return "\n\n".join(x for x in [mem, contract, NO_PROJECT_HINT.strip()] if x)

    name = cfg.get("name") or root.name
    parts = [f"# HARNESS · {name}", ""]
    if cfg.get("description"):
        parts += [str(cfg["description"]), ""]

    state_cfg = cfg.get("state") or {}
    state_path = root / (state_cfg.get("file") or ".harness/STATE.md")
    if state_path.is_file():
        try:
            body = state_path.read_text(encoding="utf-8").strip()
        except Exception:
            body = ""
        if body:
            rel = state_path.relative_to(root)
            parts += [
                f"## 当前状态（唯一事实来源：`{rel}`）",
                "",
                truncate(body, int(state_cfg.get("max_chars") or 8000)),
                "",
            ]
    else:
        parts += [
            "## 当前状态",
            "",
            f"`{state_cfg.get('file')}` 还不存在。第一次收敛运行 `/harness-compact`。",
            "",
        ]

    ctx_files = cfg.get("context_files") or []
    if ctx_files:
        parts += ["## 权威入口（按需读，不要全量加载）", ""]
        parts += [f"- `{f}`" for f in ctx_files]
        parts.append("")

    tail = journal_tail(root, cfg)
    if tail:
        parts += [tail, ""]

    mem = render_memory(cwd, root, cfg)
    if mem:
        parts += [mem, ""]

    env = render_envelope(cfg.get("autonomy") or {})
    if env:
        parts += [env, ""]

    parts.append(contract)
    return "\n".join(parts)


def main():
    event = read_event()
    cwd = event.get("cwd") or "."
    cfg, root = load_config(cwd)
    text = build(cfg, root, cwd)
    emit_context("SessionStart", text)


if __name__ == "__main__":
    safe_main(main)
