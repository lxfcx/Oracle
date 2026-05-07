#!/usr/bin/env python3
import os
import time
import html
import sqlite3
import socket
import subprocess
from datetime import datetime, timedelta

import psutil
import requests
from dateutil.parser import parse as parse_date


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


def h(v):
    return html.escape(str(v if v is not None else ""))


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
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

    conn.commit()
    conn.close()


def tg(method, payload=None):
    try:
        return requests.post(
            f"{API}/{method}",
            json=payload or {},
            timeout=20
        ).json()
    except Exception:
        return {}


def send(chat_id, text):
    return tg("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    })


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
        out = subprocess.check_output(
            cmd,
            shell=True,
            stderr=subprocess.STDOUT,
            timeout=timeout
        )
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


def get_local_status():
    cpu = psutil.cpu_percent(interval=1)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    load1, load5, load15 = os.getloadavg()

    return {
        "hostname": socket.gethostname(),
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
    total = psutil.net_io_counters()
    pernic = psutil.net_io_counters(pernic=True)
    return total, pernic


def traffic_detail():
    total1, pernic1 = get_traffic_snapshot()
    time.sleep(1)
    total2, pernic2 = get_traffic_snapshot()

    realtime_down = total2.bytes_recv - total1.bytes_recv
    realtime_up = total2.bytes_sent - total1.bytes_sent

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
        "realtime_down": realtime_down,
        "realtime_up": realtime_up,
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


def cycle_name(cycle):
    return {
        "monthly": "📆 月付",
        "quarterly": "🗓️ 季付",
        "yearly": "📅 年付"
    }.get(cycle, cycle)


def currency_name(currency):
    return {
        "CNY": "🇨🇳 ¥",
        "USD": "🇺🇸 $",
        "EUR": "🇪🇺 €",
        "GBP": "🇬🇧 £"
    }.get(currency, currency)


def normalize_cycle(cycle):
    cycle = str(cycle).lower().strip()
    return {
        "月付": "monthly",
        "月": "monthly",
        "monthly": "monthly",
        "month": "monthly",
        "季付": "quarterly",
        "季": "quarterly",
        "quarterly": "quarterly",
        "quarter": "quarterly",
        "年付": "yearly",
        "年": "yearly",
        "yearly": "yearly",
        "year": "yearly",
        "annual": "yearly",
    }.get(cycle, cycle)


def normalize_currency(currency):
    currency_raw = str(currency).strip()
    currency = currency_raw.upper()

    alias = {
        "人民币": "CNY",
        "RMB": "CNY",
        "¥": "CNY",
        "美元": "USD",
        "$": "USD",
        "欧元": "EUR",
        "€": "EUR",
        "英镑": "GBP",
        "£": "GBP",
    }

    return alias.get(currency_raw, alias.get(currency, currency))


def service_cn(status):
    status = str(status).strip()
    return {
        "active": "✅ 运行中",
        "inactive": "⚠️ 未运行",
        "failed": "🚨 运行失败",
        "unknown": "❓ 未知",
        "": "❓ 未检测到"
    }.get(status, status or "❓ 未知")


def event_add(event_type, title, content):
    try:
        conn = db()
        conn.execute(
            "INSERT INTO events(event_type, title, content, created_at) VALUES(?,?,?,?)",
            (event_type, title, content, now_text())
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def push_event(event_type, title, content):
    event_add(event_type, title, content)
    broadcast(content)


def get_recent_events(limit=8):
    conn = db()
    rows = conn.execute(
        "SELECT * FROM events ORDER BY id DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return rows


def set_bot_commands():
    commands = [
        {"command": "help", "description": "帮助菜单 / 查看所有功能"},
        {"command": "enable_commands", "description": "启用左侧命令菜单"},
        {"command": "dashboard", "description": "服务器总览：状态/流量/事件"},
        {"command": "status", "description": "查看本机状态"},
        {"command": "disk", "description": "查看磁盘使用情况"},
        {"command": "traffic", "description": "查看服务器流量"},
        {"command": "servers", "description": "查看服务器列表"},
        {"command": "check_servers", "description": "检测在线/离线"},
        {"command": "add_server", "description": "添加服务器提醒"},
        {"command": "events", "description": "查看服务器事件"},
        {"command": "security", "description": "查看安全状态"},
        {"command": "login_log", "description": "查看登录记录"},
        {"command": "fail2ban", "description": "查看防爆破状态"},
        {"command": "restart_xray", "description": "重启 Xray / x-ui"},
        {"command": "clean_cache", "description": "清理系统缓存"}
    ]

    result = tg("setMyCommands", {"commands": commands})
    return result


def cmd_enable_commands(chat_id):
    result = set_bot_commands()
    if result.get("ok"):
        send(chat_id, (
            "✅✨ <b>左侧命令菜单已启用</b> ✨✅\n\n"
            "📌 现在你可以点击 Telegram 输入框旁边的 <b>/</b> 或菜单按钮查看功能。\n\n"
            "也可以直接发送中文：\n"
            "🖥️ <code>查看状态</code>\n"
            "💾 <code>查看磁盘</code>\n"
            "🌐 <code>查看流量</code>\n"
            "📋 <code>查看服务器</code>\n"
            "📊 <code>服务器总览</code>\n"
            "🧾 <code>添加服务器</code>"
        ))
    else:
        send(chat_id, (
            "⚠️ <b>左侧命令菜单启用失败</b>\n\n"
            "请检查 BOT_TOKEN 是否正确，或稍后重试。\n\n"
            f"返回信息：<code>{h(result)}</code>"
        ))


def cmd_help(chat_id):
    send(chat_id, """
🤖✨ <b>服务器监控管理机器人</b> ✨🤖

━━━━━━━━━━━━━━━━━━
📌 <b>使用方式</b>
━━━━━━━━━━━━━━━━━━

你可以直接发送中文，不需要记英文命令。

首次使用建议发送：
<code>启用命令</code>

启用后，Telegram 左侧 / 菜单会显示所有功能。

━━━━━━━━━━━━━━━━━━
📊 <b>总览功能</b>
━━━━━━━━━━━━━━━━━━

<code>服务器总览</code>
查看本机状态、流量使用、在线离线、最近事件。

<code>查看事件</code>
查看服务器最近事件记录。

━━━━━━━━━━━━━━━━━━
🖥️ <b>本机状态</b>
━━━━━━━━━━━━━━━━━━

<code>查看状态</code>
查看 CPU、内存、磁盘、负载、运行时间。

<code>查看磁盘</code>
查看磁盘分区、容量、已用、可用、使用率。

<code>查看流量</code>
查看总上传、总下载、实时上传、实时下载、网卡流量。

━━━━━━━━━━━━━━━━━━
📡 <b>服务器监控</b>
━━━━━━━━━━━━━━━━━━

<code>添加服务器</code>
获取添加服务器模板。

<code>查看服务器</code>
查看所有服务器、备注、价格、到期时间、在线/离线。

<code>检测服务器</code>
立即检测所有服务器在线/离线。

<code>删除服务器 1</code>
删除 ID 为 1 的服务器。

━━━━━━━━━━━━━━━━━━
🛡️ <b>安全运维</b>
━━━━━━━━━━━━━━━━━━

<code>登录记录</code>
查看最近 SSH 登录记录。

<code>安全状态</code>
查看 SSH、Fail2ban、防火墙、系统更新状态。

<code>防爆破状态</code>
查看 Fail2ban 防爆破状态。

<code>重启节点</code>
重启 Xray / x-ui / 3x-ui，需要二次确认。

<code>清理缓存</code>
清理系统缓存，需要二次确认。

━━━━━━━━━━━━━━━━━━
🔔 <b>自动推送通知</b>
━━━━━━━━━━━━━━━━━━

🚨 服务器离线警报
✅ 服务器恢复在线
⏰ 到期提醒：30 / 14 / 7 / 3 / 1 / 当天
🔥 CPU 高负载警报
🧠 内存高占用警报
💽 磁盘空间警报

━━━━━━━━━━━━━━━━━━
✅ <b>英文命令兼容</b>
━━━━━━━━━━━━━━━━━━

/dashboard /status /disk /traffic
/servers /check_servers /add_server
/events /security /login_log /fail2ban
/enable_commands
""".strip())


def status_block():
    s = get_local_status()

    return (
        "🖥️ <b>本机状态</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🌐 主机名称：<code>{h(s['hostname'])}</code>\n"
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


def servers_summary_block():
    conn = db()
    rows = conn.execute("SELECT * FROM servers ORDER BY id ASC").fetchall()
    conn.close()

    if not rows:
        return (
            "📡 <b>服务器在线情况</b>\n"
            "━━━━━━━━━━━━━━\n"
            "📭 暂无服务器记录。\n"
            "发送 <code>添加服务器</code> 开始添加。"
        )

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

        lines.append(f"{status}｜{h(r['name'])}｜{h(r['host'])}:{h(r['check_port'])}")

    return (
        "📡 <b>服务器在线情况</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🟢 在线：{online_count} 台\n"
        f"🔴 离线：{offline_count} 台\n"
        f"📦 总数：{len(rows)} 台\n\n"
        + "\n".join(lines[:12])
    )


def events_block(limit=6):
    rows = get_recent_events(limit)

    if not rows:
        return (
            "🧾 <b>服务器事件</b>\n"
            "━━━━━━━━━━━━━━\n"
            "暂无事件记录。"
        )

    lines = [
        "🧾 <b>服务器事件</b>",
        "━━━━━━━━━━━━━━"
    ]

    type_icon = {
        "offline": "🚨",
        "online": "✅",
        "expiry": "⏰",
        "system": "🔥",
        "action": "🛠️",
        "security": "🛡️"
    }

    for r in rows:
        icon = type_icon.get(r["event_type"], "📌")
        lines.append(
            f"{icon} <b>{h(r['title'])}</b>\n"
            f"🕒 {h(r['created_at'])}"
        )

    return "\n".join(lines)


def cmd_dashboard(chat_id):
    text = (
        "📊✨ <b>服务器总览面板</b> ✨📊\n"
        f"🕒 更新时间：{now_text()}\n\n"
        f"{status_block()}\n\n"
        f"{traffic_block()}\n\n"
        f"{servers_summary_block()}\n\n"
        f"{events_block(6)}"
    )
    send_long(chat_id, text[:3900])


def cmd_status(chat_id):
    send(chat_id, (
        "✅✨ <b>当前机器状态</b> ✨✅\n"
        f"🕒 更新时间：{now_text()}\n\n"
        f"{status_block()}"
    ))


def cmd_disk(chat_id):
    lines = [
        "💾✨ <b>磁盘使用情况</b> ✨💾",
        f"🕒 更新时间：{now_text()}",
        ""
    ]

    try:
        partitions = psutil.disk_partitions(all=False)

        if not partitions:
            send(chat_id, "💾 当前没有检测到可用磁盘分区。")
            return

        for p in partitions:
            try:
                usage = psutil.disk_usage(p.mountpoint)
            except Exception:
                continue

            percent = usage.percent

            if percent >= DISK_ALERT:
                status = "🚨 空间严重不足"
            elif percent >= 80:
                status = "⚠️ 空间偏高"
            else:
                status = "✅ 空间正常"

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
        if root.percent >= DISK_ALERT:
            lines.append(f"🚨💥 <b>总体结论：</b>根目录使用率已超过 {DISK_ALERT}%，请尽快清理。")
        else:
            lines.append("✅🌿 <b>总体结论：</b>磁盘空间正常。")

        send_long(chat_id, "\n".join(lines)[:3900])

    except Exception as e:
        send(chat_id, f"❌ 获取磁盘信息失败：{h(e)}")


def cmd_traffic(chat_id):
    send(chat_id, (
        "🌐✨ <b>服务器流量使用情况</b> ✨🌐\n"
        f"🕒 更新时间：{now_text()}\n\n"
        f"{traffic_block()}"
    ))


def cmd_login_log(chat_id):
    raw = shell("last -w -n 10 | grep -v 'wtmp begins' || true", 10)

    if not raw.strip():
        send(chat_id, "🔐✨ <b>最近登录记录</b> ✨🔐\n\n暂无登录记录。")
        return

    lines = [
        "🔐✨ <b>最近 SSH 登录记录</b> ✨🔐",
        f"🕒 更新时间：{now_text()}",
        ""
    ]

    for line in raw.splitlines()[:10]:
        parts = line.split()
        if len(parts) < 3:
            continue

        user = parts[0]
        terminal = parts[1] if len(parts) > 1 else "未知"
        ip = parts[2] if len(parts) > 2 else "未知"
        time_info = " ".join(parts[3:8]) if len(parts) >= 8 else " ".join(parts[3:])

        lines.append(
            "━━━━━━━━━━━━━━\n"
            f"👤 <b>登录用户：</b>{h(user)}\n"
            f"💻 <b>登录终端：</b>{h(terminal)}\n"
            f"🌐 <b>来源地址：</b>{h(ip)}\n"
            f"⏰ <b>登录时间：</b>{h(time_info or '未知')}"
        )

    send_long(chat_id, "\n".join(lines)[:3900])


def cmd_fail2ban(chat_id):
    raw = shell(
        root_cmd("fail2ban-client status sshd 2>/dev/null || fail2ban-client status 2>/dev/null || echo FAIL2BAN_NOT_RUNNING"),
        10
    )

    if "FAIL2BAN_NOT_RUNNING" in raw:
        send(chat_id, (
            "🚫✨ <b>防爆破状态</b> ✨🚫\n\n"
            "⚠️ <b>当前状态：</b>Fail2ban 未运行或未配置 SSH 防护。\n\n"
            "📌 <b>建议：</b>\n"
            "建议安装并启用 Fail2ban，用来减少 SSH 暴力破解风险。"
        ))
        return

    currently_failed = "未知"
    total_failed = "未知"
    currently_banned = "未知"
    total_banned = "未知"

    for line in raw.splitlines():
        line = line.strip()
        if "Currently failed:" in line:
            currently_failed = line.split(":", 1)[1].strip()
        elif "Total failed:" in line:
            total_failed = line.split(":", 1)[1].strip()
        elif "Currently banned:" in line:
            currently_banned = line.split(":", 1)[1].strip()
        elif "Total banned:" in line:
            total_banned = line.split(":", 1)[1].strip()

    send(chat_id, (
        "🚫✨ <b>防爆破状态</b> ✨🚫\n"
        f"🕒 更新时间：{now_text()}\n\n"
        "🛡️ <b>服务状态：</b>✅ 已运行\n"
        f"⚠️ <b>当前失败登录次数：</b>{h(currently_failed)}\n"
        f"📊 <b>累计失败登录次数：</b>{h(total_failed)}\n"
        f"🔒 <b>当前封禁 IP 数量：</b>{h(currently_banned)}\n"
        f"📌 <b>累计封禁 IP 数量：</b>{h(total_banned)}\n\n"
        "✅ <b>说明：</b>如果当前封禁 IP 数量大于 0，说明服务器正在拦截异常登录。"
    ))


def cmd_security(chat_id):
    ssh = shell("systemctl is-active ssh 2>/dev/null || systemctl is-active sshd 2>/dev/null || echo unknown", 5)
    f2b = shell("systemctl is-active fail2ban 2>/dev/null || echo unknown", 5)
    updates = shell("apt list --upgradable 2>/dev/null | sed 1d | wc -l", 10)

    ufw_raw = shell("ufw status 2>/dev/null || echo 未安装或未启用", 5)
    if "Status: active" in ufw_raw:
        firewall = "✅ 已开启"
    elif "Status: inactive" in ufw_raw:
        firewall = "⚠️ 未开启"
    else:
        firewall = "❓ 未检测到或未安装"

    send(chat_id, (
        "🛡️✨ <b>综合安全状态</b> ✨🛡️\n"
        f"🕒 更新时间：{now_text()}\n\n"
        f"🔐 <b>SSH 服务：</b>{service_cn(ssh)}\n"
        f"🚫 <b>防爆破服务：</b>{service_cn(f2b)}\n"
        f"🔥 <b>防火墙状态：</b>{firewall}\n"
        f"📦 <b>可更新软件包：</b>{h(updates)} 个\n\n"
        "📌 <b>状态说明：</b>\n"
        "✅ 运行中：服务正常\n"
        "⚠️ 未运行：建议检查配置\n"
        "🚨 运行失败：需要立即处理"
    ))


def cmd_restart_xray(chat_id):
    send(chat_id, (
        "⚠️🔄 <b>重启节点确认</b>\n\n"
        "即将重启以下可能存在的服务：\n"
        "🚀 Xray\n"
        "🧩 x-ui\n"
        "🧩 3x-ui\n\n"
        "确认重启请发送：\n"
        "<code>确认重启</code>"
    ))


def cmd_restart_xray_confirm(chat_id):
    out = shell(
        root_cmd("systemctl restart xray 2>&1; systemctl restart x-ui 2>&1 || systemctl restart 3x-ui 2>&1 || true"),
        20
    )

    event_add("action", "执行重启节点", "已执行 Xray / x-ui / 3x-ui 重启命令")

    send(chat_id, (
        "✅🔄 <b>已执行重启命令</b>\n\n"
        "📌 <b>结果：</b>已提交重启操作。\n"
        "可以稍后发送 <code>查看状态</code> 或 <code>检测服务器</code> 查看状态。"
    ))


def cmd_clean_cache(chat_id):
    send(chat_id, (
        "⚠️🧹 <b>清理缓存确认</b>\n\n"
        "即将执行系统缓存清理。\n\n"
        "确认清理请发送：\n"
        "<code>确认清理</code>"
    ))


def cmd_clean_cache_confirm(chat_id):
    shell(root_cmd("sync"), 10)
    shell(root_cmd("sh -c 'echo 3 > /proc/sys/vm/drop_caches'"), 10)
    shell(root_cmd("apt clean"), 20)

    event_add("action", "清理系统缓存", "已执行系统缓存清理")

    send(chat_id, (
        "✅🧹 <b>缓存已清理</b>\n\n"
        "📌 <b>结果：</b>系统缓存清理完成。\n"
        "可以发送 <code>查看状态</code> 查看当前内存情况。"
    ))


def cmd_add_server(chat_id, text):
    raw = text.strip()

    for prefix in ["/add_server", "添加服务器", "新增服务器", "添加机器", "新增机器"]:
        if raw.startswith(prefix):
            raw = raw.replace(prefix, "", 1).strip()
            break

    if not raw:
        send(chat_id, """
🧾✨ <b>添加服务器</b> ✨🧾

直接复制下面模板到 TG 发送：

<code>添加服务器
名称: HK-Oracle
主机: 1.2.3.4
备注: 香港甲骨文 免费机器
周期: 年付
价格: 0
币种: USD
到期: 2026-08-01
检测端口: 22</code>

━━━━━━━━━━━━━━
📌 <b>字段说明</b>
━━━━━━━━━━━━━━

🖥️ 名称：自己方便识别的名字
🌐 主机：服务器 IP 或域名
📝 备注：可随便写，支持空格
🔁 周期：月付 / 季付 / 年付
💰 币种：CNY / USD / EUR / GBP
📆 到期：YYYY-MM-DD
🔌 检测端口：一般写 22，也可以写 80 / 443 / 你的 SSH 端口
""".strip())
        return

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

    name = data.get("名称") or data.get("name")
    host = data.get("主机") or data.get("host") or data.get("IP") or data.get("ip")
    note = data.get("备注") or data.get("note") or ""
    cycle = data.get("周期") or data.get("cycle")
    price = data.get("价格") or data.get("price")
    currency = data.get("币种") or data.get("currency")
    expire_at = data.get("到期") or data.get("到期日") or data.get("expire") or data.get("expire_at")
    check_port = data.get("检测端口") or data.get("端口") or data.get("port") or "22"

    missing = []
    for label, value in [
        ("名称", name),
        ("主机", host),
        ("周期", cycle),
        ("价格", price),
        ("币种", currency),
        ("到期", expire_at),
    ]:
        if not value:
            missing.append(label)

    if missing:
        send(chat_id, "❌ 缺少字段：" + "、".join(missing) + "\n\n发送 <code>添加服务器</code> 查看模板。")
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
        parse_date(expire_at)
        price = float(price)
        check_port = int(check_port)
    except Exception:
        send(chat_id, "❌ 价格、日期或检测端口格式错误。\n\n日期示例：<code>2026-08-01</code>")
        return

    online = check_tcp(host, check_port)
    status = "online" if online else "offline"

    conn = db()
    conn.execute(
        "INSERT INTO servers(name, host, note, cycle, price, currency, expire_at, check_port) VALUES(?,?,?,?,?,?,?,?)",
        (name, host, note, cycle, price, currency, expire_at, check_port)
    )
    conn.commit()
    sid = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    conn.execute(
        "INSERT OR REPLACE INTO server_status(server_id, last_status, last_checked_at, last_changed_at) VALUES(?,?,?,?)",
        (sid, status, now_text(), now_text())
    )

    conn.commit()
    conn.close()

    status_text = "🟢 在线" if online else "🔴 离线"

    event_add("action", "添加服务器", f"添加服务器：{name}，当前状态：{status_text}")

    send(chat_id, (
        "✅🎉 <b>服务器添加成功</b> 🎉✅\n\n"
        "━━━━━━━━━━━━━━\n"
        f"🆔 <b>ID：</b><code>{sid}</code>\n"
        f"🖥️ <b>名称：</b>{h(name)}\n"
        f"🌐 <b>主机：</b><code>{h(host)}</code>\n"
        f"🔌 <b>检测端口：</b>{h(check_port)}\n"
        f"📡 <b>当前状态：</b>{status_text}\n"
        f"📝 <b>备注：</b>{h(note or '无')}\n"
        f"🔁 <b>周期：</b>{cycle_name(cycle)}\n"
        f"💰 <b>价格：</b>{currency_name(currency)} {price:g} {currency}\n"
        f"📆 <b>到期：</b>{h(expire_at)}\n"
        "━━━━━━━━━━━━━━\n"
        "⏰ 到期提醒已开启\n"
        "📡 在线 / 离线检测已开启"
    ))


def cmd_list_servers(chat_id):
    conn = db()
    rows = conn.execute("SELECT * FROM servers ORDER BY expire_at ASC").fetchall()
    conn.close()

    if not rows:
        send(chat_id, "📭 暂无服务器记录。\n\n发送 <code>添加服务器</code> 添加。")
        return

    now_date = datetime.now().date()
    lines = [
        "📋✨ <b>服务器列表</b> ✨📋",
        f"🕒 更新时间：{now_text()}",
        ""
    ]

    for r in rows:
        online = check_tcp(r["host"], r["check_port"])
        status_text = "🟢 在线" if online else "🔴 离线"

        exp = parse_date(r["expire_at"]).date()
        days = (exp - now_date).days

        if days < 0:
            expire_status = f"🚨 已过期 {abs(days)} 天"
        elif days == 0:
            expire_status = "🚨 今天到期"
        elif days <= 7:
            expire_status = f"⚠️ 剩余 {days} 天"
        elif days <= 30:
            expire_status = f"⏰ 剩余 {days} 天"
        else:
            expire_status = f"✅ 剩余 {days} 天"

        lines.append(
            "━━━━━━━━━━━━━━\n"
            f"📡 <b>状态：</b>{status_text}\n"
            f"🆔 <b>ID：</b><code>{r['id']}</code>\n"
            f"🖥️ <b>名称：</b>{h(r['name'])}\n"
            f"🌐 <b>主机：</b><code>{h(r['host'])}</code>\n"
            f"🔌 <b>端口：</b>{h(r['check_port'])}\n"
            f"📝 <b>备注：</b>{h(r['note'] or '无')}\n"
            f"🔁 <b>周期：</b>{cycle_name(r['cycle'])}\n"
            f"💰 <b>价格：</b>{currency_name(r['currency'])} {r['price']:g} {r['currency']}\n"
            f"📆 <b>到期：</b>{h(r['expire_at'])}\n"
            f"⏳ <b>到期状态：</b>{expire_status}\n"
        )

    send_long(chat_id, "\n".join(lines)[:3900])


def cmd_check_servers(chat_id):
    conn = db()
    rows = conn.execute("SELECT * FROM servers ORDER BY id ASC").fetchall()
    conn.close()

    if not rows:
        send(chat_id, "📭 暂无服务器记录。")
        return

    online_count = 0
    offline_count = 0

    lines = [
        "📡✨ <b>服务器在线状态检测</b> ✨📡",
        f"🕒 检测时间：{now_text()}",
        ""
    ]

    for r in rows:
        online = check_tcp(r["host"], r["check_port"])
        if online:
            status_text = "🟢 在线"
            online_count += 1
        else:
            status_text = "🔴 离线"
            offline_count += 1

        lines.append(
            "━━━━━━━━━━━━━━\n"
            f"📡 <b>状态：</b>{status_text}\n"
            f"🖥️ <b>名称：</b>{h(r['name'])}\n"
            f"🌐 <b>地址：</b><code>{h(r['host'])}:{h(r['check_port'])}</code>\n"
            f"📝 <b>备注：</b>{h(r['note'] or '无')}"
        )

    lines.insert(2, f"🟢 在线：{online_count} 台\n🔴 离线：{offline_count} 台\n📦 总数：{len(rows)} 台\n")

    send_long(chat_id, "\n".join(lines)[:3900])


def cmd_del_server(chat_id, text):
    text = text.strip()
    sid = ""

    if text.startswith("/del_server"):
        sid = text.replace("/del_server", "", 1).strip()
    elif text.startswith("删除服务器"):
        sid = text.replace("删除服务器", "", 1).strip()
    elif text.startswith("删除机器"):
        sid = text.replace("删除机器", "", 1).strip()

    if not sid:
        send(chat_id, "❌ 格式：<code>删除服务器 1</code>")
        return

    conn = db()
    row = conn.execute("SELECT * FROM servers WHERE id=?", (sid,)).fetchone()

    if not row:
        conn.close()
        send(chat_id, "❌ 没有找到这个服务器 ID。")
        return

    conn.execute("DELETE FROM servers WHERE id=?", (sid,))
    conn.execute("DELETE FROM reminders WHERE server_id=?", (sid,))
    conn.execute("DELETE FROM server_status WHERE server_id=?", (sid,))
    conn.commit()
    conn.close()

    event_add("action", "删除服务器", f"已删除服务器：{row['name']}")

    send(chat_id, (
        "✅🗑️ <b>服务器已删除</b>\n\n"
        f"🆔 <b>ID：</b><code>{h(sid)}</code>\n"
        f"🖥️ <b>名称：</b>{h(row['name'])}"
    ))


def cmd_events(chat_id):
    rows = get_recent_events(15)

    if not rows:
        send(chat_id, "🧾 暂无服务器事件记录。")
        return

    type_icon = {
        "offline": "🚨",
        "online": "✅",
        "expiry": "⏰",
        "system": "🔥",
        "action": "🛠️",
        "security": "🛡️"
    }

    lines = [
        "🧾✨ <b>服务器事件记录</b> ✨🧾",
        f"🕒 更新时间：{now_text()}",
        ""
    ]

    for r in rows:
        icon = type_icon.get(r["event_type"], "📌")
        lines.append(
            "━━━━━━━━━━━━━━\n"
            f"{icon} <b>{h(r['title'])}</b>\n"
            f"🕒 <b>时间：</b>{h(r['created_at'])}\n"
            f"📝 <b>内容：</b>{h(r['content'])}"
        )

    send_long(chat_id, "\n".join(lines)[:3900])


def offline_push_text(r):
    return (
        "🚨🔴 <b>服务器离线警报</b> 🔴🚨\n\n"
        "━━━━━━━━━━━━━━\n"
        f"🖥️ <b>名称：</b>{h(r['name'])}\n"
        f"🌐 <b>主机：</b><code>{h(r['host'])}:{h(r['check_port'])}</code>\n"
        f"📝 <b>备注：</b>{h(r['note'] or '无')}\n"
        f"⏰ <b>时间：</b>{now_text()}\n"
        "━━━━━━━━━━━━━━\n"
        "📌 <b>当前状态：</b>🔴 离线\n"
        "⚠️ <b>可能原因：</b>服务器关机、网络异常、端口未开放、防火墙阻断。\n"
        "🛠️ <b>建议处理：</b>检查服务器电源、SSH 端口、防火墙、安全组。"
    )


def online_push_text(r):
    return (
        "✅🟢 <b>服务器恢复在线</b> 🟢✅\n\n"
        "━━━━━━━━━━━━━━\n"
        f"🖥️ <b>名称：</b>{h(r['name'])}\n"
        f"🌐 <b>主机：</b><code>{h(r['host'])}:{h(r['check_port'])}</code>\n"
        f"📝 <b>备注：</b>{h(r['note'] or '无')}\n"
        f"⏰ <b>时间：</b>{now_text()}\n"
        "━━━━━━━━━━━━━━\n"
        "📌 <b>当前状态：</b>🟢 在线\n"
        "🌿 <b>说明：</b>服务器检测端口已恢复连接。"
    )


def monitor_server_online_status():
    conn = db()
    rows = conn.execute("SELECT * FROM servers").fetchall()

    for r in rows:
        sid = r["id"]
        online = check_tcp(r["host"], r["check_port"])
        new_status = "online" if online else "offline"

        old = conn.execute("SELECT * FROM server_status WHERE server_id=?", (sid,)).fetchone()

        if not old:
            conn.execute(
                "INSERT INTO server_status(server_id, last_status, last_checked_at, last_changed_at) VALUES(?,?,?,?)",
                (sid, new_status, now_text(), now_text())
            )
            conn.commit()
            continue

        old_status = old["last_status"]

        conn.execute(
            "UPDATE server_status SET last_status=?, last_checked_at=? WHERE server_id=?",
            (new_status, now_text(), sid)
        )
        conn.commit()

        if old_status != new_status:
            conn.execute(
                "UPDATE server_status SET last_changed_at=? WHERE server_id=?",
                (now_text(), sid)
            )
            conn.commit()

            if new_status == "offline":
                content = offline_push_text(r)
                push_event("offline", f"服务器离线：{r['name']}", content)
            else:
                content = online_push_text(r)
                push_event("online", f"服务器恢复在线：{r['name']}", content)

    conn.close()


def expiry_push_text(r, days):
    if days < 0:
        title = "🚨💥 <b>服务器已过期</b> 💥🚨"
        left = f"🔴 已过期 <b>{abs(days)}</b> 天"
        level = "🆘 请立即续费，或确认是否已经停用。"
    elif days == 0:
        title = "🚨⏳ <b>服务器今天到期</b> ⏳🚨"
        left = "🟠 <b>今天到期</b>"
        level = "⚡ 建议马上处理，避免服务中断。"
    elif days <= 3:
        title = "⚠️🔥 <b>服务器即将到期</b> 🔥⚠️"
        left = f"🟡 剩余 <b>{days}</b> 天"
        level = "🔔 请尽快安排续费。"
    elif days <= 7:
        title = "⏰🌙 <b>服务器到期提醒</b> 🌙⏰"
        left = f"🟡 剩余 <b>{days}</b> 天"
        level = "📌 建议提前处理。"
    else:
        title = "📅✨ <b>服务器续费提醒</b> ✨📅"
        left = f"🟢 剩余 <b>{days}</b> 天"
        level = "✅ 当前仍有充足时间。"

    return (
        f"{title}\n\n"
        "━━━━━━━━━━━━━━\n"
        f"🖥️ <b>名称：</b>{h(r['name'])}\n"
        f"🌐 <b>主机：</b><code>{h(r['host'])}</code>\n"
        f"🔌 <b>检测端口：</b>{h(r['check_port'])}\n"
        f"📝 <b>备注：</b>{h(r['note'] or '无')}\n"
        f"🔁 <b>周期：</b>{cycle_name(r['cycle'])}\n"
        f"💰 <b>价格：</b>{currency_name(r['currency'])} {r['price']:g} {r['currency']}\n"
        f"📆 <b>到期：</b>{h(r['expire_at'])}\n"
        f"⏳ <b>状态：</b>{left}\n"
        "━━━━━━━━━━━━━━\n"
        f"{level}"
    )


def monitor_expiry():
    conn = db()
    rows = conn.execute("SELECT * FROM servers").fetchall()
    today = datetime.now().date()

    for r in rows:
        exp = parse_date(r["expire_at"]).date()
        days = (exp - today).days

        if days in DUE_REMIND_DAYS or days < 0:
            key = f"{r['id']}:{days}"
            old = conn.execute(
                "SELECT 1 FROM reminders WHERE server_id=? AND remind_key=?",
                (r["id"], key)
            ).fetchone()

            if old:
                continue

            content = expiry_push_text(r, days)
            push_event("expiry", f"到期提醒：{r['name']}", content)

            conn.execute(
                "INSERT OR REPLACE INTO reminders(server_id, remind_key, sent_at) VALUES(?,?,?)",
                (r["id"], key, now_text())
            )
            conn.commit()

    conn.close()


def alert_once(key, title, text, cooldown_minutes=60):
    conn = db()
    row = conn.execute("SELECT sent_at FROM alerts WHERE alert_key=?", (key,)).fetchone()
    now = datetime.now()

    if row:
        last = parse_date(row["sent_at"])
        if now - last < timedelta(minutes=cooldown_minutes):
            conn.close()
            return

    conn.execute(
        "INSERT OR REPLACE INTO alerts(alert_key, sent_at) VALUES(?,?)",
        (key, now.isoformat())
    )
    conn.commit()
    conn.close()

    push_event("system", title, text)


def monitor_local_system():
    try:
        s = get_local_status()

        if s["cpu"] >= CPU_ALERT:
            alert_once(
                "local_cpu_high",
                "CPU 高负载",
                "🚨🔥 <b>CPU 高负载活动警报</b> 🔥🚨\n\n"
                "━━━━━━━━━━━━━━\n"
                f"🖥️ <b>主机：</b><code>{h(s['hostname'])}</code>\n"
                f"📊 <b>CPU 使用率：</b>{s['cpu']:.0f}%\n"
                f"⚙️ <b>系统负载：</b>{s['load1']:.2f}\n"
                f"⏰ <b>时间：</b>{now_text()}\n"
                "━━━━━━━━━━━━━━\n"
                "⚠️ <b>状态：</b>CPU 使用率过高。\n"
                "🛠️ <b>建议：</b>发送 <code>查看状态</code>，或 SSH 执行 <code>top</code> / <code>htop</code> 查看。",
                30
            )

        if s["mem_percent"] >= MEM_ALERT:
            alert_once(
                "local_mem_high",
                "内存高占用",
                "🚨🧠 <b>内存高占用活动警报</b> 🧠🚨\n\n"
                "━━━━━━━━━━━━━━\n"
                f"🖥️ <b>主机：</b><code>{h(s['hostname'])}</code>\n"
                f"📈 <b>内存使用率：</b>{s['mem_percent']:.0f}%\n"
                f"💾 <b>已用内存：</b>{fmt_size(s['mem_used'])} / {fmt_size(s['mem_total'])}\n"
                f"⏰ <b>时间：</b>{now_text()}\n"
                "━━━━━━━━━━━━━━\n"
                "⚠️ <b>状态：</b>内存占用过高。\n"
                "🛠️ <b>建议：</b>发送 <code>查看状态</code>，或 SSH 执行 <code>free -h</code> 检查。",
                30
            )

        if s["disk_percent"] >= DISK_ALERT:
            alert_once(
                "local_disk_high",
                "磁盘空间不足",
                "🚨💽 <b>磁盘空间活动警报</b> 💽🚨\n\n"
                "━━━━━━━━━━━━━━\n"
                f"🖥️ <b>主机：</b><code>{h(s['hostname'])}</code>\n"
                f"📦 <b>磁盘使用率：</b>{s['disk_percent']:.0f}%\n"
                f"💾 <b>已用空间：</b>{fmt_size(s['disk_used'])} / {fmt_size(s['disk_total'])}\n"
                f"⏰ <b>时间：</b>{now_text()}\n"
                "━━━━━━━━━━━━━━\n"
                "⚠️ <b>状态：</b>磁盘空间不足。\n"
                "🧹 <b>建议：</b>发送 <code>查看磁盘</code>，或清理日志、缓存、大文件。",
                60
            )

    except Exception:
        pass


def handle(chat_id, text):
    if not is_admin(chat_id):
        send(chat_id, "⛔ 未授权用户，拒绝访问。")
        return

    text = text.strip()

    if text in ["/start", "/help", "帮助", "菜单", "功能", "命令"]:
        cmd_help(chat_id)

    elif text in ["/enable_commands", "启用命令", "启用菜单", "开启菜单", "显示命令"]:
        cmd_enable_commands(chat_id)

    elif text in ["/dashboard", "服务器总览", "总览", "面板", "控制台", "监控面板"]:
        cmd_dashboard(chat_id)

    elif text in ["/status", "查看状态", "服务器状态", "本机状态", "状态"]:
        cmd_status(chat_id)

    elif text in ["/disk", "查看磁盘", "磁盘", "磁盘状态", "磁盘使用"]:
        cmd_disk(chat_id)

    elif text in ["/traffic", "查看流量", "流量", "网络流量", "服务器流量", "流量使用"]:
        cmd_traffic(chat_id)

    elif text in ["/login_log", "登录记录", "查看登录", "SSH记录", "ssh记录"]:
        cmd_login_log(chat_id)

    elif text in ["/fail2ban", "/fail2ban_status", "防爆破状态", "防爆破", "封禁状态"]:
        cmd_fail2ban(chat_id)

    elif text in ["/security", "/security_status", "安全状态", "查看安全", "综合安全"]:
        cmd_security(chat_id)

    elif text in ["/restart_xray", "重启节点", "重启服务", "重启xray", "重启Xray"]:
        cmd_restart_xray(chat_id)

    elif text in ["/restart_xray_confirm", "确认重启", "确认重启节点", "确认重启服务"]:
        cmd_restart_xray_confirm(chat_id)

    elif text in ["/clean_cache", "清理缓存", "清理系统缓存"]:
        cmd_clean_cache(chat_id)

    elif text in ["/clean_cache_confirm", "确认清理", "确认清理缓存"]:
        cmd_clean_cache_confirm(chat_id)

    elif text in ["/add_server", "添加服务器", "新增服务器", "添加机器", "新增机器"]:
        cmd_add_server(chat_id, text)

    elif text.startswith("/add_server") or text.startswith("添加服务器") or text.startswith("新增服务器") or text.startswith("添加机器") or text.startswith("新增机器"):
        cmd_add_server(chat_id, text)

    elif text in ["/list_servers", "/servers", "查看服务器", "服务器列表", "查看机器", "机器列表"]:
        cmd_list_servers(chat_id)

    elif text in ["/check_servers", "检测服务器", "检测在线", "检测机器", "在线检测"]:
        cmd_check_servers(chat_id)

    elif text in ["/events", "查看事件", "服务器事件", "事件记录", "事件"]:
        cmd_events(chat_id)

    elif text.startswith("/del_server") or text.startswith("删除服务器") or text.startswith("删除机器"):
        cmd_del_server(chat_id, text)

    else:
        send(chat_id, (
            "❓ <b>没有识别这个操作</b>\n\n"
            "你可以发送：\n"
            "📌 <code>帮助</code>\n"
            "📌 <code>启用命令</code>\n"
            "📌 <code>服务器总览</code>\n"
            "📌 <code>查看状态</code>\n"
            "📌 <code>查看磁盘</code>\n"
            "📌 <code>查看流量</code>\n"
            "📌 <code>查看服务器</code>\n"
            "📌 <code>添加服务器</code>"
        ))


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
                monitor_expiry()
                last_check = now_ts

            r = requests.get(
                f"{API}/getUpdates",
                params={"timeout": 25, "offset": offset},
                timeout=35
            ).json()

            for item in r.get("result", []):
                offset = item["update_id"] + 1
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
            time.sleep(5)


if __name__ == "__main__":
    init_db()
    poll()
