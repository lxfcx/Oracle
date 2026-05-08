#!/usr/bin/env bash
set -e

APP_DIR="/opt/server-monitor-agent"
SERVICE_NAME="server-monitor-agent"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
ENV_FILE="${APP_DIR}/.env"
WORKER="${APP_DIR}/agent-worker.sh"

BOT_TOKEN=""
CHAT_ID=""
NODE_NAME="$(hostname)"
INTERVAL="60"
DISK_ALERT="90"
MEM_ALERT="90"
CPU_LOAD_ALERT="90"

while [ $# -gt 0 ]; do
  case "$1" in
    -t|--token) BOT_TOKEN="$2"; shift 2 ;;
    -c|--chat) CHAT_ID="$2"; shift 2 ;;
    -n|--name) NODE_NAME="$2"; shift 2 ;;
    -i|--interval) INTERVAL="$2"; shift 2 ;;
    --uninstall)
      systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
      rm -f "$SERVICE_FILE"
      systemctl daemon-reload 2>/dev/null || true
      rm -rf "$APP_DIR"
      echo "✅ server-monitor-agent 已卸载干净"
      exit 0
      ;;
    *) shift ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  echo "❌ 请使用 root 执行，或前面加 sudo"
  exit 1
fi

if [ -z "$BOT_TOKEN" ] || [ -z "$CHAT_ID" ]; then
  echo "❌ 缺少 BOT_TOKEN 或 CHAT_ID"
  echo "用法：bash agent.sh --token <BOT_TOKEN> --chat <TG数字ID> --name <服务器名称>"
  exit 1
fi

mkdir -p "$APP_DIR"
cat > "$ENV_FILE" <<EOF
BOT_TOKEN='$BOT_TOKEN'
CHAT_ID='$CHAT_ID'
NODE_NAME='$NODE_NAME'
INTERVAL='$INTERVAL'
DISK_ALERT='$DISK_ALERT'
MEM_ALERT='$MEM_ALERT'
CPU_LOAD_ALERT='$CPU_LOAD_ALERT'
EOF
chmod 600 "$ENV_FILE"

cat > "$WORKER" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

source /opt/server-monitor-agent/.env
API="https://api.telegram.org/bot${BOT_TOKEN}/sendMessage"
STATE_DIR="/opt/server-monitor-agent/state"
mkdir -p "$STATE_DIR"

send_tg() {
  local text="$1"
  curl -fsS -X POST "$API" \
    -H 'Content-Type: application/json' \
    -d "$(python3 - <<PY
import json, os
print(json.dumps({
  'chat_id': os.environ.get('CHAT_ID', '$CHAT_ID'),
  'text': '''$text''',
  'parse_mode': 'HTML',
  'disable_web_page_preview': True
}, ensure_ascii=False))
PY
)" >/dev/null 2>&1 || true
}

fmt_os() {
  if [ -f /etc/os-release ]; then
    . /etc/os-release
    echo "${PRETTY_NAME:-${NAME:-Linux}}"
  else
    uname -srm
  fi
}

public_ip() {
  curl -4 -s --max-time 6 https://api.ipify.org || curl -4 -s --max-time 6 https://icanhazip.com || echo "未知"
}

mem_percent() {
  free | awk '/Mem:/ {printf("%.0f", $3/$2*100)}'
}

disk_percent() {
  df -P / | awk 'NR==2 {gsub("%","",$5); print $5}'
}

load_percent() {
  cores=$(nproc 2>/dev/null || echo 1)
  load=$(awk '{print $1}' /proc/loadavg)
  awk -v l="$load" -v c="$cores" 'BEGIN{printf("%.0f", (l/c)*100)}'
}

state_get() { [ -f "$STATE_DIR/$1" ] && cat "$STATE_DIR/$1" || echo "0"; }
state_set() { echo "$2" > "$STATE_DIR/$1"; }

startup_msg="✅🟢 <b>探针已上线</b> 🟢✅

━━━━━━━━━━━━━━
🖥️ <b>名称：</b>${NODE_NAME}
🌍 <b>公网 IP：</b><code>$(public_ip)</code>
🧬 <b>系统：</b>$(fmt_os)
⏱️ <b>检测间隔：</b>${INTERVAL} 秒
━━━━━━━━━━━━━━
📌 Agent 会在 CPU/内存/磁盘异常时推送告警，恢复正常时推送恢复通知。"
send_tg "$startup_msg"

while true; do
  MEM=$(mem_percent || echo 0)
  DISK=$(disk_percent || echo 0)
  LOAD=$(load_percent || echo 0)
  NOW=$(date '+%Y-%m-%d %H:%M:%S')

  if [ "$DISK" -ge "$DISK_ALERT" ]; then
    if [ "$(state_get disk)" != "1" ]; then
      state_set disk 1
      send_tg "🚨💽 <b>磁盘空间告警</b> 💽🚨

🖥️ <b>名称：</b>${NODE_NAME}
📊 <b>根目录使用率：</b>${DISK}%
⏰ <b>时间：</b>${NOW}

🧹 建议清理日志、缓存或大文件。"
    fi
  else
    if [ "$(state_get disk)" = "1" ]; then
      state_set disk 0
      send_tg "✅💽 <b>磁盘空间已恢复</b>

🖥️ <b>名称：</b>${NODE_NAME}
📊 <b>根目录使用率：</b>${DISK}%
⏰ <b>时间：</b>${NOW}"
    fi
  fi

  if [ "$MEM" -ge "$MEM_ALERT" ]; then
    if [ "$(state_get mem)" != "1" ]; then
      state_set mem 1
      send_tg "🚨🧠 <b>内存高占用告警</b> 🧠🚨

🖥️ <b>名称：</b>${NODE_NAME}
📊 <b>内存使用率：</b>${MEM}%
⏰ <b>时间：</b>${NOW}"
    fi
  else
    if [ "$(state_get mem)" = "1" ]; then
      state_set mem 0
      send_tg "✅🧠 <b>内存已恢复</b>

🖥️ <b>名称：</b>${NODE_NAME}
📊 <b>内存使用率：</b>${MEM}%
⏰ <b>时间：</b>${NOW}"
    fi
  fi

  if [ "$LOAD" -ge "$CPU_LOAD_ALERT" ]; then
    if [ "$(state_get load)" != "1" ]; then
      state_set load 1
      send_tg "🚨🔥 <b>CPU/负载告警</b> 🔥🚨

🖥️ <b>名称：</b>${NODE_NAME}
📊 <b>估算负载：</b>${LOAD}%
⏰ <b>时间：</b>${NOW}"
    fi
  else
    if [ "$(state_get load)" = "1" ]; then
      state_set load 0
      send_tg "✅🔥 <b>CPU/负载已恢复</b>

🖥️ <b>名称：</b>${NODE_NAME}
📊 <b>估算负载：</b>${LOAD}%
⏰ <b>时间：</b>${NOW}"
    fi
  fi

  sleep "$INTERVAL"
done
EOF
chmod +x "$WORKER"

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Server Monitor Telegram Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$WORKER
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

echo "✅ 探针安装完成"
echo "📌 查看状态：systemctl status $SERVICE_NAME --no-pager -l"
echo "📌 查看日志：journalctl -u $SERVICE_NAME -f"
