#!/usr/bin/env bash
set -e
systemctl disable --now server-monitor-web 2>/dev/null || true
rm -f /etc/systemd/system/server-monitor-web.service
systemctl daemon-reload 2>/dev/null || true
rm -f /opt/server-monitor-bot/web_dashboard.py /opt/server-monitor-bot/.env.web
rm -rf /opt/server-monitor-bot/web-venv
echo "已卸载 Web 面板，servers.db 保留"
