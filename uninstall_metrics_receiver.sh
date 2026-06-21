#!/usr/bin/env bash
set -e
systemctl disable --now server-monitor-metrics 2>/dev/null || true
rm -f /etc/systemd/system/server-monitor-metrics.service
rm -f /opt/server-monitor-bot/metrics_receiver.py
systemctl daemon-reload
echo "✅ Metrics 接收服务已卸载"
