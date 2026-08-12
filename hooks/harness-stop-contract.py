#!/usr/bin/env python3
"""
Stop hook — spec-review guard + continue guard + output contract.

Spec-review guard (收敛检查，优先于下面两个) 见
docs/scope-and-spec-gate-design.md 架构评审：spec 收敛是硬性验收规则，不该被
"这轮是不是在问 permission" 这种风格性判断挡住或延后，所以它跑在最前面，且不受
下面 `len(text) < 40` 的短文本豁免——一句"Done."一样要过这道检查。

Continue guard (不在不合适的地方停下来):
  If the turn ends by asking for permission to keep going, and the reply does
  not declare `【需要决策】`, push it back once with the envelope restated. A
  session is nudged at most once, so this can never loop.

Output contract:
  If a substantive reply came back as long-form prose without the divider
  template (not just the bare section names — drift toward plain headers with
  no `=====【label】=====` still counts), attach a reminder. Fires up to
  CONTRACT_NUDGE_LIMIT times per session, not just once — a one-shot guard
  went silent after the first catch even when the reply kept drifting on
  later turns.

All three behaviours are per-session-guarded via ~/.harness/run/ markers, each
under its own tag, so they can never double-fire into the same block.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import (  # noqa: E402
    emit, guard_once, guard_upto, load_config, message_text, read_event, read_yaml_file,
    safe_main,
)

DECISION_MARK = "【需要决策】"
# Requires the equals-sign divider around the section name, not just the bracket
# string — a bare "结论" or even "【结论】" heading with no divider still counts
# as drift away from the skill's actual template.
SECTION_PATTERN = re.compile(r"=+\s*【(?:结论|证据|问题|下一步)】\s*=+")
CONVERGED_STATUSES = ("CLEARED", "DONE_WITH_CONCERNS")
CONTRACT_NUDGE_LIMIT = 3

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

CONTRACT_REMINDER = """harness output contract：刚才的回复没有用分隔线骨架。

用 skill `harness-output-format` 定义的骨架重写后续实质性回复，段名要带等号分隔线：
========= 【结论】 ==========================
不是裸标题、不是只加个括号——分隔线本身也是格式要求的一部分。
过程细节写进文件，回复里只留结论、证据出处和待决项。
（每轮开头的 harness-prompt-contract 提醒是这条规则的常驻版本，这里是没听进去时的兜底，
一个 session 内最多提醒 %d 次）。""" % CONTRACT_NUDGE_LIMIT


def asks_permission(text):
    return any(re.search(p, text, re.IGNORECASE) for p in ASKS_PERMISSION)


def pending_spec_reviews(root):
    """Scope artifacts that are `status: confirmed` but whose `review.status`
    hasn't converged yet. Returns a list of artifact stems (slugs)."""
    if root is None:
        return []
    scope_dir = Path(root) / ".harness" / "scope"
    if not scope_dir.is_dir():
        return []
    pending = []
    for path in sorted(scope_dir.glob("*.yml")):
        data = read_yaml_file(path)
        if data.get("status") != "confirmed":
            continue
        review = data.get("review") or {}
        if review.get("status") not in CONVERGED_STATUSES:
            pending.append(path.stem)
    return pending


def spec_review_reason(names):
    return f"""harness spec-review guard：以下 scope 已经 confirmed，但还没走完收敛判定：{", ".join(names)}。

规则：spec 的验收标准要收敛到 CLEARED 或 DONE_WITH_CONCERNS 才能结束这一轮。
跑一次 `/harness-spec-review` 把它收敛掉；如果确实卡住了，第一行写 `【需要决策】` 说明卡在哪，然后继续。

这条提醒每个 session 只出现一次。"""


def main():
    event = read_event()
    if event.get("hook_event_name") not in (None, "Stop"):
        emit()

    cwd = event.get("cwd") or "."
    cfg, root = load_config(cwd)
    session = event.get("session_id")

    gate = cfg.get("scope_gate") or {}
    if gate.get("enabled", True):
        pending = pending_spec_reviews(root)
        if pending and guard_once(session, "spec_review"):
            emit({"decision": "block", "reason": spec_review_reason(pending)})

    text = message_text(event.get("last_assistant_message")).strip()
    if len(text) < 40:
        emit()

    out = cfg.get("output") or {}

    if out.get("auto_continue", True) and DECISION_MARK not in text and asks_permission(text):
        if guard_once(session, "continue"):
            emit({"decision": "block", "reason": CONTINUE_REASON})

    if out.get("contract") == "structured" and out.get("nudge_once", True):
        if len(text) > 700 and not SECTION_PATTERN.search(text):
            if guard_upto(session, "contract", CONTRACT_NUDGE_LIMIT):
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
