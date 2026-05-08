#!/usr/bin/env python3
import os
import time
import html
import json
import re
import sqlite3
import socket
import subprocess
import ipaddress
from datetime import datetime, timedelta

import psutil
import requests
from dateutil.parser import parse as parse_date
from dateutil.relativedelta import relativedelta

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = set(x.strip() for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip())

APP_DIR = "/opt/server-monitor-bot"
DB_PATH = f"{APP_DIR}/servers.db"
API = f"https://api.telegram.org/bot{BOT_TOKEN}"

CHECK_INTERVAL = 60
DUE_REMIND_DAYS = [30, 14, 7, 3, 1, 0]
CPU_ALERT = 90
MEM_ALERT = 90
DISK_ALERT = 90

LOCAL_META_CACHE = {"time": 0, "data": None}
LOCAL_META_TTL = 600


def h(v):
    return html.escape(str(v if v is not None else ""))


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_column(conn, table, column, definition):
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db():
    os.makedirs(APP_DIR, exist_ok=True)
    conn = db()
    conn.execute("""
    CREATE TABLE IF NOT EXISTS servers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        host TEXT NOT NULL,
        note TEXT DEFAULT '',
        cycle TEXT DEFAULT 'monthly',
        price REAL DEFAULT 0,
        currency TEXT DEFAULT 'USD',
        expire_at TEXT NOT NULL,
        check_port INTEGER DEFAULT 22,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)
    for col, definition in [
        ("country", "TEXT DEFAULT ''"),
        ("country_code", "TEXT DEFAULT ''"),
        ("region", "TEXT DEFAULT ''"),
        ("city", "TEXT DEFAULT ''"),
        ("isp", "TEXT DEFAULT ''"),
        ("os_name", "TEXT DEFAULT ''"),
        ("last_meta_at", "TEXT DEFAULT ''"),
        ("free_forever", "INTEGER DEFAULT 0"),
        ("auto_renew", "INTEGER DEFAULT 0"),
    ]:
        ensure_column(conn, "servers", col, definition)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS reminders (
        server_id INTEGER,
        remind_key TEXT,
        sent_at TEXT,
        PRIMARY KEY(server_id, remind_key)
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS server_status (
        server_id INTEGER PRIMARY KEY,
        last_status TEXT DEFAULT 'unknown',
        last_checked_at TEXT,
        last_changed_at TEXT
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS alerts (
        alert_key TEXT PRIMARY KEY,
        sent_at TEXT
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT DEFAULT '',
        title TEXT DEFAULT '',
        content TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS local_profile (
        key TEXT PRIMARY KEY,
        value TEXT DEFAULT ''
    )
    """)
    for k, v in {
        "name": socket.gethostname(),
        "note": "",
        "cycle": "monthly",
        "price": "0",
        "currency": "USD",
        "expire_at": "",
    }.items():
        conn.execute("INSERT OR IGNORE INTO local_profile(key, value) VALUES(?,?)", (k, v))
    conn.commit()
    conn.close()


def tg(method, payload=None):
    try:
        return requests.post(f"{API}/{method}", json=payload or {}, timeout=20).json()
    except Exception:
        return {}


def menu_keyboard():
    return {
        "keyboard": [
            [{"text": "📊 服务器总览"}, {"text": "📋 查看服务器"}],
            [{"text": "🖥️ 查看状态"}, {"text": "🌐 查看流量"}, {"text": "💾 查看磁盘"}],
            [{"text": "🧾 添加服务器"}, {"text": "✏️ 编辑服务器"}, {"text": "🖥️ 编辑本机"}],
            [{"text": "📡 检测服务器"}, {"text": "📋 选择服务器"}],
            [{"text": "🧾 查看事件"}, {"text": "🛡️ 安全状态"}, {"text": "🔐 登录记录"}],
            [{"text": "🚫 防爆破状态"}, {"text": "🔄 重启节点"}, {"text": "🧹 清理缓存"}],
            [{"text": "🌍 刷新本机地区"}, {"text": "🌍 刷新全部地区"}],
            [{"text": "⚙️ 启用命令"}, {"text": "⌨️ 收起键盘"}],
            [{"text": "❓ 帮助"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
        "one_time_keyboard": False,
        "input_field_placeholder": "请选择功能或直接输入中文命令"
    }


def remove_keyboard_markup():
    return {"remove_keyboard": True}


def send(chat_id, text, keyboard=True):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    if keyboard:
        payload["reply_markup"] = menu_keyboard()
    return tg("sendMessage", payload)


def send_inline(chat_id, text, inline_keyboard):
    return tg("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {"inline_keyboard": inline_keyboard}
    })


def edit_inline_message(chat_id, message_id, text, inline_keyboard=None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if inline_keyboard is not None:
        payload["reply_markup"] = {"inline_keyboard": inline_keyboard}
    return tg("editMessageText", payload)


def answer_callback(callback_id, text=""):
    return tg("answerCallbackQuery", {"callback_query_id": callback_id, "text": text, "show_alert": False})


def send_hide_keyboard(chat_id):
    return tg("sendMessage", {
        "chat_id": chat_id,
        "text": "✅⌨️ <b>中文按钮键盘已收起</b>\n\n需要重新显示时，发送：<code>启用命令</code>",
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": remove_keyboard_markup()
    })


def clean_command_text(text):
    text = (text or "").strip()
    # 允许按钮带 Emoji，例如“📊 服务器总览”，处理时自动去掉前面的表情。
    text = re.sub(r"^[^0-9A-Za-z_\u4e00-\u9fff/]+", "", text).strip()
    return text


def one_line(value, default=""):
    """用于显示和保存名称/价格/周期等单行字段，防止多行命令被误存进去。"""
    value = str(value if value is not None else "").strip()
    if not value:
        return default
    first = value.splitlines()[0].strip()
    return first or default


def split_command_lines(text):
    """把一次性粘贴的多条编辑命令拆成多行命令。"""
    return [clean_command_text(x) for x in str(text or "").splitlines() if clean_command_text(x)]


LOCAL_EDIT_PREFIXES = ("编辑本机名称", "编辑本机备注", "编辑本机到期", "编辑本机价格", "编辑本机周期", "本机续费")
SERVER_EDIT_PREFIXES = ("编辑备注", "编辑到期", "编辑价格", "编辑周期", "编辑端口", "编辑名称", "编辑系统", "编辑永久", "编辑自动续费", "续费服务器", "刷新地区")


def is_batch_edit_text(text):
    lines = split_command_lines(text)
    if len(lines) <= 1:
        return False
    prefixes = LOCAL_EDIT_PREFIXES + SERVER_EDIT_PREFIXES
    return all(line.startswith(prefixes) for line in lines)


def send_long(chat_id, text):
    text = text or ""
    while len(text) > 3900:
        send(chat_id, text[:3900])
        text = text[3900:]
    if text:
        send(chat_id, text)


def broadcast(text):
    for admin in ADMIN_IDS:
        send(admin, text)


def is_admin(chat_id):
    return str(chat_id) in ADMIN_IDS


def shell(cmd, timeout=10):
    try:
        out = subprocess.check_output(cmd, shell=True, stderr=subprocess.STDOUT, timeout=timeout)
        return out.decode("utf-8", errors="ignore").strip()
    except subprocess.CalledProcessError as e:
        return e.output.decode("utf-8", errors="ignore").strip()
    except Exception as e:
        return str(e)


def root_cmd(cmd):
    if os.geteuid() == 0:
        return cmd
    if shell("command -v sudo >/dev/null 2>&1 && echo yes || echo no", 3) == "yes":
        return "sudo " + cmd
    return cmd


def fmt_size(n):
    try:
        n = float(n)
    except Exception:
        return "未知"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    for unit in units:
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def uptime_text():
    seconds = int(time.time() - psutil.boot_time())
    days = seconds // 86400
    hours = seconds % 86400 // 3600
    minutes = seconds % 3600 // 60
    return f"{days} 天 {hours} 小时 {minutes} 分钟"


def local_os_name():
    try:
        data = {}
        with open("/etc/os-release", "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    data[k] = v.strip('"')
        return data.get("PRETTY_NAME") or data.get("NAME") or "未知系统"
    except Exception:
        return shell("uname -srm", 5) or "未知系统"


def get_local_status():
    cpu = psutil.cpu_percent(interval=1)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    load1, load5, load15 = os.getloadavg()
    meta = detect_local_meta()
    return {
        "hostname": socket.gethostname(),
        "os": local_os_name(),
        "public_ip": meta.get("ip", "未知"),
        "country": meta.get("country", "未知"),
        "country_code": meta.get("country_code", ""),
        "region": meta.get("region", ""),
        "city": meta.get("city", ""),
        "isp": meta.get("isp", ""),
        "flag": meta.get("flag") or country_flag(meta.get("country_code", "")),
        "uptime": uptime_text(),
        "cpu": cpu,
        "cpu_count": psutil.cpu_count(),
        "load1": load1,
        "load5": load5,
        "load15": load15,
        "mem_used": mem.used,
        "mem_total": mem.total,
        "mem_percent": mem.percent,
        "disk_used": disk.used,
        "disk_total": disk.total,
        "disk_percent": disk.percent
    }

def get_traffic_snapshot():
    return psutil.net_io_counters(), psutil.net_io_counters(pernic=True)


def traffic_detail():
    total1, pernic1 = get_traffic_snapshot()
    time.sleep(1)
    total2, pernic2 = get_traffic_snapshot()
    interfaces = []
    for name, n2 in pernic2.items():
        if name == "lo":
            continue
        n1 = pernic1.get(name)
        if not n1:
            continue
        interfaces.append({
            "name": name,
            "recv": n2.bytes_recv,
            "sent": n2.bytes_sent,
            "down_speed": n2.bytes_recv - n1.bytes_recv,
            "up_speed": n2.bytes_sent - n1.bytes_sent,
            "packets_recv": n2.packets_recv,
            "packets_sent": n2.packets_sent
        })
    interfaces.sort(key=lambda x: x["recv"] + x["sent"], reverse=True)
    return {
        "total_recv": total2.bytes_recv,
        "total_sent": total2.bytes_sent,
        "realtime_down": total2.bytes_recv - total1.bytes_recv,
        "realtime_up": total2.bytes_sent - total1.bytes_sent,
        "interfaces": interfaces
    }


def check_tcp(host, port, timeout=5):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, int(port)))
        s.close()
        return True
    except Exception:
        return False


def country_flag(code):
    code = (code or "").upper().strip()
    if len(code) != 2 or not code.isalpha():
        return "🌐"
    return chr(ord(code[0]) + 127397) + chr(ord(code[1]) + 127397)


def resolve_ip(host):
    try:
        return socket.gethostbyname(host)
    except Exception:
        return host


def is_private_ip(ip):
    try:
        obj = ipaddress.ip_address(ip)
        return obj.is_private or obj.is_loopback or obj.is_link_local
    except Exception:
        return False


def detect_server_meta(host):
    ip = resolve_ip(host)
    if is_private_ip(ip):
        return {
            "country": "本机/内网",
            "country_code": "",
            "region": "内网",
            "city": "内网",
            "isp": "内网地址",
            "flag": "🏠"
        }
    urls = [
        f"http://ip-api.com/json/{ip}?fields=status,country,countryCode,regionName,city,isp,org,query",
        f"https://ipapi.co/{ip}/json/"
    ]
    for url in urls:
        try:
            r = requests.get(url, timeout=8)
            if not r.ok:
                continue
            j = r.json()
            if j.get("status") == "fail":
                continue
            country = j.get("country") or j.get("country_name") or "未知"
            code = j.get("countryCode") or j.get("country_code") or ""
            region = j.get("regionName") or j.get("region") or ""
            city = j.get("city") or ""
            isp = j.get("isp") or j.get("org") or j.get("asn") or ""
            return {
                "country": country,
                "country_code": code,
                "region": region,
                "city": city,
                "isp": isp,
                "flag": country_flag(code)
            }
        except Exception:
            pass
    return {"country": "未知", "country_code": "", "region": "", "city": "", "isp": "", "flag": "🌐"}



def get_public_ip():
    """获取当前部署机器的公网 IP，优先 IPv4，失败后再尝试 requests。"""
    cmds = [
        "curl -4 -s --max-time 8 https://api.ipify.org",
        "curl -4 -s --max-time 8 https://ifconfig.me/ip",
        "curl -4 -s --max-time 8 https://icanhazip.com",
    ]

    for cmd in cmds:
        try:
            ip = shell(cmd, 10).strip().splitlines()[0].strip()
            ipaddress.ip_address(ip)
            return ip
        except Exception:
            pass

    urls = [
        "https://api.ipify.org?format=json",
        "https://ifconfig.me/ip",
        "https://icanhazip.com",
        "https://ipinfo.io/ip",
    ]

    for url in urls:
        try:
            r = requests.get(url, timeout=8, headers={"User-Agent": "server-monitor-bot"})
            if not r.ok:
                continue
            text = r.text.strip()
            if "json" in r.headers.get("content-type", "") or text.startswith("{"):
                j = r.json()
                ip = j.get("ip") or j.get("query") or ""
            else:
                ip = text.splitlines()[0].strip()
            ipaddress.ip_address(ip)
            return ip
        except Exception:
            pass

    return ""


def detect_local_meta(force=False):
    """识别当前部署机器的公网 IP、国家、地区、城市、运营商和国旗。"""
    now_ts = time.time()
    if not force and LOCAL_META_CACHE["data"] and now_ts - LOCAL_META_CACHE["time"] < LOCAL_META_TTL:
        return LOCAL_META_CACHE["data"]

    public_ip = get_public_ip()
    if public_ip:
        meta = detect_server_meta(public_ip)
        meta["ip"] = public_ip
    else:
        meta = {
            "ip": "未知",
            "country": "未知",
            "country_code": "",
            "region": "",
            "city": "",
            "isp": "",
            "flag": "🌐",
        }

    LOCAL_META_CACHE["time"] = now_ts
    LOCAL_META_CACHE["data"] = meta
    return meta


def local_location_line(meta):
    place = " ".join(x for x in [meta.get("country"), meta.get("region"), meta.get("city")] if x and x != "未知")
    return f"{meta.get('flag') or country_flag(meta.get('country_code'))} {h(place or meta.get('country') or '未知')}"


def cmd_refresh_local_meta(chat_id):
    meta = detect_local_meta(force=True)
    send(chat_id, (
        "✅🌍 <b>本机国家地区已刷新</b> 🌍✅\n\n"
        "━━━━━━━━━━━━━━\n"
        f"🌐 <b>公网 IP：</b><code>{h(meta.get('ip', '未知'))}</code>\n"
        f"📍 <b>国家地区：</b>{local_location_line(meta)}\n"
        f"🏢 <b>运营商：</b>{h(meta.get('isp') or '未知')}\n"
        f"🕒 <b>刷新时间：</b>{now_text()}"
    ))

def refresh_server_meta_row(conn, row):
    meta = detect_server_meta(row["host"])
    conn.execute(
        "UPDATE servers SET country=?, country_code=?, region=?, city=?, isp=?, last_meta_at=? WHERE id=?",
        (meta["country"], meta["country_code"], meta["region"], meta["city"], meta["isp"], now_text(), row["id"])
    )
    return meta


def refresh_missing_meta(limit=20):
    try:
        conn = db()
        rows = conn.execute(
            "SELECT * FROM servers WHERE country IS NULL OR country='' OR country='未知' ORDER BY id ASC LIMIT ?",
            (limit,)
        ).fetchall()
        for r in rows:
            refresh_server_meta_row(conn, r)
        conn.commit()
        conn.close()
    except Exception:
        pass


def cmd_refresh_all_meta(chat_id):
    conn = db()
    rows = conn.execute("SELECT * FROM servers ORDER BY id ASC").fetchall()
    if not rows:
        conn.close()
        send(chat_id, "📭 暂无服务器记录，无法刷新地区。")
        return
    ok = 0
    lines = ["🌍✨ <b>国家地区信息已刷新</b> ✨🌍", f"🕒 更新时间：{now_text()}", ""]
    for r in rows:
        meta = refresh_server_meta_row(conn, r)
        ok += 1
        lines.append(f"{meta['flag']} <b>{h(r['name'])}</b>｜{h(meta['country'])} {h(meta['region'])} {h(meta['city'])}｜{h(meta['isp'] or '未知运营商')}")
    conn.commit()
    conn.close()
    event_add("action", "刷新全部地区", f"已刷新 {ok} 台服务器的国家地区/运营商信息")
    send_long(chat_id, "\n".join(lines)[:3900])


def detect_ssh_banner(host, port):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(4)
        s.connect((host, int(port)))
        banner = s.recv(120).decode("utf-8", errors="ignore").strip()
        s.close()
        if banner:
            return banner[:80]
    except Exception:
        pass
    return "未知"


def cycle_name(cycle):
    return {"monthly": "📆 月付", "quarterly": "🗓️ 季付", "yearly": "📅 年付"}.get(cycle, cycle)


def currency_name(currency):
    return {"CNY": "🇨🇳 ¥", "USD": "🇺🇸 $", "EUR": "🇪🇺 €", "GBP": "🇬🇧 £"}.get(currency, currency)


def normalize_cycle(cycle):
    cycle = str(cycle).lower().strip()
    return {
        "月付": "monthly", "月": "monthly", "monthly": "monthly", "month": "monthly",
        "季付": "quarterly", "季": "quarterly", "quarterly": "quarterly", "quarter": "quarterly",
        "年付": "yearly", "年": "yearly", "yearly": "yearly", "year": "yearly", "annual": "yearly",
    }.get(cycle, cycle)


def normalize_currency(currency):
    raw = str(currency).strip()
    upper = raw.upper()
    return {"人民币": "CNY", "RMB": "CNY", "¥": "CNY", "美元": "USD", "$": "USD", "欧元": "EUR", "€": "EUR", "英镑": "GBP", "£": "GBP"}.get(raw, upper)




def truthy(v):
    v = str(v if v is not None else "").strip().lower()
    return v in ["1", "true", "yes", "y", "on", "是", "开启", "打开", "启用", "永久", "永久免费", "免费"]


def bool_text(v, yes="✅ 是", no="❌ 否"):
    return yes if truthy(v) else no


def is_free_forever_row(r):
    try:
        return truthy(r["free_forever"])
    except Exception:
        return False


def is_auto_renew_row(r):
    try:
        return truthy(r["auto_renew"])
    except Exception:
        return False


def server_price_line(r):
    if is_free_forever_row(r):
        return "🎁 永久免费"
    return f"{currency_name(r['currency'])} {r['price']:g} {r['currency']}"


def cycle_months(cycle):
    cycle = normalize_cycle(cycle)
    if cycle == "quarterly":
        return 3
    if cycle == "yearly":
        return 12
    return 1


def is_valid_date_text(value):
    value = str(value or "").strip()
    if not value or value in ["永久", "永久免费", "未设置", "无", "none", "None", "-"]:
        return False
    try:
        parse_date(value)
        return True
    except Exception:
        return False


def next_natural_expire(expire_at, cycle):
    today = datetime.now().date()
    months = cycle_months(cycle)
    try:
        base = parse_date(expire_at).date()
    except Exception:
        base = today
    while base <= today:
        base = base + relativedelta(months=months)
    return base.strftime("%Y-%m-%d")


def add_builder_keyboard(mask=13):
    # bits: 1备注 2系统 4付费/到期 8检测端口 16永久免费 32自动续费
    items = [(1, "📝 备注"), (2, "🧬 系统"), (4, "💰 付费/到期"), (8, "🔌 检测端口"), (16, "🎁 永久免费"), (32, "🔁 自动续费")]
    rows = []
    for bit, label in items:
        mark = "✅" if (mask & bit) else "⬜"
        rows.append([{"text": f"{mark} {label}", "callback_data": f"add_toggle:{mask}:{bit}"}])
    rows.append([{"text": "📋 生成添加模板", "callback_data": f"add_template:{mask}"}])
    rows.append([{"text": "❓ 字段说明", "callback_data": "add_help_fields"}])
    return rows


def add_template_text(mask=13):
    free = bool(mask & 16)
    lines = ["添加服务器", "名称: HK-Oracle", "主机: 1.2.3.4"]
    if mask & 1:
        lines.append("备注: 香港甲骨文 免费机器")
    if mask & 2:
        lines.append("系统: Ubuntu 22.04")
    else:
        lines.append("# 系统未勾选：机器人会尝试自动识别")
    if free:
        lines.append("永久免费: 是")
    elif mask & 4:
        lines.extend(["周期: 年付", "价格: 0", "币种: USD", "到期: 2026-08-01"])
    if mask & 8:
        lines.append("检测端口: 22")
    if (mask & 32) and not free:
        lines.append("自动续费: 是")
    return "\n".join(lines)


def cmd_add_builder(chat_id, mask=13):
    send_inline(chat_id, (
        "🧾✨ <b>添加服务器字段选择</b> ✨🧾\n\n"
        "✅ 勾选哪个字段，生成模板就带哪个字段。\n"
        "⬜ 不勾选的字段不会出现在模板里。\n\n"
        "📌 <b>必填字段：</b>名称、主机\n"
        "🧬 系统不勾选：会尝试自动识别 SSH Banner。\n"
        "🎁 永久免费：不需要价格、币种、到期时间，也不会触发到期提醒。\n"
        "🔁 自动续费：服务器过期且仍在线时，自动顺延到下个自然周期。"
    ), add_builder_keyboard(mask))

def service_cn(status):
    status = str(status).strip()
    return {"active": "✅ 运行中", "inactive": "⚠️ 未运行", "failed": "🚨 运行失败", "unknown": "❓ 未知", "": "❓ 未检测到"}.get(status, status or "❓ 未知")


def event_add(event_type, title, content):
    try:
        conn = db()
        conn.execute("INSERT INTO events(event_type, title, content, created_at) VALUES(?,?,?,?)", (event_type, title, content, now_text()))
        conn.commit()
        conn.close()
    except Exception:
        pass


def push_event(event_type, title, content):
    event_add(event_type, title, content)
    broadcast(content)


def get_recent_events(limit=8):
    conn = db()
    rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return rows




def get_local_profile():
    conn = db()
    rows = conn.execute("SELECT key, value FROM local_profile").fetchall()
    data = {r["key"]: r["value"] for r in rows}

    data.setdefault("name", socket.gethostname())
    data.setdefault("note", "")
    data.setdefault("cycle", "monthly")
    data.setdefault("price", "0")
    data.setdefault("currency", "USD")
    data.setdefault("expire_at", "")

    # 自动修复旧版本误操作：如果用户一次粘贴多行“编辑本机...”命令，旧代码会把后续命令全部存进 name。
    # 这里会自动把 name 保留第一行，并把后面的编辑命令补写到对应字段。
    updates = {}
    name_raw = str(data.get("name") or "")
    if "\n" in name_raw or "\r" in name_raw:
        lines = split_command_lines(name_raw)
        if lines:
            updates["name"] = one_line(lines[0], socket.gethostname())
            for line in lines[1:]:
                try:
                    if line.startswith("编辑本机名称"):
                        v = one_line(line.replace("编辑本机名称", "", 1), "")
                        if v:
                            updates["name"] = v
                    elif line.startswith("编辑本机备注"):
                        updates["note"] = one_line(line.replace("编辑本机备注", "", 1), "")
                    elif line.startswith("编辑本机到期"):
                        v = one_line(line.replace("编辑本机到期", "", 1), "")
                        parse_date(v)
                        updates["expire_at"] = v
                    elif line.startswith("本机续费"):
                        v = one_line(line.replace("本机续费", "", 1), "")
                        parse_date(v)
                        updates["expire_at"] = v
                    elif line.startswith("编辑本机周期"):
                        v = normalize_cycle(one_line(line.replace("编辑本机周期", "", 1), ""))
                        if v in ["monthly", "quarterly", "yearly"]:
                            updates["cycle"] = v
                    elif line.startswith("编辑本机价格"):
                        parts = one_line(line.replace("编辑本机价格", "", 1), "").split()
                        if parts:
                            float(parts[0])
                            updates["price"] = parts[0]
                            if len(parts) >= 2:
                                cur = normalize_currency(parts[1])
                                if cur in ["CNY", "USD", "EUR", "GBP"]:
                                    updates["currency"] = cur
                except Exception:
                    pass

    if updates:
        for k, v in updates.items():
            conn.execute("INSERT OR REPLACE INTO local_profile(key, value) VALUES(?,?)", (k, str(v)))
            data[k] = str(v)
        conn.commit()

    conn.close()
    return data


def set_local_profile_value(key, value):
    conn = db()
    conn.execute("INSERT OR REPLACE INTO local_profile(key, value) VALUES(?,?)", (key, str(value)))
    conn.commit()
    conn.close()


def local_expire_text(profile):
    exp = (profile.get("expire_at") or "").strip()
    if not exp:
        return "未设置"
    try:
        days = (parse_date(exp).date() - datetime.now().date()).days
        if days < 0:
            return f"{h(exp)}｜🚨 已过期 {abs(days)} 天"
        if days == 0:
            return f"{h(exp)}｜🚨 今天到期"
        if days <= 7:
            return f"{h(exp)}｜⚠️ 剩余 {days} 天"
        if days <= 30:
            return f"{h(exp)}｜⏰ 剩余 {days} 天"
        return f"{h(exp)}｜✅ 剩余 {days} 天"
    except Exception:
        return h(exp)


def local_profile_lines():
    p = get_local_profile()
    name = one_line(p.get('name'), socket.gethostname())
    note = one_line(p.get('note'), '无')
    cycle = normalize_cycle(one_line(p.get('cycle'), 'monthly'))
    if cycle not in ["monthly", "quarterly", "yearly"]:
        cycle = "monthly"
    currency = normalize_currency(one_line(p.get('currency'), 'USD'))
    if currency not in ["CNY", "USD", "EUR", "GBP"]:
        currency = "USD"
    price = one_line(p.get('price'), '0')
    return (
        f"🏷️ 管理名称：{h(name)}\n"
        f"📝 本机备注：{h(note)}\n"
        f"🔁 付费周期：{cycle_name(cycle)}\n"
        f"💰 付费价格：{currency_name(currency)} {h(price)} {h(currency)}\n"
        f"📆 到期时间：{local_expire_text(p)}"
    )


def cmd_local_edit_help(chat_id):
    send(chat_id, """
🖥️✏️ <b>编辑当前机器资料</b> ✨

当前机器状态里的名称、备注、付费周期、价格、到期时间都可以编辑。

直接发送下面任意一种：

<code>编辑本机名称 Oracle主控机</code>
<code>编辑本机备注 新加坡主控节点</code>
<code>编辑本机到期 2027-05-01</code>
<code>编辑本机价格 38 CNY</code>
<code>编辑本机周期 年付</code>
<code>本机续费 2027-05-01</code>

📌 可以一次粘贴多条编辑命令，机器人会逐条处理。
📌 编辑后发送 <code>查看状态</code> 或 <code>服务器总览</code> 查看。 
""".strip())


def cmd_edit_local(chat_id, text):
    # 本函数只处理一条编辑命令；多行批量编辑由 handle() 先拆分。
    text = one_line(text.strip())
    try:
        if text.startswith("编辑本机名称"):
            val = text.replace("编辑本机名称", "", 1).strip()
            if not val: raise ValueError("名称不能为空")
            set_local_profile_value("name", val)
            field = "本机名称"
        elif text.startswith("编辑本机备注"):
            val = text.replace("编辑本机备注", "", 1).strip()
            set_local_profile_value("note", val)
            field = "本机备注"
        elif text.startswith("编辑本机到期"):
            val = text.replace("编辑本机到期", "", 1).strip()
            parse_date(val)
            set_local_profile_value("expire_at", val)
            field = "本机到期"
        elif text.startswith("本机续费"):
            val = text.replace("本机续费", "", 1).strip()
            parse_date(val)
            set_local_profile_value("expire_at", val)
            field = "本机续费到期"
        elif text.startswith("编辑本机周期"):
            val = normalize_cycle(text.replace("编辑本机周期", "", 1).strip())
            if val not in ["monthly", "quarterly", "yearly"]:
                raise ValueError("周期只支持：月付 / 季付 / 年付")
            set_local_profile_value("cycle", val)
            field = "本机周期"
        elif text.startswith("编辑本机价格"):
            val = text.replace("编辑本机价格", "", 1).strip()
            parts = val.split()
            if not parts:
                raise ValueError("价格不能为空")
            float(parts[0])
            set_local_profile_value("price", parts[0])
            if len(parts) >= 2:
                cur = normalize_currency(parts[1])
                if cur not in ["CNY", "USD", "EUR", "GBP"]:
                    raise ValueError("币种只支持 CNY / USD / EUR / GBP")
                set_local_profile_value("currency", cur)
            field = "本机价格"
        else:
            cmd_local_edit_help(chat_id)
            return
        event_add("action", "编辑当前机器", f"已更新：{field}")
        send(chat_id, f"✅🖥️ <b>{h(field)}编辑成功</b>\n\n{local_profile_lines()}")
    except Exception as e:
        send(chat_id, f"❌ 编辑本机失败：{h(e)}")


def get_server_row(sid):
    conn = db()
    row = conn.execute("SELECT * FROM servers WHERE id=?", (sid,)).fetchone()
    conn.close()
    return row


def expire_status_text(expire_at, free_forever=False):
    if truthy(free_forever) or str(expire_at or "").strip() in ["永久", "永久免费"]:
        return "🎁 永久免费"
    if not str(expire_at or "").strip():
        return "未设置"
    try:
        exp = parse_date(expire_at).date()
        days = (exp - datetime.now().date()).days
        if days < 0: return f"🚨 已过期 {abs(days)} 天"
        if days == 0: return "🚨 今天到期"
        if days <= 7: return f"⚠️ 剩余 {days} 天"
        if days <= 30: return f"⏰ 剩余 {days} 天"
        return f"✅ 剩余 {days} 天"
    except Exception:
        return "未知"

def server_detail_text(r):
    online = check_tcp(r["host"], r["check_port"])
    status_text = "🟢 在线" if online else "🔴 离线"
    free = is_free_forever_row(r)
    auto = is_auto_renew_row(r)
    return (
        "🖥️✨ <b>服务器详细信息</b> ✨🖥️\n"
        f"🕒 更新时间：{now_text()}\n\n"
        "━━━━━━━━━━━━━━\n"
        f"📡 <b>状态：</b>{status_text}\n"
        f"🆔 <b>ID：</b><code>{r['id']}</code>\n"
        f"🖥️ <b>名称：</b>{h(r['name'])}\n"
        f"🌐 <b>主机：</b><code>{h(r['host'])}</code>\n"
        f"📍 <b>地区：</b>{server_location_line(r)}\n"
        f"🏢 <b>运营商：</b>{h(r['isp'] if 'isp' in r.keys() and r['isp'] else '未知')}\n"
        f"🧬 <b>系统：</b>{h(r['os_name'] if 'os_name' in r.keys() and r['os_name'] else '未知系统')}\n"
        f"🔌 <b>检测端口：</b>{h(r['check_port'])}\n"
        f"📝 <b>备注：</b>{h(r['note'] or '无')}\n"
        f"🎁 <b>永久免费：</b>{bool_text(free)}\n"
        f"🔁 <b>自动续费：</b>{bool_text(auto)}\n"
        f"🔁 <b>周期：</b>{cycle_name(r['cycle'])}\n"
        f"💰 <b>价格：</b>{server_price_line(r)}\n"
        f"📆 <b>到期：</b>{h(r['expire_at'] if r['expire_at'] else '未设置')}\n"
        f"⏳ <b>到期状态：</b>{expire_status_text(r['expire_at'], free)}\n"
        "━━━━━━━━━━━━━━\n"
        "✏️ <b>编辑命令：</b>\n"
        f"<code>编辑备注 {r['id']} 新备注</code>\n"
        f"<code>编辑到期 {r['id']} 2027-05-01</code>\n"
        f"<code>编辑价格 {r['id']} 38 CNY</code>\n"
        f"<code>编辑永久 {r['id']} 是</code> / <code>编辑永久 {r['id']} 否</code>\n"
        f"<code>编辑自动续费 {r['id']} 是</code> / <code>编辑自动续费 {r['id']} 否</code>"
    )

def server_button_label(r):
    online = check_tcp(r["host"], r["check_port"])
    status = "🟢" if online else "🔴"
    flag = country_flag(r["country_code"] if "country_code" in r.keys() else "")
    days = expire_status_text(r["expire_at"], is_free_forever_row(r))
    free = "🎁" if is_free_forever_row(r) else ""
    auto = "🔁" if is_auto_renew_row(r) else ""
    return f"{status} {flag}{free}{auto} ID{r['id']}｜{r['name']}｜{days}"

def servers_inline_keyboard(rows):
    kb = []
    for r in rows:
        kb.append([{"text": server_button_label(r), "callback_data": f"server:{r['id']}"}])
    kb.append([
        {"text": "🔄 刷新列表", "callback_data": "servers:refresh"},
        {"text": "🧾 添加服务器", "callback_data": "servers:add_help"},
    ])
    kb.append([
        {"text": "✏️ 编辑说明", "callback_data": "servers:edit_help"},
        {"text": "🌍 刷新地区", "callback_data": "servers:refresh_meta"},
    ])
    return kb


def server_detail_keyboard(sid):
    return [
        [{"text": "✏️ 编辑说明", "callback_data": f"server_edit:{sid}"}, {"text": "⏰ 续费说明", "callback_data": f"server_renew_help:{sid}"}],
        [{"text": "📆 月付+1月", "callback_data": f"renew_month:{sid}"}, {"text": "🗓️ 季付+3月", "callback_data": f"renew_quarter:{sid}"}, {"text": "📅 年付+1年", "callback_data": f"renew_year:{sid}"}],
        [{"text": "🎁 永久免费 开/关", "callback_data": f"toggle_free:{sid}"}, {"text": "🔁 自动续费 开/关", "callback_data": f"toggle_auto:{sid}"}],
        [{"text": "🌍 刷新地区", "callback_data": f"refresh_meta:{sid}"}, {"text": "🗑️ 删除确认", "callback_data": f"delete_confirm:{sid}"}],
        [{"text": "⬅️ 返回服务器列表", "callback_data": "servers:refresh"}],
    ]

def cmd_server_detail(chat_id, sid):
    r = get_server_row(sid)
    if not r:
        send(chat_id, "❌ 没有找到这个服务器 ID。")
        return
    send_inline(chat_id, server_detail_text(r), server_detail_keyboard(sid))


def cmd_server_buttons(chat_id, title="📋✨ <b>选择服务器查看详情</b> ✨📋"):
    refresh_missing_meta()
    conn = db()
    rows = conn.execute("SELECT * FROM servers ORDER BY expire_at ASC, id ASC").fetchall()
    conn.close()
    if not rows:
        send(chat_id, "📭 暂无服务器记录。\n\n发送 <code>添加服务器</code> 添加。")
        return
    online = sum(1 for r in rows if check_tcp(r["host"], r["check_port"]))
    offline = len(rows) - online
    text = (
        f"{title}\n"
        f"🕒 更新时间：{now_text()}\n\n"
        f"🟢 在线：{online} 台\n"
        f"🔴 离线：{offline} 台\n"
        f"📦 总数：{len(rows)} 台\n\n"
        "👇 每一排就是一台服务器，点击任意服务器查看详细信息。"
    )
    send_inline(chat_id, text, servers_inline_keyboard(rows))


def add_months_to_expire(expire_at, months):
    try:
        base = parse_date(expire_at).date()
    except Exception:
        base = datetime.now().date()
    today = datetime.now().date()
    if base < today:
        base = today
    return (base + relativedelta(months=months)).strftime("%Y-%m-%d")


def quick_renew_server(sid, months):
    conn = db()
    row = conn.execute("SELECT * FROM servers WHERE id=?", (sid,)).fetchone()
    if not row:
        conn.close(); return None, "❌ 没有找到这个服务器 ID。"
    if is_free_forever_row(row):
        conn.close(); return row, "🎁 这台服务器是永久免费的，不需要续费。"
    new_date = add_months_to_expire(row["expire_at"], months)
    conn.execute("UPDATE servers SET expire_at=? WHERE id=?", (new_date, sid))
    clear_reminders(conn, sid); conn.commit()
    row = conn.execute("SELECT * FROM servers WHERE id=?", (sid,)).fetchone()
    conn.close()
    event_add("action", "快速续费服务器", f"服务器 ID {sid} 已续费到 {new_date}")
    return row, f"✅ 已续费到 {new_date}"


def auto_renew_expired_online_servers():
    """自动续费：服务器已过期且当前在线时，按月/季/年顺延到下个自然周期。"""
    conn = db()
    rows = conn.execute("SELECT * FROM servers WHERE auto_renew=1 AND IFNULL(free_forever,0)=0").fetchall()
    today = datetime.now().date()
    for r in rows:
        if not is_valid_date_text(r["expire_at"]):
            continue
        exp = parse_date(r["expire_at"]).date()
        if exp >= today:
            continue
        if not check_tcp(r["host"], r["check_port"]):
            continue
        new_date = next_natural_expire(r["expire_at"], r["cycle"])
        conn.execute("UPDATE servers SET expire_at=? WHERE id=?", (new_date, r["id"]))
        conn.execute("DELETE FROM reminders WHERE server_id=?", (r["id"],))
        conn.commit()
        event_add("action", "自动续费", f"服务器 {r['name']} 在线且已过期，已自动顺延到 {new_date}")
        broadcast(
            "✅🔁 <b>服务器已自动续费</b> 🔁✅\n\n"
            "━━━━━━━━━━━━━━\n"
            f"🖥️ <b>名称：</b>{h(r['name'])}\n"
            f"🌐 <b>主机：</b><code>{h(r['host'])}</code>\n"
            f"🔁 <b>周期：</b>{cycle_name(r['cycle'])}\n"
            f"📆 <b>原到期：</b>{h(r['expire_at'])}\n"
            f"📅 <b>新到期：</b>{h(new_date)}\n"
            "━━━━━━━━━━━━━━\n"
            "📌 触发条件：服务器已过期，但检测端口仍在线。"
        )
    conn.close()

def set_bot_commands():
    # Telegram 左侧命令菜单只支持 /英文小写命令；中文命令不能放进这个菜单。
    commands = [
        {"command": "help", "description": "帮助菜单 / 查看所有功能"},
        {"command": "enable_commands", "description": "启用中文按钮菜单"},
        {"command": "dashboard", "description": "服务器总览：状态/流量/事件"},
        {"command": "status", "description": "查看本机状态"},
        {"command": "disk", "description": "查看磁盘使用情况"},
        {"command": "traffic", "description": "查看服务器流量"},
        {"command": "servers", "description": "查看服务器列表"},
        {"command": "check_servers", "description": "检测在线/离线"},
        {"command": "add_server", "description": "添加服务器提醒"},
        {"command": "edit_server", "description": "编辑服务器资料"},
        {"command": "refresh_local_meta", "description": "刷新本机国家地区"},
        {"command": "refresh_all_meta", "description": "刷新全部国家地区"},
        {"command": "hide_keyboard", "description": "收起中文按钮键盘"},
        {"command": "events", "description": "查看服务器事件"},
        {"command": "security", "description": "查看安全状态"},
        {"command": "login_log", "description": "查看登录记录"},
        {"command": "fail2ban", "description": "查看防爆破状态"},
        {"command": "restart_xray", "description": "重启 Xray / x-ui"},
        {"command": "clean_cache", "description": "清理系统缓存"}
    ]
    return tg("setMyCommands", {"commands": commands})


def cmd_enable_commands(chat_id):
    result = set_bot_commands()
    send(chat_id, (
        "✅✨ <b>中文按钮菜单已启用</b> ✨✅\n\n"
        "📌 Telegram 左侧 <b>/ 命令菜单</b> 只能显示英文斜杠命令，这是 Telegram 官方限制。\n"
        "📌 我已经给你开启了下方 <b>中文按钮键盘</b>，可以直接点按钮自动发送给机器人。\n\n"
        "常用按钮：\n"
        "📊 <code>服务器总览</code>\n"
        "📋 <code>查看服务器</code>\n"
        "🧾 <code>添加服务器</code>\n"
        "✏️ <code>编辑服务器</code>\n"
        "💾 <code>查看磁盘</code>\n"
        "🌐 <code>查看流量</code>\n\n"
        f"左侧命令菜单设置结果：<code>{h(result.get('ok'))}</code>"
    ))


def cmd_help(chat_id):
    send(chat_id, """
🤖✨ <b>服务器监控管理机器人</b> ✨🤖

━━━━━━━━━━━━━━━━━━
📌 <b>使用方式</b>
━━━━━━━━━━━━━━━━━━

发送或点击：<code>启用命令</code>
开启下方中文按钮键盘，之后可以直接点中文按钮。

⚠️ 说明：Telegram 左侧 / 命令菜单只支持英文命令，中文不能放进去；中文可通过下方按钮键盘点击发送。

━━━━━━━━━━━━━━━━━━
📊 <b>总览功能</b>
━━━━━━━━━━━━━━━━━━

<code>服务器总览</code>：本机状态、流量、在线离线、事件。
<code>查看事件</code>：最近服务器事件。

━━━━━━━━━━━━━━━━━━
📡 <b>服务器管理</b>
━━━━━━━━━━━━━━━━━━

<code>添加服务器</code>：添加服务器到期提醒和在线检测。
<code>查看服务器</code>：查看所有服务器、备注、地区、系统、价格、到期。
<code>编辑服务器</code>：查看编辑命令模板。
<code>检测服务器</code>：立即检测在线 / 离线。
<code>删除服务器 1</code>：删除 ID 为 1 的服务器。

━━━━━━━━━━━━━━━━━━
✏️ <b>编辑示例</b>
━━━━━━━━━━━━━━━━━━

<code>编辑备注 1 香港甲骨文主力机</code>
<code>编辑到期 1 2027-05-01</code>
<code>编辑价格 1 38 CNY</code>
<code>编辑周期 1 年付</code>
<code>编辑端口 1 443</code>
<code>编辑名称 1 HK-Oracle</code>
<code>编辑系统 1 Ubuntu 22.04</code>
<code>编辑永久 1 是</code> / <code>编辑永久 1 否</code>
<code>编辑自动续费 1 是</code> / <code>编辑自动续费 1 否</code>
<code>续费服务器 1 2027-05-01</code>
<code>刷新地区 1</code>
<code>刷新全部地区</code>
<code>收起键盘</code>

━━━━━━━━━━━━━━━━━━
🖥️ <b>本机状态</b>
━━━━━━━━━━━━━━━━━━

<code>查看状态</code> / <code>查看磁盘</code> / <code>查看流量</code>

━━━━━━━━━━━━━━━━━━
🔔 <b>自动推送</b>
━━━━━━━━━━━━━━━━━━

🚨 离线警报　✅ 恢复在线　⏰ 到期提醒
🔥 CPU 警报　🧠 内存警报　💽 磁盘警报
""".strip())


def status_block():
    s = get_local_status()
    local_place = " ".join(x for x in [s.get("country"), s.get("region"), s.get("city")] if x and x != "未知")
    return (
        "🖥️ <b>本机状态</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🌐 主机名称：<code>{h(s['hostname'])}</code>\n"
        f"🌍 公网 IP：<code>{h(s.get('public_ip', '未知'))}</code>\n"
        f"📍 国家地区：{h(s.get('flag', '🌐'))} {h(local_place or s.get('country') or '未知')}\n"
        f"🏢 运营商：{h(s.get('isp') or '未知')}\n"
        f"🧬 系统版本：{h(s['os'])}\n"
        f"{local_profile_lines()}\n"
        f"⏱️ 运行时间：{h(s['uptime'])}\n"
        f"📊 CPU 使用率：{s['cpu']:.0f}%\n"
        f"⚙️ 系统负载：{s['load1']:.2f} / {s['load5']:.2f} / {s['load15']:.2f}\n"
        f"🧩 CPU 核心：{s['cpu_count']} 核\n"
        f"🧠 内存使用：{fmt_size(s['mem_used'])} / {fmt_size(s['mem_total'])} ({s['mem_percent']:.0f}%)\n"
        f"💾 磁盘使用：{fmt_size(s['disk_used'])} / {fmt_size(s['disk_total'])} ({s['disk_percent']:.0f}%)"
    )

def traffic_block():
    t = traffic_detail()
    lines = [
        "🌐 <b>流量使用情况</b>",
        "━━━━━━━━━━━━━━",
        f"⬇️ 累计下载：{fmt_size(t['total_recv'])}",
        f"⬆️ 累计上传：{fmt_size(t['total_sent'])}",
        f"🚀 实时下载：{fmt_size(t['realtime_down'])}/秒",
        f"📤 实时上传：{fmt_size(t['realtime_up'])}/秒"
    ]
    if t["interfaces"]:
        lines.append("")
        lines.append("📶 <b>网卡明细</b>")
        for item in t["interfaces"][:5]:
            lines.append(
                f"🔹 <b>{h(item['name'])}</b>\n"
                f"   ⬇️ 下载：{fmt_size(item['recv'])} ｜ 🚀 {fmt_size(item['down_speed'])}/秒\n"
                f"   ⬆️ 上传：{fmt_size(item['sent'])} ｜ 📤 {fmt_size(item['up_speed'])}/秒"
            )
    return "\n".join(lines)


def server_location_line(r):
    code = r["country_code"] if "country_code" in r.keys() else ""
    flag = country_flag(code)
    country = r["country"] if "country" in r.keys() and r["country"] else "未知"
    region = r["region"] if "region" in r.keys() and r["region"] else ""
    city = r["city"] if "city" in r.keys() and r["city"] else ""
    place = " ".join(x for x in [country, region, city] if x)
    return f"{flag} {h(place or '未知')}"


def servers_summary_block():
    refresh_missing_meta()
    conn = db()
    rows = conn.execute("SELECT * FROM servers ORDER BY id ASC").fetchall()
    conn.close()
    if not rows:
        return "📡 <b>服务器在线情况</b>\n━━━━━━━━━━━━━━\n📭 暂无服务器记录。\n发送 <code>添加服务器</code> 开始添加。"
    online_count = 0
    offline_count = 0
    lines = []
    for r in rows:
        online = check_tcp(r["host"], r["check_port"])
        if online:
            online_count += 1
            status = "🟢 在线"
        else:
            offline_count += 1
            status = "🔴 离线"
        flag = country_flag(r["country_code"] if "country_code" in r.keys() else "")
        lines.append(f"{status}｜{flag} {h(r['name'])}｜{h(r['host'])}:{h(r['check_port'])}")
    return (
        "📡 <b>服务器在线情况</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🟢 在线：{online_count} 台\n"
        f"🔴 离线：{offline_count} 台\n"
        f"📦 总数：{len(rows)} 台\n\n" + "\n".join(lines[:12])
    )


def events_block(limit=6):
    rows = get_recent_events(limit)
    if not rows:
        return "🧾 <b>服务器事件</b>\n━━━━━━━━━━━━━━\n暂无事件记录。"
    icons = {"offline": "🚨", "online": "✅", "expiry": "⏰", "system": "🔥", "action": "🛠️", "security": "🛡️"}
    lines = ["🧾 <b>服务器事件</b>", "━━━━━━━━━━━━━━"]
    for r in rows:
        lines.append(f"{icons.get(r['event_type'], '📌')} <b>{h(r['title'])}</b>\n🕒 {h(r['created_at'])}")
    return "\n".join(lines)


def cmd_dashboard(chat_id):
    send_long(chat_id, (
        "📊✨ <b>服务器总览面板</b> ✨📊\n"
        f"🕒 更新时间：{now_text()}\n\n"
        f"{status_block()}\n\n{traffic_block()}\n\n{servers_summary_block()}\n\n{events_block(6)}"
    )[:3900])


def cmd_status(chat_id):
    send(chat_id, "✅✨ <b>当前机器状态</b> ✨✅\n" + f"🕒 更新时间：{now_text()}\n\n" + status_block())


def cmd_disk(chat_id):
    lines = ["💾✨ <b>磁盘使用情况</b> ✨💾", f"🕒 更新时间：{now_text()}", ""]
    try:
        for p in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(p.mountpoint)
            except Exception:
                continue
            percent = usage.percent
            status = "🚨 空间严重不足" if percent >= DISK_ALERT else "⚠️ 空间偏高" if percent >= 80 else "✅ 空间正常"
            lines.append(
                "━━━━━━━━━━━━━━\n"
                f"📦 <b>挂载位置：</b><code>{h(p.mountpoint)}</code>\n"
                f"🧩 <b>设备名称：</b><code>{h(p.device)}</code>\n"
                f"📁 <b>文件系统：</b>{h(p.fstype or '未知')}\n"
                f"💽 <b>总容量：</b>{fmt_size(usage.total)}\n"
                f"📤 <b>已使用：</b>{fmt_size(usage.used)}\n"
                f"📥 <b>可用空间：</b>{fmt_size(usage.free)}\n"
                f"📊 <b>使用率：</b>{percent:.0f}%\n"
                f"📌 <b>状态：</b>{status}\n"
            )
        root = psutil.disk_usage("/")
        lines.append("━━━━━━━━━━━━━━")
        lines.append(f"🚨💥 <b>总体结论：</b>根目录使用率已超过 {DISK_ALERT}%，请尽快清理。" if root.percent >= DISK_ALERT else "✅🌿 <b>总体结论：</b>磁盘空间正常。")
        send_long(chat_id, "\n".join(lines)[:3900])
    except Exception as e:
        send(chat_id, f"❌ 获取磁盘信息失败：{h(e)}")


def cmd_traffic(chat_id):
    send(chat_id, "🌐✨ <b>服务器流量使用情况</b> ✨🌐\n" + f"🕒 更新时间：{now_text()}\n\n" + traffic_block())


def cmd_login_log(chat_id):
    raw = shell("last -w -n 10 | grep -v 'wtmp begins' || true", 10)
    if not raw.strip():
        send(chat_id, "🔐✨ <b>最近登录记录</b> ✨🔐\n\n暂无登录记录。")
        return
    lines = ["🔐✨ <b>最近 SSH 登录记录</b> ✨🔐", f"🕒 更新时间：{now_text()}", ""]
    for line in raw.splitlines()[:10]:
        parts = line.split()
        if len(parts) < 3:
            continue
        lines.append(
            "━━━━━━━━━━━━━━\n"
            f"👤 <b>登录用户：</b>{h(parts[0])}\n"
            f"💻 <b>登录终端：</b>{h(parts[1])}\n"
            f"🌐 <b>来源地址：</b>{h(parts[2])}\n"
            f"⏰ <b>登录时间：</b>{h(' '.join(parts[3:8]) if len(parts) >= 8 else ' '.join(parts[3:]) or '未知')}"
        )
    send_long(chat_id, "\n".join(lines)[:3900])


def cmd_fail2ban(chat_id):
    raw = shell(root_cmd("fail2ban-client status sshd 2>/dev/null || fail2ban-client status 2>/dev/null || echo FAIL2BAN_NOT_RUNNING"), 10)
    if "FAIL2BAN_NOT_RUNNING" in raw:
        send(chat_id, "🚫✨ <b>防爆破状态</b> ✨🚫\n\n⚠️ <b>当前状态：</b>Fail2ban 未运行或未配置 SSH 防护。")
        return
    vals = {"Currently failed": "未知", "Total failed": "未知", "Currently banned": "未知", "Total banned": "未知"}
    for line in raw.splitlines():
        line = line.strip()
        for k in list(vals):
            if k + ":" in line:
                vals[k] = line.split(":", 1)[1].strip()
    send(chat_id, (
        "🚫✨ <b>防爆破状态</b> ✨🚫\n"
        f"🕒 更新时间：{now_text()}\n\n"
        "🛡️ <b>服务状态：</b>✅ 已运行\n"
        f"⚠️ <b>当前失败登录次数：</b>{h(vals['Currently failed'])}\n"
        f"📊 <b>累计失败登录次数：</b>{h(vals['Total failed'])}\n"
        f"🔒 <b>当前封禁 IP 数量：</b>{h(vals['Currently banned'])}\n"
        f"📌 <b>累计封禁 IP 数量：</b>{h(vals['Total banned'])}"
    ))


def cmd_security(chat_id):
    ssh = shell("systemctl is-active ssh 2>/dev/null || systemctl is-active sshd 2>/dev/null || echo unknown", 5)
    f2b = shell("systemctl is-active fail2ban 2>/dev/null || echo unknown", 5)
    updates = shell("apt list --upgradable 2>/dev/null | sed 1d | wc -l", 10)
    ufw_raw = shell("ufw status 2>/dev/null || echo 未安装或未启用", 5)
    firewall = "✅ 已开启" if "Status: active" in ufw_raw else "⚠️ 未开启" if "Status: inactive" in ufw_raw else "❓ 未检测到或未安装"
    send(chat_id, (
        "🛡️✨ <b>综合安全状态</b> ✨🛡️\n"
        f"🕒 更新时间：{now_text()}\n\n"
        f"🔐 <b>SSH 服务：</b>{service_cn(ssh)}\n"
        f"🚫 <b>防爆破服务：</b>{service_cn(f2b)}\n"
        f"🔥 <b>防火墙状态：</b>{firewall}\n"
        f"📦 <b>可更新软件包：</b>{h(updates)} 个"
    ))


def cmd_restart_xray(chat_id):
    send(chat_id, "⚠️🔄 <b>重启节点确认</b>\n\n即将重启 Xray / x-ui / 3x-ui。\n\n确认重启请发送：\n<code>确认重启</code>")


def cmd_restart_xray_confirm(chat_id):
    shell(root_cmd("systemctl restart xray 2>&1; systemctl restart x-ui 2>&1 || systemctl restart 3x-ui 2>&1 || true"), 20)
    event_add("action", "执行重启节点", "已执行 Xray / x-ui / 3x-ui 重启命令")
    send(chat_id, "✅🔄 <b>已执行重启命令</b>\n\n可以稍后发送 <code>查看状态</code> 或 <code>检测服务器</code> 查看状态。")


def cmd_clean_cache(chat_id):
    send(chat_id, "⚠️🧹 <b>清理缓存确认</b>\n\n确认清理请发送：\n<code>确认清理</code>")


def cmd_clean_cache_confirm(chat_id):
    shell(root_cmd("sync"), 10)
    shell(root_cmd("sh -c 'echo 3 > /proc/sys/vm/drop_caches'"), 10)
    shell(root_cmd("apt clean"), 20)
    event_add("action", "清理系统缓存", "已执行系统缓存清理")
    send(chat_id, "✅🧹 <b>缓存已清理</b>\n\n可以发送 <code>查看状态</code> 查看当前内存情况。")


def parse_form(raw):
    data = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            k, v = line.split(":", 1)
        elif "：" in line:
            k, v = line.split("：", 1)
        else:
            continue
        data[k.strip()] = v.strip()
    return data


def cmd_add_server(chat_id, text):
    raw = text.strip()
    for prefix in ["/add_server", "添加服务器", "新增服务器", "添加机器", "新增机器"]:
        if raw.startswith(prefix):
            raw = raw.replace(prefix, "", 1).strip()
            break
    if not raw:
        cmd_add_builder(chat_id)
        return
    raw = "\n".join(line for line in raw.splitlines() if not line.strip().startswith("#"))
    data = parse_form(raw)
    name = data.get("名称") or data.get("name")
    host = data.get("主机") or data.get("host") or data.get("IP") or data.get("ip")
    note = data.get("备注") or data.get("note") or ""
    os_name = data.get("系统") or data.get("os") or data.get("system") or ""
    cycle = data.get("周期") or data.get("cycle") or "monthly"
    price = data.get("价格") or data.get("price") or "0"
    currency = data.get("币种") or data.get("currency") or "USD"
    expire_at = data.get("到期") or data.get("到期日") or data.get("expire") or data.get("expire_at") or ""
    check_port = data.get("检测端口") or data.get("端口") or data.get("port") or "22"
    free_forever = truthy(data.get("永久免费") or data.get("免费") or data.get("free_forever") or data.get("free"))
    auto_renew = truthy(data.get("自动续费") or data.get("auto_renew") or data.get("autorenew"))
    missing = [label for label, value in [("名称", name), ("主机", host)] if not value]
    if missing:
        send(chat_id, "❌ 缺少字段：" + "、".join(missing) + "\n\n发送 <code>添加服务器</code> 使用勾选模板生成器。")
        return
    cycle = normalize_cycle(cycle)
    currency = normalize_currency(currency)
    if cycle not in ["monthly", "quarterly", "yearly"]:
        send(chat_id, "❌ 周期只支持：月付 / 季付 / 年付，或 monthly / quarterly / yearly")
        return
    if currency not in ["CNY", "USD", "EUR", "GBP"]:
        send(chat_id, "❌ 币种只支持：CNY / USD / EUR / GBP")
        return
    try:
        price = float(price or 0)
        check_port = int(check_port)
        if not free_forever and expire_at and not is_valid_date_text(expire_at):
            raise ValueError("到期日期格式错误")
    except Exception:
        send(chat_id, "❌ 价格、日期或检测端口格式错误。日期示例：<code>2026-08-01</code>")
        return
    if free_forever:
        price = 0.0; currency = "USD"; expire_at = "永久"; auto_renew = False
    meta = detect_server_meta(host)
    if not os_name:
        banner = detect_ssh_banner(host, check_port)
        os_name = f"自动识别 / {banner}" if banner != "未知" else "自动识别：未知系统"
    online = check_tcp(host, check_port)
    status = "online" if online else "offline"
    conn = db()
    conn.execute(
        """INSERT INTO servers(name, host, note, cycle, price, currency, expire_at, check_port, country, country_code, region, city, isp, os_name, last_meta_at, free_forever, auto_renew)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (name, host, note, cycle, price, currency, expire_at, check_port, meta["country"], meta["country_code"], meta["region"], meta["city"], meta["isp"], os_name, now_text(), 1 if free_forever else 0, 1 if auto_renew else 0)
    )
    conn.commit()
    sid = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    conn.execute("INSERT OR REPLACE INTO server_status(server_id, last_status, last_checked_at, last_changed_at) VALUES(?,?,?,?)", (sid, status, now_text(), now_text()))
    conn.commit(); conn.close()
    status_text = "🟢 在线" if online else "🔴 离线"
    event_add("action", "添加服务器", f"添加服务器：{name}，当前状态：{status_text}")
    send(chat_id, (
        "✅🎉 <b>服务器添加成功</b> 🎉✅\n\n"
        "━━━━━━━━━━━━━━\n"
        f"🆔 <b>ID：</b><code>{sid}</code>\n"
        f"🖥️ <b>名称：</b>{h(name)}\n"
        f"🌐 <b>主机：</b><code>{h(host)}</code>\n"
        f"📍 <b>地区：</b>{meta['flag']} {h(meta['country'])} {h(meta['region'])} {h(meta['city'])}\n"
        f"🏢 <b>运营商：</b>{h(meta['isp'] or '未知')}\n"
        f"🧬 <b>系统：</b>{h(os_name)}\n"
        f"🔌 <b>检测端口：</b>{h(check_port)}\n"
        f"📡 <b>当前状态：</b>{status_text}\n"
        f"📝 <b>备注：</b>{h(note or '无')}\n"
        f"🎁 <b>永久免费：</b>{bool_text(free_forever)}\n"
        f"🔁 <b>自动续费：</b>{bool_text(auto_renew)}\n"
        f"💰 <b>价格：</b>{'🎁 永久免费' if free_forever else currency_name(currency) + ' ' + format(price, 'g') + ' ' + currency}\n"
        f"📆 <b>到期：</b>{h(expire_at or '未设置')}\n"
        "━━━━━━━━━━━━━━\n"
        "⏰ 到期提醒已开启\n📡 在线 / 离线检测已开启"
    ))

def cmd_list_servers(chat_id):
    cmd_server_buttons(chat_id)

def cmd_check_servers(chat_id):
    refresh_missing_meta()
    conn = db()
    rows = conn.execute("SELECT * FROM servers ORDER BY id ASC").fetchall()
    conn.close()
    if not rows:
        send(chat_id, "📭 暂无服务器记录。")
        return
    online_count = 0
    offline_count = 0
    lines = ["📡✨ <b>服务器在线状态检测</b> ✨📡", f"🕒 检测时间：{now_text()}", ""]
    for r in rows:
        online = check_tcp(r["host"], r["check_port"])
        if online:
            status_text = "🟢 在线"; online_count += 1
        else:
            status_text = "🔴 离线"; offline_count += 1
        lines.append("━━━━━━━━━━━━━━\n" f"📡 <b>状态：</b>{status_text}\n" f"🖥️ <b>名称：</b>{h(r['name'])}\n" f"📍 <b>地区：</b>{server_location_line(r)}\n" f"🌐 <b>地址：</b><code>{h(r['host'])}:{h(r['check_port'])}</code>\n" f"📝 <b>备注：</b>{h(r['note'] or '无')}")
    lines.insert(2, f"🟢 在线：{online_count} 台\n🔴 离线：{offline_count} 台\n📦 总数：{len(rows)} 台\n")
    send_long(chat_id, "\n".join(lines)[:3900])


def clear_reminders(conn, sid):
    conn.execute("DELETE FROM reminders WHERE server_id=?", (sid,))


def update_server_field(chat_id, sid, field, value, extra=None):
    allowed = {"name", "note", "cycle", "price", "currency", "expire_at", "check_port", "os_name", "free_forever", "auto_renew"}
    if field not in allowed:
        send(chat_id, "❌ 不支持编辑这个字段。")
        return
    conn = db()
    row = conn.execute("SELECT * FROM servers WHERE id=?", (sid,)).fetchone()
    if not row:
        conn.close(); send(chat_id, "❌ 没有找到这个服务器 ID。")
        return
    if field == "expire_at":
        parse_date(value)
        clear_reminders(conn, sid)
    if field == "cycle":
        value = normalize_cycle(value)
        if value not in ["monthly", "quarterly", "yearly"]:
            conn.close(); send(chat_id, "❌ 周期只支持：月付 / 季付 / 年付。")
            return
    if field == "price":
        value = float(value)
    if field == "currency":
        value = normalize_currency(value)
        if value not in ["CNY", "USD", "EUR", "GBP"]:
            conn.close(); send(chat_id, "❌ 币种只支持：CNY / USD / EUR / GBP。")
            return
    if field == "check_port":
        value = int(value)
    if field == "free_forever":
        value = 1 if truthy(value) else 0
        if value:
            conn.execute("UPDATE servers SET price=0, currency='USD', expire_at='永久', auto_renew=0 WHERE id=?", (sid,))
            clear_reminders(conn, sid)
    if field == "auto_renew":
        value = 1 if truthy(value) else 0
    conn.execute(f"UPDATE servers SET {field}=? WHERE id=?", (value, sid))
    if extra:
        for k, v in extra.items():
            conn.execute(f"UPDATE servers SET {k}=? WHERE id=?", (v, sid))
    conn.commit(); conn.close()
    event_add("action", "编辑服务器", f"服务器 ID {sid} 已更新 {field}")
    send(chat_id, f"✅✏️ <b>编辑成功</b>\n\n🆔 ID：<code>{h(sid)}</code>\n📌 字段：<code>{h(field)}</code>\n📝 新值：{h(value)}")


def cmd_edit_help(chat_id):
    send(chat_id, """
✏️✨ <b>编辑服务器</b> ✨✏️

直接发送下面任意一种：

<code>编辑备注 1 香港甲骨文主力机</code>
<code>编辑到期 1 2027-05-01</code>
<code>编辑价格 1 38 CNY</code>
<code>编辑周期 1 年付</code>
<code>编辑端口 1 443</code>
<code>编辑名称 1 HK-Oracle</code>
<code>编辑系统 1 Ubuntu 22.04</code>
<code>编辑永久 1 是</code> / <code>编辑永久 1 否</code>
<code>编辑自动续费 1 是</code> / <code>编辑自动续费 1 否</code>
<code>续费服务器 1 2027-05-01</code>
<code>刷新地区 1</code>

📌 查看 ID：发送 <code>查看服务器</code>
""".strip())


def cmd_edit_server(chat_id, text):
    # 本函数只处理一条编辑命令；多行批量编辑由 handle() 先拆分。
    text = one_line(text.strip())
    parts = text.split(maxsplit=2)
    if len(parts) < 2:
        cmd_edit_help(chat_id); return
    action = parts[0]
    if action == "刷新地区":
        sid = parts[1]
        conn = db(); row = conn.execute("SELECT * FROM servers WHERE id=?", (sid,)).fetchone()
        if not row:
            conn.close(); send(chat_id, "❌ 没有找到这个服务器 ID。"); return
        meta = detect_server_meta(row["host"])
        conn.execute("UPDATE servers SET country=?, country_code=?, region=?, city=?, isp=?, last_meta_at=? WHERE id=?", (meta["country"], meta["country_code"], meta["region"], meta["city"], meta["isp"], now_text(), sid))
        conn.commit(); conn.close()
        send(chat_id, f"✅🌍 <b>地区已刷新</b>\n\n🆔 ID：<code>{h(sid)}</code>\n📍 地区：{meta['flag']} {h(meta['country'])} {h(meta['region'])} {h(meta['city'])}\n🏢 运营商：{h(meta['isp'] or '未知')}")
        return
    if len(parts) < 3:
        cmd_edit_help(chat_id); return
    sid, val = parts[1], parts[2].strip()
    try:
        if action == "编辑备注": update_server_field(chat_id, sid, "note", val)
        elif action == "编辑到期" or action == "续费服务器": update_server_field(chat_id, sid, "expire_at", val)
        elif action == "编辑周期": update_server_field(chat_id, sid, "cycle", val)
        elif action == "编辑端口": update_server_field(chat_id, sid, "check_port", val)
        elif action == "编辑名称": update_server_field(chat_id, sid, "name", val)
        elif action == "编辑系统": update_server_field(chat_id, sid, "os_name", val)
        elif action == "编辑永久": update_server_field(chat_id, sid, "free_forever", val)
        elif action == "编辑自动续费": update_server_field(chat_id, sid, "auto_renew", val)
        elif action == "编辑价格":
            pv = val.split()
            if len(pv) == 1:
                update_server_field(chat_id, sid, "price", pv[0])
            else:
                conn = db(); row = conn.execute("SELECT * FROM servers WHERE id=?", (sid,)).fetchone(); conn.close()
                if not row: send(chat_id, "❌ 没有找到这个服务器 ID。"); return
                update_server_field(chat_id, sid, "price", pv[0], {"currency": normalize_currency(pv[1])})
        else:
            cmd_edit_help(chat_id)
    except Exception as e:
        send(chat_id, f"❌ 编辑失败：{h(e)}")


def cmd_del_server(chat_id, text):
    sid = text.replace("/del_server", "", 1).replace("删除服务器", "", 1).replace("删除机器", "", 1).strip()
    if not sid:
        send(chat_id, "❌ 格式：<code>删除服务器 1</code>"); return
    conn = db(); row = conn.execute("SELECT * FROM servers WHERE id=?", (sid,)).fetchone()
    if not row:
        conn.close(); send(chat_id, "❌ 没有找到这个服务器 ID。"); return
    conn.execute("DELETE FROM servers WHERE id=?", (sid,)); conn.execute("DELETE FROM reminders WHERE server_id=?", (sid,)); conn.execute("DELETE FROM server_status WHERE server_id=?", (sid,)); conn.commit(); conn.close()
    event_add("action", "删除服务器", f"已删除服务器：{row['name']}")
    send(chat_id, f"✅🗑️ <b>服务器已删除</b>\n\n🆔 <b>ID：</b><code>{h(sid)}</code>\n🖥️ <b>名称：</b>{h(row['name'])}")


def cmd_events(chat_id):
    rows = get_recent_events(15)
    if not rows:
        send(chat_id, "🧾 暂无服务器事件记录。"); return
    icons = {"offline": "🚨", "online": "✅", "expiry": "⏰", "system": "🔥", "action": "🛠️", "security": "🛡️"}
    lines = ["🧾✨ <b>服务器事件记录</b> ✨🧾", f"🕒 更新时间：{now_text()}", ""]
    for r in rows:
        lines.append("━━━━━━━━━━━━━━\n" f"{icons.get(r['event_type'], '📌')} <b>{h(r['title'])}</b>\n" f"🕒 <b>时间：</b>{h(r['created_at'])}\n" f"📝 <b>内容：</b>{h(r['content'])}")
    send_long(chat_id, "\n".join(lines)[:3900])


def offline_push_text(r):
    return (
        "🚨🔴 <b>服务器离线警报</b> 🔴🚨\n\n"
        "━━━━━━━━━━━━━━\n"
        f"🖥️ <b>名称：</b>{h(r['name'])}\n"
        f"📍 <b>地区：</b>{server_location_line(r)}\n"
        f"🌐 <b>主机：</b><code>{h(r['host'])}:{h(r['check_port'])}</code>\n"
        f"🧬 <b>系统：</b>{h(r['os_name'] if 'os_name' in r.keys() and r['os_name'] else '未知系统')}\n"
        f"📝 <b>备注：</b>{h(r['note'] or '无')}\n"
        f"⏰ <b>时间：</b>{now_text()}\n"
        "━━━━━━━━━━━━━━\n"
        "📌 <b>当前状态：</b>🔴 离线\n"
        "⚠️ <b>可能原因：</b>服务器关机、网络异常、端口未开放、防火墙阻断。"
    )


def online_push_text(r):
    return (
        "✅🟢 <b>服务器恢复在线</b> 🟢✅\n\n"
        "━━━━━━━━━━━━━━\n"
        f"🖥️ <b>名称：</b>{h(r['name'])}\n"
        f"📍 <b>地区：</b>{server_location_line(r)}\n"
        f"🌐 <b>主机：</b><code>{h(r['host'])}:{h(r['check_port'])}</code>\n"
        f"📝 <b>备注：</b>{h(r['note'] or '无')}\n"
        f"⏰ <b>时间：</b>{now_text()}\n"
        "━━━━━━━━━━━━━━\n"
        "📌 <b>当前状态：</b>🟢 在线"
    )


def monitor_server_online_status():
    conn = db(); rows = conn.execute("SELECT * FROM servers").fetchall()
    for r in rows:
        sid = r["id"]; online = check_tcp(r["host"], r["check_port"]); new_status = "online" if online else "offline"
        old = conn.execute("SELECT * FROM server_status WHERE server_id=?", (sid,)).fetchone()
        if not old:
            conn.execute("INSERT INTO server_status(server_id, last_status, last_checked_at, last_changed_at) VALUES(?,?,?,?)", (sid, new_status, now_text(), now_text())); conn.commit(); continue
        old_status = old["last_status"]
        conn.execute("UPDATE server_status SET last_status=?, last_checked_at=? WHERE server_id=?", (new_status, now_text(), sid)); conn.commit()
        if old_status != new_status:
            conn.execute("UPDATE server_status SET last_changed_at=? WHERE server_id=?", (now_text(), sid)); conn.commit()
            push_event("offline" if new_status == "offline" else "online", f"服务器{'离线' if new_status == 'offline' else '恢复在线'}：{r['name']}", offline_push_text(r) if new_status == "offline" else online_push_text(r))
    conn.close()


def expiry_push_text(r, days):
    if is_free_forever_row(r):
        return ""
    if days < 0:
        title = "🚨💥 <b>服务器已过期</b> 💥🚨"; left = f"🔴 已过期 <b>{abs(days)}</b> 天"; level = "🆘 请立即续费，或确认是否已经停用。"
    elif days == 0:
        title = "🚨⏳ <b>服务器今天到期</b> ⏳🚨"; left = "🟠 <b>今天到期</b>"; level = "⚡ 建议马上处理，避免服务中断。"
    elif days <= 3:
        title = "⚠️🔥 <b>服务器即将到期</b> 🔥⚠️"; left = f"🟡 剩余 <b>{days}</b> 天"; level = "🔔 请尽快安排续费。"
    elif days <= 7:
        title = "⏰🌙 <b>服务器到期提醒</b> 🌙⏰"; left = f"🟡 剩余 <b>{days}</b> 天"; level = "📌 建议提前处理。"
    else:
        title = "📅✨ <b>服务器续费提醒</b> ✨📅"; left = f"🟢 剩余 <b>{days}</b> 天"; level = "✅ 当前仍有充足时间。"
    return (
        f"{title}\n\n━━━━━━━━━━━━━━\n"
        f"🖥️ <b>名称：</b>{h(r['name'])}\n"
        f"📍 <b>地区：</b>{server_location_line(r)}\n"
        f"🌐 <b>主机：</b><code>{h(r['host'])}</code>\n"
        f"🧬 <b>系统：</b>{h(r['os_name'] if 'os_name' in r.keys() and r['os_name'] else '未知系统')}\n"
        f"🔌 <b>检测端口：</b>{h(r['check_port'])}\n"
        f"📝 <b>备注：</b>{h(r['note'] or '无')}\n"
        f"🔁 <b>周期：</b>{cycle_name(r['cycle'])}\n"
        f"🔁 <b>自动续费：</b>{bool_text(is_auto_renew_row(r))}\n"
        f"💰 <b>价格：</b>{server_price_line(r)}\n"
        f"📆 <b>到期：</b>{h(r['expire_at'])}\n"
        f"⏳ <b>状态：</b>{left}\n━━━━━━━━━━━━━━\n{level}"
    )


def monitor_expiry():
    conn = db(); rows = conn.execute("SELECT * FROM servers").fetchall(); today = datetime.now().date()
    for r in rows:
        if is_free_forever_row(r) or not is_valid_date_text(r["expire_at"]):
            continue
        days = (parse_date(r["expire_at"]).date() - today).days
        if days in DUE_REMIND_DAYS or days < 0:
            key = f"{r['id']}:{days}"
            old = conn.execute("SELECT 1 FROM reminders WHERE server_id=? AND remind_key=?", (r["id"], key)).fetchone()
            if old: continue
            push_event("expiry", f"到期提醒：{r['name']}", expiry_push_text(r, days))
            conn.execute("INSERT OR REPLACE INTO reminders(server_id, remind_key, sent_at) VALUES(?,?,?)", (r["id"], key, now_text())); conn.commit()
    conn.close()

def alert_once(key, title, text, cooldown_minutes=60):
    conn = db(); row = conn.execute("SELECT sent_at FROM alerts WHERE alert_key=?", (key,)).fetchone(); now = datetime.now()
    if row:
        last = parse_date(row["sent_at"])
        if now - last < timedelta(minutes=cooldown_minutes):
            conn.close(); return
    conn.execute("INSERT OR REPLACE INTO alerts(alert_key, sent_at) VALUES(?,?)", (key, now.isoformat())); conn.commit(); conn.close(); push_event("system", title, text)


def monitor_local_system():
    try:
        s = get_local_status()
        if s["cpu"] >= CPU_ALERT:
            alert_once("local_cpu_high", "CPU 高负载", "🚨🔥 <b>CPU 高负载活动警报</b> 🔥🚨\n\n" f"🖥️ 主机：<code>{h(s['hostname'])}</code>\n📊 CPU：{s['cpu']:.0f}%\n⚙️ 负载：{s['load1']:.2f}\n⏰ 时间：{now_text()}", 30)
        if s["mem_percent"] >= MEM_ALERT:
            alert_once("local_mem_high", "内存高占用", "🚨🧠 <b>内存高占用活动警报</b> 🧠🚨\n\n" f"🖥️ 主机：<code>{h(s['hostname'])}</code>\n📈 内存：{s['mem_percent']:.0f}%\n💾 已用：{fmt_size(s['mem_used'])} / {fmt_size(s['mem_total'])}\n⏰ 时间：{now_text()}", 30)
        if s["disk_percent"] >= DISK_ALERT:
            alert_once("local_disk_high", "磁盘空间不足", "🚨💽 <b>磁盘空间活动警报</b> 💽🚨\n\n" f"🖥️ 主机：<code>{h(s['hostname'])}</code>\n📦 磁盘：{s['disk_percent']:.0f}%\n💾 已用：{fmt_size(s['disk_used'])} / {fmt_size(s['disk_total'])}\n⏰ 时间：{now_text()}", 60)
    except Exception:
        pass


def handle(chat_id, text):
    if not is_admin(chat_id):
        send(chat_id, "⛔ 未授权用户，拒绝访问。")
        return

    # 支持一次粘贴多条编辑命令，例如：
    # 编辑本机名称 zoro
    # 编辑本机到期 2026-05-03
    # 编辑本机价格 50 CNY
    # 编辑本机周期 月付
    # 本机续费 2026-06-03
    if is_batch_edit_text(text):
        lines = split_command_lines(text)
        ok = 0
        for line in lines:
            if line.startswith(LOCAL_EDIT_PREFIXES):
                cmd_edit_local(chat_id, line)
                ok += 1
            elif line.startswith(SERVER_EDIT_PREFIXES):
                cmd_edit_server(chat_id, line)
                ok += 1
        send(chat_id, f"✅📌 <b>批量编辑处理完成</b>\n\n共处理 <b>{ok}</b> 条编辑命令。")
        return

    text = clean_command_text(text)
    if text in ["/start", "/help", "帮助", "菜单", "功能", "命令"]: cmd_help(chat_id)
    elif text in ["/enable_commands", "启用命令", "启用菜单", "开启菜单", "显示命令"]: cmd_enable_commands(chat_id)
    elif text in ["收起键盘", "隐藏键盘", "关闭键盘", "关闭菜单", "收起菜单", "/hide_keyboard"]: send_hide_keyboard(chat_id)
    elif text in ["刷新本机地区", "刷新本机IP", "刷新本机国家", "本机地区", "/refresh_local_meta"]: cmd_refresh_local_meta(chat_id)
    elif text in ["刷新全部地区", "刷新所有地区", "刷新全部IP", "刷新全部国家", "刷新国家地区", "/refresh_all_meta"]: cmd_refresh_all_meta(chat_id)
    elif text in ["/dashboard", "服务器总览", "总览", "面板", "控制台", "监控面板"]: cmd_dashboard(chat_id)
    elif text in ["选择服务器", "服务器按钮", "服务器详情", "查看详情"]: cmd_server_buttons(chat_id)
    elif text in ["/status", "查看状态", "服务器状态", "本机状态", "状态"]: cmd_status(chat_id)
    elif text in ["/disk", "查看磁盘", "磁盘", "磁盘状态", "磁盘使用"]: cmd_disk(chat_id)
    elif text in ["/traffic", "查看流量", "流量", "网络流量", "服务器流量", "流量使用"]: cmd_traffic(chat_id)
    elif text in ["/login_log", "登录记录", "查看登录", "SSH记录", "ssh记录"]: cmd_login_log(chat_id)
    elif text in ["/fail2ban", "/fail2ban_status", "防爆破状态", "防爆破", "封禁状态"]: cmd_fail2ban(chat_id)
    elif text in ["/security", "/security_status", "安全状态", "查看安全", "综合安全"]: cmd_security(chat_id)
    elif text in ["/restart_xray", "重启节点", "重启服务", "重启xray", "重启Xray"]: cmd_restart_xray(chat_id)
    elif text in ["/restart_xray_confirm", "确认重启", "确认重启节点", "确认重启服务"]: cmd_restart_xray_confirm(chat_id)
    elif text in ["/clean_cache", "清理缓存", "清理系统缓存"]: cmd_clean_cache(chat_id)
    elif text in ["/clean_cache_confirm", "确认清理", "确认清理缓存"]: cmd_clean_cache_confirm(chat_id)
    elif text in ["/add_server", "添加服务器", "新增服务器", "添加机器", "新增机器"] or text.startswith(("/add_server", "添加服务器", "新增服务器", "添加机器", "新增机器")): cmd_add_server(chat_id, text)
    elif text in ["/list_servers", "/servers", "查看服务器", "服务器列表", "查看机器", "机器列表"]: cmd_list_servers(chat_id)
    elif text in ["/check_servers", "检测服务器", "检测在线", "检测机器", "在线检测"]: cmd_check_servers(chat_id)
    elif text in ["/events", "查看事件", "服务器事件", "事件记录", "事件"]: cmd_events(chat_id)
    elif text in ["/edit_server", "编辑服务器", "编辑机器"]: cmd_edit_help(chat_id)
    elif text in ["编辑本机", "编辑当前机器", "本机编辑", "当前机器编辑"]: cmd_local_edit_help(chat_id)
    elif text.startswith(("编辑本机名称", "编辑本机备注", "编辑本机到期", "编辑本机价格", "编辑本机周期", "本机续费")): cmd_edit_local(chat_id, text)
    elif text.startswith(("编辑备注", "编辑到期", "编辑价格", "编辑周期", "编辑端口", "编辑名称", "编辑系统", "编辑永久", "编辑自动续费", "续费服务器", "刷新地区")): cmd_edit_server(chat_id, text)
    elif text.startswith(("/del_server", "删除服务器", "删除机器")): cmd_del_server(chat_id, text)
    else:
        send(chat_id, "❓ <b>没有识别这个操作</b>\n\n你可以点击下方中文按钮，或发送：<code>帮助</code> / <code>启用命令</code>")




def handle_callback(callback):
    try:
        callback_id = callback.get("id")
        data = callback.get("data") or ""
        msg = callback.get("message") or {}
        chat_id = msg.get("chat", {}).get("id")
        message_id = msg.get("message_id")

        if not is_admin(chat_id):
            answer_callback(callback_id, "未授权")
            return

        answer_callback(callback_id, "处理中…")

        if data.startswith("add_toggle:"):
            _, mask, bit = data.split(":")
            mask = int(mask); bit = int(bit)
            if mask & bit:
                mask &= ~bit
            else:
                mask |= bit
            if bit == 16 and (mask & 16):
                mask &= ~4; mask &= ~32
            if bit == 4 and (mask & 4):
                mask &= ~16
            if bit == 32 and (mask & 32):
                mask &= ~16; mask |= 4
            edit_inline_message(chat_id, message_id, (
                "🧾✨ <b>添加服务器字段选择</b> ✨🧾\n\n"
                "✅ 勾选哪个字段，生成模板就带哪个字段。\n"
                "⬜ 不勾选的字段不会出现在模板里。\n\n"
                "📌 <b>必填字段：</b>名称、主机\n"
                "🧬 系统不勾选：机器人会尝试自动识别 SSH Banner。\n"
                "🎁 永久免费：不需要价格、币种、到期时间，也不会触发到期提醒。\n"
                "🔁 自动续费：服务器过期且仍在线时，自动顺延到下个自然周期。"
            ), add_builder_keyboard(mask))
            return

        if data.startswith("add_template:"):
            mask = int(data.split(":", 1)[1])
            send(chat_id, "📋✨ <b>添加服务器模板</b> ✨📋\n\n复制下面内容，按需改名称和 IP 后发送给机器人：\n\n" + f"<code>{h(add_template_text(mask))}</code>")
            return

        if data == "add_help_fields":
            send(chat_id, "❓🧾 <b>添加服务器字段说明</b>\n\n🖥️ 名称、🌐 主机：必填。\n📝 备注：可选，不勾选就不保存备注。\n🧬 系统：可选，不填会尝试读取 SSH Banner。\n💰 付费/到期：勾选后填写周期、价格、币种、到期时间。\n🎁 永久免费：适合永久免费机器，不需要价格和到期时间。\n🔁 自动续费：过期后如果服务器在线，会按月/季/年自动顺延。\n🔌 检测端口：默认 22，可改 80/443/自定义端口。")
            return

        if data == "servers:refresh":
            refresh_missing_meta()
            conn = db()
            rows = conn.execute("SELECT * FROM servers ORDER BY expire_at ASC, id ASC").fetchall()
            conn.close()
            if not rows:
                edit_inline_message(chat_id, message_id, "📭 暂无服务器记录。", [[{"text": "🧾 添加服务器", "callback_data": "servers:add_help"}]])
                return
            online = sum(1 for r in rows if check_tcp(r["host"], r["check_port"]))
            offline = len(rows) - online
            text = (
                "📋✨ <b>选择服务器查看详情</b> ✨📋\n"
                f"🕒 更新时间：{now_text()}\n\n"
                f"🟢 在线：{online} 台\n"
                f"🔴 离线：{offline} 台\n"
                f"📦 总数：{len(rows)} 台\n\n"
                "👇 每一排就是一台服务器，点击任意服务器查看详细信息。"
            )
            edit_inline_message(chat_id, message_id, text, servers_inline_keyboard(rows))
            return

        if data == "servers:add_help":
            cmd_add_server(chat_id, "添加服务器")
            return

        if data == "servers:edit_help":
            cmd_edit_help(chat_id)
            return

        if data == "servers:refresh_meta":
            cmd_refresh_all_meta(chat_id)
            return

        if data.startswith("server:"):
            sid = data.split(":", 1)[1]
            r = get_server_row(sid)
            if not r:
                edit_inline_message(chat_id, message_id, "❌ 没有找到这个服务器。", [[{"text": "⬅️ 返回", "callback_data": "servers:refresh"}]])
                return
            edit_inline_message(chat_id, message_id, server_detail_text(r), server_detail_keyboard(sid))
            return

        if data.startswith("server_edit:"):
            sid = data.split(":", 1)[1]
            send(chat_id, (
                "✏️✨ <b>编辑服务器</b> ✨✏️\n\n"
                f"当前服务器 ID：<code>{h(sid)}</code>\n\n"
                f"<code>编辑备注 {h(sid)} 新备注</code>\n"
                f"<code>编辑到期 {h(sid)} 2027-05-01</code>\n"
                f"<code>编辑价格 {h(sid)} 38 CNY</code>\n"
                f"<code>编辑周期 {h(sid)} 年付</code>\n"
                f"<code>编辑端口 {h(sid)} 443</code>\n"
                f"<code>编辑名称 {h(sid)} HK-Oracle</code>\n"
                f"<code>编辑系统 {h(sid)} Ubuntu 22.04</code>\n"
                f"<code>编辑永久 {h(sid)} 是</code> / <code>编辑永久 {h(sid)} 否</code>\n"
                f"<code>编辑自动续费 {h(sid)} 是</code> / <code>编辑自动续费 {h(sid)} 否</code>"
            ))
            return

        if data.startswith("server_renew_help:"):
            sid = data.split(":", 1)[1]
            send(chat_id, (
                "⏰✨ <b>续费服务器</b> ✨⏰\n\n"
                f"当前服务器 ID：<code>{h(sid)}</code>\n\n"
                f"手动续费：<code>续费服务器 {h(sid)} 2027-05-01</code>\n\n"
                "也可以点详情里的：📆 月付+1月 / 🗓️ 季付+3月 / 📅 年付+1年"
            ))
            return

        if data.startswith("refresh_meta:"):
            sid = data.split(":", 1)[1]
            conn = db()
            row = conn.execute("SELECT * FROM servers WHERE id=?", (sid,)).fetchone()
            if row:
                refresh_server_meta_row(conn, row)
                conn.commit()
            row = conn.execute("SELECT * FROM servers WHERE id=?", (sid,)).fetchone()
            conn.close()
            if row:
                edit_inline_message(chat_id, message_id, server_detail_text(row), server_detail_keyboard(sid))
            return

        if data.startswith("renew_month:") or data.startswith("renew_quarter:") or data.startswith("renew_year:"):
            kind, sid = data.split(":", 1)
            months = {"renew_month": 1, "renew_quarter": 3, "renew_year": 12}[kind]
            row, msg_text = quick_renew_server(sid, months)
            if row:
                edit_inline_message(chat_id, message_id, server_detail_text(row), server_detail_keyboard(sid))
                send(chat_id, f"✅⏰ <b>{h(msg_text)}</b>\n\n🆔 ID：<code>{h(sid)}</code>")
            else:
                send(chat_id, msg_text)
            return

        if data.startswith("toggle_free:"):
            sid = data.split(":", 1)[1]
            conn = db(); row = conn.execute("SELECT * FROM servers WHERE id=?", (sid,)).fetchone()
            if not row:
                conn.close(); send(chat_id, "❌ 没有找到这个服务器 ID。"); return
            new_val = 0 if is_free_forever_row(row) else 1
            if new_val:
                conn.execute("UPDATE servers SET free_forever=1, auto_renew=0, price=0, currency='USD', expire_at='永久' WHERE id=?", (sid,))
                conn.execute("DELETE FROM reminders WHERE server_id=?", (sid,))
            else:
                conn.execute("UPDATE servers SET free_forever=0, expire_at='' WHERE id=?", (sid,))
            conn.commit(); row = conn.execute("SELECT * FROM servers WHERE id=?", (sid,)).fetchone(); conn.close()
            edit_inline_message(chat_id, message_id, server_detail_text(row), server_detail_keyboard(sid))
            return

        if data.startswith("toggle_auto:"):
            sid = data.split(":", 1)[1]
            conn = db(); row = conn.execute("SELECT * FROM servers WHERE id=?", (sid,)).fetchone()
            if not row:
                conn.close(); send(chat_id, "❌ 没有找到这个服务器 ID。"); return
            if is_free_forever_row(row):
                conn.close(); send(chat_id, "🎁 永久免费服务器不需要自动续费。"); return
            new_val = 0 if is_auto_renew_row(row) else 1
            conn.execute("UPDATE servers SET auto_renew=? WHERE id=?", (new_val, sid))
            conn.commit(); row = conn.execute("SELECT * FROM servers WHERE id=?", (sid,)).fetchone(); conn.close()
            edit_inline_message(chat_id, message_id, server_detail_text(row), server_detail_keyboard(sid))
            return

        if data.startswith("delete_confirm:"):
            sid = data.split(":", 1)[1]
            edit_inline_message(chat_id, message_id, "⚠️🗑️ <b>确认删除服务器？</b>\n\n删除后会同时删除提醒和状态记录。", [
                [{"text": "✅ 确认删除", "callback_data": f"delete_do:{sid}"}],
                [{"text": "取消", "callback_data": f"server:{sid}"}],
            ])
            return

        if data.startswith("delete_do:"):
            sid = data.split(":", 1)[1]
            row = get_server_row(sid)
            if not row:
                edit_inline_message(chat_id, message_id, "❌ 没有找到这个服务器。", [[{"text": "⬅️ 返回", "callback_data": "servers:refresh"}]])
                return
            conn = db()
            conn.execute("DELETE FROM servers WHERE id=?", (sid,))
            conn.execute("DELETE FROM reminders WHERE server_id=?", (sid,))
            conn.execute("DELETE FROM server_status WHERE server_id=?", (sid,))
            conn.commit()
            conn.close()
            event_add("action", "删除服务器", f"已删除服务器：{row['name']}")
            edit_inline_message(chat_id, message_id, f"✅🗑️ <b>服务器已删除</b>\n\n🆔 ID：<code>{h(sid)}</code>\n🖥️ 名称：{h(row['name'])}", [[{"text": "⬅️ 返回服务器列表", "callback_data": "servers:refresh"}]])
            return
    except Exception as e:
        try:
            send(callback.get("message", {}).get("chat", {}).get("id"), f"❌ 按钮操作失败：{h(e)}")
        except Exception:
            pass


# =========================
# Enhanced navigation / wizard UI overrides
# =========================

def get_all_servers(order="expire"):
    refresh_missing_meta()
    conn = db()
    if order == "id":
        rows = conn.execute("SELECT * FROM servers ORDER BY id ASC").fetchall()
    else:
        rows = conn.execute("SELECT * FROM servers ORDER BY expire_at ASC, id ASC").fetchall()
    conn.close()
    return rows


def build_server_button_rows(rows, limit=12):
    kb = []
    for r in rows[:limit]:
        kb.append([{"text": server_button_label(r), "callback_data": f"server:{r['id']}"}])
    if len(rows) > limit:
        kb.append([{"text": f"📋 查看全部服务器（共 {len(rows)} 台）", "callback_data": "nav:servers"}])
    return kb


def dashboard_inline_keyboard():
    rows = get_all_servers()
    kb = [
        [{"text": "➡️ 下一步：选择服务器", "callback_data": "nav:servers"}],
    ]
    kb.extend(build_server_button_rows(rows, limit=8))
    kb.extend([
        [{"text": "🖥️ 本机状态", "callback_data": "nav:status"}, {"text": "🌐 流量", "callback_data": "nav:traffic"}, {"text": "💾 磁盘", "callback_data": "nav:disk"}],
        [{"text": "🧾 添加服务器", "callback_data": "nav:add"}, {"text": "📡 检测服务器", "callback_data": "nav:check"}],
        [{"text": "🧾 事件记录", "callback_data": "nav:events"}, {"text": "🛡️ 安全状态", "callback_data": "nav:security"}],
    ])
    return kb


def standard_nav_keyboard(prev_cb="nav:dashboard", next_cb="nav:servers", next_text="➡️ 下一步：选择服务器"):
    return [
        [{"text": "⬅️ 上一步：总览", "callback_data": prev_cb}, {"text": next_text, "callback_data": next_cb}],
        [{"text": "🧾 添加服务器", "callback_data": "nav:add"}, {"text": "📋 服务器列表", "callback_data": "nav:servers"}],
    ]


def server_detail_keyboard(sid):
    return [
        [{"text": "✏️ 编辑说明", "callback_data": f"server_edit:{sid}"}, {"text": "⏰ 续费说明", "callback_data": f"server_renew_help:{sid}"}],
        [{"text": "📆 月付+1月", "callback_data": f"renew_month:{sid}"}, {"text": "🗓️ 季付+3月", "callback_data": f"renew_quarter:{sid}"}, {"text": "📅 年付+1年", "callback_data": f"renew_year:{sid}"}],
        [{"text": "🎁 永久免费 开/关", "callback_data": f"toggle_free:{sid}"}, {"text": "🔁 自动续费 开/关", "callback_data": f"toggle_auto:{sid}"}],
        [{"text": "🌍 刷新地区", "callback_data": f"refresh_meta:{sid}"}, {"text": "🗑️ 删除确认", "callback_data": f"delete_confirm:{sid}"}],
        [{"text": "⬅️ 上一步：服务器列表", "callback_data": "nav:servers"}, {"text": "📊 返回总览", "callback_data": "nav:dashboard"}],
    ]


def dashboard_text():
    return (
        "📊✨ <b>服务器总览面板</b> ✨📊\n"
        f"🕒 更新时间：{now_text()}\n\n"
        f"{status_block()}\n\n{traffic_block()}\n\n{servers_summary_block()}\n\n{events_block(6)}\n\n"
        "━━━━━━━━━━━━━━\n"
        "👇 <b>下一步：</b>点击下方任意服务器按钮查看详细信息，也可以进入服务器列表。"
    )[:3900]


def cmd_dashboard(chat_id):
    send_inline(chat_id, dashboard_text(), dashboard_inline_keyboard())


def cmd_status(chat_id):
    send_inline(
        chat_id,
        "✅✨ <b>当前机器状态</b> ✨✅\n" + f"🕒 更新时间：{now_text()}\n\n" + status_block(),
        standard_nav_keyboard(next_cb="nav:traffic", next_text="➡️ 下一步：查看流量")
    )


def disk_text():
    lines = ["💾✨ <b>磁盘使用情况</b> ✨💾", f"🕒 更新时间：{now_text()}", ""]
    try:
        for p in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(p.mountpoint)
            except Exception:
                continue
            percent = usage.percent
            status = "🚨 空间严重不足" if percent >= DISK_ALERT else "⚠️ 空间偏高" if percent >= 80 else "✅ 空间正常"
            lines.append(
                "━━━━━━━━━━━━━━\n"
                f"📦 <b>挂载位置：</b><code>{h(p.mountpoint)}</code>\n"
                f"🧩 <b>设备名称：</b><code>{h(p.device)}</code>\n"
                f"📁 <b>文件系统：</b>{h(p.fstype or '未知')}\n"
                f"💽 <b>总容量：</b>{fmt_size(usage.total)}\n"
                f"📤 <b>已使用：</b>{fmt_size(usage.used)}\n"
                f"📥 <b>可用空间：</b>{fmt_size(usage.free)}\n"
                f"📊 <b>使用率：</b>{percent:.0f}%\n"
                f"📌 <b>状态：</b>{status}\n"
            )
        root = psutil.disk_usage("/")
        lines.append("━━━━━━━━━━━━━━")
        lines.append(f"🚨💥 <b>总体结论：</b>根目录使用率已超过 {DISK_ALERT}%，请尽快清理。" if root.percent >= DISK_ALERT else "✅🌿 <b>总体结论：</b>磁盘空间正常。")
        lines.append("\n👇 <b>下一步：</b>可以继续查看安全状态或返回总览。")
    except Exception as e:
        return f"❌ 获取磁盘信息失败：{h(e)}"
    return "\n".join(lines)[:3900]


def cmd_disk(chat_id):
    send_inline(chat_id, disk_text(), standard_nav_keyboard(next_cb="nav:security", next_text="➡️ 下一步：安全状态"))


def cmd_traffic(chat_id):
    send_inline(
        chat_id,
        "🌐✨ <b>服务器流量使用情况</b> ✨🌐\n" + f"🕒 更新时间：{now_text()}\n\n" + traffic_block() + "\n\n👇 <b>下一步：</b>继续查看磁盘使用情况。",
        standard_nav_keyboard(next_cb="nav:disk", next_text="➡️ 下一步：查看磁盘")
    )


def security_text():
    ssh = shell("systemctl is-active ssh 2>/dev/null || systemctl is-active sshd 2>/dev/null || echo unknown", 5)
    f2b = shell("systemctl is-active fail2ban 2>/dev/null || echo unknown", 5)
    updates = shell("apt list --upgradable 2>/dev/null | sed 1d | wc -l", 10)
    ufw_raw = shell("ufw status 2>/dev/null || echo 未安装或未启用", 5)
    firewall = "✅ 已开启" if "Status: active" in ufw_raw else "⚠️ 未开启" if "Status: inactive" in ufw_raw else "❓ 未检测到或未安装"
    return (
        "🛡️✨ <b>综合安全状态</b> ✨🛡️\n"
        f"🕒 更新时间：{now_text()}\n\n"
        f"🔐 <b>SSH 服务：</b>{service_cn(ssh)}\n"
        f"🚫 <b>防爆破服务：</b>{service_cn(f2b)}\n"
        f"🔥 <b>防火墙状态：</b>{firewall}\n"
        f"📦 <b>可更新软件包：</b>{h(updates)} 个\n\n"
        "👇 <b>下一步：</b>可以查看登录记录或返回总览。"
    )


def cmd_security(chat_id):
    send_inline(chat_id, security_text(), standard_nav_keyboard(next_cb="nav:login", next_text="➡️ 下一步：登录记录"))


def login_log_text():
    raw = shell("last -w -n 10 | grep -v 'wtmp begins' || true", 10)
    if not raw.strip():
        return "🔐✨ <b>最近登录记录</b> ✨🔐\n\n暂无登录记录。"
    lines = ["🔐✨ <b>最近 SSH 登录记录</b> ✨🔐", f"🕒 更新时间：{now_text()}", ""]
    for line in raw.splitlines()[:10]:
        parts = line.split()
        if len(parts) < 3:
            continue
        lines.append(
            "━━━━━━━━━━━━━━\n"
            f"👤 <b>登录用户：</b>{h(parts[0])}\n"
            f"💻 <b>登录终端：</b>{h(parts[1])}\n"
            f"🌐 <b>来源地址：</b>{h(parts[2])}\n"
            f"⏰ <b>登录时间：</b>{h(' '.join(parts[3:8]) if len(parts) >= 8 else ' '.join(parts[3:]) or '未知')}"
        )
    lines.append("\n👇 <b>下一步：</b>继续查看防爆破状态。")
    return "\n".join(lines)[:3900]


def cmd_login_log(chat_id):
    send_inline(chat_id, login_log_text(), standard_nav_keyboard(next_cb="nav:fail2ban", next_text="➡️ 下一步：防爆破状态"))


def fail2ban_text():
    raw = shell(root_cmd("fail2ban-client status sshd 2>/dev/null || fail2ban-client status 2>/dev/null || echo FAIL2BAN_NOT_RUNNING"), 10)
    if "FAIL2BAN_NOT_RUNNING" in raw:
        return "🚫✨ <b>防爆破状态</b> ✨🚫\n\n⚠️ <b>当前状态：</b>Fail2ban 未运行或未配置 SSH 防护。"
    vals = {"Currently failed": "未知", "Total failed": "未知", "Currently banned": "未知", "Total banned": "未知"}
    for line in raw.splitlines():
        line = line.strip()
        for k in list(vals):
            if k + ":" in line:
                vals[k] = line.split(":", 1)[1].strip()
    return (
        "🚫✨ <b>防爆破状态</b> ✨🚫\n"
        f"🕒 更新时间：{now_text()}\n\n"
        "🛡️ <b>服务状态：</b>✅ 已运行\n"
        f"⚠️ <b>当前失败登录次数：</b>{h(vals['Currently failed'])}\n"
        f"📊 <b>累计失败登录次数：</b>{h(vals['Total failed'])}\n"
        f"🔒 <b>当前封禁 IP 数量：</b>{h(vals['Currently banned'])}\n"
        f"📌 <b>累计封禁 IP 数量：</b>{h(vals['Total banned'])}\n\n"
        "👇 <b>下一步：</b>返回总览或选择服务器。"
    )


def cmd_fail2ban(chat_id):
    send_inline(chat_id, fail2ban_text(), standard_nav_keyboard(next_cb="nav:servers", next_text="➡️ 下一步：选择服务器"))


def events_text(limit=15):
    rows = get_recent_events(limit)
    if not rows:
        return "🧾 暂无服务器事件记录。"
    icons = {"offline": "🚨", "online": "✅", "expiry": "⏰", "system": "🔥", "action": "🛠️", "security": "🛡️"}
    lines = ["🧾✨ <b>服务器事件记录</b> ✨🧾", f"🕒 更新时间：{now_text()}", ""]
    for r in rows:
        lines.append("━━━━━━━━━━━━━━\n" f"{icons.get(r['event_type'], '📌')} <b>{h(r['title'])}</b>\n" f"🕒 <b>时间：</b>{h(r['created_at'])}\n" f"📝 <b>内容：</b>{h(r['content'])}")
    lines.append("\n👇 <b>下一步：</b>查看服务器详情或返回总览。")
    return "\n".join(lines)[:3900]


def cmd_events(chat_id):
    send_inline(chat_id, events_text(15), standard_nav_keyboard(next_cb="nav:servers", next_text="➡️ 下一步：选择服务器"))


def cmd_server_buttons(chat_id, title="📋✨ <b>选择服务器查看详情</b> ✨📋"):
    refresh_missing_meta()
    rows = get_all_servers()
    if not rows:
        send_inline(chat_id, "📭 暂无服务器记录。\n\n👇 下一步：点击添加服务器开始。", [[{"text": "🧾 下一步：添加服务器", "callback_data": "nav:add"}], [{"text": "⬅️ 返回总览", "callback_data": "nav:dashboard"}]])
        return
    online = sum(1 for r in rows if check_tcp(r["host"], r["check_port"]))
    offline = len(rows) - online
    text = (
        f"{title}\n"
        f"🕒 更新时间：{now_text()}\n\n"
        f"🟢 在线：{online} 台\n"
        f"🔴 离线：{offline} 台\n"
        f"📦 总数：{len(rows)} 台\n\n"
        "👇 每一排就是一台服务器，点击任意服务器查看详细信息。\n"
        "➡️ 进入详情后可以继续编辑、续费、删除、刷新地区。"
    )
    kb = servers_inline_keyboard(rows)
    kb.append([{"text": "⬅️ 上一步：总览", "callback_data": "nav:dashboard"}, {"text": "➡️ 下一步：添加服务器", "callback_data": "nav:add"}])
    send_inline(chat_id, text, kb)


def cmd_check_servers(chat_id):
    refresh_missing_meta()
    rows = get_all_servers(order="id")
    if not rows:
        send_inline(chat_id, "📭 暂无服务器记录。\n\n👇 下一步：点击添加服务器开始。", [[{"text": "🧾 下一步：添加服务器", "callback_data": "nav:add"}], [{"text": "⬅️ 返回总览", "callback_data": "nav:dashboard"}]])
        return
    online_count = 0
    offline_count = 0
    lines = ["📡✨ <b>服务器在线状态检测</b> ✨📡", f"🕒 检测时间：{now_text()}", ""]
    for r in rows:
        online = check_tcp(r["host"], r["check_port"])
        if online:
            status_text = "🟢 在线"; online_count += 1
        else:
            status_text = "🔴 离线"; offline_count += 1
        lines.append("━━━━━━━━━━━━━━\n" f"📡 <b>状态：</b>{status_text}\n" f"🖥️ <b>名称：</b>{h(r['name'])}\n" f"📍 <b>地区：</b>{server_location_line(r)}\n" f"🌐 <b>地址：</b><code>{h(r['host'])}:{h(r['check_port'])}</code>\n" f"📝 <b>备注：</b>{h(r['note'] or '无')}")
    lines.insert(2, f"🟢 在线：{online_count} 台\n🔴 离线：{offline_count} 台\n📦 总数：{len(rows)} 台\n")
    lines.append("\n👇 <b>下一步：</b>选择服务器查看详细信息。")
    kb = servers_inline_keyboard(rows)
    kb.append([{"text": "⬅️ 返回总览", "callback_data": "nav:dashboard"}])
    send_inline(chat_id, "\n".join(lines)[:3900], kb)


# Keep original callback handler for existing server/add workflows, add navigation callbacks first.
_original_handle_callback = handle_callback

def handle_callback(callback):
    try:
        callback_id = callback.get("id")
        data = callback.get("data") or ""
        msg = callback.get("message") or {}
        chat_id = msg.get("chat", {}).get("id")
        message_id = msg.get("message_id")

        if not is_admin(chat_id):
            answer_callback(callback_id, "未授权")
            return

        if data.startswith("nav:"):
            answer_callback(callback_id, "已打开")
            page = data.split(":", 1)[1]

            if page == "dashboard":
                edit_inline_message(chat_id, message_id, dashboard_text(), dashboard_inline_keyboard())
                return

            if page == "servers":
                refresh_missing_meta()
                rows = get_all_servers()
                if not rows:
                    edit_inline_message(chat_id, message_id, "📭 暂无服务器记录。\n\n👇 下一步：添加服务器。", [[{"text": "🧾 下一步：添加服务器", "callback_data": "nav:add"}], [{"text": "⬅️ 返回总览", "callback_data": "nav:dashboard"}]])
                    return
                online = sum(1 for r in rows if check_tcp(r["host"], r["check_port"]))
                offline = len(rows) - online
                text = (
                    "📋✨ <b>选择服务器查看详情</b> ✨📋\n"
                    f"🕒 更新时间：{now_text()}\n\n"
                    f"🟢 在线：{online} 台\n"
                    f"🔴 离线：{offline} 台\n"
                    f"📦 总数：{len(rows)} 台\n\n"
                    "👇 每一排就是一台服务器，点击任意服务器查看详细信息。"
                )
                kb = servers_inline_keyboard(rows)
                kb.append([{"text": "⬅️ 上一步：总览", "callback_data": "nav:dashboard"}, {"text": "➡️ 下一步：添加服务器", "callback_data": "nav:add"}])
                edit_inline_message(chat_id, message_id, text, kb)
                return

            if page == "status":
                edit_inline_message(chat_id, message_id, "✅✨ <b>当前机器状态</b> ✨✅\n" + f"🕒 更新时间：{now_text()}\n\n" + status_block(), standard_nav_keyboard(next_cb="nav:traffic", next_text="➡️ 下一步：查看流量"))
                return

            if page == "traffic":
                edit_inline_message(chat_id, message_id, "🌐✨ <b>服务器流量使用情况</b> ✨🌐\n" + f"🕒 更新时间：{now_text()}\n\n" + traffic_block(), standard_nav_keyboard(next_cb="nav:disk", next_text="➡️ 下一步：查看磁盘"))
                return

            if page == "disk":
                edit_inline_message(chat_id, message_id, disk_text(), standard_nav_keyboard(next_cb="nav:security", next_text="➡️ 下一步：安全状态"))
                return

            if page == "security":
                edit_inline_message(chat_id, message_id, security_text(), standard_nav_keyboard(next_cb="nav:login", next_text="➡️ 下一步：登录记录"))
                return

            if page == "login":
                edit_inline_message(chat_id, message_id, login_log_text(), standard_nav_keyboard(next_cb="nav:fail2ban", next_text="➡️ 下一步：防爆破状态"))
                return

            if page == "fail2ban":
                edit_inline_message(chat_id, message_id, fail2ban_text(), standard_nav_keyboard(next_cb="nav:servers", next_text="➡️ 下一步：选择服务器"))
                return

            if page == "events":
                edit_inline_message(chat_id, message_id, events_text(15), standard_nav_keyboard(next_cb="nav:servers", next_text="➡️ 下一步：选择服务器"))
                return

            if page == "add":
                cmd_add_builder(chat_id)
                return

            if page == "check":
                cmd_check_servers(chat_id)
                return

            if page == "help":
                cmd_help(chat_id)
                return

        # Improve existing refresh callback to include navigation row
        if data == "servers:refresh":
            answer_callback(callback_id, "已刷新")
            rows = get_all_servers()
            if not rows:
                edit_inline_message(chat_id, message_id, "📭 暂无服务器记录。\n\n👇 下一步：添加服务器。", [[{"text": "🧾 下一步：添加服务器", "callback_data": "nav:add"}], [{"text": "⬅️ 返回总览", "callback_data": "nav:dashboard"}]])
                return
            online = sum(1 for r in rows if check_tcp(r["host"], r["check_port"]))
            offline = len(rows) - online
            text = (
                "📋✨ <b>选择服务器查看详情</b> ✨📋\n"
                f"🕒 更新时间：{now_text()}\n\n"
                f"🟢 在线：{online} 台\n"
                f"🔴 离线：{offline} 台\n"
                f"📦 总数：{len(rows)} 台\n\n"
                "👇 每一排就是一台服务器，点击任意服务器查看详细信息。"
            )
            kb = servers_inline_keyboard(rows)
            kb.append([{"text": "⬅️ 上一步：总览", "callback_data": "nav:dashboard"}, {"text": "➡️ 下一步：添加服务器", "callback_data": "nav:add"}])
            edit_inline_message(chat_id, message_id, text, kb)
            return

    except Exception as e:
        try:
            send(callback.get("message", {}).get("chat", {}).get("id"), f"❌ 按钮操作失败：{h(e)}")
        except Exception:
            pass
        return

    return _original_handle_callback(callback)



# ============================================================
# Final optimization overrides: quiet keyboard, stable online/offline,
# dashboard server buttons, and per-server probe deployment command.
# ============================================================

# 1) Do not attach the large Chinese keyboard to every message.
#    Only functions that explicitly pass keyboard=True will show it.
def send(chat_id, text, keyboard=False):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    if keyboard:
        payload["reply_markup"] = menu_keyboard()
    return tg("sendMessage", payload)


def broadcast(text):
    for admin in ADMIN_IDS:
        send(admin, text, keyboard=False)


_old_init_db_final = init_db
def init_db():
    _old_init_db_final()
    conn = db()
    # Debounce columns prevent repeated online/offline spam caused by network jitter.
    for col, definition in [
        ("fail_count", "INTEGER DEFAULT 0"),
        ("success_count", "INTEGER DEFAULT 0"),
        ("notified_offline", "INTEGER DEFAULT 0"),
    ]:
        ensure_column(conn, "server_status", col, definition)
    conn.commit()
    conn.close()


def monitor_server_online_status():
    """Stable online/offline monitor.

    - Requires 2 consecutive failures before sending offline alert.
    - Requires 2 consecutive successes after a notified offline alert before sending recovery.
    - Never sends repeated online messages while the server remains online.
    """
    conn = db()
    rows = conn.execute("SELECT * FROM servers").fetchall()
    for r in rows:
        sid = r["id"]
        raw_online = check_tcp(r["host"], r["check_port"])
        old = conn.execute("SELECT * FROM server_status WHERE server_id=?", (sid,)).fetchone()
        now = now_text()

        if not old:
            conn.execute(
                "INSERT INTO server_status(server_id, last_status, last_checked_at, last_changed_at, fail_count, success_count, notified_offline) VALUES(?,?,?,?,?,?,?)",
                (sid, "online" if raw_online else "offline", now, now, 0 if raw_online else 1, 1 if raw_online else 0, 0)
            )
            conn.commit()
            continue

        old_status = old["last_status"] or "unknown"
        fail_count = int(old["fail_count"] if "fail_count" in old.keys() and old["fail_count"] is not None else 0)
        success_count = int(old["success_count"] if "success_count" in old.keys() and old["success_count"] is not None else 0)
        notified_offline = int(old["notified_offline"] if "notified_offline" in old.keys() and old["notified_offline"] is not None else 0)

        if raw_online:
            success_count += 1
            fail_count = 0
            if old_status == "offline" and notified_offline == 1 and success_count >= 2:
                conn.execute(
                    "UPDATE server_status SET last_status='online', last_checked_at=?, last_changed_at=?, fail_count=?, success_count=?, notified_offline=0 WHERE server_id=?",
                    (now, now, fail_count, success_count, sid)
                )
                conn.commit()
                push_event("online", f"服务器恢复在线：{r['name']}", online_push_text(r))
            else:
                # If it was unknown/offline but no offline alert was sent, silently mark stable online.
                new_status = "online" if success_count >= 2 else old_status
                conn.execute(
                    "UPDATE server_status SET last_status=?, last_checked_at=?, fail_count=?, success_count=? WHERE server_id=?",
                    (new_status, now, fail_count, success_count, sid)
                )
                conn.commit()
        else:
            fail_count += 1
            success_count = 0
            if old_status != "offline" and fail_count >= 2:
                conn.execute(
                    "UPDATE server_status SET last_status='offline', last_checked_at=?, last_changed_at=?, fail_count=?, success_count=?, notified_offline=1 WHERE server_id=?",
                    (now, now, fail_count, success_count, sid)
                )
                conn.commit()
                push_event("offline", f"服务器离线：{r['name']}", offline_push_text(r))
            else:
                conn.execute(
                    "UPDATE server_status SET last_checked_at=?, fail_count=?, success_count=? WHERE server_id=?",
                    (now, fail_count, success_count, sid)
                )
                conn.commit()
    conn.close()


def dashboard_inline_keyboard():
    rows = get_all_servers()
    kb = []
    if rows:
        kb.append([{"text": "📋 下一步：选择服务器", "callback_data": "nav:servers"}])
        kb.extend(build_server_button_rows(rows, limit=12))
    else:
        kb.append([{"text": "🧾 下一步：添加服务器", "callback_data": "nav:add"}])
    kb.extend([
        [{"text": "🖥️ 本机状态", "callback_data": "nav:status"}, {"text": "🌐 流量", "callback_data": "nav:traffic"}, {"text": "💾 磁盘", "callback_data": "nav:disk"}],
        [{"text": "📡 检测服务器", "callback_data": "nav:check"}, {"text": "🧾 添加服务器", "callback_data": "nav:add"}],
        [{"text": "🧾 事件记录", "callback_data": "nav:events"}, {"text": "🛡️ 安全状态", "callback_data": "nav:security"}],
    ])
    return kb


def server_detail_keyboard(sid):
    return [
        [{"text": "📡 一键部署探针", "callback_data": f"agent_cmd:{sid}"}],
        [{"text": "✏️ 编辑说明", "callback_data": f"server_edit:{sid}"}, {"text": "⏰ 续费说明", "callback_data": f"server_renew_help:{sid}"}],
        [{"text": "📆 月付+1月", "callback_data": f"renew_month:{sid}"}, {"text": "🗓️ 季付+3月", "callback_data": f"renew_quarter:{sid}"}, {"text": "📅 年付+1年", "callback_data": f"renew_year:{sid}"}],
        [{"text": "🎁 永久免费 开/关", "callback_data": f"toggle_free:{sid}"}, {"text": "🔁 自动续费 开/关", "callback_data": f"toggle_auto:{sid}"}],
        [{"text": "🌍 刷新地区", "callback_data": f"refresh_meta:{sid}"}, {"text": "🗑️ 删除确认", "callback_data": f"delete_confirm:{sid}"}],
        [{"text": "⬅️ 上一步：服务器列表", "callback_data": "nav:servers"}, {"text": "📊 返回总览", "callback_data": "nav:dashboard"}],
    ]


def agent_install_command(server_name="server"):
    admin_id = next(iter(ADMIN_IDS), "你的TG数字ID")
    token = BOT_TOKEN or "你的TG_BOT_TOKEN"
    safe_name = str(server_name or "server").replace('"', '').replace("'", "")
    return (
        "wget -qO- https://raw.githubusercontent.com/lxfcx/Oracle/main/agent.sh | "
        f"bash -s -- --token \"{token}\" --chat \"{admin_id}\" --name \"{safe_name}\""
    )


def cmd_agent_command(chat_id, sid=None):
    server_name = "server"
    title = "📡✨ <b>一键部署探针命令</b> ✨📡"
    if sid:
        r = get_server_row(sid)
        if r:
            server_name = r["name"]
            title = f"📡✨ <b>{h(server_name)} 一键部署探针</b> ✨📡"
    cmd = agent_install_command(server_name)
    send_inline(chat_id, (
        f"{title}\n\n"
        "━━━━━━━━━━━━━━\n"
        "📌 <b>用途：</b>复制下面命令到对应服务器 SSH 执行。\n"
        "📌 <b>效果：</b>目标服务器会安装轻量探针，直接向 TG 推送上线、磁盘/内存/CPU 告警。\n"
        "📌 <b>说明：</b>主机器人当前的在线/离线检测仍使用端口检测；真正类似探针的实时状态需要在每台服务器安装这个 Agent。\n"
        "━━━━━━━━━━━━━━\n\n"
        f"<code>{h(cmd)}</code>"
    ), [[{"text": "⬅️ 返回服务器列表", "callback_data": "nav:servers"}, {"text": "📊 返回总览", "callback_data": "nav:dashboard"}]])


def cmd_enable_commands(chat_id):
    result = set_bot_commands()
    send(chat_id, (
        "✅✨ <b>中文按钮菜单已启用</b> ✨✅\n\n"
        "📌 下方中文按钮键盘只在这里显示。\n"
        "📌 其它功能页面改为使用页面内按钮，不会每条消息都附带一模一样的大键盘。\n\n"
        "常用：\n"
        "📊 <code>服务器总览</code>\n"
        "📋 <code>查看服务器</code>\n"
        "📡 <code>部署命令</code>\n"
        "🧾 <code>添加服务器</code>\n"
        "⌨️ <code>收起键盘</code>\n\n"
        f"左侧命令菜单设置结果：<code>{h(result.get('ok'))}</code>"
    ), keyboard=True)


_old_handle_final = handle
def handle(chat_id, text):
    ct = clean_command_text(text)
    if ct in ["部署命令", "探针命令", "一键部署命令", "/agent", "/agent_command"]:
        cmd_agent_command(chat_id)
        return
    if ct.startswith(("部署命令 ", "探针命令 ", "一键部署命令 ")):
        parts = ct.split(maxsplit=1)
        cmd_agent_command(chat_id, parts[1].strip())
        return
    _old_handle_final(chat_id, text)


_old_handle_callback_final = handle_callback
def handle_callback(callback):
    try:
        callback_id = callback.get("id")
        data = callback.get("data") or ""
        msg = callback.get("message") or {}
        chat_id = msg.get("chat", {}).get("id")
        if data.startswith("agent_cmd:"):
            if not is_admin(chat_id):
                answer_callback(callback_id, "未授权")
                return
            answer_callback(callback_id, "已生成部署命令")
            sid = data.split(":", 1)[1]
            cmd_agent_command(chat_id, sid)
            return
    except Exception:
        pass
    return _old_handle_callback_final(callback)


def poll():
    offset = 0
    last_check = 0
    set_bot_commands()
    while True:
        try:
            now_ts = time.time()
            if now_ts - last_check >= CHECK_INTERVAL:
                monitor_local_system(); monitor_server_online_status(); auto_renew_expired_online_servers(); monitor_expiry(); last_check = now_ts
            r = requests.get(f"{API}/getUpdates", params={"timeout": 25, "offset": offset}, timeout=35).json()
            for item in r.get("result", []):
                offset = item["update_id"] + 1
                if item.get("callback_query"):
                    handle_callback(item["callback_query"])
                    continue
                msg = item.get("message") or item.get("edited_message")
                if not msg: continue
                chat_id = msg["chat"]["id"]
                text = msg.get("text", "").strip()
                if text: handle(chat_id, text)
        except KeyboardInterrupt:
            break
        except Exception:
            time.sleep(5)


# ============================================================
# Ultra navigation overrides: compact dashboard, local-as-server,
# per-target status/traffic/disk/events/edit, 300s offline grace,
# configurable expiry reminder days.
# ============================================================

OFFLINE_GRACE_SECONDS = int(os.getenv("OFFLINE_GRACE_SECONDS", "300"))
RECOVERY_STABLE_SECONDS = int(os.getenv("RECOVERY_STABLE_SECONDS", "120"))


def get_reminder_days():
    try:
        p = get_local_profile()
        raw = p.get("reminder_days") or ",".join(str(x) for x in DUE_REMIND_DAYS)
        vals = []
        for item in str(raw).replace("，", ",").split(","):
            item = item.strip()
            if item == "":
                continue
            vals.append(int(item))
        vals = sorted(set(vals), reverse=True)
        return vals or DUE_REMIND_DAYS
    except Exception:
        return DUE_REMIND_DAYS


def set_reminder_days(value):
    raw = str(value or "").replace("，", ",")
    vals = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        vals.append(int(item))
    vals = sorted(set(vals), reverse=True)
    if not vals:
        raise ValueError("提醒天数不能为空")
    set_local_profile_value("reminder_days", ",".join(str(x) for x in vals))
    return vals


def compact_local_row():
    s = get_local_status()
    p = get_local_profile()
    name = one_line(p.get("name"), socket.gethostname())
    place = " ".join(x for x in [s.get("country"), s.get("region"), s.get("city")] if x and x != "未知")
    return {
        "id": "local",
        "name": name,
        "label": f"🟢 {s.get('flag','🌐')} 🏠 本机｜{name}｜{local_expire_text(p)}",
        "short": f"🟢 本机｜{name}｜{s.get('flag','🌐')} {place or s.get('country','未知')}",
    }


def server_status_icon_quick(r):
    return "🟢" if check_tcp(r["host"], r["check_port"], timeout=2) else "🔴"


def target_buttons(include_local=True, limit=20, cb_prefix="target"):
    kb = []
    if include_local:
        kb.append([{"text": compact_local_row()["label"][:62], "callback_data": f"{cb_prefix}:local"}])
    rows = get_all_servers(order="id")
    for r in rows[:limit]:
        kb.append([{"text": server_button_label(r)[:62], "callback_data": f"{cb_prefix}:server:{r['id']}"}])
    return kb


def main_nav_row():
    return [
        {"text": "📊 总览", "callback_data": "nav:dashboard"},
        {"text": "📋 选择服务器", "callback_data": "nav:servers"},
    ]


def bottom_nav(extra=None):
    rows = []
    if extra:
        rows.extend(extra)
    rows.append(main_nav_row())
    rows.append([
        {"text": "🧾 添加", "callback_data": "nav:add"},
        {"text": "🛠️ 工具", "callback_data": "nav:tools"},
    ])
    return rows


def compact_dashboard_text():
    rows = get_all_servers(order="id")
    online = 0
    offline = 0
    for r in rows:
        if check_tcp(r["host"], r["check_port"], timeout=2):
            online += 1
        else:
            offline += 1
    local = compact_local_row()
    due_days = ",".join(str(x) for x in get_reminder_days())
    return (
        "📊✨ <b>服务器总览</b> ✨📊\n"
        f"🕒 更新时间：{now_text()}\n\n"
        "📌 <b>概览</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🏠 本机：1 台\n"
        f"🟢 在线：{online + 1} 台\n"
        f"🔴 离线：{offline} 台\n"
        f"📦 总数：{len(rows) + 1} 台\n"
        f"⏰ 到期提醒：提前 {h(due_days)} 天\n"
        f"⏳ 离线宽限：{OFFLINE_GRACE_SECONDS} 秒\n\n"
        "👇 <b>下一步：请选择本机或任意服务器。</b>"
    )


def dashboard_inline_keyboard():
    kb = target_buttons(include_local=True, cb_prefix="target")
    kb.extend([
        [
            {"text": "🌐 流量", "callback_data": "select:traffic"},
            {"text": "💾 磁盘", "callback_data": "select:disk"},
            {"text": "📡 检测", "callback_data": "nav:check"},
        ],
        [
            {"text": "🧾 事件", "callback_data": "select:events"},
            {"text": "✏️ 编辑", "callback_data": "select:edit"},
            {"text": "⏰ 提醒", "callback_data": "nav:reminder"},
        ],
        [
            {"text": "🧾 添加服务器", "callback_data": "nav:add"},
            {"text": "🛡️ 安全状态", "callback_data": "nav:security"},
        ],
    ])
    return kb


def cmd_dashboard(chat_id):
    send_inline(chat_id, compact_dashboard_text(), dashboard_inline_keyboard())


def local_detail_text():
    return (
        "🏠✨ <b>本机详细信息</b> ✨🏠\n"
        f"🕒 更新时间：{now_text()}\n\n"
        f"{status_block()}\n\n"
        "👇 <b>下一步：</b>可继续查看本机流量、磁盘、事件或编辑资料。"
    )[:3900]


def local_detail_keyboard():
    return bottom_nav([
        [
            {"text": "🌐 本机流量", "callback_data": "view:traffic:local"},
            {"text": "💾 本机磁盘", "callback_data": "view:disk:local"},
        ],
        [
            {"text": "🧾 本机事件", "callback_data": "view:events:local"},
            {"text": "✏️ 编辑本机", "callback_data": "edit:local"},
        ],
        [{"text": "🌍 刷新本机地区", "callback_data": "local:refresh_meta"}],
    ])


def remote_detail_text(r):
    # Keep remote detail clean; no long command block by default.
    online = check_tcp(r["host"], r["check_port"], timeout=3)
    status_text = "🟢 在线" if online else "🔴 离线"
    free = is_free_forever_row(r)
    auto = is_auto_renew_row(r)
    return (
        "🖥️✨ <b>服务器详情</b> ✨🖥️\n"
        f"🕒 更新时间：{now_text()}\n\n"
        "━━━━━━━━━━━━━━\n"
        f"📡 状态：{status_text}\n"
        f"🆔 ID：<code>{r['id']}</code>\n"
        f"🖥️ 名称：{h(r['name'])}\n"
        f"🌐 主机：<code>{h(r['host'])}:{h(r['check_port'])}</code>\n"
        f"📍 地区：{server_location_line(r)}\n"
        f"🏢 运营商：{h(r['isp'] if 'isp' in r.keys() and r['isp'] else '未知')}\n"
        f"🧬 系统：{h(r['os_name'] if 'os_name' in r.keys() and r['os_name'] else '未知系统')}\n"
        f"📝 备注：{h(r['note'] or '无')}\n"
        f"🎁 永久免费：{bool_text(free)}\n"
        f"🔁 自动续费：{bool_text(auto)}\n"
        f"💰 价格：{server_price_line(r)}\n"
        f"📆 到期：{h(r['expire_at'] if r['expire_at'] else '未设置')}｜{expire_status_text(r['expire_at'], free)}\n"
        "━━━━━━━━━━━━━━\n"
        "👇 <b>下一步：</b>查看该服务器流量/磁盘/事件，或编辑续费。"
    )[:3900]


def remote_detail_keyboard(sid):
    return bottom_nav([
        [
            {"text": "🌐 流量", "callback_data": f"view:traffic:server:{sid}"},
            {"text": "💾 磁盘", "callback_data": f"view:disk:server:{sid}"},
            {"text": "🧾 事件", "callback_data": f"view:events:server:{sid}"},
        ],
        [
            {"text": "✏️ 编辑", "callback_data": f"edit:server:{sid}"},
            {"text": "⏰ 续费", "callback_data": f"server_renew_help:{sid}"},
            {"text": "📡 探针", "callback_data": f"agent_cmd:{sid}"},
        ],
        [
            {"text": "📆 +1月", "callback_data": f"renew_month:{sid}"},
            {"text": "🗓️ +3月", "callback_data": f"renew_quarter:{sid}"},
            {"text": "📅 +1年", "callback_data": f"renew_year:{sid}"},
        ],
        [
            {"text": "🎁 永久免费", "callback_data": f"toggle_free:{sid}"},
            {"text": "🔁 自动续费", "callback_data": f"toggle_auto:{sid}"},
        ],
        [
            {"text": "🌍 刷新地区", "callback_data": f"refresh_meta:{sid}"},
            {"text": "🗑️ 删除", "callback_data": f"delete_confirm:{sid}"},
        ],
    ])


def target_select_text(action):
    names = {
        "status": "查看状态",
        "traffic": "查看流量",
        "disk": "查看磁盘",
        "events": "查看事件",
        "edit": "编辑资料",
        "check": "检测状态",
    }
    return (
        f"📋✨ <b>选择目标：{h(names.get(action, action))}</b> ✨📋\n"
        f"🕒 更新时间：{now_text()}\n\n"
        "👇 请选择本机或任意服务器继续下一步。"
    )


def target_select_keyboard(action):
    kb = target_buttons(include_local=True, cb_prefix=f"view:{action}")
    kb.extend(bottom_nav())
    return kb


def cmd_server_buttons(chat_id, title="📋✨ <b>选择服务器查看详情</b> ✨📋"):
    rows = get_all_servers(order="id")
    online = sum(1 for r in rows if check_tcp(r["host"], r["check_port"], timeout=2))
    offline = len(rows) - online
    text = (
        f"{title}\n"
        f"🕒 更新时间：{now_text()}\n\n"
        "🏠 本机：1 台\n"
        f"🟢 在线：{online + 1} 台\n"
        f"🔴 离线：{offline} 台\n"
        f"📦 总数：{len(rows) + 1} 台\n\n"
        "👇 每一排就是一台机器，点击进入详情。"
    )
    kb = target_buttons(include_local=True, cb_prefix="target")
    kb.extend(bottom_nav([[{"text": "🔄 刷新列表", "callback_data": "nav:servers"}]]))
    send_inline(chat_id, text, kb)


def cmd_status(chat_id):
    send_inline(chat_id, target_select_text("status"), target_select_keyboard("status"))


def cmd_traffic(chat_id):
    send_inline(chat_id, target_select_text("traffic"), target_select_keyboard("traffic"))


def cmd_disk(chat_id):
    send_inline(chat_id, target_select_text("disk"), target_select_keyboard("disk"))


def cmd_events(chat_id):
    send_inline(chat_id, target_select_text("events"), target_select_keyboard("events"))


def cmd_edit_help(chat_id):
    send_inline(chat_id, target_select_text("edit"), target_select_keyboard("edit"))


def cmd_local_edit_help(chat_id):
    send_inline(chat_id, (
        "🏠✏️ <b>编辑本机资料</b> ✨\n\n"
        "直接发送下面任意一种：\n\n"
        "<code>编辑本机名称 Oracle主控机</code>\n"
        "<code>编辑本机备注 新加坡主控节点</code>\n"
        "<code>编辑本机到期 2027-05-01</code>\n"
        "<code>编辑本机价格 38 CNY</code>\n"
        "<code>编辑本机周期 年付</code>\n"
        "<code>本机续费 2027-05-01</code>\n\n"
        "📌 可以一次粘贴多条编辑命令。"
    ), local_detail_keyboard())


def server_edit_help_text(sid):
    return (
        "✏️✨ <b>编辑服务器</b> ✨✏️\n\n"
        f"当前服务器 ID：<code>{h(sid)}</code>\n\n"
        f"<code>编辑备注 {h(sid)} 新备注</code>\n"
        f"<code>编辑到期 {h(sid)} 2027-05-01</code>\n"
        f"<code>编辑价格 {h(sid)} 38 CNY</code>\n"
        f"<code>编辑周期 {h(sid)} 年付</code>\n"
        f"<code>编辑端口 {h(sid)} 443</code>\n"
        f"<code>编辑名称 {h(sid)} HK-Oracle</code>\n"
        f"<code>编辑系统 {h(sid)} Ubuntu 22.04</code>\n"
        f"<code>编辑永久 {h(sid)} 是</code> / <code>编辑永久 {h(sid)} 否</code>\n"
        f"<code>编辑自动续费 {h(sid)} 是</code> / <code>编辑自动续费 {h(sid)} 否</code>\n\n"
        "👇 编辑后返回详情查看结果。"
    )


def local_traffic_text():
    return "🌐✨ <b>本机流量</b> ✨🌐\n" + f"🕒 更新时间：{now_text()}\n\n" + traffic_block()


def local_disk_text():
    # Reuse old disk_text if present.
    try:
        return disk_text()
    except Exception:
        return "💾✨ <b>本机磁盘</b> ✨💾\n\n" + "获取失败"


def remote_probe_required_text(r, kind):
    icon = "🌐" if kind == "traffic" else "💾"
    name = "流量" if kind == "traffic" else "磁盘"
    return (
        f"{icon}✨ <b>{h(r['name'])}｜{name}数据</b> ✨{icon}\n"
        f"🕒 更新时间：{now_text()}\n\n"
        "━━━━━━━━━━━━━━\n"
        f"🖥️ 服务器：{h(r['name'])}\n"
        f"🌐 地址：<code>{h(r['host'])}:{h(r['check_port'])}</code>\n"
        f"📡 连通性：{'🟢 在线' if check_tcp(r['host'], r['check_port'], timeout=3) else '🔴 离线'}\n\n"
        "📌 <b>说明：</b>主机器人只能检测端口在线/离线，无法隔空读取其它服务器的真实流量/磁盘。\n"
        "✅ 要像哪吒/Y 探针一样查看真实数据，请先给这台服务器安装探针。\n\n"
        "👇 下一步：点击 <b>一键部署探针</b>，复制命令到目标服务器 SSH 执行。"
    )


def target_events_text(target_type, sid=None):
    rows = get_recent_events(30)
    title = "本机事件" if target_type == "local" else "服务器事件"
    filter_name = None
    if target_type == "server" and sid:
        r = get_server_row(sid)
        filter_name = r["name"] if r else None
        title = f"{filter_name or sid}｜事件"
    out = [f"🧾✨ <b>{h(title)}</b> ✨🧾", f"🕒 更新时间：{now_text()}", ""]
    icons = {"offline": "🚨", "online": "✅", "expiry": "⏰", "system": "🔥", "action": "🛠️", "security": "🛡️"}
    count = 0
    for ev in rows:
        content = f"{ev['title']} {ev['content']}"
        if target_type == "local":
            # local/system events: include system/security/action not mentioning a remote server name.
            if ev["event_type"] not in ["system", "security", "action"]:
                continue
        elif filter_name and filter_name not in content:
            continue
        out.append(f"{icons.get(ev['event_type'], '📌')} <b>{h(ev['title'])}</b>\n🕒 {h(ev['created_at'])}")
        count += 1
        if count >= 10:
            break
    if count == 0:
        out.append("暂无该目标的事件记录。")
    return "\n\n".join(out)[:3900]


def cmd_check_servers(chat_id):
    send_inline(chat_id, target_select_text("check"), target_select_keyboard("check"))


def reminder_settings_text():
    days = ",".join(str(x) for x in get_reminder_days())
    return (
        "⏰✨ <b>到期提醒设置</b> ✨⏰\n\n"
        f"当前提前提醒天数：<code>{h(days)}</code>\n"
        f"离线推送宽限期：<code>{OFFLINE_GRACE_SECONDS} 秒</code>\n\n"
        "修改提醒天数直接发送：\n"
        "<code>设置到期提醒 30,14,7,3,1,0</code>\n\n"
        "说明：0 表示到期当天，负数不用手动设置，过期后系统会继续每日提醒。"
    )


def monitor_server_online_status():
    """300s grace offline monitor, no repeated online spam."""
    conn = db()
    for col, definition in [
        ("fail_count", "INTEGER DEFAULT 0"),
        ("success_count", "INTEGER DEFAULT 0"),
        ("notified_offline", "INTEGER DEFAULT 0"),
        ("first_fail_at", "TEXT DEFAULT ''"),
        ("first_recover_at", "TEXT DEFAULT ''"),
    ]:
        ensure_column(conn, "server_status", col, definition)
    conn.commit()
    rows = conn.execute("SELECT * FROM servers").fetchall()
    now_dt = datetime.now()
    now = now_text()
    for r in rows:
        sid = r["id"]
        raw_online = check_tcp(r["host"], r["check_port"], timeout=5)
        old = conn.execute("SELECT * FROM server_status WHERE server_id=?", (sid,)).fetchone()
        if not old:
            conn.execute(
                "INSERT INTO server_status(server_id, last_status, last_checked_at, last_changed_at, fail_count, success_count, notified_offline, first_fail_at, first_recover_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (sid, "online" if raw_online else "unknown", now, now, 0 if raw_online else 1, 1 if raw_online else 0, 0, "" if raw_online else now, "")
            )
            conn.commit()
            continue
        old_status = old["last_status"] or "unknown"
        notified = int(old["notified_offline"] if "notified_offline" in old.keys() and old["notified_offline"] is not None else 0)
        first_fail_at = old["first_fail_at"] if "first_fail_at" in old.keys() and old["first_fail_at"] else ""
        first_recover_at = old["first_recover_at"] if "first_recover_at" in old.keys() and old["first_recover_at"] else ""
        try:
            fail_elapsed = (now_dt - parse_date(first_fail_at)).total_seconds() if first_fail_at else 0
        except Exception:
            fail_elapsed = 0
        try:
            recover_elapsed = (now_dt - parse_date(first_recover_at)).total_seconds() if first_recover_at else 0
        except Exception:
            recover_elapsed = 0
        if raw_online:
            if old_status == "offline" and notified == 1:
                if not first_recover_at:
                    conn.execute("UPDATE server_status SET first_recover_at=?, last_checked_at=? WHERE server_id=?", (now, now, sid)); conn.commit()
                elif recover_elapsed >= RECOVERY_STABLE_SECONDS:
                    conn.execute("UPDATE server_status SET last_status='online', last_checked_at=?, last_changed_at=?, fail_count=0, success_count=1, notified_offline=0, first_fail_at='', first_recover_at='' WHERE server_id=?", (now, now, sid)); conn.commit()
                    push_event("online", f"服务器恢复在线：{r['name']}", online_push_text(r))
                else:
                    conn.execute("UPDATE server_status SET last_checked_at=? WHERE server_id=?", (now, sid)); conn.commit()
            else:
                conn.execute("UPDATE server_status SET last_status='online', last_checked_at=?, fail_count=0, success_count=success_count+1, first_fail_at='', first_recover_at='' WHERE server_id=?", (now, sid)); conn.commit()
        else:
            if old_status != "offline":
                if not first_fail_at:
                    conn.execute("UPDATE server_status SET first_fail_at=?, last_checked_at=?, fail_count=1 WHERE server_id=?", (now, now, sid)); conn.commit()
                elif fail_elapsed >= OFFLINE_GRACE_SECONDS:
                    conn.execute("UPDATE server_status SET last_status='offline', last_checked_at=?, last_changed_at=?, fail_count=fail_count+1, success_count=0, notified_offline=1, first_recover_at='' WHERE server_id=?", (now, now, sid)); conn.commit()
                    push_event("offline", f"服务器离线：{r['name']}", offline_push_text(r))
                else:
                    conn.execute("UPDATE server_status SET last_checked_at=?, fail_count=fail_count+1 WHERE server_id=?", (now, sid)); conn.commit()
            else:
                # Already offline, do not repeat offline alert.
                conn.execute("UPDATE server_status SET last_checked_at=?, fail_count=fail_count+1, success_count=0, first_recover_at='' WHERE server_id=?", (now, sid)); conn.commit()
    conn.close()


def monitor_expiry():
    conn = db(); rows = conn.execute("SELECT * FROM servers").fetchall(); today = datetime.now().date()
    due_days = get_reminder_days()
    for r in rows:
        if is_free_forever_row(r) or not is_valid_date_text(r["expire_at"]):
            continue
        days = (parse_date(r["expire_at"]).date() - today).days
        should = days in due_days or days < 0
        if should:
            # For expired servers, remind once per day rather than every poll.
            key = f"{r['id']}:{days}:{today.isoformat()}" if days < 0 else f"{r['id']}:{days}"
            old = conn.execute("SELECT 1 FROM reminders WHERE server_id=? AND remind_key=?", (r["id"], key)).fetchone()
            if old: continue
            push_event("expiry", f"到期提醒：{r['name']}", expiry_push_text(r, days))
            conn.execute("INSERT OR REPLACE INTO reminders(server_id, remind_key, sent_at) VALUES(?,?,?)", (r["id"], key, now_text())); conn.commit()
    conn.close()


_old_handle_callback_ultra = handle_callback

def handle_callback(callback):
    try:
        callback_id = callback.get("id")
        data = callback.get("data") or ""
        msg = callback.get("message") or {}
        chat_id = msg.get("chat", {}).get("id")
        message_id = msg.get("message_id")
        if not is_admin(chat_id):
            answer_callback(callback_id, "未授权")
            return
        if data == "nav:dashboard":
            answer_callback(callback_id, "已打开总览")
            edit_inline_message(chat_id, message_id, compact_dashboard_text(), dashboard_inline_keyboard())
            return
        if data == "nav:servers":
            answer_callback(callback_id, "已打开列表")
            rows = get_all_servers(order="id")
            online = sum(1 for r in rows if check_tcp(r["host"], r["check_port"], timeout=2))
            offline = len(rows) - online
            text = (
                "📋✨ <b>选择机器</b> ✨📋\n"
                f"🕒 更新时间：{now_text()}\n\n"
                f"🏠 本机：1 台\n🟢 在线：{online + 1} 台\n🔴 离线：{offline} 台\n📦 总数：{len(rows)+1} 台\n\n"
                "👇 点击本机或任意服务器进入详情。"
            )
            kb = target_buttons(include_local=True, cb_prefix="target") + bottom_nav([[{"text": "🔄 刷新列表", "callback_data": "nav:servers"}]])
            edit_inline_message(chat_id, message_id, text, kb)
            return
        if data.startswith("target:"):
            answer_callback(callback_id, "已打开详情")
            parts = data.split(":")
            if parts[1] == "local":
                edit_inline_message(chat_id, message_id, local_detail_text(), local_detail_keyboard())
                return
            sid = parts[2]
            r = get_server_row(sid)
            if not r:
                edit_inline_message(chat_id, message_id, "❌ 没有找到这个服务器。", bottom_nav())
                return
            edit_inline_message(chat_id, message_id, remote_detail_text(r), remote_detail_keyboard(sid))
            return
        if data.startswith("select:"):
            action = data.split(":", 1)[1]
            answer_callback(callback_id, "请选择目标")
            edit_inline_message(chat_id, message_id, target_select_text(action), target_select_keyboard(action))
            return
        if data.startswith("view:"):
            parts = data.split(":")
            action = parts[1]
            typ = parts[2]
            answer_callback(callback_id, "已打开")
            if typ == "local":
                if action == "status" or action == "check":
                    edit_inline_message(chat_id, message_id, local_detail_text(), local_detail_keyboard()); return
                if action == "traffic":
                    edit_inline_message(chat_id, message_id, local_traffic_text(), bottom_nav([[{"text": "💾 下一步：本机磁盘", "callback_data": "view:disk:local"}, {"text": "🏠 本机详情", "callback_data": "target:local"}]])); return
                if action == "disk":
                    edit_inline_message(chat_id, message_id, local_disk_text(), bottom_nav([[{"text": "🧾 下一步：本机事件", "callback_data": "view:events:local"}, {"text": "🏠 本机详情", "callback_data": "target:local"}]])); return
                if action == "events":
                    edit_inline_message(chat_id, message_id, target_events_text("local"), bottom_nav([[{"text": "🏠 本机详情", "callback_data": "target:local"}]])); return
                if action == "edit":
                    edit_inline_message(chat_id, message_id, "🏠✏️ <b>编辑本机</b>\n\n请选择下面按钮或直接发送编辑命令。", local_detail_keyboard()); cmd_local_edit_help(chat_id); return
            if typ == "server" and len(parts) >= 4:
                sid = parts[3]
                r = get_server_row(sid)
                if not r:
                    edit_inline_message(chat_id, message_id, "❌ 没有找到这个服务器。", bottom_nav()); return
                if action == "status" or action == "check":
                    edit_inline_message(chat_id, message_id, remote_detail_text(r), remote_detail_keyboard(sid)); return
                if action == "traffic":
                    edit_inline_message(chat_id, message_id, remote_probe_required_text(r, "traffic"), bottom_nav([[{"text": "📡 一键部署探针", "callback_data": f"agent_cmd:{sid}"}, {"text": "🖥️ 服务器详情", "callback_data": f"target:server:{sid}"}]])); return
                if action == "disk":
                    edit_inline_message(chat_id, message_id, remote_probe_required_text(r, "disk"), bottom_nav([[{"text": "📡 一键部署探针", "callback_data": f"agent_cmd:{sid}"}, {"text": "🖥️ 服务器详情", "callback_data": f"target:server:{sid}"}]])); return
                if action == "events":
                    edit_inline_message(chat_id, message_id, target_events_text("server", sid), bottom_nav([[{"text": "🖥️ 服务器详情", "callback_data": f"target:server:{sid}"}]])); return
                if action == "edit":
                    edit_inline_message(chat_id, message_id, server_edit_help_text(sid), remote_detail_keyboard(sid)); return
        if data == "nav:reminder":
            answer_callback(callback_id, "提醒设置")
            edit_inline_message(chat_id, message_id, reminder_settings_text(), bottom_nav())
            return
        if data == "nav:check":
            answer_callback(callback_id, "请选择目标")
            edit_inline_message(chat_id, message_id, target_select_text("check"), target_select_keyboard("check"))
            return
        if data == "nav:traffic":
            answer_callback(callback_id, "请选择目标")
            edit_inline_message(chat_id, message_id, target_select_text("traffic"), target_select_keyboard("traffic"))
            return
        if data == "nav:disk":
            answer_callback(callback_id, "请选择目标")
            edit_inline_message(chat_id, message_id, target_select_text("disk"), target_select_keyboard("disk"))
            return
        if data == "nav:events":
            answer_callback(callback_id, "请选择目标")
            edit_inline_message(chat_id, message_id, target_select_text("events"), target_select_keyboard("events"))
            return
        if data == "nav:tools":
            answer_callback(callback_id, "工具")
            edit_inline_message(chat_id, message_id, "🛠️✨ <b>工具</b> ✨🛠️\n\n请选择下一步。", bottom_nav([
                [{"text": "🌍 刷新本机地区", "callback_data": "local:refresh_meta"}, {"text": "🌍 刷新全部地区", "callback_data": "servers:refresh_meta"}],
                [{"text": "🔄 重启节点", "callback_data": "tool:restart"}, {"text": "🧹 清理缓存", "callback_data": "tool:cache"}],
                [{"text": "⏰ 提醒设置", "callback_data": "nav:reminder"}],
            ])); return
        if data == "local:refresh_meta":
            answer_callback(callback_id, "已刷新")
            meta = detect_local_meta(force=True)
            edit_inline_message(chat_id, message_id, "✅🌍 <b>本机国家地区已刷新</b>\n\n" + f"🌐 IP：<code>{h(meta.get('ip','未知'))}</code>\n📍 地区：{local_location_line(meta)}\n🏢 运营商：{h(meta.get('isp') or '未知')}", local_detail_keyboard())
            return
        if data == "tool:restart":
            answer_callback(callback_id, "确认页")
            edit_inline_message(chat_id, message_id, "⚠️🔄 <b>重启节点确认</b>\n\n确认重启 Xray / x-ui / 3x-ui 请发送：<code>确认重启</code>", bottom_nav())
            return
        if data == "tool:cache":
            answer_callback(callback_id, "确认页")
            edit_inline_message(chat_id, message_id, "⚠️🧹 <b>清理缓存确认</b>\n\n确认清理请发送：<code>确认清理</code>", bottom_nav())
            return
    except Exception as e:
        try:
            send(callback.get("message", {}).get("chat", {}).get("id"), f"❌ 按钮操作失败：{h(e)}")
        except Exception:
            pass
        return
    return _old_handle_callback_ultra(callback)


_old_handle_ultra = handle

def handle(chat_id, text):
    ct = clean_command_text(text)
    if ct.startswith("设置到期提醒"):
        try:
            v = ct.replace("设置到期提醒", "", 1).strip()
            vals = set_reminder_days(v)
            send_inline(chat_id, f"✅⏰ <b>到期提醒已更新</b>\n\n当前提醒天数：<code>{h(','.join(map(str, vals)))}</code>", bottom_nav())
        except Exception as e:
            send_inline(chat_id, f"❌ 设置失败：{h(e)}\n\n示例：<code>设置到期提醒 30,14,7,3,1,0</code>", bottom_nav())
        return
    if ct in ["服务器总览", "总览", "面板", "/dashboard"]:
        cmd_dashboard(chat_id); return
    if ct in ["查看服务器", "服务器列表", "选择服务器", "查看机器", "机器列表", "/servers", "/list_servers"]:
        cmd_server_buttons(chat_id); return
    if ct in ["查看状态", "服务器状态", "本机状态", "状态", "/status"]:
        cmd_status(chat_id); return
    if ct in ["查看流量", "流量", "网络流量", "服务器流量", "流量使用", "/traffic"]:
        cmd_traffic(chat_id); return
    if ct in ["查看磁盘", "磁盘", "磁盘状态", "磁盘使用", "/disk"]:
        cmd_disk(chat_id); return
    if ct in ["查看事件", "服务器事件", "事件记录", "事件", "/events"]:
        cmd_events(chat_id); return
    if ct in ["检测服务器", "检测在线", "检测机器", "在线检测", "/check_servers"]:
        cmd_check_servers(chat_id); return
    if ct in ["编辑服务器", "编辑机器", "/edit_server"]:
        cmd_edit_help(chat_id); return
    if ct in ["编辑本机", "编辑当前机器", "编辑当前服务器"]:
        cmd_local_edit_help(chat_id); return
    if ct in ["提醒设置", "到期提醒", "续费提醒"]:
        send_inline(chat_id, reminder_settings_text(), bottom_nav()); return
    return _old_handle_ultra(chat_id, text)


# Re-finalize poll so the runtime uses the latest monitor/handlers above.
def poll():
    offset = 0
    last_check = 0
    set_bot_commands()
    while True:
        try:
            now_ts = time.time()
            if now_ts - last_check >= CHECK_INTERVAL:
                monitor_local_system()
                monitor_server_online_status()
                auto_renew_expired_online_servers()
                monitor_expiry()
                last_check = now_ts
            r = requests.get(f"{API}/getUpdates", params={"timeout": 10, "offset": offset}, timeout=15).json()
            for item in r.get("result", []):
                offset = item["update_id"] + 1
                if item.get("callback_query"):
                    handle_callback(item["callback_query"])
                    continue
                msg = item.get("message") or item.get("edited_message")
                if not msg:
                    continue
                chat_id = msg["chat"]["id"]
                text = msg.get("text", "").strip()
                if text:
                    handle(chat_id, text)
        except KeyboardInterrupt:
            break
        except Exception:
            time.sleep(3)





# ============================================================
# FINAL EDIT BUTTON FLOW PATCH
# 只新增/覆盖按钮编辑流程，不改监控、提醒、探针等原有逻辑。
# ============================================================

EDIT_PENDING_SQL = """
CREATE TABLE IF NOT EXISTS pending_actions (
    chat_id TEXT PRIMARY KEY,
    target_type TEXT DEFAULT '',
    target_id TEXT DEFAULT '',
    field TEXT DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
)
"""


def ensure_pending_table():
    conn = db()
    conn.execute(EDIT_PENDING_SQL)
    conn.commit()
    conn.close()


def set_pending_action(chat_id, target_type, target_id, field):
    ensure_pending_table()
    conn = db()
    conn.execute(
        "INSERT OR REPLACE INTO pending_actions(chat_id,target_type,target_id,field,created_at) VALUES(?,?,?,?,?)",
        (str(chat_id), str(target_type), str(target_id), str(field), now_text())
    )
    conn.commit()
    conn.close()


def get_pending_action(chat_id):
    ensure_pending_table()
    conn = db()
    row = conn.execute("SELECT * FROM pending_actions WHERE chat_id=?", (str(chat_id),)).fetchone()
    conn.close()
    return row


def clear_pending_action(chat_id):
    ensure_pending_table()
    conn = db()
    conn.execute("DELETE FROM pending_actions WHERE chat_id=?", (str(chat_id),))
    conn.commit()
    conn.close()


def field_cn(field):
    return {
        "price": "付费价格",
        "expire_at": "到期时间",
        "cycle": "付费周期",
        "note": "备注",
        "check_port": "检测端口",
        "os_name": "系统",
        "name": "名称",
        "free_forever": "永久免费",
        "auto_renew": "自动续费",
    }.get(field, field)


def field_example(field):
    return {
        "price": "50 CNY  或  6 USD",
        "expire_at": "2027-05-01",
        "cycle": "月付 / 季付 / 年付",
        "note": "香港甲骨文主力机",
        "check_port": "22  或  443",
        "os_name": "Ubuntu 22.04",
        "name": "HK-Oracle",
        "free_forever": "是 / 否",
        "auto_renew": "是 / 否",
    }.get(field, "请输入新内容")


def edit_prompt_text(target_type, target_id, field):
    title = "🏠 本机" if target_type == "local" else f"🖥️ 服务器 ID {target_id}"
    return (
        f"✏️✨ <b>编辑{title}：{h(field_cn(field))}</b> ✨✏️\n\n"
        f"请直接发送新的 <b>{h(field_cn(field))}</b>。\n\n"
        f"📌 示例：\n<code>{h(field_example(field))}</code>\n\n"
        "发送后会自动保存并返回详情页。\n"
        "取消编辑请发送：<code>取消</code>"
    )


def edit_nav_keyboard(target_type="server", target_id="0"):
    if target_type == "local":
        return [[{"text": "⬅️ 上一步：本机详情", "callback_data": "target:local"}]] + bottom_nav()
    return [[{"text": "⬅️ 上一步：服务器详情", "callback_data": f"target:server:{target_id}"}]] + bottom_nav()


def edit_server_field_keyboard(sid):
    return [
        [
            {"text": "💰 编辑价格", "callback_data": f"field:server:{sid}:price"},
            {"text": "📆 编辑到期", "callback_data": f"field:server:{sid}:expire_at"},
        ],
        [
            {"text": "🔁 编辑周期", "callback_data": f"field:server:{sid}:cycle"},
            {"text": "📝 编辑备注", "callback_data": f"field:server:{sid}:note"},
        ],
        [
            {"text": "🔌 编辑端口", "callback_data": f"field:server:{sid}:check_port"},
            {"text": "🧬 编辑系统", "callback_data": f"field:server:{sid}:os_name"},
        ],
        [
            {"text": "🏷️ 编辑名称", "callback_data": f"field:server:{sid}:name"},
        ],
        [
            {"text": "🎁 永久免费", "callback_data": f"field:server:{sid}:free_forever"},
            {"text": "🔁 自动续费", "callback_data": f"field:server:{sid}:auto_renew"},
        ],
        [{"text": "⬅️ 上一步：服务器详情", "callback_data": f"target:server:{sid}"}],
        [
            {"text": "📋 返回列表", "callback_data": "nav:servers"},
            {"text": "📊 返回总览", "callback_data": "nav:dashboard"},
            {"text": "🛠️ 工具", "callback_data": "nav:tools"},
        ],
    ]


def edit_local_field_keyboard():
    return [
        [
            {"text": "💰 编辑价格", "callback_data": "field:local:0:price"},
            {"text": "📆 编辑到期", "callback_data": "field:local:0:expire_at"},
        ],
        [
            {"text": "🔁 编辑周期", "callback_data": "field:local:0:cycle"},
            {"text": "📝 编辑备注", "callback_data": "field:local:0:note"},
        ],
        [
            {"text": "🏷️ 编辑名称", "callback_data": "field:local:0:name"},
        ],
        [{"text": "⬅️ 上一步：本机详情", "callback_data": "target:local"}],
        [
            {"text": "📋 返回列表", "callback_data": "nav:servers"},
            {"text": "📊 返回总览", "callback_data": "nav:dashboard"},
            {"text": "🛠️ 工具", "callback_data": "nav:tools"},
        ],
    ]


# 覆盖底部导航，所有下一步页面都带上一步、返回列表、返回总览、工具。
def bottom_nav(extra=None):
    rows = []
    if extra:
        rows.extend(extra)
    rows.append([
        {"text": "⬅️ 上一步：服务器列表", "callback_data": "nav:servers"},
        {"text": "📋 返回列表", "callback_data": "nav:servers"},
    ])
    rows.append([
        {"text": "📊 返回总览", "callback_data": "nav:dashboard"},
        {"text": "🛠️ 工具", "callback_data": "nav:tools"},
    ])
    return rows


# 覆盖服务器详情按钮，加入字段级编辑入口。
def remote_detail_keyboard(sid):
    return bottom_nav([
        [
            {"text": "🌐 流量", "callback_data": f"view:traffic:server:{sid}"},
            {"text": "💾 磁盘", "callback_data": f"view:disk:server:{sid}"},
            {"text": "🧾 事件", "callback_data": f"view:events:server:{sid}"},
        ],
        [
            {"text": "✏️ 编辑资料", "callback_data": f"edit:server:{sid}"},
            {"text": "⏰ 续费", "callback_data": f"server_renew_help:{sid}"},
            {"text": "📡 探针", "callback_data": f"agent_cmd:{sid}"},
        ],
        [
            {"text": "💰 价格", "callback_data": f"field:server:{sid}:price"},
            {"text": "📆 到期", "callback_data": f"field:server:{sid}:expire_at"},
            {"text": "🔁 周期", "callback_data": f"field:server:{sid}:cycle"},
        ],
        [
            {"text": "📝 备注", "callback_data": f"field:server:{sid}:note"},
            {"text": "🔌 端口", "callback_data": f"field:server:{sid}:check_port"},
            {"text": "🧬 系统", "callback_data": f"field:server:{sid}:os_name"},
        ],
        [
            {"text": "🏷️ 名称", "callback_data": f"field:server:{sid}:name"},
            {"text": "🎁 永久免费", "callback_data": f"field:server:{sid}:free_forever"},
            {"text": "🔁 自动续费", "callback_data": f"field:server:{sid}:auto_renew"},
        ],
        [
            {"text": "🌍 刷新地区", "callback_data": f"refresh_meta:{sid}"},
            {"text": "🗑️ 删除", "callback_data": f"delete_confirm:{sid}"},
        ],
        [
            {"text": "📆 +1月", "callback_data": f"renew_month:{sid}"},
            {"text": "🗓️ +3月", "callback_data": f"renew_quarter:{sid}"},
            {"text": "📅 +1年", "callback_data": f"renew_year:{sid}"},
        ],
    ])


# 覆盖本机详情按钮，加入字段级编辑入口。
def local_detail_keyboard():
    return bottom_nav([
        [
            {"text": "🌐 本机流量", "callback_data": "view:traffic:local"},
            {"text": "💾 本机磁盘", "callback_data": "view:disk:local"},
        ],
        [
            {"text": "🧾 本机事件", "callback_data": "view:events:local"},
            {"text": "✏️ 编辑本机", "callback_data": "edit:local"},
        ],
        [
            {"text": "💰 价格", "callback_data": "field:local:0:price"},
            {"text": "📆 到期", "callback_data": "field:local:0:expire_at"},
            {"text": "🔁 周期", "callback_data": "field:local:0:cycle"},
        ],
        [
            {"text": "📝 备注", "callback_data": "field:local:0:note"},
            {"text": "🏷️ 名称", "callback_data": "field:local:0:name"},
        ],
        [{"text": "🌍 刷新本机地区", "callback_data": "local:refresh_meta"}],
    ])


def update_server_field(sid, field, value):
    value = one_line(value)
    conn = db()
    row = conn.execute("SELECT * FROM servers WHERE id=?", (sid,)).fetchone()
    if not row:
        conn.close()
        raise ValueError("没有找到这个服务器 ID")

    if field == "price":
        parts = value.split()
        if not parts:
            raise ValueError("价格不能为空，例如：50 CNY")
        price = float(parts[0])
        currency = row["currency"]
        if len(parts) >= 2:
            currency = normalize_currency(parts[1])
            if currency not in ["CNY", "USD", "EUR", "GBP"]:
                raise ValueError("币种只支持 CNY / USD / EUR / GBP")
        conn.execute("UPDATE servers SET price=?, currency=?, free_forever=0 WHERE id=?", (price, currency, sid))

    elif field == "expire_at":
        parse_date(value)
        conn.execute("UPDATE servers SET expire_at=?, free_forever=0 WHERE id=?", (value, sid))
        conn.execute("DELETE FROM reminders WHERE server_id=?", (sid,))

    elif field == "cycle":
        cycle = normalize_cycle(value)
        if cycle not in ["monthly", "quarterly", "yearly"]:
            raise ValueError("周期只支持：月付 / 季付 / 年付")
        conn.execute("UPDATE servers SET cycle=? WHERE id=?", (cycle, sid))

    elif field == "note":
        conn.execute("UPDATE servers SET note=? WHERE id=?", (value, sid))

    elif field == "check_port":
        port = int(value)
        if port < 1 or port > 65535:
            raise ValueError("端口必须是 1-65535")
        conn.execute("UPDATE servers SET check_port=? WHERE id=?", (port, sid))

    elif field == "os_name":
        conn.execute("UPDATE servers SET os_name=? WHERE id=?", (value, sid))

    elif field == "name":
        if not value:
            raise ValueError("名称不能为空")
        conn.execute("UPDATE servers SET name=? WHERE id=?", (value, sid))

    elif field == "free_forever":
        enabled = 1 if truthy(value) else 0
        if enabled:
            conn.execute("UPDATE servers SET free_forever=1, auto_renew=0, price=0, currency='USD', expire_at='永久' WHERE id=?", (sid,))
            conn.execute("DELETE FROM reminders WHERE server_id=?", (sid,))
        else:
            conn.execute("UPDATE servers SET free_forever=0, expire_at='' WHERE id=?", (sid,))

    elif field == "auto_renew":
        enabled = 1 if truthy(value) else 0
        conn.execute("UPDATE servers SET auto_renew=? WHERE id=?", (enabled, sid))

    else:
        conn.close()
        raise ValueError("不支持的编辑字段")

    conn.commit()
    new_row = conn.execute("SELECT * FROM servers WHERE id=?", (sid,)).fetchone()
    conn.close()
    event_add("action", "编辑服务器", f"服务器 ID {sid} 已更新 {field_cn(field)}")
    return new_row


def update_local_field(field, value):
    value = one_line(value)
    if field == "price":
        parts = value.split()
        if not parts:
            raise ValueError("价格不能为空，例如：50 CNY")
        float(parts[0])
        set_local_profile_value("price", parts[0])
        if len(parts) >= 2:
            currency = normalize_currency(parts[1])
            if currency not in ["CNY", "USD", "EUR", "GBP"]:
                raise ValueError("币种只支持 CNY / USD / EUR / GBP")
            set_local_profile_value("currency", currency)

    elif field == "expire_at":
        parse_date(value)
        set_local_profile_value("expire_at", value)

    elif field == "cycle":
        cycle = normalize_cycle(value)
        if cycle not in ["monthly", "quarterly", "yearly"]:
            raise ValueError("周期只支持：月付 / 季付 / 年付")
        set_local_profile_value("cycle", cycle)

    elif field == "note":
        set_local_profile_value("note", value)

    elif field == "name":
        if not value:
            raise ValueError("名称不能为空")
        set_local_profile_value("name", value)

    else:
        raise ValueError("本机不支持这个字段")

    event_add("action", "编辑本机", f"已更新本机 {field_cn(field)}")


def process_pending_input(chat_id, text):
    if clean_command_text(text) in ["取消", "取消编辑", "退出编辑"]:
        clear_pending_action(chat_id)
        send_inline(chat_id, "✅ 已取消编辑。", bottom_nav())
        return True

    row = get_pending_action(chat_id)
    if not row:
        return False

    target_type = row["target_type"]
    target_id = row["target_id"]
    field = row["field"]

    try:
        if target_type == "local":
            update_local_field(field, text)
            clear_pending_action(chat_id)
            send_inline(
                chat_id,
                f"✅🏠 <b>本机 {h(field_cn(field))} 已更新</b>\n\n{local_detail_text()}",
                local_detail_keyboard()
            )
            return True

        if target_type == "server":
            new_row = update_server_field(target_id, field, text)
            clear_pending_action(chat_id)
            send_inline(
                chat_id,
                f"✅🖥️ <b>服务器 {h(field_cn(field))} 已更新</b>\n\n{remote_detail_text(new_row)}",
                remote_detail_keyboard(target_id)
            )
            return True

    except Exception as e:
        send_inline(
            chat_id,
            f"❌ <b>编辑失败：</b>{h(e)}\n\n请重新发送正确内容，或发送 <code>取消</code> 退出编辑。\n\n示例：<code>{h(field_example(field))}</code>",
            edit_nav_keyboard(target_type, target_id)
        )
        return True

    return False


_prev_handle_callback_edit_buttons = handle_callback

def handle_callback(callback):
    try:
        callback_id = callback.get("id")
        data = callback.get("data") or ""
        msg = callback.get("message") or {}
        chat_id = msg.get("chat", {}).get("id")
        message_id = msg.get("message_id")

        if not is_admin(chat_id):
            answer_callback(callback_id, "未授权")
            return

        if data == "edit:local":
            answer_callback(callback_id, "编辑本机")
            edit_inline_message(
                chat_id,
                message_id,
                "🏠✏️ <b>编辑本机资料</b>\n\n请选择要编辑的字段。",
                edit_local_field_keyboard()
            )
            return

        if data.startswith("edit:server:"):
            sid = data.split(":")[2]
            answer_callback(callback_id, "编辑服务器")
            edit_inline_message(
                chat_id,
                message_id,
                f"🖥️✏️ <b>编辑服务器 ID {h(sid)}</b>\n\n请选择要编辑的字段。",
                edit_server_field_keyboard(sid)
            )
            return

        # 兼容旧按钮：server_edit:ID
        if data.startswith("server_edit:"):
            sid = data.split(":", 1)[1]
            answer_callback(callback_id, "编辑服务器")
            edit_inline_message(
                chat_id,
                message_id,
                f"🖥️✏️ <b>编辑服务器 ID {h(sid)}</b>\n\n请选择要编辑的字段。",
                edit_server_field_keyboard(sid)
            )
            return

        if data.startswith("field:"):
            parts = data.split(":")
            if len(parts) < 4:
                answer_callback(callback_id, "按钮格式错误")
                return
            target_type, target_id, field = parts[1], parts[2], parts[3]
            answer_callback(callback_id, f"请输入{field_cn(field)}")
            set_pending_action(chat_id, target_type, target_id, field)
            edit_inline_message(
                chat_id,
                message_id,
                edit_prompt_text(target_type, target_id, field),
                edit_nav_keyboard(target_type, target_id)
            )
            return

    except Exception as e:
        try:
            answer_callback(callback.get("id"), "操作失败")
            send(callback.get("message", {}).get("chat", {}).get("id"), f"❌ 按钮操作失败：{h(e)}", keyboard=False)
        except Exception:
            pass
        return

    return _prev_handle_callback_edit_buttons(callback)


_prev_handle_edit_buttons = handle

def handle(chat_id, text):
    if process_pending_input(chat_id, text):
        return
    return _prev_handle_edit_buttons(chat_id, text)





# ============================================================
# FINAL ONLINE DURATION PATCH
# 新增：本机 + 所有远程服务器在线时长/离线时长显示。
# 不修改原来的监控、提醒、探针、编辑按钮逻辑。
# ============================================================

def duration_from_seconds(seconds):
    try:
        seconds = int(max(0, float(seconds)))
    except Exception:
        return "未知"
    days = seconds // 86400
    hours = seconds % 86400 // 3600
    minutes = seconds % 3600 // 60
    if days > 0:
        return f"{days} 天 {hours} 小时 {minutes} 分钟"
    if hours > 0:
        return f"{hours} 小时 {minutes} 分钟"
    return f"{minutes} 分钟"


def duration_since_text(dt_text):
    if not dt_text:
        return "未知"
    try:
        start = parse_date(str(dt_text))
        return duration_from_seconds((datetime.now() - start).total_seconds())
    except Exception:
        return "未知"


def local_online_duration_text():
    """本机在线时长，使用系统启动时间计算。"""
    try:
        return duration_from_seconds(time.time() - psutil.boot_time())
    except Exception:
        return uptime_text()


def get_server_status_row(sid):
    try:
        conn = db()
        row = conn.execute("SELECT * FROM server_status WHERE server_id=?", (sid,)).fetchone()
        conn.close()
        return row
    except Exception:
        return None


def server_online_duration_text(r, online=None):
    """
    远程服务器在线/离线时长：
    - 在线：从 server_status.last_changed_at 计算在线时长
    - 离线：从 server_status.last_changed_at 计算离线时长
    - 没有历史记录时显示“刚刚记录”
    """
    try:
        sid = r["id"]
        if online is None:
            online = check_tcp(r["host"], r["check_port"], timeout=3)

        st = get_server_status_row(sid)
        if not st:
            return "刚刚记录" if online else "未知"

        status = (st["last_status"] or "").strip()
        changed = st["last_changed_at"] or st["last_checked_at"] or ""

        # 如果当前实时检测和数据库状态不一致，以当前检测状态为准，但时长显示为“等待确认”
        if online and status != "online":
            return "等待确认"
        if (not online) and status != "offline":
            return "等待确认"

        return duration_since_text(changed)
    except Exception:
        return "未知"


# 覆盖本机状态块：增加在线时长。
def status_block():
    s = get_local_status()
    local_place = " ".join(x for x in [s.get("country"), s.get("region"), s.get("city")] if x and x != "未知")
    return (
        "🖥️ <b>本机状态</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🌐 主机名称：<code>{h(s['hostname'])}</code>\n"
        f"🌍 公网 IP：<code>{h(s.get('public_ip', '未知'))}</code>\n"
        f"📍 国家地区：{h(s.get('flag', '🌐'))} {h(local_place or s.get('country') or '未知')}\n"
        f"🏢 运营商：{h(s.get('isp') or '未知')}\n"
        f"🧬 系统版本：{h(s['os'])}\n"
        f"{local_profile_lines()}\n"
        f"🟢 在线时长：{h(local_online_duration_text())}\n"
        f"⏱️ 运行时间：{h(s['uptime'])}\n"
        f"📊 CPU 使用率：{s['cpu']:.0f}%\n"
        f"⚙️ 系统负载：{s['load1']:.2f} / {s['load5']:.2f} / {s['load15']:.2f}\n"
        f"🧩 CPU 核心：{s['cpu_count']} 核\n"
        f"🧠 内存使用：{fmt_size(s['mem_used'])} / {fmt_size(s['mem_total'])} ({s['mem_percent']:.0f}%)\n"
        f"💾 磁盘使用：{fmt_size(s['disk_used'])} / {fmt_size(s['disk_total'])} ({s['disk_percent']:.0f}%)"
    )


# 覆盖本机列表行：按钮上也显示在线时长。
def compact_local_row():
    s = get_local_status()
    p = get_local_profile()
    name = one_line(p.get("name"), socket.gethostname())
    place = " ".join(x for x in [s.get("country"), s.get("region"), s.get("city")] if x and x != "未知")
    online_time = local_online_duration_text()
    return {
        "id": "local",
        "name": name,
        "label": f"🟢 {s.get('flag','🌐')} 🏠 本机｜{name}｜在线 {online_time}",
        "short": f"🟢 本机｜{name}｜在线 {online_time}｜{s.get('flag','🌐')} {place or s.get('country','未知')}",
    }


# 覆盖服务器按钮行：每台远程服务器都显示在线/离线时长。
def server_button_label(r):
    online = check_tcp(r["host"], r["check_port"], timeout=3)
    status = "🟢" if online else "🔴"
    state = "在线" if online else "离线"
    flag = country_flag(r["country_code"] if "country_code" in r.keys() else "")
    days = expire_status_text(r["expire_at"], is_free_forever_row(r))
    free = "🎁" if is_free_forever_row(r) else ""
    auto = "🔁" if is_auto_renew_row(r) else ""
    duration = server_online_duration_text(r, online)
    return f"{status} {flag}{free}{auto} ID{r['id']}｜{r['name']}｜{state} {duration}｜{days}"


# 覆盖远程服务器详情：增加在线/离线时长。
def remote_detail_text(r):
    online = check_tcp(r["host"], r["check_port"], timeout=3)
    status_text = "🟢 在线" if online else "🔴 离线"
    duration_label = "🟢 在线时长" if online else "🔴 离线时长"
    duration = server_online_duration_text(r, online)
    free = is_free_forever_row(r)
    auto = is_auto_renew_row(r)
    return (
        "🖥️✨ <b>服务器详情</b> ✨🖥️\n"
        f"🕒 更新时间：{now_text()}\n\n"
        "━━━━━━━━━━━━━━\n"
        f"📡 状态：{status_text}\n"
        f"{duration_label}：{h(duration)}\n"
        f"🆔 ID：<code>{r['id']}</code>\n"
        f"🖥️ 名称：{h(r['name'])}\n"
        f"🌐 主机：<code>{h(r['host'])}:{h(r['check_port'])}</code>\n"
        f"📍 地区：{server_location_line(r)}\n"
        f"🏢 运营商：{h(r['isp'] if 'isp' in r.keys() and r['isp'] else '未知')}\n"
        f"🧬 系统：{h(r['os_name'] if 'os_name' in r.keys() and r['os_name'] else '未知系统')}\n"
        f"📝 备注：{h(r['note'] or '无')}\n"
        f"🎁 永久免费：{bool_text(free)}\n"
        f"🔁 自动续费：{bool_text(auto)}\n"
        f"💰 价格：{server_price_line(r)}\n"
        f"📆 到期：{h(r['expire_at'] if r['expire_at'] else '未设置')}｜{expire_status_text(r['expire_at'], free)}\n"
        "━━━━━━━━━━━━━━\n"
        "👇 <b>下一步：</b>查看该服务器流量/磁盘/事件，或编辑续费。"
    )[:3900]


# 覆盖服务器概览块：总览里也显示远程服务器在线/离线时长。
def servers_summary_block():
    refresh_missing_meta()
    conn = db()
    rows = conn.execute("SELECT * FROM servers ORDER BY id ASC").fetchall()
    conn.close()
    if not rows:
        return "📡 <b>服务器在线情况</b>\n━━━━━━━━━━━━━━\n📭 暂无服务器记录。\n发送 <code>添加服务器</code> 开始添加。"
    online_count = 0
    offline_count = 0
    lines = []
    for r in rows:
        online = check_tcp(r["host"], r["check_port"], timeout=3)
        duration = server_online_duration_text(r, online)
        if online:
            online_count += 1
            status = "🟢 在线"
            duration_label = f"在线 {duration}"
        else:
            offline_count += 1
            status = "🔴 离线"
            duration_label = f"离线 {duration}"
        flag = country_flag(r["country_code"] if "country_code" in r.keys() else "")
        lines.append(f"{status}｜{flag} {h(r['name'])}｜{h(r['host'])}:{h(r['check_port'])}｜{h(duration_label)}")
    return (
        "📡 <b>服务器在线情况</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🟢 在线：{online_count} 台\n"
        f"🔴 离线：{offline_count} 台\n"
        f"📦 总数：{len(rows)} 台\n\n" + "\n".join(lines[:12])
    )


# 覆盖本机详情：明确显示本机在线时长。
def local_detail_text():
    return (
        "🏠✨ <b>本机详细信息</b> ✨🏠\n"
        f"🕒 更新时间：{now_text()}\n\n"
        f"{status_block()}\n\n"
        "👇 <b>下一步：</b>可继续查看本机流量、磁盘、事件或编辑资料。"
    )[:3900]




# ============================================================
# FINAL DELETE + DURATION + RENEW COUNTDOWN PATCH
# 修正：
# 1) 增加“选择服务器删除”完整按钮流程；
# 2) 远程服务器在线时长改为“监控在线时长”，使用 online_since/offline_since 记录，避免用旧 last_changed_at 误导；
# 3) 本机和所有服务器增加“续费倒计时”显示。
# ============================================================

def ensure_status_duration_columns():
    try:
        conn = db()
        for col, definition in [
            ("online_since", "TEXT DEFAULT ''"),
            ("offline_since", "TEXT DEFAULT ''"),
        ]:
            ensure_column(conn, "server_status", col, definition)

        rows = conn.execute("SELECT * FROM server_status").fetchall()
        for st in rows:
            status = (st["last_status"] or "").strip()
            changed = st["last_changed_at"] or st["last_checked_at"] or now_text()
            online_since = st["online_since"] if "online_since" in st.keys() else ""
            offline_since = st["offline_since"] if "offline_since" in st.keys() else ""
            if status == "online" and not online_since:
                conn.execute("UPDATE server_status SET online_since=?, offline_since='' WHERE server_id=?", (changed, st["server_id"]))
            elif status == "offline" and not offline_since:
                conn.execute("UPDATE server_status SET offline_since=?, online_since='' WHERE server_id=?", (changed, st["server_id"]))
        conn.commit()
        conn.close()
    except Exception:
        pass


def renew_countdown_text(expire_at, free_forever=False):
    if truthy(free_forever) or str(expire_at or "").strip() in ["永久", "永久免费"]:
        return "🎁 永久免费｜无需续费"
    exp = str(expire_at or "").strip()
    if not exp:
        return "未设置"
    try:
        days = (parse_date(exp).date() - datetime.now().date()).days
        if days < 0:
            return f"🚨 已过期 {abs(days)} 天｜请尽快续费"
        if days == 0:
            return "🚨 今天到期｜请今天续费"
        if days <= 3:
            return f"🚨 剩余 {days} 天｜非常紧急"
        if days <= 7:
            return f"⚠️ 剩余 {days} 天｜建议续费"
        if days <= 30:
            return f"⏰ 剩余 {days} 天"
        return f"✅ 剩余 {days} 天"
    except Exception:
        return "未知"


def local_renew_countdown_text():
    p = get_local_profile()
    return renew_countdown_text(p.get("expire_at"), False)


def duration_from_seconds(seconds):
    try:
        seconds = int(max(0, float(seconds)))
    except Exception:
        return "未知"
    days = seconds // 86400
    hours = seconds % 86400 // 3600
    minutes = seconds % 3600 // 60
    if days > 0:
        return f"{days} 天 {hours} 小时 {minutes} 分钟"
    if hours > 0:
        return f"{hours} 小时 {minutes} 分钟"
    return f"{minutes} 分钟"


def duration_since_text(dt_text):
    if not dt_text:
        return "刚刚记录"
    try:
        return duration_from_seconds((datetime.now() - parse_date(str(dt_text))).total_seconds())
    except Exception:
        return "未知"


def local_online_duration_text():
    try:
        return duration_from_seconds(time.time() - psutil.boot_time())
    except Exception:
        return uptime_text()


def get_server_status_row(sid):
    try:
        ensure_status_duration_columns()
        conn = db()
        row = conn.execute("SELECT * FROM server_status WHERE server_id=?", (sid,)).fetchone()
        conn.close()
        return row
    except Exception:
        return None


def server_monitor_duration_text(r, online=None):
    """
    远程服务器无法只靠端口检测读取真实系统 uptime。
    这里显示的是“监控在线/离线时长”：从机器人确认状态变化开始计时。
    如需真实系统运行时长，需要给远程服务器部署探针。
    """
    try:
        if online is None:
            online = check_tcp(r["host"], r["check_port"], timeout=3)
        st = get_server_status_row(r["id"])
        if not st:
            return "刚刚记录"
        status = (st["last_status"] or "").strip()
        if online:
            start = ""
            if "online_since" in st.keys() and st["online_since"]:
                start = st["online_since"]
            elif status == "online":
                start = st["last_changed_at"] or st["last_checked_at"]
            else:
                return "等待确认"
            return duration_since_text(start)
        start = ""
        if "offline_since" in st.keys() and st["offline_since"]:
            start = st["offline_since"]
        elif status == "offline":
            start = st["last_changed_at"] or st["last_checked_at"]
        else:
            return "等待确认"
        return duration_since_text(start)
    except Exception:
        return "未知"


def local_profile_lines():
    p = get_local_profile()
    name = one_line(p.get('name'), socket.gethostname())
    note = one_line(p.get('note'), '无')
    cycle = normalize_cycle(one_line(p.get('cycle'), 'monthly'))
    if cycle not in ["monthly", "quarterly", "yearly"]:
        cycle = "monthly"
    currency = normalize_currency(one_line(p.get('currency'), 'USD'))
    if currency not in ["CNY", "USD", "EUR", "GBP"]:
        currency = "USD"
    price = one_line(p.get('price'), '0')
    return (
        f"🏷️ 管理名称：{h(name)}\n"
        f"📝 本机备注：{h(note)}\n"
        f"🔁 付费周期：{cycle_name(cycle)}\n"
        f"💰 付费价格：{currency_name(currency)} {h(price)} {h(currency)}\n"
        f"📆 到期时间：{local_expire_text(p)}\n"
        f"⏳ 续费倒计时：{h(local_renew_countdown_text())}"
    )


def status_block():
    s = get_local_status()
    local_place = " ".join(x for x in [s.get("country"), s.get("region"), s.get("city")] if x and x != "未知")
    return (
        "🖥️ <b>本机状态</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🌐 主机名称：<code>{h(s['hostname'])}</code>\n"
        f"🌍 公网 IP：<code>{h(s.get('public_ip', '未知'))}</code>\n"
        f"📍 国家地区：{h(s.get('flag', '🌐'))} {h(local_place or s.get('country') or '未知')}\n"
        f"🏢 运营商：{h(s.get('isp') or '未知')}\n"
        f"🧬 系统版本：{h(s['os'])}\n"
        f"{local_profile_lines()}\n"
        f"🟢 在线时长：{h(local_online_duration_text())}\n"
        f"⏱️ 运行时间：{h(s['uptime'])}\n"
        f"📊 CPU 使用率：{s['cpu']:.0f}%\n"
        f"⚙️ 系统负载：{s['load1']:.2f} / {s['load5']:.2f} / {s['load15']:.2f}\n"
        f"🧩 CPU 核心：{s['cpu_count']} 核\n"
        f"🧠 内存使用：{fmt_size(s['mem_used'])} / {fmt_size(s['mem_total'])} ({s['mem_percent']:.0f}%)\n"
        f"💾 磁盘使用：{fmt_size(s['disk_used'])} / {fmt_size(s['disk_total'])} ({s['disk_percent']:.0f}%)"
    )


def compact_local_row():
    s = get_local_status()
    p = get_local_profile()
    name = one_line(p.get("name"), socket.gethostname())
    online_time = local_online_duration_text()
    return {
        "id": "local",
        "name": name,
        "label": f"🟢 {s.get('flag','🌐')} 🏠 本机｜{name}｜在线 {online_time}｜续费 {local_renew_countdown_text()}",
        "short": f"🟢 本机｜{name}｜在线 {online_time}",
    }


def server_button_label(r):
    online = check_tcp(r["host"], r["check_port"], timeout=3)
    status = "🟢" if online else "🔴"
    state = "在线" if online else "离线"
    flag = country_flag(r["country_code"] if "country_code" in r.keys() else "")
    free = "🎁" if is_free_forever_row(r) else ""
    auto = "🔁" if is_auto_renew_row(r) else ""
    duration = server_monitor_duration_text(r, online)
    countdown = renew_countdown_text(r["expire_at"], is_free_forever_row(r))
    return f"{status} {flag}{free}{auto} ID{r['id']}｜{r['name']}｜{state} {duration}｜续费 {countdown}"


def remote_detail_text(r):
    online = check_tcp(r["host"], r["check_port"], timeout=3)
    status_text = "🟢 在线" if online else "🔴 离线"
    duration_label = "🟢 监控在线时长" if online else "🔴 监控离线时长"
    duration = server_monitor_duration_text(r, online)
    free = is_free_forever_row(r)
    auto = is_auto_renew_row(r)
    return (
        "🖥️✨ <b>服务器详情</b> ✨🖥️\n"
        f"🕒 更新时间：{now_text()}\n\n"
        "━━━━━━━━━━━━━━\n"
        f"📡 状态：{status_text}\n"
        f"{duration_label}：{h(duration)}\n"
        f"🆔 ID：<code>{r['id']}</code>\n"
        f"🖥️ 名称：{h(r['name'])}\n"
        f"🌐 主机：<code>{h(r['host'])}:{h(r['check_port'])}</code>\n"
        f"📍 地区：{server_location_line(r)}\n"
        f"🏢 运营商：{h(r['isp'] if 'isp' in r.keys() and r['isp'] else '未知')}\n"
        f"🧬 系统：{h(r['os_name'] if 'os_name' in r.keys() and r['os_name'] else '未知系统')}\n"
        f"📝 备注：{h(r['note'] or '无')}\n"
        f"🎁 永久免费：{bool_text(free)}\n"
        f"🔁 自动续费：{bool_text(auto)}\n"
        f"💰 价格：{server_price_line(r)}\n"
        f"📆 到期：{h(r['expire_at'] if r['expire_at'] else '未设置')}｜{expire_status_text(r['expire_at'], free)}\n"
        f"⏳ 续费倒计时：{h(renew_countdown_text(r['expire_at'], free))}\n"
        "━━━━━━━━━━━━━━\n"
        "📌 说明：远程服务器这里显示的是机器人监控到的在线/离线持续时间；如需真实系统 uptime，请部署探针。\n\n"
        "👇 <b>下一步：</b>查看该服务器流量/磁盘/事件，或编辑续费。"
    )[:3900]


def servers_summary_block():
    refresh_missing_meta()
    rows = get_all_servers(order="id") if "get_all_servers" in globals() else []
    if not rows:
        conn = db()
        rows = conn.execute("SELECT * FROM servers ORDER BY id ASC").fetchall()
        conn.close()
    if not rows:
        return "📡 <b>服务器在线情况</b>\n━━━━━━━━━━━━━━\n📭 暂无服务器记录。\n发送 <code>添加服务器</code> 开始添加。"
    online_count = 0
    offline_count = 0
    lines = []
    for r in rows:
        online = check_tcp(r["host"], r["check_port"], timeout=3)
        duration = server_monitor_duration_text(r, online)
        countdown = renew_countdown_text(r["expire_at"], is_free_forever_row(r))
        if online:
            online_count += 1
            status = "🟢 在线"
            duration_label = f"在线 {duration}"
        else:
            offline_count += 1
            status = "🔴 离线"
            duration_label = f"离线 {duration}"
        flag = country_flag(r["country_code"] if "country_code" in r.keys() else "")
        lines.append(f"{status}｜{flag} {h(r['name'])}｜{h(duration_label)}｜续费 {h(countdown)}")
    return (
        "📡 <b>服务器在线情况</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🟢 在线：{online_count} 台\n"
        f"🔴 离线：{offline_count} 台\n"
        f"📦 总数：{len(rows)} 台\n\n" + "\n".join(lines[:12])
    )


# 覆盖监控函数：增加 online_since/offline_since，状态时长从确认状态变化开始计算。
def monitor_server_online_status():
    conn = db()
    for col, definition in [
        ("fail_count", "INTEGER DEFAULT 0"),
        ("success_count", "INTEGER DEFAULT 0"),
        ("notified_offline", "INTEGER DEFAULT 0"),
        ("first_fail_at", "TEXT DEFAULT ''"),
        ("first_recover_at", "TEXT DEFAULT ''"),
        ("online_since", "TEXT DEFAULT ''"),
        ("offline_since", "TEXT DEFAULT ''"),
    ]:
        ensure_column(conn, "server_status", col, definition)
    conn.commit()

    rows = conn.execute("SELECT * FROM servers").fetchall()
    now_dt = datetime.now()
    now = now_text()

    for r in rows:
        sid = r["id"]
        raw_online = check_tcp(r["host"], r["check_port"], timeout=5)
        old = conn.execute("SELECT * FROM server_status WHERE server_id=?", (sid,)).fetchone()

        if not old:
            status = "online" if raw_online else "unknown"
            conn.execute(
                "INSERT INTO server_status(server_id,last_status,last_checked_at,last_changed_at,fail_count,success_count,notified_offline,first_fail_at,first_recover_at,online_since,offline_since) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (sid, status, now, now, 0 if raw_online else 1, 1 if raw_online else 0, 0, "" if raw_online else now, "", now if raw_online else "", "")
            )
            conn.commit()
            continue

        old_status = old["last_status"] or "unknown"
        notified = int(old["notified_offline"] if "notified_offline" in old.keys() and old["notified_offline"] is not None else 0)
        first_fail_at = old["first_fail_at"] if "first_fail_at" in old.keys() and old["first_fail_at"] else ""
        first_recover_at = old["first_recover_at"] if "first_recover_at" in old.keys() and old["first_recover_at"] else ""

        try:
            fail_elapsed = (now_dt - parse_date(first_fail_at)).total_seconds() if first_fail_at else 0
        except Exception:
            fail_elapsed = 0
        try:
            recover_elapsed = (now_dt - parse_date(first_recover_at)).total_seconds() if first_recover_at else 0
        except Exception:
            recover_elapsed = 0

        if raw_online:
            if old_status == "offline" and notified == 1:
                if not first_recover_at:
                    conn.execute("UPDATE server_status SET first_recover_at=?, last_checked_at=? WHERE server_id=?", (now, now, sid))
                    conn.commit()
                elif recover_elapsed >= RECOVERY_STABLE_SECONDS:
                    conn.execute(
                        "UPDATE server_status SET last_status='online', last_checked_at=?, last_changed_at=?, fail_count=0, success_count=1, notified_offline=0, first_fail_at='', first_recover_at='', online_since=?, offline_since='' WHERE server_id=?",
                        (now, now, now, sid)
                    )
                    conn.commit()
                    push_event("online", f"服务器恢复在线：{r['name']}", online_push_text(r))
                else:
                    conn.execute("UPDATE server_status SET last_checked_at=? WHERE server_id=?", (now, sid))
                    conn.commit()
            else:
                online_since = old["online_since"] if "online_since" in old.keys() and old["online_since"] else ""
                if old_status != "online":
                    online_since = now
                elif not online_since:
                    online_since = old["last_changed_at"] or now
                conn.execute(
                    "UPDATE server_status SET last_status='online', last_checked_at=?, fail_count=0, success_count=success_count+1, first_fail_at='', first_recover_at='', online_since=?, offline_since='' WHERE server_id=?",
                    (now, online_since, sid)
                )
                conn.commit()
        else:
            if old_status != "offline":
                if not first_fail_at:
                    conn.execute("UPDATE server_status SET first_fail_at=?, last_checked_at=?, fail_count=1 WHERE server_id=?", (now, now, sid))
                    conn.commit()
                elif fail_elapsed >= OFFLINE_GRACE_SECONDS:
                    conn.execute(
                        "UPDATE server_status SET last_status='offline', last_checked_at=?, last_changed_at=?, fail_count=fail_count+1, success_count=0, notified_offline=1, first_recover_at='', offline_since=?, online_since='' WHERE server_id=?",
                        (now, now, now, sid)
                    )
                    conn.commit()
                    push_event("offline", f"服务器离线：{r['name']}", offline_push_text(r))
                else:
                    conn.execute("UPDATE server_status SET last_checked_at=?, fail_count=fail_count+1 WHERE server_id=?", (now, sid))
                    conn.commit()
            else:
                offline_since = old["offline_since"] if "offline_since" in old.keys() and old["offline_since"] else (old["last_changed_at"] or now)
                conn.execute(
                    "UPDATE server_status SET last_checked_at=?, fail_count=fail_count+1, success_count=0, first_recover_at='', offline_since=?, online_since='' WHERE server_id=?",
                    (now, offline_since, sid)
                )
                conn.commit()

    conn.close()


def delete_select_text():
    rows = get_all_servers(order="id") if "get_all_servers" in globals() else []
    if not rows:
        conn = db()
        rows = conn.execute("SELECT * FROM servers ORDER BY id ASC").fetchall()
        conn.close()
    if not rows:
        return "🗑️✨ <b>删除服务器</b> ✨🗑️\n\n📭 当前没有可删除的服务器。"
    return (
        "🗑️✨ <b>选择要删除的服务器</b> ✨🗑️\n"
        f"🕒 更新时间：{now_text()}\n\n"
        "⚠️ 删除后会同时清理该服务器的状态、提醒记录。\n\n"
        "👇 请点击下面任意服务器进入删除确认。"
    )


def delete_select_keyboard():
    rows = get_all_servers(order="id") if "get_all_servers" in globals() else []
    if not rows:
        conn = db()
        rows = conn.execute("SELECT * FROM servers ORDER BY id ASC").fetchall()
        conn.close()
    kb = []
    for r in rows:
        flag = country_flag(r["country_code"] if "country_code" in r.keys() else "")
        kb.append([{"text": f"🗑️ {flag} ID{r['id']}｜{r['name']}｜{r['host']}", "callback_data": f"delete_confirm:{r['id']}"}])
    kb.extend(bottom_nav())
    return kb


def delete_confirm_text(sid):
    r = get_server_row(sid)
    if not r:
        return "❌ 没有找到这个服务器，可能已经被删除。"
    return (
        "⚠️🗑️ <b>确认删除服务器？</b> 🗑️⚠️\n\n"
        "━━━━━━━━━━━━━━\n"
        f"🆔 ID：<code>{r['id']}</code>\n"
        f"🖥️ 名称：{h(r['name'])}\n"
        f"🌐 主机：<code>{h(r['host'])}:{h(r['check_port'])}</code>\n"
        f"📍 地区：{server_location_line(r)}\n"
        "━━━━━━━━━━━━━━\n"
        "⚠️ 删除后会清理该服务器的：资料、状态、到期提醒。\n\n"
        "确认删除请点击下面红色按钮。"
    )


def delete_confirm_keyboard(sid):
    return [
        [{"text": "🚨 确认删除这台服务器", "callback_data": f"delete_do:{sid}"}],
        [{"text": "⬅️ 取消，返回详情", "callback_data": f"target:server:{sid}"}],
        [{"text": "📋 返回列表", "callback_data": "nav:servers"}, {"text": "📊 返回总览", "callback_data": "nav:dashboard"}],
    ]


def delete_server_by_id(sid):
    r = get_server_row(sid)
    if not r:
        return None
    conn = db()
    conn.execute("DELETE FROM servers WHERE id=?", (sid,))
    conn.execute("DELETE FROM server_status WHERE server_id=?", (sid,))
    conn.execute("DELETE FROM reminders WHERE server_id=?", (sid,))
    conn.commit()
    conn.close()
    event_add("action", "删除服务器", f"已删除服务器 ID {sid}：{r['name']} / {r['host']}")
    return r


# 覆盖服务器详情按钮：增加“删除服务器”明确入口。
def remote_detail_keyboard(sid):
    return bottom_nav([
        [
            {"text": "🌐 流量", "callback_data": f"view:traffic:server:{sid}"},
            {"text": "💾 磁盘", "callback_data": f"view:disk:server:{sid}"},
            {"text": "🧾 事件", "callback_data": f"view:events:server:{sid}"},
        ],
        [
            {"text": "✏️ 编辑", "callback_data": f"edit:server:{sid}"},
            {"text": "⏰ 续费", "callback_data": f"server_renew_help:{sid}"},
            {"text": "📡 探针", "callback_data": f"agent_cmd:{sid}"},
        ],
        [
            {"text": "💰 价格", "callback_data": f"field:server:{sid}:price"},
            {"text": "📆 到期", "callback_data": f"field:server:{sid}:expire_at"},
            {"text": "🔁 周期", "callback_data": f"field:server:{sid}:cycle"},
        ],
        [
            {"text": "📝 备注", "callback_data": f"field:server:{sid}:note"},
            {"text": "🔌 端口", "callback_data": f"field:server:{sid}:check_port"},
            {"text": "🧬 系统", "callback_data": f"field:server:{sid}:os_name"},
        ],
        [
            {"text": "🏷️ 名称", "callback_data": f"field:server:{sid}:name"},
            {"text": "🎁 永久免费", "callback_data": f"field:server:{sid}:free_forever"},
            {"text": "🔁 自动续费", "callback_data": f"field:server:{sid}:auto_renew"},
        ],
        [
            {"text": "🌍 刷新地区", "callback_data": f"refresh_meta:{sid}"},
            {"text": "🗑️ 删除服务器", "callback_data": f"delete_confirm:{sid}"},
        ],
        [
            {"text": "📆 +1月", "callback_data": f"renew_month:{sid}"},
            {"text": "🗓️ +3月", "callback_data": f"renew_quarter:{sid}"},
            {"text": "📅 +1年", "callback_data": f"renew_year:{sid}"},
        ],
    ])


_prev_handle_callback_delete_duration = handle_callback

def handle_callback(callback):
    try:
        callback_id = callback.get("id")
        data = callback.get("data") or ""
        msg = callback.get("message") or {}
        chat_id = msg.get("chat", {}).get("id")
        message_id = msg.get("message_id")

        if not is_admin(chat_id):
            answer_callback(callback_id, "未授权")
            return

        if data == "nav:delete" or data == "delete:select":
            answer_callback(callback_id, "选择删除")
            edit_inline_message(chat_id, message_id, delete_select_text(), delete_select_keyboard())
            return

        if data.startswith("delete_confirm:"):
            sid = data.split(":", 1)[1]
            answer_callback(callback_id, "删除确认")
            edit_inline_message(chat_id, message_id, delete_confirm_text(sid), delete_confirm_keyboard(sid))
            return

        if data.startswith("delete_do:"):
            sid = data.split(":", 1)[1]
            r = delete_server_by_id(sid)
            answer_callback(callback_id, "已删除" if r else "不存在")
            if not r:
                edit_inline_message(chat_id, message_id, "❌ 服务器不存在或已经被删除。", bottom_nav())
                return
            edit_inline_message(
                chat_id,
                message_id,
                "✅🗑️ <b>服务器已删除</b>\n\n"
                f"🆔 ID：<code>{h(sid)}</code>\n"
                f"🖥️ 名称：{h(r['name'])}\n"
                f"🌐 主机：<code>{h(r['host'])}</code>\n\n"
                "已清理该服务器的状态和提醒记录。",
                bottom_nav([[{"text": "📋 继续查看列表", "callback_data": "nav:servers"}]])
            )
            return

    except Exception as e:
        try:
            answer_callback(callback.get("id"), "操作失败")
            send(callback.get("message", {}).get("chat", {}).get("id"), f"❌ 删除/按钮操作失败：{h(e)}", keyboard=False)
        except Exception:
            pass
        return

    return _prev_handle_callback_delete_duration(callback)


_prev_handle_delete_duration = handle

def handle(chat_id, text):
    ct = clean_command_text(text)

    if ct in ["删除服务器", "删除机器", "移除服务器", "/delete_server"]:
        send_inline(chat_id, delete_select_text(), delete_select_keyboard())
        return

    m = re.match(r"^(删除服务器|删除机器|移除服务器)\s+(\d+)$", ct)
    if m:
        sid = m.group(2)
        send_inline(chat_id, delete_confirm_text(sid), delete_confirm_keyboard(sid))
        return

    return _prev_handle_delete_duration(chat_id, text)


# 启动时确保新增字段存在。
_old_init_db_delete_duration = init_db

def init_db():
    _old_init_db_delete_duration()
    ensure_status_duration_columns()




# ============================================================
# FINAL AGENT METRICS RECEIVER PATCH
# 真实在线运行时长说明：
# 远程服务器真实 uptime 必须由探针上报。
# 旧 agent 只会用 Bot Token 给 TG 发消息，主机器人收不到机器人自己发出的消息，
# 所以面板不会变化。这里新增 HTTP 上报接收器 + 数据表 + 面板读取。
# ============================================================

def metrics_secret():
    return os.getenv("METRICS_SECRET") or (BOT_TOKEN[-16:] if BOT_TOKEN else "server-monitor-secret")


def metrics_port():
    try:
        return int(os.getenv("METRICS_PORT", "8765"))
    except Exception:
        return 8765


def metrics_url_for_agent():
    ip = get_public_ip() or "你的主控服务器公网IP"
    return f"http://{ip}:{metrics_port()}/report"


def ensure_metrics_table():
    conn = db()
    conn.execute("""
    CREATE TABLE IF NOT EXISTS server_metrics (
        server_id INTEGER PRIMARY KEY,
        name TEXT DEFAULT '',
        hostname TEXT DEFAULT '',
        public_ip TEXT DEFAULT '',
        uptime_seconds INTEGER DEFAULT 0,
        boot_time TEXT DEFAULT '',
        cpu_percent REAL DEFAULT 0,
        mem_percent REAL DEFAULT 0,
        disk_percent REAL DEFAULT 0,
        rx_bytes INTEGER DEFAULT 0,
        tx_bytes INTEGER DEFAULT 0,
        updated_at TEXT DEFAULT '',
        raw TEXT DEFAULT ''
    )
    """)
    conn.commit()
    conn.close()


def save_agent_metrics(payload):
    ensure_metrics_table()
    sid = str(payload.get("server_id") or payload.get("sid") or "").strip()
    if not sid.isdigit():
        return False, "missing server_id"

    conn = db()
    row = conn.execute("SELECT id FROM servers WHERE id=?", (sid,)).fetchone()
    if not row:
        conn.close()
        return False, "server not found"

    name = str(payload.get("name") or "")
    hostname = str(payload.get("hostname") or "")
    public_ip = str(payload.get("public_ip") or "")
    boot_time = str(payload.get("boot_time") or "")
    updated_at = now_text()

    def to_int(v, default=0):
        try:
            return int(float(v))
        except Exception:
            return default

    def to_float(v, default=0):
        try:
            return float(v)
        except Exception:
            return default

    conn.execute("""
    INSERT OR REPLACE INTO server_metrics(
        server_id,name,hostname,public_ip,uptime_seconds,boot_time,cpu_percent,mem_percent,disk_percent,rx_bytes,tx_bytes,updated_at,raw
    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        int(sid),
        name,
        hostname,
        public_ip,
        to_int(payload.get("uptime_seconds")),
        boot_time,
        to_float(payload.get("cpu_percent")),
        to_float(payload.get("mem_percent")),
        to_float(payload.get("disk_percent")),
        to_int(payload.get("rx_bytes")),
        to_int(payload.get("tx_bytes")),
        updated_at,
        json.dumps(payload, ensure_ascii=False)
    ))
    conn.commit()
    conn.close()
    return True, "ok"


def get_agent_metrics(server_id):
    ensure_metrics_table()
    conn = db()
    row = conn.execute("SELECT * FROM server_metrics WHERE server_id=?", (server_id,)).fetchone()
    conn.close()
    return row


def metrics_fresh(row, max_age=180):
    if not row:
        return False
    try:
        return (datetime.now() - parse_date(row["updated_at"])).total_seconds() <= max_age
    except Exception:
        return False


def agent_runtime_line(r):
    m = get_agent_metrics(r["id"])
    if not m:
        return "⏱️ 系统运行时长：未收到探针数据"
    status = "🟢 探针在线" if metrics_fresh(m) else "🟠 探针超时"
    return (
        f"⏱️ 系统运行时长：{h(duration_from_seconds(m['uptime_seconds']))}\n"
        f"🕒 系统开机时间：{h(m['boot_time'] or '未知')}\n"
        f"📡 探针状态：{status}｜最后上报 {h(m['updated_at'] or '未知')}"
    )


def start_metrics_receiver():
    if getattr(start_metrics_receiver, "_started", False):
        return
    start_metrics_receiver._started = True

    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class MetricsHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def _send(self, code, body):
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps(body, ensure_ascii=False).encode("utf-8"))
            except Exception:
                pass

        def do_GET(self):
            if self.path.startswith("/health"):
                self._send(200, {"ok": True, "service": "server-monitor-metrics"})
            else:
                self._send(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            try:
                if not self.path.startswith("/report"):
                    self._send(404, {"ok": False, "error": "not found"})
                    return
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(min(length, 1024 * 256)).decode("utf-8", errors="ignore")
                payload = json.loads(raw or "{}")
                secret = str(payload.get("secret") or self.headers.get("X-Metrics-Secret") or "")
                if secret != metrics_secret():
                    self._send(403, {"ok": False, "error": "bad secret"})
                    return
                ok, msg = save_agent_metrics(payload)
                self._send(200 if ok else 400, {"ok": ok, "message": msg})
            except Exception as e:
                self._send(500, {"ok": False, "error": str(e)})

    def run():
        try:
            ensure_metrics_table()
            srv = ThreadingHTTPServer(("0.0.0.0", metrics_port()), MetricsHandler)
            print(f"[METRICS] receiver started on 0.0.0.0:{metrics_port()}", flush=True)
            srv.serve_forever()
        except Exception as e:
            print(f"[METRICS] receiver failed: {e}", flush=True)

    threading.Thread(target=run, daemon=True).start()


def agent_install_command(server_name="server", sid=None):
    admin_id = next(iter(ADMIN_IDS), "你的TG数字ID")
    token = BOT_TOKEN or "你的TG_BOT_TOKEN"
    safe_name = str(server_name or "server").replace('"', '').replace("'", "")
    sid_arg = str(sid or "0")
    return (
        "wget -qO- https://raw.githubusercontent.com/lxfcx/Oracle/main/agent.sh | "
        f"bash -s -- --url \"{metrics_url_for_agent()}\" --secret \"{metrics_secret()}\" "
        f"--sid \"{sid_arg}\" --token \"{token}\" --chat \"{admin_id}\" --name \"{safe_name}\""
    )


def cmd_agent_command(chat_id, sid=None):
    server_name = "server"
    title = "📡✨ <b>一键部署探针命令</b> ✨📡"
    real_sid = None
    if sid:
        r = get_server_row(sid)
        if r:
            real_sid = r["id"]
            server_name = r["name"]
            title = f"📡✨ <b>{h(server_name)} 一键部署探针</b> ✨📡"
    cmd = agent_install_command(server_name, real_sid)
    send_inline(chat_id, (
        f"{title}\n\n"
        "━━━━━━━━━━━━━━\n"
        "📌 <b>用途：</b>复制下面命令到对应服务器 SSH 执行。\n"
        "📌 <b>效果：</b>探针会每 60 秒向主机器人上报真实 uptime、CPU、内存、磁盘、流量。\n"
        "📌 <b>重要：</b>主控服务器需要放行 TCP 端口 "
        f"<code>{metrics_port()}</code>，否则探针无法上报。\n"
        "📌 <b>测试：</b>部署后返回服务器详情，等待 1 分钟查看 <code>系统运行时长</code> 是否更新。\n"
        "━━━━━━━━━━━━━━\n\n"
        f"<code>{h(cmd)}</code>"
    ), [[{"text": "⬅️ 返回服务器列表", "callback_data": "nav:servers"}, {"text": "📊 返回总览", "callback_data": "nav:dashboard"}]])


def server_button_label(r):
    online = check_tcp(r["host"], r["check_port"], timeout=3)
    status = "🟢" if online else "🔴"
    flag = country_flag(r["country_code"] if "country_code" in r.keys() else "")
    free = "🎁" if is_free_forever_row(r) else ""
    auto = "🔁" if is_auto_renew_row(r) else ""
    countdown = renew_countdown_text(r["expire_at"], is_free_forever_row(r)) if "renew_countdown_text" in globals() else expire_status_text(r["expire_at"], is_free_forever_row(r))
    m = get_agent_metrics(r["id"])
    if m:
        runtime = duration_from_seconds(m["uptime_seconds"])
        probe = "🟢" if metrics_fresh(m) else "🟠"
        run_text = f"{probe}运行 {runtime}"
    else:
        run_text = "⚪未装探针"
    return f"{status} {flag}{free}{auto} ID{r['id']}｜{r['name']}｜{run_text}｜续费 {countdown}"


def remote_detail_text(r):
    online = check_tcp(r["host"], r["check_port"], timeout=3)
    status_text = "🟢 在线" if online else "🔴 离线"
    free = is_free_forever_row(r)
    auto = is_auto_renew_row(r)
    m = get_agent_metrics(r["id"])
    countdown = renew_countdown_text(r["expire_at"], free) if "renew_countdown_text" in globals() else expire_status_text(r["expire_at"], free)

    probe_extra = ""
    if m:
        probe_extra = (
            f"\n📊 探针 CPU：{m['cpu_percent']:.0f}%"
            f"\n🧠 探针内存：{m['mem_percent']:.0f}%"
            f"\n💾 探针磁盘：{m['disk_percent']:.0f}%"
            f"\n🌐 探针流量：⬇️{fmt_size(m['rx_bytes'])} / ⬆️{fmt_size(m['tx_bytes'])}"
        )
    else:
        probe_extra = "\n📡 探针数据：未收到，请重新点击“📡 探针”部署新版探针"

    return (
        "🖥️✨ <b>服务器详情</b> ✨🖥️\n"
        f"🕒 更新时间：{now_text()}\n\n"
        "━━━━━━━━━━━━━━\n"
        f"📡 状态：{status_text}\n"
        f"{agent_runtime_line(r)}"
        f"{probe_extra}\n"
        f"🆔 ID：<code>{r['id']}</code>\n"
        f"🖥️ 名称：{h(r['name'])}\n"
        f"🌐 主机：<code>{h(r['host'])}:{h(r['check_port'])}</code>\n"
        f"📍 地区：{server_location_line(r)}\n"
        f"🏢 运营商：{h(r['isp'] if 'isp' in r.keys() and r['isp'] else '未知')}\n"
        f"🧬 系统：{h(r['os_name'] if 'os_name' in r.keys() and r['os_name'] else '未知系统')}\n"
        f"📝 备注：{h(r['note'] or '无')}\n"
        f"🎁 永久免费：{bool_text(free)}\n"
        f"🔁 自动续费：{bool_text(auto)}\n"
        f"💰 价格：{server_price_line(r)}\n"
        f"📆 到期：{h(r['expire_at'] if r['expire_at'] else '未设置')}｜{expire_status_text(r['expire_at'], free)}\n"
        f"⏳ 续费倒计时：{h(countdown)}\n"
        "━━━━━━━━━━━━━━\n"
        "👇 <b>下一步：</b>查看该服务器流量/磁盘/事件，或编辑续费。"
    )[:3900]


def servers_summary_block():
    ensure_metrics_table()
    refresh_missing_meta()
    rows = get_all_servers(order="id") if "get_all_servers" in globals() else []
    if not rows:
        conn = db()
        rows = conn.execute("SELECT * FROM servers ORDER BY id ASC").fetchall()
        conn.close()
    if not rows:
        return "📡 <b>服务器在线情况</b>\n━━━━━━━━━━━━━━\n📭 暂无服务器记录。\n发送 <code>添加服务器</code> 开始添加。"
    online_count = 0
    offline_count = 0
    lines = []
    for r in rows:
        online = check_tcp(r["host"], r["check_port"], timeout=3)
        countdown = renew_countdown_text(r["expire_at"], is_free_forever_row(r)) if "renew_countdown_text" in globals() else expire_status_text(r["expire_at"], is_free_forever_row(r))
        if online:
            online_count += 1
            status = "🟢 在线"
        else:
            offline_count += 1
            status = "🔴 离线"
        m = get_agent_metrics(r["id"])
        if m:
            run_text = f"运行 {duration_from_seconds(m['uptime_seconds'])}"
            probe = "🟢" if metrics_fresh(m) else "🟠"
        else:
            run_text = "未收到探针"
            probe = "⚪"
        flag = country_flag(r["country_code"] if "country_code" in r.keys() else "")
        lines.append(f"{status}｜{flag} {h(r['name'])}｜{probe}{h(run_text)}｜续费 {h(countdown)}")
    return (
        "📡 <b>服务器在线情况</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🟢 在线：{online_count} 台\n"
        f"🔴 离线：{offline_count} 台\n"
        f"📦 总数：{len(rows)} 台\n\n" + "\n".join(lines[:12])
    )


_old_poll_metrics_receiver = poll

def poll():
    start_metrics_receiver()
    return _old_poll_metrics_receiver()


_old_init_db_metrics_receiver = init_db

def init_db():
    _old_init_db_metrics_receiver()
    ensure_metrics_table()




# ============================================================
# FINAL HARDWARE CONFIG METRICS PATCH
# 新增：探针自动上报服务器配置数据：
#   🧩 CPU 核心数 / 🧠 内存总量 / 💾 硬盘总量
# 并在服务器列表、服务器详情、服务器总览自动显示。
# 说明：这些数据必须由新版 agent.sh 上报；旧探针不会上报这些字段。
# ============================================================

def ensure_metrics_hardware_columns():
    ensure_metrics_table()
    conn = db()
    for col, definition in [
        ("cpu_cores", "INTEGER DEFAULT 0"),
        ("mem_total", "INTEGER DEFAULT 0"),
        ("disk_total", "INTEGER DEFAULT 0"),
        ("disk_used", "INTEGER DEFAULT 0"),
        ("mem_used", "INTEGER DEFAULT 0"),
    ]:
        ensure_column(conn, "server_metrics", col, definition)
    conn.commit()
    conn.close()


def save_agent_metrics(payload):
    ensure_metrics_hardware_columns()
    sid = str(payload.get("server_id") or payload.get("sid") or "").strip()
    if not sid.isdigit():
        return False, "missing server_id"

    conn = db()
    row = conn.execute("SELECT id FROM servers WHERE id=?", (sid,)).fetchone()
    if not row:
        conn.close()
        return False, "server not found"

    def to_int(v, default=0):
        try:
            return int(float(v))
        except Exception:
            return default

    def to_float(v, default=0):
        try:
            return float(v)
        except Exception:
            return default

    name = str(payload.get("name") or "")
    hostname = str(payload.get("hostname") or "")
    public_ip = str(payload.get("public_ip") or "")
    boot_time = str(payload.get("boot_time") or "")
    updated_at = now_text()

    conn.execute("""
    INSERT OR REPLACE INTO server_metrics(
        server_id,name,hostname,public_ip,
        uptime_seconds,boot_time,
        cpu_percent,mem_percent,disk_percent,
        rx_bytes,tx_bytes,
        cpu_cores,mem_total,disk_total,disk_used,mem_used,
        updated_at,raw
    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        int(sid),
        name,
        hostname,
        public_ip,
        to_int(payload.get("uptime_seconds")),
        boot_time,
        to_float(payload.get("cpu_percent")),
        to_float(payload.get("mem_percent")),
        to_float(payload.get("disk_percent")),
        to_int(payload.get("rx_bytes")),
        to_int(payload.get("tx_bytes")),
        to_int(payload.get("cpu_cores")),
        to_int(payload.get("mem_total")),
        to_int(payload.get("disk_total")),
        to_int(payload.get("disk_used")),
        to_int(payload.get("mem_used")),
        updated_at,
        json.dumps(payload, ensure_ascii=False)
    ))
    conn.commit()
    conn.close()
    return True, "ok"


def get_agent_metrics(server_id):
    ensure_metrics_hardware_columns()
    conn = db()
    row = conn.execute("SELECT * FROM server_metrics WHERE server_id=?", (server_id,)).fetchone()
    conn.close()
    return row


def hardware_config_line(m):
    if not m:
        return "⚙️ 服务器配置：未收到探针数据"
    cpu = int(m["cpu_cores"] or 0) if "cpu_cores" in m.keys() else 0
    mem = int(m["mem_total"] or 0) if "mem_total" in m.keys() else 0
    disk = int(m["disk_total"] or 0) if "disk_total" in m.keys() else 0
    parts = []
    parts.append(f"🧩 {cpu} Cores" if cpu else "🧩 CPU 未知")
    parts.append(f"🧠 {fmt_size(mem)}" if mem else "🧠 内存未知")
    parts.append(f"💾 {fmt_size(disk)}" if disk else "💾 硬盘未知")
    return "⚙️ 服务器配置：" + " ｜ ".join(parts)


def hardware_config_short(m):
    if not m:
        return "⚪ 配置未知"
    cpu = int(m["cpu_cores"] or 0) if "cpu_cores" in m.keys() else 0
    mem = int(m["mem_total"] or 0) if "mem_total" in m.keys() else 0
    disk = int(m["disk_total"] or 0) if "disk_total" in m.keys() else 0
    return f"🧩 {cpu or '?'}C ｜ 🧠 {fmt_size(mem) if mem else '?'} ｜ 💾 {fmt_size(disk) if disk else '?'}"


def agent_install_command(server_name="server", sid=None):
    admin_id = next(iter(ADMIN_IDS), "你的TG数字ID")
    token = BOT_TOKEN or "你的TG_BOT_TOKEN"
    safe_name = str(server_name or "server").replace('"', '').replace("'", "")
    sid_arg = str(sid or "0")
    return (
        "wget -qO- https://raw.githubusercontent.com/lxfcx/Oracle/main/agent.sh | "
        f"bash -s -- --url \"{metrics_url_for_agent()}\" --secret \"{metrics_secret()}\" "
        f"--sid \"{sid_arg}\" --token \"{token}\" --chat \"{admin_id}\" --name \"{safe_name}\""
    )


def cmd_agent_command(chat_id, sid=None):
    server_name = "server"
    title = "📡✨ <b>一键部署探针命令</b> ✨📡"
    real_sid = None
    if sid:
        r = get_server_row(sid)
        if r:
            real_sid = r["id"]
            server_name = r["name"]
            title = f"📡✨ <b>{h(server_name)} 一键部署探针</b> ✨📡"
    cmd = agent_install_command(server_name, real_sid)
    send_inline(chat_id, (
        f"{title}\n\n"
        "━━━━━━━━━━━━━━\n"
        "📌 <b>用途：</b>复制下面命令到对应服务器 SSH 执行。\n"
        "📌 <b>效果：</b>探针会每 60 秒向主机器人上报真实 uptime、CPU、内存、磁盘、流量。\n"
        "📌 <b>新增：</b>自动显示服务器配置：CPU 核心数 / 内存总量 / 硬盘总量。\n"
        "📌 <b>重要：</b>主控服务器需要放行 TCP 端口 "
        f"<code>{metrics_port()}</code>，否则探针无法上报。\n"
        "📌 <b>测试：</b>部署后等待 1 分钟，再打开服务器详情查看配置数据。\n"
        "━━━━━━━━━━━━━━\n\n"
        f"<code>{h(cmd)}</code>"
    ), [[{"text": "⬅️ 返回服务器列表", "callback_data": "nav:servers"}, {"text": "📊 返回总览", "callback_data": "nav:dashboard"}]])


def server_button_label(r):
    online = check_tcp(r["host"], r["check_port"], timeout=3)
    status = "🟢" if online else "🔴"
    flag = country_flag(r["country_code"] if "country_code" in r.keys() else "")
    free = "🎁" if is_free_forever_row(r) else ""
    auto = "🔁" if is_auto_renew_row(r) else ""
    countdown = renew_countdown_text(r["expire_at"], is_free_forever_row(r)) if "renew_countdown_text" in globals() else expire_status_text(r["expire_at"], is_free_forever_row(r))
    m = get_agent_metrics(r["id"])
    if m:
        runtime = duration_from_seconds(m["uptime_seconds"])
        probe = "🟢" if metrics_fresh(m) else "🟠"
        run_text = f"{probe}运行 {runtime}"
        hw = hardware_config_short(m)
    else:
        run_text = "⚪未装探针"
        hw = "⚪配置未知"
    return f"{status} {flag}{free}{auto} ID{r['id']}｜{r['name']}｜{hw}｜{run_text}｜续费 {countdown}"


def remote_detail_text(r):
    online = check_tcp(r["host"], r["check_port"], timeout=3)
    status_text = "🟢 在线" if online else "🔴 离线"
    free = is_free_forever_row(r)
    auto = is_auto_renew_row(r)
    m = get_agent_metrics(r["id"])
    countdown = renew_countdown_text(r["expire_at"], free) if "renew_countdown_text" in globals() else expire_status_text(r["expire_at"], free)

    if m:
        probe_extra = (
            f"\n{hardware_config_line(m)}"
            f"\n📊 探针 CPU：{m['cpu_percent']:.0f}%"
            f"\n🧠 探针内存：{fmt_size(m['mem_used']) if 'mem_used' in m.keys() and m['mem_used'] else '未知'} / {fmt_size(m['mem_total']) if 'mem_total' in m.keys() and m['mem_total'] else '未知'} ({m['mem_percent']:.0f}%)"
            f"\n💾 探针硬盘：{fmt_size(m['disk_used']) if 'disk_used' in m.keys() and m['disk_used'] else '未知'} / {fmt_size(m['disk_total']) if 'disk_total' in m.keys() and m['disk_total'] else '未知'} ({m['disk_percent']:.0f}%)"
            f"\n🌐 探针流量：⬇️{fmt_size(m['rx_bytes'])} / ⬆️{fmt_size(m['tx_bytes'])}"
        )
    else:
        probe_extra = "\n⚙️ 服务器配置：未收到探针数据\n📡 探针数据：未收到，请点击“📡 探针”部署新版探针"

    return (
        "🖥️✨ <b>服务器详情</b> ✨🖥️\n"
        f"🕒 更新时间：{now_text()}\n\n"
        "━━━━━━━━━━━━━━\n"
        f"📡 状态：{status_text}\n"
        f"{agent_runtime_line(r)}"
        f"{probe_extra}\n"
        f"🆔 ID：<code>{r['id']}</code>\n"
        f"🖥️ 名称：{h(r['name'])}\n"
        f"🌐 主机：<code>{h(r['host'])}:{h(r['check_port'])}</code>\n"
        f"📍 地区：{server_location_line(r)}\n"
        f"🏢 运营商：{h(r['isp'] if 'isp' in r.keys() and r['isp'] else '未知')}\n"
        f"🧬 系统：{h(r['os_name'] if 'os_name' in r.keys() and r['os_name'] else '未知系统')}\n"
        f"📝 备注：{h(r['note'] or '无')}\n"
        f"🎁 永久免费：{bool_text(free)}\n"
        f"🔁 自动续费：{bool_text(auto)}\n"
        f"💰 价格：{server_price_line(r)}\n"
        f"📆 到期：{h(r['expire_at'] if r['expire_at'] else '未设置')}｜{expire_status_text(r['expire_at'], free)}\n"
        f"⏳ 续费倒计时：{h(countdown)}\n"
        "━━━━━━━━━━━━━━\n"
        "👇 <b>下一步：</b>查看该服务器流量/磁盘/事件，或编辑续费。"
    )[:3900]


def servers_summary_block():
    ensure_metrics_hardware_columns()
    refresh_missing_meta()
    rows = get_all_servers(order="id") if "get_all_servers" in globals() else []
    if not rows:
        conn = db()
        rows = conn.execute("SELECT * FROM servers ORDER BY id ASC").fetchall()
        conn.close()
    if not rows:
        return "📡 <b>服务器在线情况</b>\n━━━━━━━━━━━━━━\n📭 暂无服务器记录。\n发送 <code>添加服务器</code> 开始添加。"
    online_count = 0
    offline_count = 0
    lines = []
    for r in rows:
        online = check_tcp(r["host"], r["check_port"], timeout=3)
        countdown = renew_countdown_text(r["expire_at"], is_free_forever_row(r)) if "renew_countdown_text" in globals() else expire_status_text(r["expire_at"], is_free_forever_row(r))
        if online:
            online_count += 1
            status = "🟢 在线"
        else:
            offline_count += 1
            status = "🔴 离线"
        m = get_agent_metrics(r["id"])
        if m:
            run_text = f"运行 {duration_from_seconds(m['uptime_seconds'])}"
            probe = "🟢" if metrics_fresh(m) else "🟠"
            hw = hardware_config_short(m)
        else:
            run_text = "未收到探针"
            probe = "⚪"
            hw = "配置未知"
        flag = country_flag(r["country_code"] if "country_code" in r.keys() else "")
        lines.append(f"{status}｜{flag} {h(r['name'])}｜{h(hw)}｜{probe}{h(run_text)}｜续费 {h(countdown)}")
    return (
        "📡 <b>服务器在线情况</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🟢 在线：{online_count} 台\n"
        f"🔴 离线：{offline_count} 台\n"
        f"📦 总数：{len(rows)} 台\n\n" + "\n".join(lines[:12])
    )


_old_init_db_hardware_metrics = init_db

def init_db():
    _old_init_db_hardware_metrics()
    ensure_metrics_hardware_columns()




# ============================================================
# FINAL PATCH: 编辑服务器 IP / ID 不跳号 / 本机一键探针入口
# 只追加覆盖最后一层函数，不删除原逻辑。
# ============================================================

def reset_server_id_sequence():
    """
    修复 SQLite AUTOINCREMENT 删除最大 ID 后继续跳号的问题。
    例如当前最大 ID=6，删除 6 后会把 sqlite_sequence 重置为 5，
    下一次添加服务器继续使用 ID=6。
    """
    try:
        conn = db()
        max_id = conn.execute("SELECT COALESCE(MAX(id), 0) AS max_id FROM servers").fetchone()["max_id"]
        try:
            conn.execute("UPDATE sqlite_sequence SET seq=? WHERE name='servers'", (int(max_id),))
            conn.execute("INSERT OR IGNORE INTO sqlite_sequence(name, seq) VALUES('servers', ?)", (int(max_id),))
        except Exception:
            pass
        conn.commit()
        conn.close()
    except Exception:
        pass


def validate_host_value(value):
    value = one_line(value, "").strip()
    if not value:
        raise ValueError("IP / 主机不能为空")
    if len(value) > 253:
        raise ValueError("IP / 主机太长")
    if value.startswith("http://") or value.startswith("https://"):
        raise ValueError("这里只填写 IP 或域名，不要带 http:// 或 https://")
    if "/" in value or " " in value:
        raise ValueError("IP / 主机格式不正确")
    if ":" in value:
        raise ValueError("不要在主机里带端口；端口请单独用“编辑端口”。")
    return value


def refresh_server_meta_after_host_change(sid, host):
    conn = db()
    row = conn.execute("SELECT * FROM servers WHERE id=?", (sid,)).fetchone()
    if not row:
        conn.close()
        return None, False

    meta = detect_server_meta(host)
    online = check_tcp(host, row["check_port"], timeout=5)
    status = "online" if online else "offline"

    conn.execute(
        "UPDATE servers SET country=?, country_code=?, region=?, city=?, isp=?, last_meta_at=? WHERE id=?",
        (meta["country"], meta["country_code"], meta["region"], meta["city"], meta["isp"], now_text(), sid)
    )

    # IP/主机变更后，状态重新按新地址检测，避免旧状态误导。
    conn.execute(
        "INSERT OR REPLACE INTO server_status(server_id,last_status,last_checked_at,last_changed_at) VALUES(?,?,?,?)",
        (sid, status, now_text(), now_text())
    )

    # 如果启用了探针数据缓存，换 IP 后旧探针数据可能属于旧机器，清掉。
    try:
        conn.execute("DELETE FROM server_metrics WHERE server_id=?", (sid,))
    except Exception:
        pass

    conn.commit()
    row = conn.execute("SELECT * FROM servers WHERE id=?", (sid,)).fetchone()
    conn.close()
    return row, online


def update_server_host(sid, value):
    host = validate_host_value(value)
    conn = db()
    row = conn.execute("SELECT * FROM servers WHERE id=?", (sid,)).fetchone()
    if not row:
        conn.close()
        raise ValueError("没有找到这个服务器 ID")
    old_host = row["host"]
    conn.execute("UPDATE servers SET host=? WHERE id=?", (host, sid))
    conn.commit()
    conn.close()

    new_row, online = refresh_server_meta_after_host_change(sid, host)
    event_add("action", "编辑服务器IP", f"服务器 ID {sid} 主机已从 {old_host} 修改为 {host}")
    return new_row or get_server_row(sid), old_host, host, online


# 覆盖字段中文名，加入 host。
def field_cn(field):
    return {
        "host": "IP / 主机",
        "ip": "IP / 主机",
        "price": "付费价格",
        "expire_at": "到期时间",
        "cycle": "付费周期",
        "note": "备注",
        "check_port": "检测端口",
        "os_name": "系统",
        "name": "名称",
        "opened_at": "开通时间",
        "free_forever": "永久免费",
        "auto_renew": "自动续费",
    }.get(field, field)


def field_example(field):
    return {
        "host": "1.2.3.4  或  example.com",
        "ip": "1.2.3.4  或  example.com",
        "price": "50 CNY  或  6 USD",
        "expire_at": "2027-05-01",
        "opened_at": "2026-05-01 10:00:00  或  2026-05-01",
        "cycle": "月付 / 季付 / 年付",
        "note": "香港甲骨文主力机",
        "check_port": "22  或  443",
        "os_name": "Ubuntu 22.04",
        "name": "HK-Oracle",
        "free_forever": "是 / 否",
        "auto_renew": "是 / 否",
    }.get(field, "请输入新内容")


# 覆盖服务器编辑菜单，加入编辑 IP/主机。
def edit_server_field_keyboard(sid):
    return [
        [
            {"text": "🌐 编辑IP/主机", "callback_data": f"field:server:{sid}:host"},
        ],
        [
            {"text": "💰 编辑价格", "callback_data": f"field:server:{sid}:price"},
            {"text": "📆 编辑到期", "callback_data": f"field:server:{sid}:expire_at"},
        ],
        [
            {"text": "🔁 编辑周期", "callback_data": f"field:server:{sid}:cycle"},
            {"text": "📝 编辑备注", "callback_data": f"field:server:{sid}:note"},
        ],
        [
            {"text": "🔌 编辑端口", "callback_data": f"field:server:{sid}:check_port"},
            {"text": "🧬 编辑系统", "callback_data": f"field:server:{sid}:os_name"},
        ],
        [
            {"text": "🏷️ 编辑名称", "callback_data": f"field:server:{sid}:name"},
        ],
        [
            {"text": "🎁 永久免费", "callback_data": f"field:server:{sid}:free_forever"},
            {"text": "🔁 自动续费", "callback_data": f"field:server:{sid}:auto_renew"},
        ],
        [{"text": "⬅️ 上一步：服务器详情", "callback_data": f"target:server:{sid}"}],
        [
            {"text": "📋 返回列表", "callback_data": "nav:servers"},
            {"text": "📊 返回总览", "callback_data": "nav:dashboard"},
            {"text": "🛠️ 工具", "callback_data": "nav:tools"},
        ],
    ]


# 覆盖本机详情按钮，加入“一键部署探针”入口。
def local_detail_keyboard():
    return bottom_nav([
        [
            {"text": "🌐 本机流量", "callback_data": "view:traffic:local"},
            {"text": "💾 本机磁盘", "callback_data": "view:disk:local"},
        ],
        [
            {"text": "🧾 本机事件", "callback_data": "view:events:local"},
            {"text": "✏️ 编辑本机", "callback_data": "edit:local"},
        ],
        [
            {"text": "📡 本机一键部署探针", "callback_data": "agent_cmd:local"},
        ],
        [
            {"text": "💰 价格", "callback_data": "field:local:0:price"},
            {"text": "📆 到期", "callback_data": "field:local:0:expire_at"},
            {"text": "🔁 周期", "callback_data": "field:local:0:cycle"},
        ],
        [
            {"text": "📝 备注", "callback_data": "field:local:0:note"},
            {"text": "🏷️ 名称", "callback_data": "field:local:0:name"},
        ],
        [{"text": "🌍 刷新本机地区", "callback_data": "local:refresh_meta"}],
    ])


# 覆盖服务器详情按钮，直接加入“改IP/主机”。
def remote_detail_keyboard(sid):
    return bottom_nav([
        [
            {"text": "🌐 流量", "callback_data": f"view:traffic:server:{sid}"},
            {"text": "💾 磁盘", "callback_data": f"view:disk:server:{sid}"},
            {"text": "🧾 事件", "callback_data": f"view:events:server:{sid}"},
        ],
        [
            {"text": "✏️ 编辑", "callback_data": f"edit:server:{sid}"},
            {"text": "🌐 改IP/主机", "callback_data": f"field:server:{sid}:host"},
            {"text": "📡 探针", "callback_data": f"agent_cmd:{sid}"},
        ],
        [
            {"text": "⏰ 续费", "callback_data": f"server_renew_help:{sid}"},
            {"text": "🌍 刷新地区", "callback_data": f"refresh_meta:{sid}"},
            {"text": "🗑️ 删除服务器", "callback_data": f"delete_confirm:{sid}"},
        ],
        [
            {"text": "💰 价格", "callback_data": f"field:server:{sid}:price"},
            {"text": "📆 到期", "callback_data": f"field:server:{sid}:expire_at"},
            {"text": "🔁 周期", "callback_data": f"field:server:{sid}:cycle"},
        ],
        [
            {"text": "📝 备注", "callback_data": f"field:server:{sid}:note"},
            {"text": "🔌 端口", "callback_data": f"field:server:{sid}:check_port"},
            {"text": "🧬 系统", "callback_data": f"field:server:{sid}:os_name"},
        ],
        [
            {"text": "🏷️ 名称", "callback_data": f"field:server:{sid}:name"},
            {"text": "🎁 永久免费", "callback_data": f"field:server:{sid}:free_forever"},
            {"text": "🔁 自动续费", "callback_data": f"field:server:{sid}:auto_renew"},
        ],
        [
            {"text": "📆 +1月", "callback_data": f"renew_month:{sid}"},
            {"text": "🗓️ +3月", "callback_data": f"renew_quarter:{sid}"},
            {"text": "📅 +1年", "callback_data": f"renew_year:{sid}"},
        ],
    ])


# 包装字段保存函数：只新增 host/ip，其它字段走旧逻辑。
_prev_update_server_field_host_patch = update_server_field

def update_server_field(sid, field, value):
    if field in ["host", "ip"]:
        row, old_host, new_host, online = update_server_host(sid, value)
        return row
    return _prev_update_server_field_host_patch(sid, field, value)


# 包装 pending 输入：点击“编辑IP/主机”后，用户发新 IP 时走这里。
_prev_process_pending_input_host_patch = process_pending_input

def process_pending_input(chat_id, text):
    row = get_pending_action(chat_id)
    if row and row["target_type"] == "server" and row["field"] in ["host", "ip"]:
        sid = row["target_id"]
        try:
            new_row, old_host, new_host, online = update_server_host(sid, text)
            clear_pending_action(chat_id)
            status_text = "🟢 在线" if online else "🔴 离线"
            send_inline(
                chat_id,
                "✅🌐 <b>服务器 IP / 主机已更新</b>\n\n"
                f"🆔 ID：<code>{h(sid)}</code>\n"
                f"旧主机：<code>{h(old_host)}</code>\n"
                f"新主机：<code>{h(new_host)}</code>\n"
                f"当前检测：{status_text}\n\n"
                "📌 已自动刷新国家地区/运营商。\n"
                "📌 已清理旧探针缓存。\n"
                "📌 如果这是新机器，请重新点击 <b>📡 探针</b> 部署新版探针。\n\n"
                f"{remote_detail_text(new_row)}",
                remote_detail_keyboard(sid)
            )
            return True
        except Exception as e:
            send_inline(
                chat_id,
                f"❌ <b>编辑失败：</b>{h(e)}\n\n请重新发送正确 IP / 主机，或发送 <code>取消</code> 退出编辑。\n\n示例：<code>1.2.3.4</code>",
                edit_nav_keyboard("server", sid)
            )
            return True
    return _prev_process_pending_input_host_patch(chat_id, text)


def update_host_command(chat_id, sid, value):
    try:
        new_row, old_host, new_host, online = update_server_host(sid, value)
        status_text = "🟢 在线" if online else "🔴 离线"
        send_inline(
            chat_id,
            "✅🌐 <b>服务器 IP / 主机已更新</b>\n\n"
            f"🆔 ID：<code>{h(sid)}</code>\n"
            f"旧主机：<code>{h(old_host)}</code>\n"
            f"新主机：<code>{h(new_host)}</code>\n"
            f"当前检测：{status_text}\n\n"
            "📌 已自动刷新国家地区/运营商。\n"
            "📌 已清理旧探针缓存。\n"
            "📌 如果这是新机器，请重新点击 <b>📡 探针</b> 部署新版探针。\n\n"
            f"{remote_detail_text(new_row)}",
            remote_detail_keyboard(sid)
        )
    except Exception as e:
        send(chat_id, f"❌ 编辑服务器 IP / 主机失败：{h(e)}\n\n示例：<code>编辑IP {h(sid)} 1.2.3.4</code>")


def cmd_local_agent_command(chat_id):
    """
    本机就是主控服务器，状态/CPU/内存/磁盘/流量已经由 bot.py 直接读取。
    这里保留按钮入口，避免本机页面缺少“一键部署探针”功能。
    """
    send_inline(chat_id, (
        "📡✨ <b>本机一键部署探针</b> ✨📡\n\n"
        "━━━━━━━━━━━━━━\n"
        "🏠 <b>说明：</b>本机就是主控服务器，机器人已经直接读取本机真实数据：CPU、内存、磁盘、流量、运行时间。\n\n"
        "✅ 所以本机通常 <b>不需要额外安装探针</b>。\n\n"
        "如果你想把本机也当成“远程服务器”一样用探针上报，请先把本机公网 IP 添加为一台服务器，然后进入那台服务器详情点击 <b>📡 探针</b>。\n"
        "━━━━━━━━━━━━━━"
    ), bottom_nav([[{"text": "🏠 返回本机详情", "callback_data": "target:local"}]]))


# 包装删除函数：所有使用 delete_server_by_id 的删除都会重置 ID。
_prev_delete_server_by_id_host_patch = delete_server_by_id

def delete_server_by_id(sid):
    r = _prev_delete_server_by_id_host_patch(sid)
    reset_server_id_sequence()
    return r


# 包装添加：添加前校正 sqlite_sequence，删除最大 ID 后新建不会跳号。
_prev_cmd_add_server_host_patch = cmd_add_server

def cmd_add_server(chat_id, text):
    reset_server_id_sequence()
    return _prev_cmd_add_server_host_patch(chat_id, text)


# 最后一层 callback：优先接管本机探针和删除确认，避免旧 callback 先执行。
_prev_handle_callback_host_patch = handle_callback

def handle_callback(callback):
    try:
        callback_id = callback.get("id")
        data = callback.get("data") or ""
        msg = callback.get("message") or {}
        chat_id = msg.get("chat", {}).get("id")
        message_id = msg.get("message_id")

        if data == "agent_cmd:local":
            if not is_admin(chat_id):
                answer_callback(callback_id, "未授权")
                return
            answer_callback(callback_id, "本机探针")
            cmd_local_agent_command(chat_id)
            return

        if data.startswith("delete_do:"):
            if not is_admin(chat_id):
                answer_callback(callback_id, "未授权")
                return
            sid = data.split(":", 1)[1]
            r = delete_server_by_id(sid)
            answer_callback(callback_id, "已删除" if r else "不存在")
            if not r:
                edit_inline_message(chat_id, message_id, "❌ 服务器不存在或已经被删除。", bottom_nav())
                return
            edit_inline_message(
                chat_id,
                message_id,
                "✅🗑️ <b>服务器已删除</b>\n\n"
                f"🆔 ID：<code>{h(sid)}</code>\n"
                f"🖥️ 名称：{h(r['name'])}\n"
                f"🌐 主机：<code>{h(r['host'])}</code>\n\n"
                "已清理该服务器的状态和提醒记录。\n"
                "📌 编号已重置：如果这是最大 ID，下一台新服务器会继续使用这个编号。",
                bottom_nav([[{"text": "📋 继续查看列表", "callback_data": "nav:servers"}]])
            )
            return
    except Exception as e:
        try:
            send(callback.get("message", {}).get("chat", {}).get("id"), f"❌ 删除/按钮操作失败：{h(e)}", keyboard=False)
        except Exception:
            pass
        return

    return _prev_handle_callback_host_patch(callback)


# 最后一层文字命令：支持编辑IP/主机，并兼容多行批量。
_prev_handle_host_patch = handle

def handle(chat_id, text):
    ct = clean_command_text(text)

    m = re.match(r"^(编辑IP|编辑ip|编辑主机|编辑地址|修改IP|修改ip|修改主机|修改地址)\s+(\d+)\s+(.+)$", ct)
    if m:
        update_host_command(chat_id, m.group(2), m.group(3).strip())
        return

    if "\n" in str(text or ""):
        lines = split_command_lines(text)
        edit_prefixes = ("编辑IP", "编辑ip", "编辑主机", "编辑地址", "修改IP", "修改ip", "修改主机", "修改地址")
        if lines and all(line.startswith(edit_prefixes + LOCAL_EDIT_PREFIXES + SERVER_EDIT_PREFIXES) for line in lines):
            for line in lines:
                mm = re.match(r"^(编辑IP|编辑ip|编辑主机|编辑地址|修改IP|修改ip|修改主机|修改地址)\s+(\d+)\s+(.+)$", line)
                if mm:
                    update_host_command(chat_id, mm.group(2), mm.group(3).strip())
                else:
                    _prev_handle_host_patch(chat_id, line)
            return

    return _prev_handle_host_patch(chat_id, text)


# 启动时校正一次编号序列。
_old_init_db_host_patch = init_db

def init_db():
    _old_init_db_host_patch()
    reset_server_id_sequence()


if __name__ == "__main__":
    init_db()
    poll()
