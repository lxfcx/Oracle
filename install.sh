#!/usr/bin/env bash
set -e

# ============================================================
# Telegram Server Monitor Bot 一键安装 / 更新 / 配置 / 卸载脚本
# GitHub Raw: https://raw.githubusercontent.com/lxfcx/Oracle/main
#
# 菜单：
#   1 安装 / 更新机器人
#   2 编辑 BOT_TOKEN 和 ADMIN_IDS
#   0 卸载机器人并清理所有数据
#   3 退出脚本
# ============================================================

REPO_URL="${REPO_URL:-https://raw.githubusercontent.com/lxfcx/Oracle/main}"

APP_DIR="/opt/server-monitor-bot"
SERVICE_NAME="server-monitor-bot"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
ENV_FILE="${APP_DIR}/.env"
BOT_FILE="${APP_DIR}/bot.py"

BOT_TOKEN="${BOT_TOKEN:-}"
ADMIN_IDS="${ADMIN_IDS:-}"

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "❌ 请使用 root 执行，或前面加 sudo"
    exit 1
  fi
}

pause() {
  echo
  read -r -p "按回车返回菜单..."
}

load_env() {
  if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    . "$ENV_FILE"
  fi

  BOT_TOKEN="${BOT_TOKEN:-}"
  ADMIN_IDS="${ADMIN_IDS:-}"
}

save_env() {
  mkdir -p "$APP_DIR"

  cat > "$ENV_FILE" <<EOF
BOT_TOKEN='$BOT_TOKEN'
ADMIN_IDS='$ADMIN_IDS'
EOF

  chmod 600 "$ENV_FILE"
}

edit_config() {
  echo
  echo "⚙️ 编辑机器人配置"
  echo "━━━━━━━━━━━━━━━━━━━━"
  echo

  load_env

  if [ -n "$BOT_TOKEN" ]; then
    echo "当前 BOT_TOKEN：${BOT_TOKEN:0:10}******"
  else
    echo "当前 BOT_TOKEN：未设置"
  fi

  if [ -n "$ADMIN_IDS" ]; then
    echo "当前 ADMIN_IDS：$ADMIN_IDS"
  else
    echo "当前 ADMIN_IDS：未设置"
  fi

  echo
  read -r -p "请输入新的 BOT_TOKEN，留空则保持不变：" NEW_BOT_TOKEN
  read -r -p "请输入新的 ADMIN_IDS，多个用英文逗号分隔，留空则保持不变：" NEW_ADMIN_IDS

  if [ -n "$NEW_BOT_TOKEN" ]; then
    BOT_TOKEN="$NEW_BOT_TOKEN"
  fi

  if [ -n "$NEW_ADMIN_IDS" ]; then
    ADMIN_IDS="$NEW_ADMIN_IDS"
  fi

  if [ -z "$BOT_TOKEN" ] || [ -z "$ADMIN_IDS" ]; then
    echo
    echo "❌ BOT_TOKEN 和 ADMIN_IDS 都不能为空"
    return 1
  fi

  save_env

  if [ -f "$SERVICE_FILE" ]; then
    create_service
    systemctl daemon-reload
    systemctl restart "$SERVICE_NAME" 2>/dev/null || true
  fi

  echo
  echo "✅ 配置已保存：$ENV_FILE"
  echo "📌 如果机器人已安装，服务已自动重启"
}

install_or_update() {
  echo
  echo "🚀 开始安装 / 更新 Telegram Server Monitor Bot..."
  echo "━━━━━━━━━━━━━━━━━━━━"
  echo

  load_env

  if [ -z "$BOT_TOKEN" ] || [ -z "$ADMIN_IDS" ]; then
    echo "⚠️ 未检测到 BOT_TOKEN 或 ADMIN_IDS，请先填写配置。"
    edit_config
    load_env
  fi

  if [ -z "$BOT_TOKEN" ] || [ -z "$ADMIN_IDS" ]; then
    echo "❌ 缺少 BOT_TOKEN 或 ADMIN_IDS，无法继续安装"
    return 1
  fi

  echo "📦 安装系统依赖..."
  export DEBIAN_FRONTEND=noninteractive
  apt update
  apt install -y python3 python3-venv python3-pip sqlite3 curl fail2ban procps net-tools iputils-ping

  mkdir -p "$APP_DIR"
  cd "$APP_DIR"

  save_env

  echo "📥 下载 bot.py..."
  curl -fL "$REPO_URL/bot.py" -o /tmp/server-monitor-bot.py.new

  echo "🧪 检查 Python 语法..."
  python3 -m py_compile /tmp/server-monitor-bot.py.new

  mv /tmp/server-monitor-bot.py.new "$BOT_FILE"
  chmod +x "$BOT_FILE"

  echo "🐍 创建 / 更新 Python 虚拟环境..."
  if [ ! -d "$APP_DIR/venv" ]; then
    python3 -m venv "$APP_DIR/venv"
  fi

  "$APP_DIR/venv/bin/pip" install --upgrade pip
  "$APP_DIR/venv/bin/pip" install --upgrade requests psutil python-dateutil

  echo "⚙️ 创建 systemd 服务..."
  create_service

  systemctl daemon-reload
  systemctl enable --now "$SERVICE_NAME"
  systemctl restart "$SERVICE_NAME"

  echo
  echo "✅ 安装 / 更新完成"
  echo
  echo "📌 查看状态："
  echo "systemctl status $SERVICE_NAME --no-pager -l"
  echo
  echo "📌 查看日志："
  echo "journalctl -u $SERVICE_NAME -f"
  echo
  echo "📱 现在去 Telegram 给 Bot 发送：启用命令"
}

create_service() {
  cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Telegram Server Monitor Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/venv/bin/python $APP_DIR/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
}

uninstall_clean() {
  echo
  echo "⚠️ 即将彻底卸载 Telegram Server Monitor Bot"
  echo "━━━━━━━━━━━━━━━━━━━━"
  echo
  echo "将会删除："
  echo "🧹 systemd 服务：$SERVICE_FILE"
  echo "🧹 程序目录：$APP_DIR"
  echo "🧹 数据库：$APP_DIR/servers.db"
  echo "🧹 配置文件：$ENV_FILE"
  echo "🧹 Python 虚拟环境：$APP_DIR/venv"
  echo
  echo "不会删除：python3、curl、sqlite3、fail2ban 等系统公共依赖，避免影响其它程序。"
  echo

  read -r -p "确认彻底卸载？输入 YES 继续：" CONFIRM
  if [ "$CONFIRM" != "YES" ]; then
    echo "已取消卸载"
    return 0
  fi

  echo
  echo "🧹 正在停止并删除服务..."
  systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
  rm -f "$SERVICE_FILE"
  systemctl daemon-reload 2>/dev/null || true
  systemctl reset-failed 2>/dev/null || true

  echo "🧹 正在删除程序和所有数据..."
  rm -rf "$APP_DIR"

  echo
  echo "✅ 已彻底卸载干净"
  echo
  echo "📌 验证命令："
  echo "systemctl status $SERVICE_NAME --no-pager -l"
  echo "ls -ld $APP_DIR"
  echo "ls -l $SERVICE_FILE"
}

show_menu() {
  clear
  echo "🤖 Telegram Server Monitor Bot 管理脚本"
  echo "━━━━━━━━━━━━━━━━━━━━"
  echo "1) 安装 / 更新机器人"
  echo "2) 编辑 BOT_TOKEN 和 ADMIN_IDS"
  echo "3) 退出脚本"
  echo "0) 卸载机器人并清理所有数据"
  echo "━━━━━━━━━━━━━━━━━━━━"
  echo
}

main_menu() {
  while true; do
    show_menu
    read -r -p "请选择操作 [1/2/0/3]：" CHOICE

    case "$CHOICE" in
      1)
        install_or_update
        pause
        ;;
      2)
        edit_config
        pause
        ;;
      0)
        uninstall_clean
        pause
        ;;
      3)
        echo "已退出"
        exit 0
        ;;
      *)
        echo "❌ 无效选项，请输入 1、2、0 或 3"
        sleep 1
        ;;
    esac
  done
}

require_root

# 兼容一行无交互安装：
# BOT_TOKEN="xxx" ADMIN_IDS="123" AUTO_INSTALL=1 bash <(curl -fsSL ...)
if [ "${AUTO_INSTALL:-}" = "1" ]; then
  install_or_update
else
  main_menu
fi
