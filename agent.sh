#!/usr/bin/env bash
set -e

APP_DIR="/opt/server-monitor-agent"
SERVICE_NAME="server-monitor-agent"
URL=""
SECRET=""
SID=""
NAME="server"
BOT_TOKEN=""
CHAT_ID=""
INTERVAL="60"

while [ $# -gt 0 ]; do
  case "$1" in
    --url) URL="$2"; shift 2 ;;
    --secret) SECRET="$2"; shift 2 ;;
    --sid) SID="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --token) BOT_TOKEN="$2"; shift 2 ;;
    --chat) CHAT_ID="$2"; shift 2 ;;
    --interval) INTERVAL="$2"; shift 2 ;;
    *) shift ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  echo "❌ 请使用 root 执行"
  exit 1
fi

if [ -z "$URL" ] || [ -z "$SECRET" ] || [ -z "$SID" ]; then
  echo "❌ 缺少参数：--url --secret --sid"
  exit 1
fi

mkdir -p "$APP_DIR"

cat > "$APP_DIR/agent.py" <<'PY'
#!/usr/bin/env python3
import os, time, json, socket, urllib.request, subprocess

URL = os.environ.get("AGENT_URL", "")
SECRET = os.environ.get("AGENT_SECRET", "")
SID = os.environ.get("SERVER_ID", "")
NAME = os.environ.get("SERVER_NAME", "server")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
INTERVAL = int(os.environ.get("INTERVAL", "60"))

def sh(cmd):
    try:
        return subprocess.check_output(cmd, shell=True, stderr=subprocess.DEVNULL, timeout=8).decode(errors="ignore").strip()
    except Exception:
        return ""

def uptime_seconds():
    try:
        return int(float(open("/proc/uptime").read().split()[0]))
    except Exception:
        return 0

def boot_time_text(up):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - int(up)))
    except Exception:
        return ""

def cpu_cores():
    try:
        return os.cpu_count() or int(sh("nproc") or "0")
    except Exception:
        return 0

def mem_info():
    try:
        data = {}
        for line in open("/proc/meminfo"):
            k, v = line.split(":", 1)
            data[k] = int(v.strip().split()[0]) * 1024
        total = data.get("MemTotal", 0)
        avail = data.get("MemAvailable", 0)
        used = max(0, total - avail)
        percent = round(used * 100 / total, 1) if total else 0
        return total, used, percent
    except Exception:
        return 0, 0, 0

def disk_info():
    try:
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = max(0, total - free)
        percent = round(used * 100 / total, 1) if total else 0
        return total, used, percent
    except Exception:
        return 0, 0, 0

def cpu_percent():
    def read():
        vals = list(map(int, open("/proc/stat").readline().split()[1:]))
        idle = vals[3] + vals[4]
        total = sum(vals)
        return idle, total
    try:
        i1, t1 = read()
        time.sleep(0.2)
        i2, t2 = read()
        total = t2 - t1
        idle = i2 - i1
        return round((1 - idle / total) * 100, 1) if total else 0
    except Exception:
        return 0

def net_bytes():
    rx = tx = 0
    try:
        for line in open("/proc/net/dev").read().splitlines()[2:]:
            iface, rest = line.split(":", 1)
            iface = iface.strip()
            if iface == "lo":
                continue
            vals = rest.split()
            rx += int(vals[0])
            tx += int(vals[8])
    except Exception:
        pass
    return rx, tx

def public_ip():
    for url in ["https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"]:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                return r.read().decode().strip().splitlines()[0]
        except Exception:
            pass
    return ""

def post_json(url, data):
    raw = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=raw, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read().decode(errors="ignore")

def tg_send(text):
    if not BOT_TOKEN or not CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        data = json.dumps({"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=8).read()
    except Exception:
        pass

def collect():
    up = uptime_seconds()
    rx, tx = net_bytes()
    mem_total, mem_used, mem_percent = mem_info()
    disk_total, disk_used, disk_percent = disk_info()
    return {
        "secret": SECRET,
        "server_id": SID,
        "name": NAME,
        "hostname": socket.gethostname(),
        "public_ip": public_ip(),
        "uptime_seconds": up,
        "boot_time": boot_time_text(up),
        "cpu_cores": cpu_cores(),
        "cpu_percent": cpu_percent(),
        "mem_total": mem_total,
        "mem_used": mem_used,
        "mem_percent": mem_percent,
        "disk_total": disk_total,
        "disk_used": disk_used,
        "disk_percent": disk_percent,
        "rx_bytes": rx,
        "tx_bytes": tx,
        "ts": int(time.time()),
    }

def main():
    tg_send(f"✅📡 <b>探针已启动</b>\n\n🖥️ {NAME}\n🆔 ID：{SID}\n⚙️ 已启用配置上报：CPU核心 / 内存总量 / 硬盘总量\n🌐 上报地址：<code>{URL}</code>")
    while True:
        try:
            data = collect()
            post_json(URL, data)
            print("report ok", time.strftime("%F %T"), "cores", data.get("cpu_cores"), "mem", data.get("mem_total"), "disk", data.get("disk_total"), flush=True)
        except Exception as e:
            print("report failed", e, flush=True)
        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()
PY

chmod +x "$APP_DIR/agent.py"

cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=Server Monitor Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment="AGENT_URL=$URL"
Environment="AGENT_SECRET=$SECRET"
Environment="SERVER_ID=$SID"
Environment="SERVER_NAME=$NAME"
Environment="BOT_TOKEN=$BOT_TOKEN"
Environment="CHAT_ID=$CHAT_ID"
Environment="INTERVAL=$INTERVAL"
ExecStart=/usr/bin/python3 $APP_DIR/agent.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

echo "✅ 探针安装/更新完成"
echo "📌 查看状态：systemctl status $SERVICE_NAME --no-pager -l"
echo "📌 查看日志：journalctl -u $SERVICE_NAME -f"
