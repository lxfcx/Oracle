#!/usr/bin/env bash
set -e

BOT_TOKEN="${BOT_TOKEN:-}"
ADMIN_IDS="${ADMIN_IDS:-}"

REPO_URL="https://raw.githubusercontent.com/你的用户名/server-monitor-bot/main"

APP_DIR="/opt/server-monitor-bot"
SERVICE_NAME="server-monitor-bot"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

if [ "$(id -u)" -ne 0 ]; then
  echo "❌ 请使用 root 执行，或前面加 sudo"
  exit 1
fi

if [ -z "$BOT_TOKEN" ] || [ -z "$ADMIN_IDS" ]; then
  echo "❌ 缺少 BOT_TOKEN 或 ADMIN_IDS"
  echo
  echo "正确用法："
  echo 'BOT_TOKEN="你的TG_BOT_TOKEN" ADMIN_IDS="你的TG数字ID" bash <(curl -fsSL https://raw.githubusercontent.com/你的用户名/server-monitor-bot/main/install.sh)'
  echo
  exit 1
fi

echo "🚀 开始安装 Telegram Server Monitor Bot..."

apt update
apt install -y python3 python3-venv python3-pip sqlite3 curl fail2ban procps net-tools iputils-ping

mkdir -p "$APP_DIR"
cd "$APP_DIR"

echo "📥 下载 bot.py..."
curl -fsSL "$REPO_URL/bot.py" -o "$APP_DIR/bot.py"
chmod +x "$APP_DIR/bot.py"

echo "🐍 创建 Python 虚拟环境..."
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install requests psutil python-dateutil

echo "⚙️ 创建 systemd 服务..."
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Telegram Server Monitor Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
Environment="BOT_TOKEN=$BOT_TOKEN"
Environment="ADMIN_IDS=$ADMIN_IDS"
ExecStart=$APP_DIR/venv/bin/python $APP_DIR/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

echo
echo "✅ 安装完成"
echo
echo "📌 查看状态："
echo "systemctl status $SERVICE_NAME"
echo
echo "📌 查看日志："
echo "journalctl -u $SERVICE_NAME -f"
echo
echo "📱 现在去 Telegram 给 Bot 发送：/help"
