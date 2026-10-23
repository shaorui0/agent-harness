#!/usr/bin/env python3
"""
UserPromptSubmit hook — prompt 发出前的轻量认知对齐（不调模型，确定性规则）。

做两件事：
  1. 公理对齐：prompt 命中「主动检索触发」表里的关键词，注入公理 ID + 一行含义。
     只有配置了公理索引文件才启用；没配置就没有这一段。
  2. prompt 质量提示：构建类请求没写产出 / 验收 / 范围边界时，提示补这三件事。
短对话（go/ok/跑）、裸路径、裸 URL、斜杠命令一律静默。

公理索引位置解析顺序（先命中先用）：
  1. 环境变量 HARNESS_PREHOOK_AXIOMS
  2. <project>/.harness/project.yml 的 prehook.axioms_index（相对路径按项目根解析）
  3. ~/.harness/config.yml 的 prehook.axioms_index
  4. 都没有 → 只出通用 prompt 质量提示

索引格式见 docs/prompt-prehook.md。永远 exit 0，输出 <= MAX_CHARS。
"""

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import emit_context, load_config, read_event, safe_main  # noqa: E402

MAX_CHARS = 600
MIN_LEN = 8          # 短于此视为对话式确认，不注入
MAX_AXIOMS = 4
ENV_VAR = "HARNESS_PREHOOK_AXIOMS"

BUILD_RE = re.compile(r"(搞个|做个|写个|搭个|加个|实现|开发|搭建|设计一个|构建|build|implement|create|add a|写一个|做一个|搞一个|重构|refactor)", re.I)
GOAL_RE = re.compile(r"(验收|成功标准|完成标准|done when|DoD|通过.{0,6}测试|测试通过|输出[:：到成]|产出|交付|不要(动|碰|改)|不碰|只改|只动|范围|scope|禁止|必须|expected|acceptance)", re.I)
URL_RE = re.compile(r"^\s*(https?://\S+)\s*$")
PATH_RE = re.compile(r"^\s*[~./]?[\w.\-@/]*/[\w.\-@/]*\s*$|^\s*[~/][\w.\-@/]+\s*$")

HINTS = (
    "产出什么(文件/格式/位置)",
    "怎么验证做完(可跑的命令/判据)",
    "不碰什么(范围边界)",
)


def resolve_axioms_path(cwd):
    """按 env > project.yml > ~/.harness/config.yml 解析；没配返回 None。"""
    env = os.environ.get(ENV_VAR)
    if env:
        return Path(os.path.expanduser(env))
    cfg, root = load_config(cwd)  # 已按 global < project 合并，project 覆盖 global
    val = (cfg.get("prehook") or {}).get("axioms_index")
    if not val or not isinstance(val, str):
        return None
    p = Path(os.path.expanduser(val))
    if not p.is_absolute() and root:
        p = root / p
    return p


def load_axioms(path):
    """返回 (triggers[(keywords, ids)], meanings{id: 'title：含义'})。"""
    if not path:
        return [], {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception:
        return [], {}
    triggers, meanings = [], {}
    for line in text.splitlines():
        m = re.match(r"^- ((?:「[^」]+」)+)\s*→\s*(.+)$", line)
        if m:
            kws = re.findall(r"「([^」]+)」", m.group(1))
            ids = re.findall(r"\b([A-Z]\d{2})\b", m.group(2))
            if ids:
                triggers.append((kws, ids))
            continue
        m = re.match(r"^\|\s*([A-Z]\d{2})\s*\|\s*\[([^\]]+)\]\([^)]*\)\s*\|\s*(.+?)\s*\|\s*$", line)
        if m:
            meanings[m.group(1)] = f"{m.group(2)}：{m.group(3)}"
    return triggers, meanings


def is_noise(prompt):
    p = prompt.strip()
    if len(p) < MIN_LEN:
        return True
    if URL_RE.match(p) or PATH_RE.match(p):
        return True
    if p.startswith("/") and " " not in p:  # slash command
        return True
    return False


def match_axioms(prompt, triggers, meanings):
    low = prompt.lower()
    seen = []
    for kws, ids in triggers:
        if any(k.lower() in low for k in kws):
            for i in ids:
                if i in meanings and i not in seen:
                    seen.append(i)
    return seen[:MAX_AXIOMS]


def is_vague_build(prompt):
    return bool(BUILD_RE.search(prompt)) and not GOAL_RE.search(prompt)


def build_note(prompt, axioms_path=None):
    if is_noise(prompt):
        return ""
    triggers, meanings = load_axioms(axioms_path)
    ids = match_axioms(prompt, triggers, meanings)
    lines = []
    if ids:
        parts = []
        for i in ids:
            t = meanings[i]
            if len(t) > 36:
                t = t[:35] + "…"
            parts.append(f"{i} {t}")
        lines.append("【对齐·公理】" + "；".join(parts))
    if is_vague_build(prompt):
        lines.append("【对齐·prompt】构建类请求未写清：" + "、".join(HINTS)
                     + "。先自拟并说明成功标准，别默认；极简优先，不加没要的功能。")
    if not lines:
        return ""
    out = "\n".join(lines)
    if len(out) > MAX_CHARS:
        out = out[:MAX_CHARS - 1] + "…"
    return out


def main():
    event = read_event()
    prompt = event.get("prompt") if isinstance(event, dict) else None
    if not isinstance(prompt, str):
        sys.exit(0)
    if is_noise(prompt):  # 短 prompt 不读任何配置，最快返回
        sys.exit(0)
    cwd = event.get("cwd") or "."
    emit_context("UserPromptSubmit", build_note(prompt, resolve_axioms_path(cwd)))


if __name__ == "__main__":
    safe_main(main)
