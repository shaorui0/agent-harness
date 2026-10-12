#!/usr/bin/env bash
# compact-cron.sh — 定时收敛一个或多个已接入 harness 的项目。
#
# 用法：
#   compact-cron.sh /path/to/repo [/path/to/another-repo ...]
#   compact-cron.sh --projects-file ~/.harness/projects.txt
#
# crontab 示例（每天 03:00 JST）：
#   0 3 * * * /Users/rshao/work/context-infrastructure/projects/agent-harness/periodic/compact-cron.sh \
#               --projects-file "$HOME/.harness/projects.txt" >> /tmp/harness-compact.log 2>&1
#
# 设计约束：
#   - 只在距上次收敛超过 MIN_INTERVAL_DAYS 天时才真跑，重复调度是幂等的
#   - 工作区脏就跳过，不在别人的改动上面做归档
#   - 全部动作由 /harness-compact skill 执行，这个脚本只负责调度和守卫

set -uo pipefail

MIN_INTERVAL_DAYS="${HARNESS_COMPACT_MIN_DAYS:-7}"
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

[[ ${#PROJECTS[@]} -gt 0 ]] || { echo "usage: compact-cron.sh REPO [REPO...]"; exit 1; }

now=$(date -u +%s)

for repo in "${PROJECTS[@]}"; do
  echo "=== $(date -u +%FT%TZ) $repo"

  if [[ ! -d "$repo/.harness" ]]; then
    echo "  skip: no .harness/ (run harness init first)"
    continue
  fi

  stamp="$repo/.harness/.last-compact"
  if [[ -f "$stamp" ]]; then
    last=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$(cat "$stamp")" +%s 2>/dev/null || echo 0)
    if [[ "$last" -gt 0 ]]; then
      age_days=$(( (now - last) / 86400 ))
      if [[ "$age_days" -lt "$MIN_INTERVAL_DAYS" ]]; then
        echo "  skip: last compact ${age_days}d ago (< ${MIN_INTERVAL_DAYS}d)"
        continue
      fi
    fi
  fi

  if [[ -n "$(git -C "$repo" status --porcelain 2>/dev/null)" ]]; then
    echo "  skip: working tree dirty, refusing to archive on top of uncommitted changes"
    continue
  fi

  echo "  running /harness-compact"
  (cd "$repo" && "$CLAUDE_BIN" -p "/harness-compact 按 skill 的步骤对当前项目做一次收敛。无人值守模式：判不准的一律保留并标注存疑，不要猜；需要用户确认的写进报告的【仍然存疑】一节。") \
    && echo "  done" || echo "  FAILED (exit $?)"
done
