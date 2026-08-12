#!/usr/bin/env bash
# context-observe-cron.sh — 定时给一个或多个已接入 .context/ 的项目做洞察观测。
#
# 用法：
#   context-observe-cron.sh /path/to/repo [/path/to/another-repo ...]
#   context-observe-cron.sh --projects-file ~/.harness/context-projects.txt
#
# crontab 示例（每天 21:00 JST，一天一次）：
#   0 21 * * * /Users/rshao/work/agent-harness/periodic/context-observe-cron.sh \
#               --projects-file "$HOME/.harness/context-projects.txt" >> /tmp/context-observe.log 2>&1
#
# 设计约束：
#   - 幂等靠 /context-observe skill 自己查 .context/.last-observe + OBSERVATIONS.md 里的 Date header，
#     这个脚本本身不做去重判断，只负责「有没有 .context/」这一层前置检查。
#   - append-only，不像 compact 那样要求工作区干净——观测这一步不动既有内容。
#   - 全部动作由 /context-observe skill 执行，这个脚本只负责调度。

set -uo pipefail

CLAUDE_BIN="${CLAUDE_BIN:-$HOME/.local/bin/claude}"

PROJECTS=()
if [[ "${1:-}" == "--projects-file" ]]; then
  [[ -f "${2:-}" ]] || { echo "projects file not found: ${2:-}"; exit 1; }
  while IFS= read -r line; do
    [[ -z "$line" || "$line" == \#* ]] && continue
    PROJECTS+=("$line")
  done < "$2"
else
  PROJECTS=("$@")
fi

[[ ${#PROJECTS[@]} -gt 0 ]] || { echo "usage: context-observe-cron.sh REPO [REPO...]"; exit 1; }

for repo in "${PROJECTS[@]}"; do
  echo "=== $(date -u +%FT%TZ) $repo"

  if [[ ! -d "$repo/.context" ]]; then
    echo "  skip: no .context/ (run: harness context-init $repo)"
    continue
  fi

  echo "  running /context-observe"
  (cd "$repo" && "$CLAUDE_BIN" -p "/context-observe 按 skill 的步骤对当前项目做一次观测。无人值守模式：判不准归哪个交通灯的标🟡存疑，不要漏记也不要瞎归🔴；今天已有条目就跳过。") \
    && echo "  done" || echo "  FAILED (exit $?)"
done
