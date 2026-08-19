#!/bin/bash
# 每日自动采集：补最近几天 + 重新导出 JSON / Excel / 周报 / 看板。
# 由 launchd（macOS）或 cron 调用，日志按天写到 data/logs/。
#
# 失败一定要能被人看见。以前每一步都写成 `cmd && log "完成"`，
# 失败时连一行日志都不留；launchd 配的是 KeepAlive:false，非零退出也不触发任何动作。
# 结果就是采集挂了、令牌过期了、导出失败了没人知道，直到某天发现数据不动了。
# 现在每步都显式判定，收尾时把失败项汇总推一条钉钉消息（见 notify）。
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

# 令牌用满这么多天就提醒该换了。抓包换令牌是唯一的人工环节，
# 等它失效后才发现意味着中间那几天的数据已经漏采。
TOKEN_WARN_DAYS="${DLJY_TOKEN_WARN_DAYS:-25}"

# 告警走 OpenClaw 的钉钉通道（这台机器上本来就跑着机器人）。
# 找不到 openclaw 就只记日志，不因为通知失败而影响采集本身。
OPENCLAW_NODE="${DLJY_OPENCLAW_NODE:-/opt/homebrew/opt/node/bin/node}"
OPENCLAW_JS="${DLJY_OPENCLAW_JS:-/opt/homebrew/lib/node_modules/openclaw/dist/index.js}"
NOTIFY_ACCOUNT="${DLJY_NOTIFY_ACCOUNT:-power-trader-bot}"
NOTIFY_TARGET="${DLJY_NOTIFY_TARGET:-01074655060636459194}"

FAILURES=()

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

# run <人话步骤名> <命令...>：失败也留痕，并记进 FAILURES
run() {
  local label="$1"; shift
  # 必须显式捕获退出码：if 语句结束后的 $? 是 if 本身的状态（条件为假且无 else 时为 0），
  # 不是被判断命令的退出码——照直觉写 `if ...; fi; local code=$?` 会永远拿到 0。
  local code=0
  "$@" >>"$LOG_FILE" 2>&1 || code=$?
  if [ "$code" -eq 0 ]; then
    log "$label 完成"
    return 0
  fi
  log "❌ $label 失败（退出码 ${code}）"
  FAILURES+=("$label(exit $code)")
  return "$code"
}

notify() {
  local text="$1"
  if [ ! -x "$OPENCLAW_NODE" ] || [ ! -f "$OPENCLAW_JS" ]; then
    log "（未找到 openclaw，跳过钉钉通知）"
    return 0
  fi
  "$OPENCLAW_NODE" "$OPENCLAW_JS" message send \
    --channel dingtalk-connector \
    --account "$NOTIFY_ACCOUNT" \
    --target "$NOTIFY_TARGET" \
    -m "$text" >>"$LOG_FILE" 2>&1 \
    && log "已推送钉钉通知" \
    || log "钉钉通知发送失败（不影响采集结果）"
}

log "=== 每日采集开始（回溯 ${LOOKBACK} 天）==="

# ── 令牌 ──
if ! "$PYTHON_BIN" - <<'PY' >>"$LOG_FILE" 2>&1
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "scripts"))
import config
sys.exit(0 if config.load_token() else 1)
PY
then
  log "❌ 未找到 ELECHECK_TOKEN，跳过采集"
  notify "⚠️ 电价采集未执行：没有找到采集令牌。
在宿主机跑 python run.py sniff（自动抓）或 python run.py token（手工粘贴）更新。"
  exit 78
fi

# 令牌年龄提醒。令牌里只有 iat 没有 exp（实测），所以无法知道它哪天失效，
# 只能按"已经用了多久"做经验阈值——总比等失效后漏采几天才发现强。
TOKEN_AGE_DAYS="$("$PYTHON_BIN" - <<'PY' 2>/dev/null
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "scripts"))
try:
    import config
    st = config.token_state()
    if st.get("expired"):
        print("EXPIRED")
    elif st.get("age_hours") is not None:
        print(int(st["age_hours"] // 24))
except Exception:
    pass
PY
)"
if [ "${TOKEN_AGE_DAYS:-}" = "EXPIRED" ]; then
  log "❌ 采集令牌已过期"
  notify "🔴 电价采集令牌已过期，今天的数据采不到。
在宿主机跑 python run.py sniff 更新后补跑 ./automation/daily_update.sh"
  TOKEN_AGE_DAYS=""
fi
if [ -n "${TOKEN_AGE_DAYS:-}" ] && [ "$TOKEN_AGE_DAYS" -ge "$TOKEN_WARN_DAYS" ] 2>/dev/null; then
  log "⚠️ 采集令牌已使用 ${TOKEN_AGE_DAYS} 天（阈值 ${TOKEN_WARN_DAYS} 天）"
  notify "⚠️ 电价采集令牌已使用 ${TOKEN_AGE_DAYS} 天，建议尽快更新。
失效后当天的数据会直接漏采：python run.py sniff"
fi

# ── 采集 ──
"$PYTHON_BIN" scripts/collect.py --last-days "$LOOKBACK" --workers 3 >>"$LOG_FILE" 2>&1
COLLECT_CODE=$?
log "采集结束，退出码 ${COLLECT_CODE}"

if [ "$COLLECT_CODE" -eq 2 ]; then
  log "❌ 令牌可能已过期，后续导出仍会基于已有数据执行"
  FAILURES+=("采集(令牌失效)")
  notify "🔴 电价采集失败：令牌已过期，今天的数据没有采到。
在宿主机跑 python run.py sniff 更新令牌后，补跑：
  ./automation/daily_update.sh"
elif [ "$COLLECT_CODE" -ne 0 ]; then
  FAILURES+=("采集(exit $COLLECT_CODE)")
fi

# ── 导出 ──
run "JSON 导出"        "$PYTHON_BIN" scripts/export_json.py
run "Excel 导出"       "$PYTHON_BIN" scripts/export_excel.py
run "分层 Excel 更新"  "$PYTHON_BIN" scripts/export_tree.py
run "看板重建"         "$PYTHON_BIN" scripts/dashboard.py
run "Artifact 页面"    "$PYTHON_BIN" scripts/dashboard.py --artifact

if [ "$WEEKDAY" = "$REPORT_DOW" ]; then
  run "周报生成" "$PYTHON_BIN" scripts/weekly_report.py
  # 周报会进看板的「本周速览」，所以要在周报之后再刷一次页面
  run "看板重建(含周报)"      "$PYTHON_BIN" scripts/dashboard.py
  run "Artifact 页面(含周报)" "$PYTHON_BIN" scripts/dashboard.py --artifact
fi

# 只保留最近 60 天的日志
find "$LOG_DIR" -name 'daily_*.log' -type f -mtime +60 -delete 2>/dev/null

# ── 收尾 ──
if [ ${#FAILURES[@]} -gt 0 ]; then
  log "=== 每日采集结束，有 ${#FAILURES[@]} 项失败：${FAILURES[*]} ==="
  # 令牌失效已经单独推过一条，这里不重复轰炸
  if [ "$COLLECT_CODE" -ne 2 ]; then
    notify "🔴 电价每日任务有 ${#FAILURES[@]} 项失败：
${FAILURES[*]}

日志：$LOG_FILE"
  fi
  exit 1
fi

log "=== 每日采集结束，全部成功 ==="
exit 0
