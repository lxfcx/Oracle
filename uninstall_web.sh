#!/usr/bin/env bash
set -e
SERVICE_NAME="server-monitor-web"
APP_DIR="/opt/server-monitor-bot"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
if [ "$(id -u)" -ne 0 ]; then echo "❌ 请使用 root 执行，或前面加 sudo"; exit 1; fi
echo "⚠️ 即将卸载 Telegram Server Monitor Web 面板"
echo "不会删除主机器人 bot.py 和 servers.db 数据库。"
read -r -p "确认卸载？输入 YES 继续：" CONFIRM
[ "$CONFIRM" = "YES" ] || { echo "已取消卸载"; exit 0; }
systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
rm -f "$SERVICE_FILE" "$APP_DIR/web_dashboard.py" "$APP_DIR/.env.web"
rm -rf "$APP_DIR/web-venv"
systemctl daemon-reload 2>/dev/null || true
systemctl reset-failed 2>/dev/null || true
echo "✅ Web 面板已卸载完成"
