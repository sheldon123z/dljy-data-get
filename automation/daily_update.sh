#!/bin/bash
# 每日自动采集：补最近几天 + 重新导出 JSON / Excel / 周报 / 看板。
# 由 launchd（macOS）或 cron 调用，日志按天写到 data/logs/。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

PYTHON_BIN="${DLJY_PYTHON:-python3}"
LOG_DIR="$ROOT/data/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/daily_$(date +%Y%m%d).log"

# 回溯天数：接口常有 T+1/T+2 的补录，默认多看几天
LOOKBACK="${DLJY_LOOKBACK:-4}"
# 周报生成日：1=周一 … 5=周五（默认），7=周日
REPORT_DOW="${DLJY_REPORT_DOW:-5}"
WEEKDAY="$(date +%u)"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

log "=== 每日采集开始（回溯 ${LOOKBACK} 天）==="

if ! "$PYTHON_BIN" - <<'PY' >>"$LOG_FILE" 2>&1
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "scripts"))
import config
sys.exit(0 if config.load_token() else 1)
PY
then
  log "未找到 ELECHECK_TOKEN，跳过采集。请运行 python run.py token 更新令牌。"
  exit 78
fi

"$PYTHON_BIN" scripts/collect.py --last-days "$LOOKBACK" --workers 3 >>"$LOG_FILE" 2>&1
COLLECT_CODE=$?
log "采集结束，退出码 ${COLLECT_CODE}"

if [ "$COLLECT_CODE" -eq 2 ]; then
  log "令牌可能已过期，后续导出仍会基于已有数据执行。"
fi

"$PYTHON_BIN" scripts/export_json.py    >>"$LOG_FILE" 2>&1 && log "JSON 导出完成"
"$PYTHON_BIN" scripts/export_excel.py   >>"$LOG_FILE" 2>&1 && log "Excel 导出完成"
"$PYTHON_BIN" scripts/export_tree.py    >>"$LOG_FILE" 2>&1 && log "分层 Excel 增量更新完成"
"$PYTHON_BIN" scripts/dashboard.py      >>"$LOG_FILE" 2>&1 && log "看板重建完成"
"$PYTHON_BIN" scripts/dashboard.py --artifact >>"$LOG_FILE" 2>&1 && log "Artifact 页面重建完成"

if [ "$WEEKDAY" = "$REPORT_DOW" ]; then
  "$PYTHON_BIN" scripts/weekly_report.py >>"$LOG_FILE" 2>&1 && log "周报生成完成"
  # 周报会进看板的「本周速览」，所以要在周报之后再刷一次页面
  "$PYTHON_BIN" scripts/dashboard.py           >>"$LOG_FILE" 2>&1
  "$PYTHON_BIN" scripts/dashboard.py --artifact >>"$LOG_FILE" 2>&1
  log "周报已并入看板与 Artifact 页面"
fi

# 只保留最近 60 天的日志
find "$LOG_DIR" -name 'daily_*.log' -type f -mtime +60 -delete 2>/dev/null

log "=== 每日采集结束 ==="
exit "$COLLECT_CODE"
