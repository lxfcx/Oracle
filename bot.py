#!/usr/bin/env python3
import os
import time
import sqlite3
import socket
import subprocess
from datetime import datetime, timedelta

import psutil
import requests
from dateutil.parser import parse as parse_date

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = set(x.strip() for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip())

DB_PATH = "/opt/server-monitor-bot/servers.db"

api = f"https://api.telegram.org/bot{BOT_TOKEN}"

CHECK_INTERVAL = 60

DUE_REMIND_DAYS = [30, 14, 7, 3, 1, 0]

CPU_ALERT = 90
MEM_ALERT = 90
DISK_ALERT = 90


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

    conn.commit()
    conn.close()


def tg(method, payload=None):
    try:
        return requests.post(
            f"{api}/{method}",
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


def fmt_size(n):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
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
    currency = str(currency).upper().strip()
    return {
        "人民币": "CNY",
        "RMB": "CNY",
        "¥": "CNY",
        "美元": "USD",
        "$": "USD",
        "欧元": "EUR",
        "€": "EUR",
        "英镑": "GBP",
        "£": "GBP",
    }.get(currency, currency)


def cmd_help(chat_id):
    send(chat_id, """
🤖✨ <b>服务器监控管理 Bot</b> ✨🤖

📌 <b>服务器监控</b>

/add_server
🧾 在 TG 里添加服务器

/list_servers
📋 查看所有服务器，包含在线 / 离线状态

/check_servers
📡 立即检测所有服务器在线 / 离线

/del_server ID
🗑️ 删除服务器

📌 <b>当前部署机器状态</b>

/status
🖥️ 查看当前机器 CPU / 内存 / 磁盘 / 负载

/disk
💾 查看磁盘

/traffic
🌐 查看流量

/login_log
🔐 查看 SSH 登录记录

/security_status
🛡️ 查看安全状态

/fail2ban_status
🚫 查看防爆破状态

/restart_xray
🔄 重启 Xray / x-ui，需要二次确认

/clean_cache
🧹 清理缓存，需要二次确认

📌 <b>自动 TG 推送</b>

🚨 服务器离线警报
✅ 服务器恢复在线
⏰ 到期提醒：30 / 14 / 7 / 3 / 1 / 当天
🔥 CPU 高负载警报
🧠 内存高占用警报
💽 磁盘空间警报
""".strip())


def cmd_status(chat_id):
    s = get_local_status()
    send(chat_id, (
        "✅🖥️ <b>当前机器状态</b> 🖥️✅\n\n"
        f"🌐 <b>主机：</b><code>{s['hostname']}</code>\n"
        f"⏱️ <b>运行时间：</b>{s['uptime']}\n\n"
        f"📊 <b>CPU：</b>{s['cpu']:.0f}%\n"
        f"⚙️ <b>负载：</b>{s['load1']:.2f} / {s['load5']:.2f} / {s['load15']:.2f}\n"
        f"🧩 <b>核心：</b>{s['cpu_count']} 核\n\n"
        f"🧠 <b>内存：</b>{fmt_size(s['mem_used'])} / {fmt_size(s['mem_total'])} ({s['mem_percent']:.0f}%)\n"
        f"💾 <b>磁盘：</b>{fmt_size(s['disk_used'])} / {fmt_size(s['disk_total'])} ({s['disk_percent']:.0f}%)"
    ))


def cmd_disk(chat_id):
    out = shell("df -h --output=source,size,used,avail,pcent,target | sed 1d", 10)
    disk = psutil.disk_usage("/")
    text = "💾✨ <b>磁盘使用情况</b> ✨💾\n\n<pre>" + out[:3500] + "</pre>"

    if disk.percent >= DISK_ALERT:
        text += f"\n\n🚨💥 <b>磁盘警报：</b>根目录已超过 {DISK_ALERT}%"
    else:
        text += "\n\n✅🌿 <b>状态：</b>磁盘空间正常"

    send(chat_id, text)


def cmd_traffic(chat_id):
    net1 = psutil.net_io_counters()
    time.sleep(1)
    net2 = psutil.net_io_counters()

    send(chat_id, (
        "🌐✨ <b>网络流量</b> ✨🌐\n\n"
        f"⬇️ <b>总下载：</b>{fmt_size(net2.bytes_recv)}\n"
        f"⬆️ <b>总上传：</b>{fmt_size(net2.bytes_sent)}\n\n"
        f"🚀 <b>实时下载：</b>{fmt_size(net2.bytes_recv - net1.bytes_recv)}/s\n"
        f"📤 <b>实时上传：</b>{fmt_size(net2.bytes_sent - net1.bytes_sent)}/s"
    ))


def cmd_login_log(chat_id):
    out = shell("last -a | head -30", 10)
    send(chat_id, "🔐✨ <b>最近 SSH 登录记录</b> ✨🔐\n\n<pre>" + out[:3500] + "</pre>")


def cmd_fail2ban(chat_id):
    out = shell("sudo fail2ban-client status sshd 2>/dev/null || sudo fail2ban-client status 2>/dev/null || echo 'fail2ban 未运行或未配置 sshd jail'", 10)
    send(chat_id, "🚫✨ <b>Fail2ban 状态</b> ✨🚫\n\n<pre>" + out[:3500] + "</pre>")


def cmd_security(chat_id):
    ssh = shell("systemctl is-active ssh 2>/dev/null || systemctl is-active sshd 2>/dev/null", 5)
    f2b = shell("systemctl is-active fail2ban 2>/dev/null", 5)
    ufw = shell("ufw status 2>/dev/null | head -20 || true", 5)
    updates = shell("apt list --upgradable 2>/dev/null | sed 1d | wc -l", 10)

    send(chat_id, (
        "🛡️✨ <b>综合安全状态</b> ✨🛡️\n\n"
        f"🔐 <b>SSH：</b><code>{ssh}</code>\n"
        f"🚫 <b>fail2ban：</b><code>{f2b}</code>\n"
        f"📦 <b>可更新软件包：</b><code>{updates}</code>\n\n"
        f"🔥 <b>防火墙：</b>\n<pre>{ufw[:1500]}</pre>"
    ))


def cmd_restart_xray(chat_id):
    send(chat_id, "⚠️🔄 <b>重启确认</b>\n\n确认重启 Xray / x-ui / 3x-ui 请发送：\n<code>/restart_xray_confirm</code>")


def cmd_restart_xray_confirm(chat_id):
    out = shell("sudo systemctl restart xray 2>&1; sudo systemctl restart x-ui 2>&1 || sudo systemctl restart 3x-ui 2>&1 || true", 20)
    send(chat_id, "✅🔄 <b>已执行重启</b>\n\n<pre>" + out[:2000] + "</pre>")


def cmd_clean_cache(chat_id):
    send(chat_id, "⚠️🧹 <b>清理缓存确认</b>\n\n确认清理请发送：\n<code>/clean_cache_confirm</code>")


def cmd_clean_cache_confirm(chat_id):
    out = shell("sudo sync; echo 3 | sudo tee /proc/sys/vm/drop_caches >/dev/null; sudo apt clean; echo OK", 20)
    send(chat_id, "✅🧹 <b>缓存已清理</b>\n\n<pre>" + out[:2000] + "</pre>")


def cmd_add_server(chat_id, text):
    raw = text.replace("/add_server", "", 1).strip()

    if not raw:
        send(chat_id, """
🧾✨ <b>添加服务器</b> ✨🧾

直接复制下面模板到 TG 发送：

<code>/add_server
名称: HK-Oracle
主机: 1.2.3.4
备注: 香港甲骨文 免费机器
周期: 年付
价格: 0
币种: USD
到期: 2026-08-01
检测端口: 22</code>

📌 <b>周期支持：</b>
📆 月付 / monthly
🗓️ 季付 / quarterly
📅 年付 / yearly

💰 <b>币种支持：</b>
🇨🇳 CNY
🇺🇸 USD
🇪🇺 EUR
🇬🇧 GBP

🌐 <b>检测端口：</b>
默认 22，也可以写 80 / 443 / 你的 SSH 端口
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
        send(chat_id, "❌ 缺少字段：" + "、".join(missing) + "\n\n发送 <code>/add_server</code> 查看模板。")
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
        (sid, status, datetime.now().isoformat(), datetime.now().isoformat())
    )

    conn.commit()
    conn.close()

    status_text = "🟢 在线" if online else "🔴 离线"

    send(chat_id, (
        "✅🎉 <b>服务器添加成功</b> 🎉✅\n\n"
        f"🆔 <b>ID：</b><code>{sid}</code>\n"
        f"🖥️ <b>名称：</b>{name}\n"
        f"🌐 <b>主机：</b><code>{host}</code>\n"
        f"🔌 <b>检测端口：</b>{check_port}\n"
        f"📡 <b>当前状态：</b>{status_text}\n"
        f"📝 <b>备注：</b>{note or '无'}\n"
        f"🔁 <b>周期：</b>{cycle_name(cycle)}\n"
        f"💰 <b>价格：</b>{currency_name(currency)} {price:g} {currency}\n"
        f"📆 <b>到期：</b>{expire_at}\n\n"
        "⏰ 到期提醒已开启\n"
        "📡 在线 / 离线检测已开启"
    ))


def cmd_list_servers(chat_id):
    conn = db()
    rows = conn.execute("SELECT * FROM servers ORDER BY expire_at ASC").fetchall()
    conn.close()

    if not rows:
        send(chat_id, "📭 暂无服务器记录。\n\n发送 <code>/add_server</code> 添加。")
        return

    now = datetime.now().date()
    lines = ["📋✨ <b>服务器列表</b> ✨📋\n"]

    for r in rows:
        online = check_tcp(r["host"], r["check_port"])
        status_text = "🟢 在线" if online else "🔴 离线"

        exp = parse_date(r["expire_at"]).date()
        days = (exp - now).days

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
            f"{status_text}\n"
            f"🆔 ID：<code>{r['id']}</code>\n"
            f"🖥️ 名称：{r['name']}\n"
            f"🌐 主机：<code>{r['host']}</code>\n"
            f"🔌 端口：{r['check_port']}\n"
            f"📝 备注：{r['note'] or '无'}\n"
            f"🔁 周期：{cycle_name(r['cycle'])}\n"
            f"💰 价格：{currency_name(r['currency'])} {r['price']:g} {r['currency']}\n"
            f"📆 到期：{r['expire_at']}\n"
            f"⏳ 状态：{expire_status}\n"
        )

    send(chat_id, "\n".join(lines)[:3900])


def cmd_check_servers(chat_id):
    conn = db()
    rows = conn.execute("SELECT * FROM servers ORDER BY id ASC").fetchall()
    conn.close()

    if not rows:
        send(chat_id, "📭 暂无服务器记录。")
        return

    lines = ["📡✨ <b>服务器在线状态检测</b> ✨📡\n"]

    for r in rows:
        online = check_tcp(r["host"], r["check_port"])
        status_text = "🟢 在线" if online else "🔴 离线"

        lines.append(
            f"{status_text} <b>{r['name']}</b>\n"
            f"🌐 <code>{r['host']}:{r['check_port']}</code>\n"
            f"📝 {r['note'] or '无'}\n"
        )

    send(chat_id, "\n".join(lines)[:3900])


def cmd_del_server(chat_id, text):
    parts = text.split()
    if len(parts) != 2:
        send(chat_id, "❌ 格式：<code>/del_server ID</code>")
        return

    sid = parts[1]
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

    send(chat_id, f"✅🗑️ 已删除服务器：{row['name']}\n🆔 ID：<code>{sid}</code>")


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
                (sid, new_status, datetime.now().isoformat(), datetime.now().isoformat())
            )
            conn.commit()
            continue

        old_status = old["last_status"]

        conn.execute(
            "UPDATE server_status SET last_status=?, last_checked_at=? WHERE server_id=?",
            (new_status, datetime.now().isoformat(), sid)
        )
        conn.commit()

        if old_status != new_status:
            conn.execute(
                "UPDATE server_status SET last_changed_at=? WHERE server_id=?",
                (datetime.now().isoformat(), sid)
            )
            conn.commit()

            if new_status == "offline":
                broadcast(
                    "🚨🔴 <b>服务器离线警报</b> 🔴🚨\n\n"
                    f"🖥️ <b>名称：</b>{r['name']}\n"
                    f"🌐 <b>主机：</b><code>{r['host']}:{r['check_port']}</code>\n"
                    f"📝 <b>备注：</b>{r['note'] or '无'}\n"
                    f"⏰ <b>时间：</b>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                    "⚠️ <b>状态：</b>检测端口无法连接，服务器可能离线或端口被防火墙阻断。"
                )
            else:
                broadcast(
                    "✅🟢 <b>服务器恢复在线</b> 🟢✅\n\n"
                    f"🖥️ <b>名称：</b>{r['name']}\n"
                    f"🌐 <b>主机：</b><code>{r['host']}:{r['check_port']}</code>\n"
                    f"📝 <b>备注：</b>{r['note'] or '无'}\n"
                    f"⏰ <b>时间：</b>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                    "🌿 <b>状态：</b>服务器已恢复连接。"
                )

    conn.close()


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

            if days < 0:
                title = "🚨💥 <b>服务器已过期</b> 💥🚨"
                left = f"🔴 已过期 <b>{abs(days)}</b> 天"
                level = "🆘 请立即续费或确认是否停用"
            elif days == 0:
                title = "🚨⏳ <b>服务器今天到期</b> ⏳🚨"
                left = "🟠 <b>今天到期</b>"
                level = "⚡ 建议马上处理，避免服务中断"
            elif days <= 3:
                title = "⚠️🔥 <b>服务器即将到期</b> 🔥⚠️"
                left = f"🟡 剩余 <b>{days}</b> 天"
                level = "🔔 请尽快安排续费"
            elif days <= 7:
                title = "⏰🌙 <b>服务器到期提醒</b> 🌙⏰"
                left = f"🟡 剩余 <b>{days}</b> 天"
                level = "📌 建议提前处理"
            else:
                title = "📅✨ <b>服务器续费提醒</b> ✨📅"
                left = f"🟢 剩余 <b>{days}</b> 天"
                level = "✅ 当前仍有充足时间"

            text = (
                f"{title}\n\n"
                f"🖥️ <b>名称：</b>{r['name']}\n"
                f"🌐 <b>主机：</b><code>{r['host']}</code>\n"
                f"🔌 <b>检测端口：</b>{r['check_port']}\n"
                f"📝 <b>备注：</b>{r['note'] or '无'}\n"
                f"🔁 <b>周期：</b>{cycle_name(r['cycle'])}\n"
                f"💰 <b>价格：</b>{currency_name(r['currency'])} {r['price']:g} {r['currency']}\n"
                f"📆 <b>到期：</b>{r['expire_at']}\n"
                f"⏳ <b>状态：</b>{left}\n\n"
                f"{level}"
            )

            broadcast(text)

            conn.execute(
                "INSERT OR REPLACE INTO reminders(server_id, remind_key, sent_at) VALUES(?,?,?)",
                (r["id"], key, datetime.now().isoformat())
            )
            conn.commit()

    conn.close()


def alert_once(key, text, cooldown_minutes=60):
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

    broadcast(text)


def monitor_local_system():
    try:
        s = get_local_status()

        if s["cpu"] >= CPU_ALERT:
            alert_once(
                "local_cpu_high",
                "🚨🔥 <b>CPU 高负载活动警报</b> 🔥🚨\n\n"
                f"🖥️ <b>主机：</b><code>{s['hostname']}</code>\n"
                f"📊 <b>CPU：</b>{s['cpu']:.0f}%\n"
                f"⚙️ <b>负载：</b>{s['load1']:.2f}\n\n"
                "🛠️ 建议执行 <code>top</code> 或 <code>htop</code> 查看。",
                30
            )

        if s["mem_percent"] >= MEM_ALERT:
            alert_once(
                "local_mem_high",
                "🚨🧠 <b>内存高占用活动警报</b> 🧠🚨\n\n"
                f"🖥️ <b>主机：</b><code>{s['hostname']}</code>\n"
                f"📈 <b>内存：</b>{s['mem_percent']:.0f}%\n"
                f"💾 <b>已用：</b>{fmt_size(s['mem_used'])} / {fmt_size(s['mem_total'])}\n\n"
                "🛠️ 建议执行 <code>free -h</code> 检查。",
                30
            )

        if s["disk_percent"] >= DISK_ALERT:
            alert_once(
                "local_disk_high",
                "🚨💽 <b>磁盘空间活动警报</b> 💽🚨\n\n"
                f"🖥️ <b>主机：</b><code>{s['hostname']}</code>\n"
                f"📦 <b>磁盘：</b>{s['disk_percent']:.0f}%\n"
                f"💾 <b>已用：</b>{fmt_size(s['disk_used'])} / {fmt_size(s['disk_total'])}\n\n"
                "🧹 建议执行 <code>du -sh /* 2>/dev/null</code> 查找大文件。",
                60
            )

    except Exception:
        pass


def handle(chat_id, text):
    if not is_admin(chat_id):
        send(chat_id, "⛔ 未授权用户，拒绝访问。")
        return

    if text in ["/start", "/help"]:
        cmd_help(chat_id)
    elif text == "/status":
        cmd_status(chat_id)
    elif text == "/disk":
        cmd_disk(chat_id)
    elif text == "/traffic":
        cmd_traffic(chat_id)
    elif text == "/login_log":
        cmd_login_log(chat_id)
    elif text == "/fail2ban_status":
        cmd_fail2ban(chat_id)
    elif text == "/security_status":
        cmd_security(chat_id)
    elif text == "/restart_xray":
        cmd_restart_xray(chat_id)
    elif text == "/restart_xray_confirm":
        cmd_restart_xray_confirm(chat_id)
    elif text == "/clean_cache":
        cmd_clean_cache(chat_id)
    elif text == "/clean_cache_confirm":
        cmd_clean_cache_confirm(chat_id)
    elif text.startswith("/add_server"):
        cmd_add_server(chat_id, text)
    elif text == "/list_servers":
        cmd_list_servers(chat_id)
    elif text == "/check_servers":
        cmd_check_servers(chat_id)
    elif text.startswith("/del_server"):
        cmd_del_server(chat_id, text)
    else:
        send(chat_id, "❓ 未知命令，发送 /help 查看菜单。")


def poll():
    offset = 0
    last_check = 0

    while True:
        try:
            now = time.time()

            if now - last_check >= CHECK_INTERVAL:
                monitor_local_system()
                monitor_server_online_status()
                monitor_expiry()
                last_check = now

            r = requests.get(
                f"{api}/getUpdates",
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
