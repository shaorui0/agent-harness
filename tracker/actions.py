"""actions.py — 把整个 session 的 transcript 压成「动作索引」，供一致性核对使用。

与 session_recap 的骨架不同：不省略中间回合。每个回合一个标题行 + 改变状态的动作（一行一条）
+ 该回合最后一条 assistant 文字的结论。只读命令一律不收。超预算时先保标题和结论，
再按「靠近开头和结尾优先」裁剪动作行。纯 stdlib；解析判定复用 session_recap。
"""
from __future__ import annotations

import re
from pathlib import Path

import session_recap as sr

TITLE_PROMPT_CHARS = 120
BASH_CHARS = 140
AGENT_DESC_CHARS = 100
CONCLUSION_CHARS = 300
RESULT_CHARS = 80

# ---------------------------------------------------------------- 脱敏

_REDACT = "<redacted>"
_SECRET_PATTERNS = [
    (re.compile(r"eyJ[A-Za-z0-9_\-]{10,}(?:\.[A-Za-z0-9_\-]*)*"), _REDACT),
    (re.compile(r"AKIA[0-9A-Z]{12,}"), _REDACT),
    (re.compile(r"(?i)\b((?:aws_)?(?:secret_access_key|session_token)|password|passwd|token|secret|api[_-]?key)"
                r"(\s*[=:]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s\"';&|]+)"), r"\1\2" + _REDACT),
    (re.compile(r"(?i)(--(?:password|token|secret)[ =])(?:\"[^\"]*\"|'[^']*'|[^\s\"';&|]+)"), r"\1" + _REDACT),
]


def redact(text: str) -> str:
    for pat, repl in _SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return text


# ---------------------------------------------------------------- 只读命令判定

_READONLY_CMDS = {
    "ls", "cat", "grep", "egrep", "fgrep", "rg", "head", "tail", "wc", "pwd", "cd", "echo", "printf", "find",
    "which", "type", "sort", "uniq", "diff", "stat", "file", "jq", "date", "tree", "du", "df", "env", "true",
    "false", "test", "[", "awk", "cut", "tr", "basename", "dirname", "realpath", "readlink", "whoami", "id",
    "sleep", "less", "more", "ps", "lsof", "column", "nl", "seq", "hostname", "uname", "open", "export",
    "source", ".", "type", "alias", "set", "pgrep", "unset", "wait", "read", "for", "case", "select", "done", "fi",
    "esac", "}", ")", "nslookup", "dig", "ping", "nc", "timeout", "tput", "md5", "md5sum", "shasum", "wc",
}
_LEAD_WORDS = {"sudo", "time", "nohup", "do", "then", "else", "elif", "if", "while", "until", "!", "{", "(", "exec"}
_GIT_READ = {"status", "log", "diff", "show", "rev-parse", "ls-files", "blame", "describe",
             "shortlog", "rev-list", "ls-tree", "cat-file", "grep"}
_KUBECTL_READ = {"get", "describe", "logs", "top", "config", "version", "explain", "api-resources", "cluster-info",
                 "auth", "events", "diff", "wait"}
_HELM_READ = {"list", "ls", "status", "get", "history", "show", "search", "repo", "template", "lint", "version"}
_AWS_READ = ("describe", "list", "get", "head", "ls", "sts", "configure", "help")
_NOISE_REDIRECT = re.compile(r"(?:\d*|&)>\s*/dev/null|\d>&\d|>&\d")
_QUOTED = re.compile(r'"(?:\\.|[^"\\])*"|\'[^\']*\'')
_HEREDOC = re.compile(r"<<-?\s*['\"]?\w+['\"]?")
_SPLIT = re.compile(r"\|\||&&|;|\||\n")


def _segment_readonly(seg: str) -> bool:
    words = seg.strip().split()
    while words and (re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=\S*", words[0]) or words[0] in _LEAD_WORDS):
        words = words[1:]
    if not words or words[0].startswith("#"):
        return True
    cmd = Path(words[0]).name
    rest = words[1:]
    if cmd == "git":
        args = [w for w in rest if not w.startswith("-")]
        sub = args[0] if args else ""
        if sub in ("branch", "tag", "remote", "config"):   # 带额外参数才是写
            return len(args) == 1 or (sub == "config" and any(w in ("--get", "--list", "-l") for w in rest)) \
                or (sub == "remote" and args[1:2] == ["-v"])
        return sub in _GIT_READ
    if cmd in ("kubectl",) or re.fullmatch(r"k[a-z]{3,}(?:dev|prod|qa)\w*", cmd):
        sub = next((w for w in rest if not w.startswith("-")), "")
        return sub in _KUBECTL_READ
    if cmd == "helm":
        sub = next((w for w in rest if not w.startswith("-")), "")
        return sub in _HELM_READ
    if cmd == "aws":
        subs = [w for w in rest if not w.startswith("-")]
        return len(subs) >= 2 and subs[1].startswith(_AWS_READ) or (subs[:1] == ["sts"])
    if cmd == "sed":
        return "-i" not in rest and not any(w.startswith("-i") for w in rest)
    if cmd == "find":
        return not any(w in ("-delete", "-exec", "-execdir", "-ok") for w in rest)
    if cmd == "curl":
        return not any(w in ("-X", "--request", "-d", "--data", "--data-raw", "-F", "-T", "--upload-file", "-o", "-O")
                       or w.startswith("--data") for w in rest)
    return cmd in _READONLY_CMDS


def is_readonly_bash(cmd: str) -> bool:
    body = _QUOTED.sub('""', _HEREDOC.split(cmd)[0])   # 引号里的 > ; | 不算
    if re.search(r">", _NOISE_REDIRECT.sub("", body)):
        return False
    return all(_segment_readonly(s) for s in _SPLIT.split(body))


# ---------------------------------------------------------------- tool_result 里的证明信息

_RESULT_PATTERNS = [
    re.compile(r"\b\d+ (?:passed|failed|errors?)\b(?:, \d+ \w+)*"),
    re.compile(r"\bRan \d+ tests?\b"),
    re.compile(r"^(?:OK|FAILED)\b[^\n]{0,30}$", re.M),
    re.compile(r"(?i)\bexit(?:ed with)? code:? ?-?\d+"),
    re.compile(r"\bHTTP/[\d.]+ \d{3}\b"),
    re.compile(r"(?i)\b(?:http[ _])?status(?:[_ ]code)?[\":= ]+\d{3}\b"),
]


def result_hint(body: str, is_error: bool = False) -> str:
    found: list[str] = []
    for pat in _RESULT_PATTERNS:
        for m in pat.finditer(body or ""):
            s = " ".join(m.group(0).split())
            if s not in found:
                found.append(s)
    if is_error and not any("exit" in f.lower() for f in found):
        found.insert(0, "error")
    return redact(", ".join(found))[:RESULT_CHARS]


# ---------------------------------------------------------------- 文本清理

def _oneline(text: str, n: int) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:n]


def clean_conclusion(text: str, n: int = CONCLUSION_CHARS) -> str:
    lines = []
    for ln in (text or "").splitlines():
        if re.fullmatch(r"\s*(?:[-*_=]{3,}|\|?[-: |]{3,}\|?)\s*", ln):
            continue
        ln = re.sub(r"^\s*#+\s*", "", ln).replace("**", "")
        ln = re.sub(r"(?:^|\s)[=\-]{4,}(?:\s|$)", " ", ln)
        if ln.strip():
            lines.append(ln.strip())
    return _oneline(redact(" ".join(lines)), n)


def _text_of(entry: dict) -> str:
    return "\n".join(b.get("text", "") for b in sr.blocks_of(entry) if b.get("type") == "text").strip()


# ---------------------------------------------------------------- 解析

_PREFIX = re.compile(r"^\s*(?:cd\s+\S+|source\s+\S+(?:\s*>\s*/dev/null)?(?:\s*2>&1)?)\s*(?:&&|;|\n)\s*")


def _strip_prefix(cmd: str) -> str:
    """去掉开头的 cd <dir> && / source <file>; 之类，把 140 字留给真正的命令。"""
    while True:
        m = _PREFIX.match(cmd)
        if not m or not cmd[m.end():].strip():
            return cmd
        cmd = cmd[m.end():]


def _action_of(block: dict, cwd: str | None) -> str | None:
    name = block.get("name", "")
    inp = block.get("input") or {}
    if name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        path = inp.get("file_path") or inp.get("notebook_path") or "?"
        return f"{'Edit' if name == 'MultiEdit' else name} {sr.display_path(path, cwd)}"
    if name == "Bash":
        cmd = inp.get("command", "")
        if is_readonly_bash(cmd):
            return None
        return "Bash: " + _oneline(_strip_prefix(redact(cmd)), BASH_CHARS)
    if name in ("Agent", "Task"):
        desc = inp.get("description") or inp.get("prompt") or ""
        return f"Agent[{inp.get('subagent_type', '?')}]: " + _oneline(redact(desc), AGENT_DESC_CHARS)
    if name == "SendMessage":
        summ = inp.get("summary") or (inp.get("message") if isinstance(inp.get("message"), str) else "") or ""
        return f"SendMessage → {inp.get('to', '?')}: " + _oneline(redact(summ), AGENT_DESC_CHARS)
    return None


def parse_turns(transcript_path: str) -> list[dict]:
    """[{n, ts, prompt, actions: [{text, count, hint}], conclusion}]"""
    turns: list[dict] = []
    cur = None
    cwd = None
    by_id: dict[str, dict] = {}
    for entry in sr.read_entries(Path(transcript_path)):
        if entry.get("cwd") and not cwd:
            cwd = entry["cwd"]
        if entry.get("isSidechain"):
            continue
        etype = entry.get("type")
        if etype == "user":
            for b in sr.blocks_of(entry):
                if b.get("type") != "tool_result":
                    continue
                act = by_id.get(b.get("tool_use_id"))
                if act is None:
                    continue
                body = b.get("content")
                if isinstance(body, list):
                    body = " ".join(x.get("text", "") for x in body if isinstance(x, dict))
                hint = result_hint(str(body or ""), bool(b.get("is_error")))
                if hint:
                    act["hint"] = hint
            if sr.is_human_prompt(entry):
                raw = " ".join(b.get("text", "") for b in sr.blocks_of(entry) if b.get("type") == "text")
                cur = {"n": len(turns) + 1, "ts": sr.ts_of(entry),
                       "prompt": _oneline(redact(sr.strip_reminders(raw)), TITLE_PROMPT_CHARS),
                       "actions": [], "conclusion": ""}
                turns.append(cur)
        elif etype == "assistant" and cur is not None:
            txt = _text_of(entry)
            if txt:
                cur["conclusion_raw"] = txt
            for b in sr.blocks_of(entry):
                if b.get("type") != "tool_use":
                    continue
                text = _action_of(b, cwd)
                if text is None:
                    continue
                acts = cur["actions"]
                if acts and acts[-1]["text"] == text:
                    acts[-1]["count"] += 1
                    by_id[b.get("id")] = acts[-1]
                else:
                    act = {"text": text, "count": 1, "hint": ""}
                    acts.append(act)
                    by_id[b.get("id")] = act
    for t in turns:
        t["conclusion_raw"] = t.get("conclusion_raw", "")
    return turns


# ---------------------------------------------------------------- 渲染 + 预算

def _title(t: dict) -> str:
    ts = t["ts"].strftime("%m-%d %H:%M") if t["ts"] else "??-?? ??:??"
    return f"T{t['n']} {ts} 人类：{t['prompt']}"


def _action_lines(a: dict) -> list[str]:
    lines = [a["text"] + (f" ×{a['count']}" if a["count"] > 1 else "")]
    if a["hint"]:
        lines.append(f"  ↳ {a['hint']}")
    return lines


def _render(turns: list[dict], keep: set, concl_n: int) -> tuple[str, int]:
    out: list[str] = []
    omitted_total = 0
    for ti, t in enumerate(turns):
        out.append(_title(t))
        gap = 0
        for ai, a in enumerate(t["actions"]):
            if (ti, ai) in keep:
                if gap:
                    out.append(f"… 省略 {gap} 条动作")
                    gap = 0
                out.extend(_action_lines(a))
            else:
                gap += 1
                omitted_total += 1
        if gap:
            out.append(f"… 省略 {gap} 条动作")
        c = clean_conclusion(t["conclusion_raw"], concl_n)
        if c:
            out.append("→ " + c)
    return "\n".join(out), omitted_total


_STRONG = re.compile(r"^(?:Write|Edit|NotebookEdit|Agent|SendMessage)\b|^Bash: .*\b(?:git (?:commit|push|add|merge|rebase|tag)|"
                     r"sed -i|tee|rm|mv|cp|kubectl \w*\s*(?:apply|delete|patch|scale|create|rollout)|helm|aws)\b|^Bash: python3? - ?<<")


def _strong(text: str) -> bool:
    """改动类动作（文件写入、subagent、提交、部署类命令）预算紧时优先保留。"""
    return bool(_STRONG.search(text))


def build_index(transcript_path: str, max_chars: int = 60000) -> str:
    turns = parse_turns(transcript_path)
    if not turns:
        return "（session 里没有人类回合）"
    order = [(ti, ai) for ti, t in enumerate(turns) for ai in range(len(t["actions"]))]
    cost = {k: sum(len(l) + 1 for l in _action_lines(turns[k[0]]["actions"][k[1]])) for k in order}
    for concl_n in (CONCLUSION_CHARS, 120, 60, 0):
        keep_all = set(order)
        full, _ = _render(turns, keep_all, concl_n)
        if len(full) <= max_chars:
            return full
        base, _ = _render(turns, set(), concl_n)   # 只有标题 + 结论 + 省略行
        budget = max_chars - len(base)
        if budget <= 0 and concl_n:
            continue
        # 靠近开头/结尾优先
        ranked = sorted(range(len(order)), key=lambda i: (
            0 if _strong(turns[order[i][0]]["actions"][order[i][1]]["text"]) else 1,
            min(i, len(order) - 1 - i), i))
        for _attempt in range(8):
            keep, used = set(), 0
            for i in ranked:
                c = cost[order[i]]
                if used + c > budget:
                    continue
                keep.add(order[i])
                used += c
            text, _ = _render(turns, keep, concl_n)
            if len(text) <= max_chars:
                return text
            budget -= len(text) - max_chars + 20   # 省略行占的字符，收紧后重来
            if budget <= 0:
                break
    text, _ = _render(turns, set(), 0)
    return text


if __name__ == "__main__":  # 手动查看：python3 actions.py <transcript.jsonl>
    import sys
    print(build_index(sys.argv[1]))
