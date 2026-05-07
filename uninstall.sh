#!/usr/bin/env bash
set -e

SERVICE_NAME="server-monitor-bot"
APP_DIR="/opt/server-monitor-bot"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

if [ "$(id -u)" -ne 0 ]; then
  echo "❌ 请使用 root 执行，或前面加 sudo"
  exit 1
fi

echo "⚠️ 即将彻底卸载 Telegram Server Monitor Bot"
echo "━━━━━━━━━━━━━━━━━━━━"
echo
echo "将会删除："
echo "🧹 systemd 服务：$SERVICE_FILE"
echo "🧹 程序目录：$APP_DIR"
echo "🧹 数据库：$APP_DIR/servers.db"
echo "🧹 配置文件：$APP_DIR/.env"
echo "🧹 Python 虚拟环境：$APP_DIR/venv"
echo
echo "不会删除：python3、curl、sqlite3、fail2ban 等系统公共依赖，避免影响其它程序。"
echo

read -r -p "确认彻底卸载？输入 YES 继续：" CONFIRM
if [ "$CONFIRM" != "YES" ]; then
  echo "已取消卸载"
  exit 0
fi

systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
rm -f "$SERVICE_FILE"

systemctl daemon-reload 2>/dev/null || true
systemctl reset-failed 2>/dev/null || true

rm -rf "$APP_DIR"

echo
echo "✅ 已彻底卸载干净"
echo
echo "📌 已删除："
echo "- $SERVICE_FILE"
echo "- $APP_DIR"
