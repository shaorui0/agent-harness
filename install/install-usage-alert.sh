#!/bin/sh
# 渲染 plist 模板到 ~/Library/LaunchAgents 并 (重新) 加载。幂等。
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
TRACKER_DIR="$(cd "$HERE/../tracker" && pwd)"
DST="$HOME/Library/LaunchAgents/ai.harness.usage-alert.plist"
mkdir -p "$HOME/.harness/tracker/state"
sed -e "s|__PYTHON3__|$(command -v python3)|g" -e "s|__TRACKER_DIR__|$TRACKER_DIR|g" -e "s|__HOME__|$HOME|g" \
  "$HERE/ai.harness.usage-alert.plist.template" > "$DST"
launchctl bootout "gui/$(id -u)/ai.harness.usage-alert" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$DST"
launchctl print "gui/$(id -u)/ai.harness.usage-alert" | grep -E "state|interval|runs|last exit"
