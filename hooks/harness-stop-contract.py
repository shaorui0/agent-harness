#!/usr/bin/env python3
"""
Stop hook — continue guard + output contract, both one-shot per session.

Continue guard (不在不合适的地方停下来):
  If the turn ends by asking for permission to keep going, and the reply does
  not declare `【需要决策】`, push it back once with the envelope restated. A
  session is nudged at most once, so this can never loop.

Output contract:
  If a substantive reply came back as long-form prose with none of the required
  section markers, attach a one-shot reminder. Non-blocking.

Both behaviours are per-session-guarded via ~/.harness/run/ markers.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import (  # noqa: E402
    emit, guard_once, load_config, message_text, read_event, safe_main,
)

DECISION_MARK = "【需要决策】"
SECTION_MARKS = ("【结论】", "【问题】", "【下一步】", "【证据】")

ASKS_PERMISSION = [
    r"要我继续",
    r"需要我继续",
    r"是否需要我",
    r"要不要我",
    r"你确认(一下)?(后|再)?我",
    r"确认(无误|后)我(就|再)",
    r"我可以.{0,12}你确认",
    r"告诉我.{0,10}我(就|再|来)",
    r"(shall|should) I (proceed|continue|go ahead)",
    r"(do you )?want me to (continue|proceed|go ahead)",
    r"let me know (if|whether) (you|i)",
]

CONTINUE_REASON = """harness continue guard：这一轮停在了征求同意上。

规则：项目 envelope 内的动作直接做完，不要逐步确认。envelope 见 session 开头注入的自主边界。
如果停下来的原因是真实歧义或 envelope 外的动作，重发一次回复，第一行写 `【需要决策】`，
然后给出选项、各自的代价、以及你的推荐。除此之外，请继续把活干完。

这条提醒每个 session 只出现一次。"""

CONTRACT_REMINDER = """harness output contract：刚才的回复是长篇散文。

用 skill `harness-output-format` 定义的骨架重写后续实质性回复：【结论】/【证据】/【问题】/【下一步】。
过程细节写进文件，回复里只留结论、证据出处和待决项。这条提醒每个 session 只出现一次
（每轮开头的 harness-prompt-contract 提醒是这条规则的常驻版本，这里是失效时的兜底）。"""


def asks_permission(text):
    return any(re.search(p, text, re.IGNORECASE) for p in ASKS_PERMISSION)


def main():
    event = read_event()
    if event.get("hook_event_name") not in (None, "Stop"):
        emit()

    text = message_text(event.get("last_assistant_message")).strip()
    if len(text) < 40:
        emit()

    cwd = event.get("cwd") or "."
    cfg, _root = load_config(cwd)
    out = cfg.get("output") or {}
    session = event.get("session_id")

    if out.get("auto_continue", True) and DECISION_MARK not in text and asks_permission(text):
        if guard_once(session, "continue"):
            emit({"decision": "block", "reason": CONTINUE_REASON})

    if out.get("contract") == "structured" and out.get("nudge_once", True):
        if len(text) > 700 and not any(m in text for m in SECTION_MARKS):
            if guard_once(session, "contract"):
                emit({
                    "hookSpecificOutput": {
                        "hookEventName": "Stop",
                        "additionalContext": CONTRACT_REMINDER,
                    },
                    "suppressOutput": True,
                })
    emit()


if __name__ == "__main__":
    safe_main(main)
