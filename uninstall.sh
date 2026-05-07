#!/usr/bin/env bash
set -e

SERVICE_NAME="server-monitor-bot"
APP_DIR="/opt/server-monitor-bot"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

if [ "$(id -u)" -ne 0 ]; then
  echo "❌ 请使用 root 执行，或前面加 sudo"
  exit 1
fi

echo "🧹 正在卸载 Telegram Server Monitor Bot..."

systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
rm -f "$SERVICE_FILE"

systemctl daemon-reload
systemctl reset-failed 2>/dev/null || true

rm -rf "$APP_DIR"

echo "✅ 已卸载干净"
echo "📌 已删除："
echo "- $SERVICE_FILE"
echo "- $APP_DIR"
