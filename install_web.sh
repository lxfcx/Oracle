#!/usr/bin/env bash
set -e
REPO_URL="${REPO_URL:-https://raw.githubusercontent.com/lxfcx/Oracle/main}"
APP_DIR="/opt/server-monitor-bot"
SERVICE_NAME="server-monitor-web"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
WEB_ENV="${APP_DIR}/.env.web"
WEB_VENV="${APP_DIR}/web-venv"
WEB_FILE="${APP_DIR}/web_dashboard.py"
if [ "$(id -u)" -ne 0 ]; then echo "❌ 请使用 root 执行，或前面加 sudo"; exit 1; fi
banner(){ echo; echo "🌐✨ Telegram Server Monitor Web 星空磨砂玻璃大屏面板 ✨🌐"; echo "━━━━━━━━━━━━━━━━━━━━"; }
install_or_update(){
  banner; echo "🚀 开始安装 / 更新 Web 面板..."
  apt update
  apt install -y python3 python3-venv python3-pip curl sqlite3
  mkdir -p "$APP_DIR"
  echo "📥 下载 web_dashboard.py..."
  curl -fsSL "${REPO_URL}/web_dashboard.py" -o "$WEB_FILE"
  chmod +x "$WEB_FILE"
  echo "🐍 创建 Web 虚拟环境..."
  python3 -m venv "$WEB_VENV"
  "$WEB_VENV/bin/pip" install --upgrade pip
  "$WEB_VENV/bin/pip" install flask werkzeug requests python-dateutil psutil
  if [ ! -f "$WEB_ENV" ]; then
    read -r -p "请输入网页登录账号 [admin]：" WEB_USERNAME
    WEB_USERNAME="${WEB_USERNAME:-admin}"
    while true; do
      read -r -p "请输入网页登录密码（明文显示，方便确认）：" WEB_PASSWORD
      read -r -p "请再次输入密码（明文显示，方便确认）：" WEB_PASSWORD2
      [ "$WEB_PASSWORD" = "$WEB_PASSWORD2" ] && [ -n "$WEB_PASSWORD" ] && break
      echo "❌ 两次密码不一致或为空，请重新输入"
    done
    WEB_PASSWORD_HASH="$("$WEB_VENV/bin/python" - <<PY
from werkzeug.security import generate_password_hash
print(generate_password_hash(${WEB_PASSWORD@Q}))
PY
)"
    WEB_SECRET_KEY="$("$WEB_VENV/bin/python" - <<'PY'
import secrets
print(secrets.token_hex(32))
PY
)"
    cat > "$WEB_ENV" <<EOF
WEB_USERNAME="$WEB_USERNAME"
WEB_PASSWORD_HASH="$WEB_PASSWORD_HASH"
WEB_SECRET_KEY="$WEB_SECRET_KEY"
WEB_HOST="0.0.0.0"
WEB_PORT="8899"
DATABASE_PATH="${APP_DIR}/servers.db"
APP_DIR="${APP_DIR}"
METRICS_PORT="8765"
EOF
  fi
  cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Telegram Server Monitor Web Dashboard
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
WorkingDirectory=${APP_DIR}
EnvironmentFile=-${APP_DIR}/.env
EnvironmentFile=-${WEB_ENV}
ExecStart=${WEB_VENV}/bin/python ${WEB_FILE}
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now "$SERVICE_NAME"
  systemctl restart "$SERVICE_NAME"
  echo; echo "✅ Web 面板安装/更新完成"; echo "🌐 访问：http://服务器公网IP:8899"; echo "📌 状态：systemctl status ${SERVICE_NAME} --no-pager -l"; echo "📌 日志：journalctl -u ${SERVICE_NAME} -f"; echo; echo "放行端口：ufw allow 8899/tcp 2>/dev/null || true"
}
reset_password(){
  banner
  [ -x "$WEB_VENV/bin/python" ] || { echo "⚠️ Web 环境不存在，先安装。"; install_or_update; return; }
  read -r -p "请输入新的网页登录账号 [admin]：" WEB_USERNAME
  WEB_USERNAME="${WEB_USERNAME:-admin}"
  while true; do
    read -r -p "请输入新的网页登录密码（明文显示，方便确认）：" WEB_PASSWORD
    read -r -p "请再次输入密码（明文显示，方便确认）：" WEB_PASSWORD2
    [ "$WEB_PASSWORD" = "$WEB_PASSWORD2" ] && [ -n "$WEB_PASSWORD" ] && break
    echo "❌ 两次密码不一致或为空"
  done
  WEB_PASSWORD_HASH="$("$WEB_VENV/bin/python" - <<PY
from werkzeug.security import generate_password_hash
print(generate_password_hash(${WEB_PASSWORD@Q}))
PY
)"
  WEB_SECRET_KEY="$("$WEB_VENV/bin/python" - <<'PY'
import secrets
print(secrets.token_hex(32))
PY
)"
  cat > "$WEB_ENV" <<EOF
WEB_USERNAME="$WEB_USERNAME"
WEB_PASSWORD_HASH="$WEB_PASSWORD_HASH"
WEB_SECRET_KEY="$WEB_SECRET_KEY"
WEB_HOST="0.0.0.0"
WEB_PORT="8899"
DATABASE_PATH="${APP_DIR}/servers.db"
APP_DIR="${APP_DIR}"
METRICS_PORT="8765"
EOF
  systemctl restart "$SERVICE_NAME" 2>/dev/null || true
  echo "✅ Web 登录账号密码已更新"
}
uninstall_web(){
  banner; echo "⚠️ 即将卸载 Web 面板，不删除主机器人和 servers.db 数据库。"
  read -r -p "确认卸载？输入 YES 继续：" CONFIRM
  [ "$CONFIRM" = "YES" ] || { echo "已取消"; exit 0; }
  systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
  rm -f "$SERVICE_FILE" "$WEB_FILE" "$WEB_ENV"
  rm -rf "$WEB_VENV"
  systemctl daemon-reload 2>/dev/null || true
  systemctl reset-failed 2>/dev/null || true
  echo "✅ Web 面板已卸载，数据库已保留。"
}
if [ "${AUTO_INSTALL:-}" = "1" ]; then install_or_update; exit 0; fi
while true; do
  banner; echo "1) 安装 / 更新 Web 面板"; echo "2) 修改 Web 登录账号密码"; echo "0) 卸载 Web 面板"; echo "3) 退出脚本"; echo
  read -r -p "请选择：" CHOICE
  case "$CHOICE" in 1) install_or_update; exit 0;; 2) reset_password; exit 0;; 0) uninstall_web; exit 0;; 3) exit 0;; *) echo "❌ 输入错误";; esac
done
