#!/usr/bin/env bash
set -e
APP_DIR="/opt/server-monitor-bot"
WEB_FILE="$APP_DIR/web_dashboard.py"
WEB_VENV="$APP_DIR/web-venv"
SERVICE_FILE="/etc/systemd/system/server-monitor-web.service"
if [ "$(id -u)" -ne 0 ]; then echo "❌ 请使用 root 执行"; exit 1; fi
apt update
apt install -y python3 python3-venv python3-pip curl sqlite3
mkdir -p "$APP_DIR"
curl -fsSL https://raw.githubusercontent.com/lxfcx/Oracle/main/web_dashboard.py -o "$WEB_FILE"
python3 -m venv "$WEB_VENV"
"$WEB_VENV/bin/pip" install -U pip flask werkzeug requests python-dateutil psutil
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Server Monitor Web
After=network-online.target
[Service]
WorkingDirectory=$APP_DIR
EnvironmentFile=-$APP_DIR/.env
EnvironmentFile=-$APP_DIR/.env.web
ExecStart=$WEB_VENV/bin/python $WEB_FILE
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now server-monitor-web
systemctl restart server-monitor-web
systemctl status server-monitor-web --no-pager -l
