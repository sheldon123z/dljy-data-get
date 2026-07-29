#!/bin/bash
# 安装 / 卸载 macOS launchd 定时任务，每天固定时间执行 daily_update.sh。
#   ./automation/install_daily.sh          安装（默认每天 09:30）
#   DLJY_HOUR=7 DLJY_MINUTE=0 ./automation/install_daily.sh
#   ./automation/install_daily.sh uninstall
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.dljy.dataget.daily"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
HOUR="${DLJY_HOUR:-9}"
MINUTE="${DLJY_MINUTE:-30}"
PYTHON_BIN="${DLJY_PYTHON:-$(command -v python3)}"

if [ "${1:-install}" = "uninstall" ]; then
  launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "已卸载定时任务 ${LABEL}"
  exit 0
fi

chmod +x "$ROOT/automation/daily_update.sh"
mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/data/logs"

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${ROOT}/automation/daily_update.sh</string>
  </array>
  <key>WorkingDirectory</key><string>${ROOT}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>DLJY_PYTHON</key><string>${PYTHON_BIN}</string>
    <key>PATH</key><string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>${HOUR}</integer>
    <key>Minute</key><integer>${MINUTE}</integer>
  </dict>
  <!-- 到点时如果电脑在睡眠，唤醒后补跑一次 -->
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>${ROOT}/data/logs/launchd.out.log</string>
  <key>StandardErrorPath</key><string>${ROOT}/data/logs/launchd.err.log</string>
</dict>
</plist>
PLIST_EOF

launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "已安装定时任务 ${LABEL}"
echo "  执行时间：每天 ${HOUR}:$(printf '%02d' "$MINUTE")"
echo "  脚本：${ROOT}/automation/daily_update.sh"
echo "  日志：${ROOT}/data/logs/daily_YYYYMMDD.log"
echo
echo "立即试跑一次： launchctl kickstart -k gui/$(id -u)/${LABEL}"
echo "查看状态：     launchctl print gui/$(id -u)/${LABEL} | head -20"
echo "卸载：         ./automation/install_daily.sh uninstall"
