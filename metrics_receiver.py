#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import json
import time
import sqlite3
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = os.getenv("APP_DIR", "/opt/server-monitor-bot")
DB_PATH = os.getenv("DATABASE_PATH", f"{APP_DIR}/servers.db")
METRICS_PORT = int(os.getenv("METRICS_PORT", "8765"))

def load_env(path):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

load_env(f"{APP_DIR}/.env")
load_env(f"{APP_DIR}/.env.web")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
METRICS_SECRET = os.getenv("METRICS_SECRET") or (BOT_TOKEN[-16:] if BOT_TOKEN else "server-monitor-secret")

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def db():
    os.makedirs(APP_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn

def ensure_col(conn, table, col, definition):
    cols = [x[1] for x in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")

def init_db():
    conn = db()
    try:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS server_metrics(
            server_id INTEGER PRIMARY KEY,
            name TEXT DEFAULT '',
            hostname TEXT DEFAULT '',
            public_ip TEXT DEFAULT '',
            uptime_seconds INTEGER DEFAULT 0,
            boot_time TEXT DEFAULT '',
            cpu_percent REAL DEFAULT 0,
            mem_percent REAL DEFAULT 0,
            disk_percent REAL DEFAULT 0,
            swap_percent REAL DEFAULT 0,
            load1 REAL DEFAULT 0,
            load5 REAL DEFAULT 0,
            load15 REAL DEFAULT 0,
            rx_bytes INTEGER DEFAULT 0,
            tx_bytes INTEGER DEFAULT 0,
            cpu_cores INTEGER DEFAULT 0,
            mem_total INTEGER DEFAULT 0,
            mem_used INTEGER DEFAULT 0,
            swap_total INTEGER DEFAULT 0,
            swap_used INTEGER DEFAULT 0,
            disk_total INTEGER DEFAULT 0,
            disk_used INTEGER DEFAULT 0,
            updated_at TEXT DEFAULT '',
            raw TEXT DEFAULT ''
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS server_status(
            server_id INTEGER PRIMARY KEY,
            last_status TEXT DEFAULT 'unknown',
            last_checked_at TEXT,
            last_changed_at TEXT,
            fail_count INTEGER DEFAULT 0,
            success_count INTEGER DEFAULT 0,
            notified_offline INTEGER DEFAULT 0,
            first_fail_at TEXT DEFAULT '',
            first_recover_at TEXT DEFAULT '',
            online_since TEXT DEFAULT '',
            offline_since TEXT DEFAULT ''
        )
        """)
        for col, definition in [
            ("name", "TEXT DEFAULT ''"),
            ("hostname", "TEXT DEFAULT ''"),
            ("public_ip", "TEXT DEFAULT ''"),
            ("uptime_seconds", "INTEGER DEFAULT 0"),
            ("boot_time", "TEXT DEFAULT ''"),
            ("cpu_percent", "REAL DEFAULT 0"),
            ("mem_percent", "REAL DEFAULT 0"),
            ("disk_percent", "REAL DEFAULT 0"),
            ("swap_percent", "REAL DEFAULT 0"),
            ("load1", "REAL DEFAULT 0"),
            ("load5", "REAL DEFAULT 0"),
            ("load15", "REAL DEFAULT 0"),
            ("rx_bytes", "INTEGER DEFAULT 0"),
            ("tx_bytes", "INTEGER DEFAULT 0"),
            ("cpu_cores", "INTEGER DEFAULT 0"),
            ("mem_total", "INTEGER DEFAULT 0"),
            ("mem_used", "INTEGER DEFAULT 0"),
            ("swap_total", "INTEGER DEFAULT 0"),
            ("swap_used", "INTEGER DEFAULT 0"),
            ("disk_total", "INTEGER DEFAULT 0"),
            ("disk_used", "INTEGER DEFAULT 0"),
            ("updated_at", "TEXT DEFAULT ''"),
            ("raw", "TEXT DEFAULT ''"),
        ]:
            ensure_col(conn, "server_metrics", col, definition)
        conn.commit()
    finally:
        conn.close()

def to_int(v):
    try:
        return int(float(v or 0))
    except Exception:
        return 0

def to_float(v):
    try:
        return float(v or 0)
    except Exception:
        return 0.0

def save_metrics(payload):
    sid = to_int(payload.get("server_id"))
    if not sid:
        raise ValueError("missing server_id")

    updated_at = now()
    conn = db()
    try:
        init_db()
        conn.execute("""
        INSERT OR REPLACE INTO server_metrics(
            server_id,name,hostname,public_ip,uptime_seconds,boot_time,
            cpu_percent,mem_percent,disk_percent,swap_percent,
            load1,load5,load15,
            rx_bytes,tx_bytes,cpu_cores,
            mem_total,mem_used,swap_total,swap_used,disk_total,disk_used,
            updated_at,raw
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            sid,
            str(payload.get("name") or ""),
            str(payload.get("hostname") or ""),
            str(payload.get("public_ip") or ""),
            to_int(payload.get("uptime_seconds")),
            str(payload.get("boot_time") or ""),
            to_float(payload.get("cpu_percent")),
            to_float(payload.get("mem_percent")),
            to_float(payload.get("disk_percent")),
            to_float(payload.get("swap_percent")),
            to_float(payload.get("load1")),
            to_float(payload.get("load5")),
            to_float(payload.get("load15")),
            to_int(payload.get("rx_bytes")),
            to_int(payload.get("tx_bytes")),
            to_int(payload.get("cpu_cores")),
            to_int(payload.get("mem_total")),
            to_int(payload.get("mem_used")),
            to_int(payload.get("swap_total")),
            to_int(payload.get("swap_used")),
            to_int(payload.get("disk_total")),
            to_int(payload.get("disk_used")),
            updated_at,
            json.dumps(payload, ensure_ascii=False),
        ))
        old = conn.execute("SELECT last_status,last_changed_at,online_since FROM server_status WHERE server_id=?", (sid,)).fetchone()
        changed_at = old["last_changed_at"] if old and old["last_changed_at"] else updated_at
        online_since = old["online_since"] if old and old["online_since"] else updated_at
        if not old or old["last_status"] != "online":
            changed_at = updated_at
            online_since = updated_at
        conn.execute("""
        INSERT OR REPLACE INTO server_status(
            server_id,last_status,last_checked_at,last_changed_at,
            fail_count,success_count,notified_offline,online_since,offline_since
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """, (sid, "online", updated_at, changed_at, 0, 1, 0, online_since, ""))
        conn.commit()
        return updated_at
    finally:
        conn.close()

class Handler(BaseHTTPRequestHandler):
    server_version = "ServerMonitorMetrics/1.0"

    def log_message(self, fmt, *args):
        return

    def send_json(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.startswith("/health"):
            self.send_json(200, {"ok": True, "service": "server-monitor-metrics", "time": now()})
        else:
            self.send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/report"):
            self.send_json(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
            payload = json.loads(self.rfile.read(length).decode("utf-8", errors="ignore") or "{}")
            if str(payload.get("secret") or "") != str(METRICS_SECRET):
                self.send_json(403, {"ok": False, "error": "bad secret"})
                return
            updated_at = save_metrics(payload)
            self.send_json(200, {"ok": True, "server_id": to_int(payload.get("server_id")), "updated_at": updated_at})
        except Exception as e:
            self.send_json(500, {"ok": False, "error": str(e)})

def main():
    init_db()
    print(f"server-monitor-metrics listening on 0.0.0.0:{METRICS_PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", METRICS_PORT), Handler).serve_forever()

if __name__ == "__main__":
    main()
