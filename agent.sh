#!/usr/bin/env bash
set -e

APP_DIR="/opt/server-monitor-agent"
SERVICE_NAME="server-monitor-agent"

URL=""
SECRET=""
SID=""
NAME="server"
INTERVAL="2"

while [ $# -gt 0 ]; do
  case "$1" in
    --url) URL="${2:-}"; shift 2 ;;
    --secret) SECRET="${2:-}"; shift 2 ;;
    --sid) SID="${2:-}"; shift 2 ;;
    --name) NAME="${2:-server}"; shift 2 ;;
    --interval) INTERVAL="${2:-2}"; shift 2 ;;
    --token) shift 2 ;;
    --chat) shift 2 ;;
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
import os
import time
import json
import socket
import urllib.request

URL = os.environ.get("AGENT_URL", "")
SECRET = os.environ.get("AGENT_SECRET", "")
SID = os.environ.get("SERVER_ID", "")
NAME = os.environ.get("SERVER_NAME", "server")
INTERVAL = int(os.environ.get("INTERVAL", "2") or "2")

PUBLIC_IP_CACHE = {"t": 0, "v": ""}

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
        return os.cpu_count() or 0
    except Exception:
        return 0

def meminfo():
    data = {}
    try:
        for line in open("/proc/meminfo"):
            k, v = line.split(":", 1)
            data[k] = int(v.strip().split()[0]) * 1024
    except Exception:
        pass
    return data

def mem_info():
    try:
        data = meminfo()
        total = data.get("MemTotal", 0)
        avail = data.get("MemAvailable", 0)
        used = max(0, total - avail)
        percent = round(used * 100 / total, 1) if total else 0
        return total, used, percent
    except Exception:
        return 0, 0, 0

def swap_info():
    try:
        data = meminfo()
        total = data.get("SwapTotal", 0)
        free = data.get("SwapFree", 0)
        used = max(0, total - free)
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

def loads():
    try:
        a,b,c = os.getloadavg()
        return round(a, 2), round(b, 2), round(c, 2)
    except Exception:
        return 0, 0, 0


def os_name():
    try:
        data = {}
        for line in open("/etc/os-release", "r", encoding="utf-8", errors="ignore"):
            if "=" in line:
                k, v = line.rstrip().split("=", 1)
                data[k] = v.strip().strip('"')
        pretty = data.get("PRETTY_NAME") or data.get("NAME") or ""
        if pretty:
            return pretty[:120]
    except Exception:
        pass
    try:
        return os.uname().sysname + " " + os.uname().release
    except Exception:
        return ""

def public_ip():
    now = time.time()
    if PUBLIC_IP_CACHE["v"] and now - PUBLIC_IP_CACHE["t"] < 600:
        return PUBLIC_IP_CACHE["v"]
    for url in ["https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"]:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                ip = r.read().decode().strip().splitlines()[0]
                PUBLIC_IP_CACHE["t"] = now
                PUBLIC_IP_CACHE["v"] = ip
                return ip
        except Exception:
            pass
    return PUBLIC_IP_CACHE["v"]

def post_json(url, data):
    raw = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=raw, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read().decode(errors="ignore")

def collect():
    up = uptime_seconds()
    rx, tx = net_bytes()
    mem_total, mem_used, mem_percent = mem_info()
    swap_total, swap_used, swap_percent = swap_info()
    disk_total, disk_used, disk_percent = disk_info()
    load1, load5, load15 = loads()
    return {
        "secret": SECRET,
        "server_id": SID,
        "name": NAME,
        "hostname": socket.gethostname(),
        "public_ip": public_ip(),
        "os_name": os_name(),
        "uptime_seconds": up,
        "boot_time": boot_time_text(up),
        "cpu_cores": cpu_cores(),
        "cpu_percent": cpu_percent(),
        "load1": load1,
        "load5": load5,
        "load15": load15,
        "mem_total": mem_total,
        "mem_used": mem_used,
        "mem_percent": mem_percent,
        "swap_total": swap_total,
        "swap_used": swap_used,
        "swap_percent": swap_percent,
        "disk_total": disk_total,
        "disk_used": disk_used,
        "disk_percent": disk_percent,
        "rx_bytes": rx,
        "tx_bytes": tx,
        "ts": int(time.time()),
    }

def main():
    print("server-monitor-agent realtime reporter started", flush=True)
    while True:
        try:
            data = collect()
            post_json(URL, data)
            print("report ok", time.strftime("%F %T"), "sid", SID, "cpu", data["cpu_percent"], "mem", data["mem_percent"], "disk", data["disk_percent"], "rx", data["rx_bytes"], "tx", data["tx_bytes"], "load", data["load1"], data["load5"], data["load15"], "cores", data["cpu_cores"], flush=True)
        except Exception as e:
            print("report failed", e, flush=True)
        time.sleep(max(1, INTERVAL))

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
Environment="INTERVAL=$INTERVAL"
ExecStart=/usr/bin/python3 $APP_DIR/agent.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

echo "✅ 探针安装/更新完成"
echo "📌 每 ${INTERVAL} 秒上报：CPU/内存/SWAP/硬盘/网络速率基础数据/总流量/负载/运行时长"
echo "📌 查看状态：systemctl status $SERVICE_NAME --no-pager -l"
echo "📌 查看日志：journalctl -u $SERVICE_NAME -f"
