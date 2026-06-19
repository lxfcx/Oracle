#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, time, socket, sqlite3, ipaddress, secrets, html
from datetime import datetime
from functools import wraps

import requests
from flask import Flask, request, redirect, url_for, session, render_template_string, jsonify, flash
from werkzeug.security import check_password_hash
try:
    from dateutil.parser import parse as parse_date
except Exception:
    parse_date = None
try:
    import psutil
except Exception:
    psutil = None

APP_DIR = os.getenv('APP_DIR','/opt/server-monitor-bot')
DB_PATH = os.getenv('DATABASE_PATH', f'{APP_DIR}/servers.db')

def load_env(path):
    if not os.path.exists(path): return
    with open(path,'r',encoding='utf-8',errors='ignore') as f:
        for line in f:
            line=line.strip()
            if not line or line.startswith('#') or '=' not in line: continue
            k,v=line.split('=',1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

load_env(f'{APP_DIR}/.env')
load_env(f'{APP_DIR}/.env.web')
WEB_USERNAME=os.getenv('WEB_USERNAME','admin')
WEB_PASSWORD=os.getenv('WEB_PASSWORD','')
WEB_PASSWORD_HASH=os.getenv('WEB_PASSWORD_HASH','')
WEB_SECRET_KEY=os.getenv('WEB_SECRET_KEY',secrets.token_hex(32))
WEB_HOST=os.getenv('WEB_HOST','0.0.0.0')
WEB_PORT=int(os.getenv('WEB_PORT','8899'))
BOT_TOKEN=os.getenv('BOT_TOKEN','')
METRICS_PORT=int(os.getenv('METRICS_PORT','8765'))
METRICS_SECRET=os.getenv('METRICS_SECRET') or (BOT_TOKEN[-16:] if BOT_TOKEN else 'server-monitor-secret')
app=Flask(__name__); app.secret_key=WEB_SECRET_KEY

def now(): return datetime.now().strftime('%Y-%m-%d %H:%M:%S')
def db():
    os.makedirs(APP_DIR,exist_ok=True); c=sqlite3.connect(DB_PATH); c.row_factory=sqlite3.Row; return c
def rd(r): return dict(r) if r else None
def ensure_col(c,t,col,d):
    cols=[x[1] for x in c.execute(f'PRAGMA table_info({t})').fetchall()]
    if col not in cols: c.execute(f'ALTER TABLE {t} ADD COLUMN {col} {d}')
def init_db():
    c=db()
    c.execute("""CREATE TABLE IF NOT EXISTS servers(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,host TEXT NOT NULL,note TEXT DEFAULT '',cycle TEXT DEFAULT 'monthly',price REAL DEFAULT 0,currency TEXT DEFAULT 'USD',expire_at TEXT NOT NULL,check_port INTEGER DEFAULT 22,created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    for col,d in [('country',"TEXT DEFAULT ''"),('country_code',"TEXT DEFAULT ''"),('region',"TEXT DEFAULT ''"),('city',"TEXT DEFAULT ''"),('isp',"TEXT DEFAULT ''"),('os_name',"TEXT DEFAULT ''"),('last_meta_at',"TEXT DEFAULT ''"),('free_forever','INTEGER DEFAULT 0'),('auto_renew','INTEGER DEFAULT 0'),('cpu_alert','REAL DEFAULT 90'),('mem_alert','REAL DEFAULT 90'),('disk_alert','REAL DEFAULT 90')]: ensure_col(c,'servers',col,d)
    c.execute("""CREATE TABLE IF NOT EXISTS server_status(server_id INTEGER PRIMARY KEY,last_status TEXT DEFAULT 'unknown',last_checked_at TEXT,last_changed_at TEXT,fail_count INTEGER DEFAULT 0,success_count INTEGER DEFAULT 0,notified_offline INTEGER DEFAULT 0,first_fail_at TEXT DEFAULT '',first_recover_at TEXT DEFAULT '',online_since TEXT DEFAULT '',offline_since TEXT DEFAULT '')""")
    c.execute("""CREATE TABLE IF NOT EXISTS server_metrics(server_id INTEGER PRIMARY KEY,name TEXT DEFAULT '',hostname TEXT DEFAULT '',public_ip TEXT DEFAULT '',uptime_seconds INTEGER DEFAULT 0,boot_time TEXT DEFAULT '',cpu_percent REAL DEFAULT 0,mem_percent REAL DEFAULT 0,disk_percent REAL DEFAULT 0,rx_bytes INTEGER DEFAULT 0,tx_bytes INTEGER DEFAULT 0,cpu_cores INTEGER DEFAULT 0,mem_total INTEGER DEFAULT 0,disk_total INTEGER DEFAULT 0,disk_used INTEGER DEFAULT 0,mem_used INTEGER DEFAULT 0,updated_at TEXT DEFAULT '',raw TEXT DEFAULT '')""")
    c.execute("""CREATE TABLE IF NOT EXISTS metric_alert_state(server_id INTEGER,metric TEXT,active INTEGER DEFAULT 0,last_value REAL DEFAULT 0,threshold REAL DEFAULT 0,last_sent_at TEXT DEFAULT '',PRIMARY KEY(server_id,metric))""")
    c.execute("""CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,event_type TEXT DEFAULT '',title TEXT DEFAULT '',content TEXT DEFAULT '',created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS reminders(server_id INTEGER,remind_key TEXT,sent_at TEXT,PRIMARY KEY(server_id,remind_key))""")
    c.execute("""CREATE TABLE IF NOT EXISTS local_profile(key TEXT PRIMARY KEY,value TEXT DEFAULT '')""")
    for k,v in {'name':socket.gethostname(),'note':'','cycle':'monthly','price':'0','currency':'USD','expire_at':''}.items(): c.execute('INSERT OR IGNORE INTO local_profile(key,value) VALUES(?,?)',(k,v))
    c.commit(); c.close()

def fmt(n):
    try: n=float(n or 0)
    except Exception: return '未知'
    for u in ['B','KB','MB','GB','TB','PB']:
        if n<1024: return f'{n:.1f}{u}'
        n/=1024
    return f'{n:.1f}PB'
def dur(s):
    try: s=int(max(0,float(s or 0)))
    except Exception: return '未知'
    d=s//86400; h=(s%86400)//3600; m=(s%3600)//60
    return f'{d}天 {h}小时 {m}分钟' if d else f'{h}小时 {m}分钟' if h else f'{m}分钟'
def pdt(v):
    if parse_date: return parse_date(str(v))
    for f in ['%Y-%m-%d %H:%M:%S','%Y-%m-%d']:
        try: return datetime.strptime(str(v),f)
        except Exception: pass
    raise ValueError('日期格式错误')
def truth(v): return str(v if v is not None else '').lower().strip() in ['1','true','yes','y','on','是','开启','启用','永久','永久免费','免费']
def flag(code):
    code=(code or '').upper().strip(); return chr(ord(code[0])+127397)+chr(ord(code[1])+127397) if len(code)==2 and code.isalpha() else '🌐'
def cycle(v): return {'monthly':'📆 月付','quarterly':'🗓️ 季付','yearly':'📅 年付'}.get(v or '',v or '月付')
def ncycle(v): return {'月付':'monthly','月':'monthly','monthly':'monthly','季付':'quarterly','季':'quarterly','quarterly':'quarterly','年付':'yearly','年':'yearly','yearly':'yearly'}.get(str(v or '').strip().lower(),str(v or 'monthly'))
def ncur(v):
    raw=str(v or 'USD').strip(); return {'人民币':'CNY','RMB':'CNY','¥':'CNY','美元':'USD','$':'USD','欧元':'EUR','€':'EUR','英镑':'GBP','£':'GBP'}.get(raw,raw.upper())
def cicon(c): return {'CNY':'🇨🇳 ¥','USD':'🇺🇸 $','EUR':'🇪🇺 €','GBP':'🇬🇧 £'}.get(c or '',c or '')
def exptext(exp,free=False):
    if truth(free) or str(exp or '').strip() in ['永久','永久免费']: return '🎁 永久免费'
    if not str(exp or '').strip(): return '未设置'
    try:
        d=(pdt(exp).date()-datetime.now().date()).days
        if d<0: return f'🚨 已过期 {abs(d)} 天'
        if d==0: return '🚨 今天到期'
        if d<=3: return f'🚨 剩余 {d} 天'
        if d<=7: return f'⚠️ 剩余 {d} 天'
        if d<=30: return f'⏰ 剩余 {d} 天'
        return f'✅ 剩余 {d} 天'
    except Exception: return '未知'
def pricet(s):
    if truth(s.get('free_forever')): return '🎁 永久免费'
    c=ncur(s.get('currency') or 'USD')
    try: p=f"{float(s.get('price') or 0):g}"
    except Exception: p=str(s.get('price') or 0)
    return f'{cicon(c)} {p} {c}'
def public_ip():
    for u in ['https://api.ipify.org','https://ifconfig.me/ip','https://icanhazip.com']:
        try:
            ip=requests.get(u,timeout=4).text.strip().splitlines()[0]; ipaddress.ip_address(ip); return ip
        except Exception: pass
    return ''
def check(host,port,timeout=2):
    try:
        with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as sk: sk.settimeout(timeout); sk.connect((host,int(port))); return True
    except Exception: return False
def resolve(host):
    try: return socket.gethostbyname(host)
    except Exception: return host
def detect(host):
    ip=resolve(host)
    try:
        obj=ipaddress.ip_address(ip)
        if obj.is_private or obj.is_loopback: return {'country':'本机/内网','country_code':'','region':'内网','city':'内网','isp':'内网地址'}
    except Exception: pass
    for u in [f'http://ip-api.com/json/{ip}?fields=status,country,countryCode,regionName,city,isp,org',f'https://ipapi.co/{ip}/json/']:
        try:
            j=requests.get(u,timeout=7,headers={'User-Agent':'server-monitor-web'}).json()
            if j.get('status')=='fail': continue
            return {'country':j.get('country') or j.get('country_name') or '未知','country_code':j.get('countryCode') or j.get('country_code') or '','region':j.get('regionName') or j.get('region') or '','city':j.get('city') or '','isp':j.get('isp') or j.get('org') or j.get('asn') or ''}
        except Exception: pass
    return {'country':'未知','country_code':'','region':'','city':'','isp':''}
def reset_seq():
    c=db()
    try:
        mid=c.execute('SELECT COALESCE(MAX(id),0) FROM servers').fetchone()[0]
        try: c.execute('UPDATE sqlite_sequence SET seq=? WHERE name="servers"',(mid,)); c.execute('INSERT OR IGNORE INTO sqlite_sequence(name,seq) VALUES("servers",?)',(mid,))
        except Exception: pass
        c.commit()
    finally: c.close()
def event(t,title,content):
    c=db()
    try: c.execute('INSERT INTO events(event_type,title,content,created_at) VALUES(?,?,?,?)',(t,title,content,now())); c.commit()
    finally: c.close()
def vhost(v):
    v=str(v or '').strip()
    if not v: raise ValueError('IP/主机不能为空')
    if v.startswith('http://') or v.startswith('https://') or '/' in v or ' ' in v or ':' in v: raise ValueError('只填写 IP/域名，端口单独填写')
    return v

def passok(p): return check_password_hash(WEB_PASSWORD_HASH,p) if WEB_PASSWORD_HASH else (bool(WEB_PASSWORD) and p==WEB_PASSWORD)
def login_required(fn):
    @wraps(fn)
    def w(*a,**kw):
        if not session.get('ok'): return redirect(url_for('login',next=request.path))
        return fn(*a,**kw)
    return w
@app.route('/login',methods=['GET','POST'])
def login():
    if request.method=='POST':
        if request.form.get('username','').strip()==WEB_USERNAME and passok(request.form.get('password','')):
            session['ok']=True; session['username']=WEB_USERNAME; return redirect(request.args.get('next') or url_for('dashboard'))
        flash('账号或密码错误','error')
    return render_template_string(LOGIN)
@app.route('/logout')
def logout(): session.clear(); return redirect(url_for('login'))

def local_status():
    if not psutil: return {}
    mem=psutil.virtual_memory(); disk=psutil.disk_usage('/')
    return {'hostname':socket.gethostname(),'public_ip':public_ip(),'uptime':dur(time.time()-psutil.boot_time()),'cpu':psutil.cpu_percent(interval=0.1),'cpu_count':psutil.cpu_count() or 0,'mem_used':mem.used,'mem_total':mem.total,'mem_percent':mem.percent,'disk_used':disk.used,'disk_total':disk.total,'disk_percent':disk.percent}
def lprof():
    c=db()
    try: d={r['key']:r['value'] for r in c.execute('SELECT key,value FROM local_profile').fetchall()}
    finally: c.close()
    for k,v in {'name':socket.gethostname(),'note':'','cycle':'monthly','price':'0','currency':'USD','expire_at':''}.items(): d.setdefault(k,v)
    return d
def get_server(sid):
    c=db()
    try: return rd(c.execute('SELECT * FROM servers WHERE id=?',(sid,)).fetchone())
    finally: c.close()
def get_status(sid):
    c=db()
    try: return rd(c.execute('SELECT * FROM server_status WHERE server_id=?',(sid,)).fetchone()) or {}
    finally: c.close()
def get_metrics(sid):
    c=db()
    try: return rd(c.execute('SELECT * FROM server_metrics WHERE server_id=?',(sid,)).fetchone()) or {}
    finally: c.close()
def fresh(m,max_age=180):
    try: return (datetime.now()-pdt(m.get('updated_at'))).total_seconds()<=max_age
    except Exception: return False
def states(sid):
    c=db()
    try: return {r['metric']:rd(r) for r in c.execute('SELECT * FROM metric_alert_state WHERE server_id=?',(sid,)).fetchall()}
    finally: c.close()
def all_servers():
    c=db()
    try: rows=[rd(r) for r in c.execute('SELECT * FROM servers ORDER BY id ASC').fetchall()]
    finally: c.close()
    out=[]
    for s in rows:
        st=get_status(s['id']); m=get_metrics(s['id'])
        s.update({'status':st,'metrics':m,'online':st.get('last_status')=='online','probe_fresh':fresh(m),'flag':flag(s.get('country_code')),'location':' '.join([x for x in [s.get('country'),s.get('region'),s.get('city')] if x]) or '未知','expire_label':exptext(s.get('expire_at'),s.get('free_forever')),'price_label':pricet(s)})
        out.append(s)
    return out
def summary():
    ss=all_servers(); total=len(ss); online=sum(1 for s in ss if s['online']); offline=sum(1 for s in ss if s['status'].get('last_status')=='offline'); probes=sum(1 for s in ss if s['probe_fresh']); expiring=expired=0
    for s in ss:
        if truth(s.get('free_forever')): continue
        e=str(s.get('expire_at') or '').strip()
        if not e: continue
        try:
            d=(pdt(e).date()-datetime.now().date()).days
            if d<0: expired+=1
            elif d<=7: expiring+=1
        except Exception: pass
    return {'servers':ss,'total':total,'online':online,'offline':offline,'unknown':total-online-offline,'probes':probes,'expiring':expiring,'expired':expired}
def events(limit=100):
    c=db()
    try: return [rd(r) for r in c.execute('SELECT * FROM events ORDER BY id DESC LIMIT ?',(limit,)).fetchall()]
    finally: c.close()
def agent_cmd(s):
    ip=public_ip() or '你的主控服务器公网IP'; name=str(s.get('name') or 'server').replace('"','').replace("'",'')
    return f'wget -qO- https://raw.githubusercontent.com/lxfcx/Oracle/main/agent.sh | bash -s -- --url "http://{ip}:{METRICS_PORT}/report" --secret "{METRICS_SECRET}" --sid "{s.get("id")}" --name "{name}"'

def render(active,body,**ctx): init_db(); return render_template_string(BASE,active=active,body=render_template_string(body,**ctx),now=now(),username=session.get('username',WEB_USERNAME))
@app.route('/')
@login_required
def dashboard(): return render('dashboard',DASH,data=summary(),local=local_status(),profile=lprof(),events=events(8))
@app.route('/servers')
@login_required
def servers_page(): return render('servers',SERVERS,data=summary())
@app.route('/servers/add',methods=['GET','POST'])
@login_required
def add_server():
    if request.method=='POST':
        try:
            name=request.form.get('name','').strip(); host=vhost(request.form.get('host',''))
            if not name: raise ValueError('名称不能为空')
            port=int(request.form.get('check_port') or 22); meta=detect(host); free=1 if request.form.get('free_forever')=='on' else 0; auto=1 if request.form.get('auto_renew')=='on' else 0
            expire='永久' if free else request.form.get('expire_at','').strip(); price=0 if free else float(request.form.get('price') or 0)
            reset_seq(); c=db()
            try:
                c.execute('''INSERT INTO servers(name,host,note,cycle,price,currency,expire_at,check_port,country,country_code,region,city,isp,os_name,last_meta_at,free_forever,auto_renew,cpu_alert,mem_alert,disk_alert) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(name,host,request.form.get('note','').strip(),ncycle(request.form.get('cycle','monthly')),price,ncur(request.form.get('currency','USD')),expire,port,meta['country'],meta['country_code'],meta['region'],meta['city'],meta['isp'],request.form.get('os_name','').strip(),now(),free,auto,float(request.form.get('cpu_alert') or 90),float(request.form.get('mem_alert') or 90),float(request.form.get('disk_alert') or 90)))
                c.commit()
            finally: c.close()
            event('action','网页登录添加服务器',f'{name} / {host}'); flash('添加成功','success'); return redirect(url_for('servers_page'))
        except Exception as e: flash(f'添加失败：{e}','error')
    return render('add',FORM,s={},action='添加服务器',is_add=True)
@app.route('/servers/<int:sid>')
@login_required
def server_detail(sid):
    s=get_server(sid)
    if not s: flash('服务器不存在','error'); return redirect(url_for('servers_page'))
    s.update({'status':get_status(sid),'metrics':get_metrics(sid),'states':states(sid),'flag':flag(s.get('country_code')),'location':' '.join([x for x in [s.get('country'),s.get('region'),s.get('city')] if x]) or '未知'}); s['agent_cmd']=agent_cmd(s)
    return render('servers',DETAIL,s=s)
@app.route('/servers/<int:sid>/edit',methods=['GET','POST'])
@login_required
def edit_server(sid):
    s=get_server(sid)
    if not s: flash('服务器不存在','error'); return redirect(url_for('servers_page'))
    if request.method=='POST':
        try:
            name=request.form.get('name','').strip(); host=vhost(request.form.get('host',''))
            if not name: raise ValueError('名称不能为空')
            port=int(request.form.get('check_port') or 22); free=1 if request.form.get('free_forever')=='on' else 0; auto=1 if request.form.get('auto_renew')=='on' else 0
            expire='永久' if free else request.form.get('expire_at','').strip(); price=0 if free else float(request.form.get('price') or 0)
            meta=detect(host) if host!=s.get('host') else {'country':s.get('country',''),'country_code':s.get('country_code',''),'region':s.get('region',''),'city':s.get('city',''),'isp':s.get('isp','')}
            c=db()
            try:
                c.execute('''UPDATE servers SET name=?,host=?,note=?,cycle=?,price=?,currency=?,expire_at=?,check_port=?,country=?,country_code=?,region=?,city=?,isp=?,os_name=?,last_meta_at=?,free_forever=?,auto_renew=?,cpu_alert=?,mem_alert=?,disk_alert=? WHERE id=?''',(name,host,request.form.get('note','').strip(),ncycle(request.form.get('cycle','monthly')),price,ncur(request.form.get('currency','USD')),expire,port,meta['country'],meta['country_code'],meta['region'],meta['city'],meta['isp'],request.form.get('os_name','').strip(),now(),free,auto,float(request.form.get('cpu_alert') or 90),float(request.form.get('mem_alert') or 90),float(request.form.get('disk_alert') or 90),sid))
                if host!=s.get('host'): c.execute('DELETE FROM server_metrics WHERE server_id=?',(sid,)); c.execute('DELETE FROM metric_alert_state WHERE server_id=?',(sid,))
                c.commit()
            finally: c.close()
            event('action','网页登录编辑服务器',f'ID {sid} / {name}'); flash('保存成功','success'); return redirect(url_for('server_detail',sid=sid))
        except Exception as e: flash(f'保存失败：{e}','error')
    return render('servers',FORM,s=s,action='编辑服务器',is_add=False)
@app.route('/servers/<int:sid>/delete',methods=['POST'])
@login_required
def delete_server(sid):
    s=get_server(sid)
    if not s: flash('服务器不存在','error'); return redirect(url_for('servers_page'))
    c=db()
    try:
        for t,col in [('servers','id'),('server_status','server_id'),('server_metrics','server_id'),('metric_alert_state','server_id'),('reminders','server_id')]: c.execute(f'DELETE FROM {t} WHERE {col}=?',(sid,))
        c.commit()
    finally: c.close()
    reset_seq(); event('action','网页登录删除服务器',f'ID {sid} / {s.get("name")}'); flash('已删除，编号序列已重置','success'); return redirect(url_for('servers_page'))
@app.route('/servers/<int:sid>/check',methods=['POST'])
@login_required
def check_server(sid):
    s=get_server(sid)
    if not s: flash('服务器不存在','error'); return redirect(url_for('servers_page'))
    ok=check(s['host'],s['check_port'],5); c=db()
    try: c.execute('INSERT OR REPLACE INTO server_status(server_id,last_status,last_checked_at,last_changed_at) VALUES(?,?,?,?)',(sid,'online' if ok else 'offline',now(),now())); c.commit()
    finally: c.close()
    flash('检测完成：'+('在线' if ok else '离线'),'success' if ok else 'error'); return redirect(url_for('server_detail',sid=sid))
@app.route('/servers/<int:sid>/refresh',methods=['POST'])
@login_required
def refresh(sid):
    s=get_server(sid); 
    if not s: flash('服务器不存在','error'); return redirect(url_for('servers_page'))
    meta=detect(s['host']); c=db()
    try: c.execute('UPDATE servers SET country=?,country_code=?,region=?,city=?,isp=?,last_meta_at=? WHERE id=?',(meta['country'],meta['country_code'],meta['region'],meta['city'],meta['isp'],now(),sid)); c.commit()
    finally: c.close()
    flash('地区已刷新','success'); return redirect(url_for('server_detail',sid=sid))
@app.route('/local',methods=['GET','POST'])
@login_required
def local_page():
    if request.method=='POST':
        c=db()
        try:
            for k in ['name','note','cycle','price','currency','expire_at']:
                v=request.form.get(k,'').strip(); v=ncycle(v) if k=='cycle' else ncur(v) if k=='currency' else v; c.execute('INSERT OR REPLACE INTO local_profile(key,value) VALUES(?,?)',(k,v))
            c.commit(); flash('本机资料已保存','success')
        finally: c.close()
        return redirect(url_for('local_page'))
    return render('local',LOCAL,local=local_status(),profile=lprof())
@app.route('/events')
@login_required
def events_page(): return render('events',EVENTS,events=events(200))
@app.route('/api/summary')
@login_required
def api_summary():
    d=summary(); return jsonify({k:d[k] for k in ['total','online','offline','unknown','probes','expiring','expired']}|{'time':now()})

LOGIN='''<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>登录</title><style>
:root{--bg:#07111f;--text:#edf5ff;--muted:#9fb0c7;--line:#ffffff24;--glass:#ffffff14;--a:#6ee7ff;--b:#a78bfa}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;overflow:hidden;background:#07111f}
body:before{content:"";position:fixed;inset:0;background:radial-gradient(1px 1px at 12% 20%,#fff,transparent),radial-gradient(1.5px 1.5px at 22% 76%,#dbeafe,transparent),radial-gradient(1px 1px at 36% 38%,#fff,transparent),radial-gradient(2px 2px at 50% 12%,#e0e7ff,transparent),radial-gradient(1px 1px at 70% 64%,#fff,transparent),radial-gradient(1.5px 1.5px at 84% 30%,#cffafe,transparent),radial-gradient(circle at 20% 20%,#1d4ed866,transparent 34%),radial-gradient(circle at 80% 0,#7c3aed66,transparent 36%),linear-gradient(180deg,#081426,#050913);animation:stars 18s linear infinite}
body:after{content:"";position:fixed;inset:-40%;background:radial-gradient(circle,#ffffff0c 0 1px,transparent 2px);background-size:80px 80px;animation:drift 50s linear infinite}
@keyframes stars{0%,100%{filter:brightness(1)}50%{filter:brightness(1.25)}}@keyframes drift{from{transform:translate3d(0,0,0)}to{transform:translate3d(80px,80px,0)}}
.card{position:relative;z-index:2;width:min(440px,92vw);padding:34px;border:1px solid var(--line);background:linear-gradient(180deg,#ffffff24,#ffffff10);border-radius:30px;box-shadow:0 30px 90px #0009;backdrop-filter:blur(22px);-webkit-backdrop-filter:blur(22px)}
h1{margin:0 0 8px;font-size:30px}p{color:var(--muted)}input,button{width:100%;padding:15px;border-radius:16px;margin:9px 0;border:1px solid var(--line);font-size:16px}input{background:#0005;color:#fff}button{background:linear-gradient(135deg,var(--a),var(--b));color:#07111f;font-weight:900;border:0}.flash{background:#fb718522;border:1px solid #fb718577;padding:12px;border-radius:14px}.tip{color:var(--muted);font-size:13px;line-height:1.7;margin-top:10px}
</style></head><body><form class=card method=post><h1>🛡️✨ Web 面板登录</h1><p>Server Monitor 星空控制台</p>{% with messages=get_flashed_messages(with_categories=true) %}{% for c,m in messages %}<div class=flash>{{m}}</div>{% endfor %}{% endwith %}<input name=username placeholder=账号 required><input name=password type=password placeholder=密码 required><button>🚀 登录控制台</button><div class=tip>登录后可查看大屏统计、服务器资源、探针、阈值告警、事件记录。</div></form></body></html>'''
BASE='''<!doctype html><html lang=zh-CN><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Server Monitor Web</title><style>
:root{--bg:#07111f;--card:#ffffff14;--card2:#ffffff22;--line:#ffffff24;--text:#edf5ff;--muted:#9fb0c7;--blue:#6ee7ff;--purple:#a78bfa;--green:#34d399;--red:#fb7185;--yellow:#fbbf24;--shadow:#0006}
body.light{--bg:#dbeafe;--card:#ffffffa6;--card2:#ffffffe6;--line:#0f172a22;--text:#0f172a;--muted:#475569;--shadow:#64748b3a}
*{box-sizing:border-box}body{margin:0;min-height:100vh;color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;background:var(--bg);transition:background .35s,color .35s;overflow-x:hidden}
body:before{content:"";position:fixed;inset:0;z-index:-2;background:radial-gradient(1px 1px at 8% 18%,#fff,transparent),radial-gradient(1.4px 1.4px at 16% 72%,#bfdbfe,transparent),radial-gradient(1px 1px at 31% 41%,#fff,transparent),radial-gradient(2px 2px at 45% 15%,#e0e7ff,transparent),radial-gradient(1px 1px at 61% 78%,#fff,transparent),radial-gradient(1.8px 1.8px at 77% 29%,#cffafe,transparent),radial-gradient(1px 1px at 92% 62%,#fff,transparent),radial-gradient(circle at 15% 8%,#1d4e8966,transparent 32%),radial-gradient(circle at 95% 0,#7c3aed55,transparent 35%),linear-gradient(180deg,#07111f,#030712 70%);animation:twinkle 18s ease-in-out infinite}
body:after{content:"";position:fixed;inset:-35%;z-index:-1;background:radial-gradient(circle,#ffffff10 0 1px,transparent 2px);background-size:90px 90px;animation:starDrift 70s linear infinite}
body.light:before{background:radial-gradient(circle at 20% 12%,#93c5fd80,transparent 30%),radial-gradient(circle at 90% 0,#c4b5fd80,transparent 34%),linear-gradient(180deg,#eff6ff,#dbeafe 62%,#f8fafc)}
body.light:after{background:radial-gradient(circle,#33415520 0 1px,transparent 2px);background-size:86px 86px}
@keyframes twinkle{0%,100%{filter:brightness(1)}50%{filter:brightness(1.25)}}@keyframes starDrift{from{transform:translate3d(0,0,0)}to{transform:translate3d(90px,90px,0)}}
a{color:inherit;text-decoration:none}.layout{display:grid;grid-template-columns:270px 1fr;min-height:100vh}.side{position:sticky;top:0;height:100vh;padding:22px;background:linear-gradient(180deg,#06101fd9,#06101fa8);border-right:1px solid var(--line);backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);box-shadow:20px 0 70px var(--shadow)}body.light .side{background:#ffffffe0}
.brand{display:flex;gap:12px;align-items:center;margin-bottom:20px}.brand .ico{width:50px;height:50px;border-radius:19px;display:grid;place-items:center;font-size:25px;background:linear-gradient(135deg,var(--blue),var(--purple));box-shadow:0 16px 40px #67e8f944}.brand b{display:block}.brand span,.muted,.small{color:var(--muted)}.themebtn{width:100%;margin:0 0 18px;padding:11px 12px;border-radius:16px;border:1px solid var(--line);background:var(--card);color:var(--text);font-weight:900;cursor:pointer;backdrop-filter:blur(16px)}
.nav a{display:flex;gap:10px;padding:12px;margin:8px 0;border:1px solid transparent;border-radius:16px}.nav a.active,.nav a:hover{background:var(--card2);border-color:var(--line);box-shadow:0 12px 30px var(--shadow)}
.main{padding:24px 28px 60px;max-width:1680px;width:100%;margin:auto}.top{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:20px}.top h1{margin:0;font-size:31px;letter-spacing:.2px}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:16px}.grid2{display:grid;grid-template-columns:repeat(2,1fr);gap:16px}.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
.card{border:1px solid var(--line);background:linear-gradient(180deg,var(--card2),var(--card));border-radius:26px;padding:20px;box-shadow:0 24px 70px var(--shadow);backdrop-filter:blur(22px);-webkit-backdrop-filter:blur(22px)}.kpi{position:relative;overflow:hidden}.kpi:after{content:"";position:absolute;right:-35px;top:-45px;width:145px;height:145px;border-radius:50%;background:#ffffff12}.kpi .label{color:var(--muted)}.kpi .value{font-size:36px;font-weight:950;margin-top:8px}.ok{color:var(--green)}.bad{color:var(--red)}.warn{color:var(--yellow)}
.badge{display:inline-flex;border:1px solid var(--line);border-radius:999px;padding:6px 10px;background:var(--card);font-size:13px}.table{width:100%;border-collapse:separate;border-spacing:0 10px}.table th{text-align:left;color:var(--muted);padding:0 12px}.table td{padding:14px 12px;background:var(--card);border-top:1px solid var(--line);border-bottom:1px solid var(--line);vertical-align:middle}.table td:first-child{border-left:1px solid var(--line);border-radius:16px 0 0 16px}.table td:last-child{border-right:1px solid var(--line);border-radius:0 16px 16px 0}
.progress{height:10px;background:#ffffff18;border-radius:99px;overflow:hidden;margin-top:7px}.bar{height:100%;background:linear-gradient(90deg,var(--green),var(--blue));border-radius:99px}.bar.warn{background:linear-gradient(90deg,var(--yellow),#fb923c)}.bar.bad{background:linear-gradient(90deg,#fb923c,var(--red))}
.btns{display:flex;gap:8px;flex-wrap:wrap}.btn,button{border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:13px;padding:9px 12px;font-weight:850;cursor:pointer}.btn.primary,button.primary{background:linear-gradient(135deg,var(--blue),var(--purple));color:#07111f;border:0}.btn.danger,button.danger{background:#fb718522;border-color:#fb718577;color:#fecdd3}
input,select,textarea{width:100%;padding:12px;border-radius:14px;border:1px solid var(--line);background:#00000030;color:var(--text)}body.light input,body.light select,body.light textarea{background:#ffffffb8}label{display:block;margin:13px 0 7px;color:var(--text);font-weight:850}.formgrid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px 16px}.flash{padding:12px 14px;border-radius:16px;margin-bottom:14px;border:1px solid var(--line);background:var(--card)}.flash.success{background:#34d39922;border-color:#34d39966}.flash.error{background:#fb718522;border-color:#fb718577}pre{white-space:pre-wrap;word-break:break-all;background:#00000038;border:1px solid var(--line);padding:16px;border-radius:18px}.small{font-size:13px;line-height:1.65}hr{border:0;border-top:1px solid var(--line);margin:18px 0}
.scrollbox{max-height:560px;overflow-y:auto;overflow-x:auto;padding-right:8px;border-radius:18px}.scrollbox.compact{max-height:360px}.scrollbox::-webkit-scrollbar{width:10px;height:10px}.scrollbox::-webkit-scrollbar-track{background:#ffffff12;border-radius:99px}.scrollbox::-webkit-scrollbar-thumb{background:linear-gradient(180deg,var(--blue),var(--purple));border-radius:99px}
@media(max-width:1100px){.layout{grid-template-columns:1fr}.side{height:auto;position:relative}.nav{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}.nav a{margin:0}.grid{grid-template-columns:repeat(2,1fr)}.grid2,.grid3,.formgrid{grid-template-columns:1fr}}@media(max-width:640px){.main{padding:16px}.grid{grid-template-columns:1fr}.top{display:block}}
</style><script>
function applyTheme(){let light=localStorage.getItem('theme')==='light';document.body.classList.toggle('light',light);let e=document.getElementById('themeText');if(e)e.textContent=light?'🌙 夜间星空':'☀️ 日间明亮'}
function toggleTheme(){let light=!document.body.classList.contains('light');localStorage.setItem('theme',light?'light':'dark');applyTheme()}
document.addEventListener('DOMContentLoaded',applyTheme);
async function r(){try{let j=await(await fetch('/api/summary')).json();for(let k of ['total','online','offline','probes','expiring','expired']){let e=document.querySelector('[data-kpi="'+k+'"]');if(e)e.textContent=j[k]}let t=document.querySelector('[data-now]');if(t)t.textContent=j.time}catch(e){}}setInterval(r,10000);function delok(){return confirm('确认删除服务器？')}
</script></head><body><div class=layout><aside class=side><div class=brand><div class=ico>🛡️</div><div><b>Server Monitor</b><span>Star Glass Dashboard</span></div></div><button class=themebtn onclick="toggleTheme()" type=button><span id=themeText>☀️ 日间明亮</span></button><nav class=nav><a class="{{'active' if active=='dashboard' else ''}}" href="/">📊 总览大屏</a><a class="{{'active' if active=='servers' else ''}}" href="/servers">🖥️ 服务器</a><a class="{{'active' if active=='add' else ''}}" href="/servers/add">➕ 添加服务器</a><a class="{{'active' if active=='local' else ''}}" href="/local">🏠 本机</a><a class="{{'active' if active=='events' else ''}}" href="/events">🧾 事件</a><a href="/logout">🚪 退出</a></nav><div class=small style="margin-top:22px">👤 {{username}}<br>🕒 <span data-now>{{now}}</span><br>✨ 星空磨砂玻璃 UI</div></aside><main class=main>{% with messages=get_flashed_messages(with_categories=true) %}{% for c,m in messages %}<div class="flash {{c}}">{{m}}</div>{% endfor %}{% endwith %}{{body|safe}}</main></div></body></html>'''
DASH='''<div class=top><h1>📊✨ 服务器总览大屏</h1><div class=btns><a class="btn primary" href="/servers/add">➕ 添加服务器</a></div></div><div class=grid><div class="card kpi"><div>📦 总数</div><div class=value data-kpi=total>{{data.total}}</div></div><div class="card kpi"><div>🟢 在线</div><div class="value ok" data-kpi=online>{{data.online}}</div></div><div class="card kpi"><div>🔴 离线</div><div class="value bad" data-kpi=offline>{{data.offline}}</div></div><div class="card kpi"><div>📡 探针在线</div><div class=value data-kpi=probes>{{data.probes}}</div></div></div><div class=grid2 style="margin-top:16px"><div class=card><h2>🏠 本机状态</h2><p><span class=badge>🌐 {{local.public_ip or '未知'}}</span> <span class=badge>🧩 {{local.cpu_count or 0}} 核</span> <span class=badge>⏱️ {{local.uptime or '未知'}}</span></p><hr>🔥 CPU {{'%.0f'|format(local.cpu or 0)}}%<div class=progress><div class="bar {{'bad' if (local.cpu or 0)>=90 else 'warn' if (local.cpu or 0)>=80 else ''}}" style="width:{{local.cpu or 0}}%"></div></div><br>🧠 内存 {{fmt(local.mem_used or 0)}} / {{fmt(local.mem_total or 0)}} ({{'%.0f'|format(local.mem_percent or 0)}}%)<div class=progress><div class=bar style="width:{{local.mem_percent or 0}}%"></div></div><br>💾 磁盘 {{fmt(local.disk_used or 0)}} / {{fmt(local.disk_total or 0)}} ({{'%.0f'|format(local.disk_percent or 0)}}%)<div class=progress><div class=bar style="width:{{local.disk_percent or 0}}%"></div></div></div><div class=card><h2>⏰ 到期和风险</h2><div class=grid3><div class=card><div>⚠️ 7天内到期</div><div class="value warn" data-kpi=expiring>{{data.expiring}}</div></div><div class=card><div>🚨 已过期</div><div class="value bad" data-kpi=expired>{{data.expired}}</div></div><div class=card><div>⚪ 未知</div><div class=value data-kpi=unknown>{{data.unknown}}</div></div></div><p class=small>支持探针真实 uptime、CPU/内存/硬盘/流量、阈值告警、续费到期、事件统计。</p></div></div><div class=card style="margin-top:16px"><h2>🖥️ 服务器面板</h2><table class=table><thead><tr><th>服务器</th><th>状态</th><th>配置</th><th>资源</th><th>续费</th><th>操作</th></tr></thead><tbody>{% for s in data.servers %}{% set m=s.metrics %}<tr><td><b>{{'🟢' if s.online else '🔴'}} {{s.flag}} {{s.name}}</b><br><span class=muted>ID{{s.id}}｜{{s.host}}:{{s.check_port}}｜{{s.location}}</span></td><td><span class=badge>{{'🟢 在线' if s.online else '🔴 离线'}}</span><br><span class=small>探针：{{'🟢 在线' if s.probe_fresh else '🟠 超时/未上报'}}</span></td><td>🧩 {{m.cpu_cores or '?'}}C<br>🧠 {{fmt(m.mem_total or 0) if m.mem_total else '未知'}}<br>💾 {{fmt(m.disk_total or 0) if m.disk_total else '未知'}}</td><td>🔥 {{'%.0f'|format(m.cpu_percent or 0)}}%<div class=progress><div class="bar {{'bad' if (m.cpu_percent or 0)>=(s.cpu_alert or 90) else ''}}" style="width:{{m.cpu_percent or 0}}%"></div></div>🧠 {{'%.0f'|format(m.mem_percent or 0)}}%<div class=progress><div class="bar {{'bad' if (m.mem_percent or 0)>=(s.mem_alert or 90) else ''}}" style="width:{{m.mem_percent or 0}}%"></div></div>💾 {{'%.0f'|format(m.disk_percent or 0)}}%<div class=progress><div class="bar {{'bad' if (m.disk_percent or 0)>=(s.disk_alert or 90) else ''}}" style="width:{{m.disk_percent or 0}}%"></div></div></td><td>{{s.expire_label}}<br>{{s.price_label}}</td><td><a class=btn href="/servers/{{s.id}}">详情</a></td></tr>{% else %}<tr><td colspan=6>📭 暂无服务器</td></tr>{% endfor %}</tbody></table></div><div class=card style="margin-top:16px"><h2>🧾 最新事件</h2><div class="scrollbox compact"><table class=table>{% for e in events %}<tr><td><b>{{e.title}}</b><br><span class=muted>{{e.created_at}}｜{{e.event_type}}</span></td><td>{{e.content}}</td></tr>{% else %}<tr><td>暂无事件</td></tr>{% endfor %}</table></div></div>'''
SERVERS='''<div class=top><h1>🖥️✨ 服务器管理</h1><div class=btns><a class="btn primary" href="/servers/add">➕ 添加服务器</a><a class=btn href="/">📊 总览</a></div></div><div class=card><table class=table><thead><tr><th>ID</th><th>服务器</th><th>探针/配置</th><th>阈值</th><th>续费</th><th>操作</th></tr></thead><tbody>{% for s in data.servers %}{% set m=s.metrics %}<tr><td><b>#{{s.id}}</b></td><td><b>{{'🟢' if s.online else '🔴'}} {{s.flag}} {{s.name}}</b><br><span class=muted>{{s.host}}:{{s.check_port}}｜{{s.location}}</span><br><span class=small>{{s.note or '无备注'}}</span></td><td>📡 {{'🟢 在线' if s.probe_fresh else '🟠 未上报/超时'}}<br>🧩 {{m.cpu_cores or '?'}}C｜🧠 {{fmt(m.mem_total or 0) if m.mem_total else '?'}}｜💾 {{fmt(m.disk_total or 0) if m.disk_total else '?'}}</td><td>🔥 {{'%.0f'|format(s.cpu_alert or 90)}}%<br>🧠 {{'%.0f'|format(s.mem_alert or 90)}}%<br>💾 {{'%.0f'|format(s.disk_alert or 90)}}%</td><td>{{s.expire_label}}<br>{{s.price_label}}</td><td><div class=btns><a class="btn primary" href="/servers/{{s.id}}">查看</a><a class=btn href="/servers/{{s.id}}/edit">编辑</a></div></td></tr>{% else %}<tr><td colspan=6>📭 暂无服务器</td></tr>{% endfor %}</tbody></table></div>'''
DETAIL='''<div class=top><h1>🖥️ {{s.name}}</h1><div class=btns><a class=btn href="/servers">📋 返回</a><a class="btn primary" href="/servers/{{s.id}}/edit">✏️ 编辑</a></div></div>{% set m=s.metrics %}<div class=grid3><div class="card kpi"><div>📡 状态</div><div class="value {{'ok' if s.status.last_status=='online' else 'bad' if s.status.last_status=='offline' else ''}}">{{'🟢 在线' if s.status.last_status=='online' else '🔴 离线' if s.status.last_status=='offline' else '⚪ 未知'}}</div><div class=small>{{s.status.last_checked_at or '未知'}}</div></div><div class="card kpi"><div>⏱️ 运行时长</div><div class=value>{{dur(m.uptime_seconds or 0)}}</div><div class=small>开机：{{m.boot_time or '未知'}}</div></div><div class="card kpi"><div>⏰ 到期</div><div class=value style="font-size:22px">{{exptext(s.expire_at,s.free_forever)}}</div><div class=small>{{pricet(s)}}｜{{cycle(s.cycle)}}</div></div></div><div class=grid2 style="margin-top:16px"><div class=card><h2>⚙️ 服务器配置</h2><p><span class=badge>🆔 ID {{s.id}}</span> <span class=badge>{{s.flag}} {{s.location}}</span></p><p>🌐 主机：<code>{{s.host}}:{{s.check_port}}</code></p><p>🏢 运营商：{{s.isp or '未知'}}</p><p>🧬 系统：{{s.os_name or '未知系统'}}</p><p>📝 备注：{{s.note or '无'}}</p><hr><div class=grid3><div class=card>🧩 CPU<br><b>{{m.cpu_cores or '?'}} Cores</b></div><div class=card>🧠 内存<br><b>{{fmt(m.mem_total or 0) if m.mem_total else '未知'}}</b></div><div class=card>💾 硬盘<br><b>{{fmt(m.disk_total or 0) if m.disk_total else '未知'}}</b></div></div></div><div class=card><h2>📊 资源使用</h2>🔥 CPU {{'%.0f'|format(m.cpu_percent or 0)}}% / {{'%.0f'|format(s.cpu_alert or 90)}}%<div class=progress><div class="bar {{'bad' if (m.cpu_percent or 0)>=(s.cpu_alert or 90) else ''}}" style="width:{{m.cpu_percent or 0}}%"></div></div><br>🧠 内存 {{fmt(m.mem_used or 0) if m.mem_used else '未知'}} / {{fmt(m.mem_total or 0) if m.mem_total else '未知'}} ({{'%.0f'|format(m.mem_percent or 0)}}%) / {{'%.0f'|format(s.mem_alert or 90)}}%<div class=progress><div class="bar {{'bad' if (m.mem_percent or 0)>=(s.mem_alert or 90) else ''}}" style="width:{{m.mem_percent or 0}}%"></div></div><br>💾 硬盘 {{fmt(m.disk_used or 0) if m.disk_used else '未知'}} / {{fmt(m.disk_total or 0) if m.disk_total else '未知'}} ({{'%.0f'|format(m.disk_percent or 0)}}%) / {{'%.0f'|format(s.disk_alert or 90)}}%<div class=progress><div class="bar {{'bad' if (m.disk_percent or 0)>=(s.disk_alert or 90) else ''}}" style="width:{{m.disk_percent or 0}}%"></div></div><hr>🌐 流量：⬇️ {{fmt(m.rx_bytes or 0)}} / ⬆️ {{fmt(m.tx_bytes or 0)}}<br>📡 探针：{{'🟢 在线' if fresh(m) else '🟠 超时/未上报'}}｜{{m.updated_at or '未知'}}</div></div><div class=grid2 style="margin-top:16px"><div class=card><h2>📡 一键部署探针</h2><p class=small>复制到这台服务器 SSH 执行，探针静默上报，离线/恢复由主机器人统一推送。</p><pre>{{s.agent_cmd}}</pre></div><div class=card><h2>🛠️ 操作</h2><div class=btns><form method=post action="/servers/{{s.id}}/check"><button class=primary>📡 立即检测</button></form><form method=post action="/servers/{{s.id}}/refresh"><button>🌍 刷新地区</button></form><a class=btn href="/servers/{{s.id}}/edit">✏️ 编辑资料/阈值</a><form method=post action="/servers/{{s.id}}/delete" onsubmit="return delok()"><button class=danger>🗑️ 删除</button></form></div><hr><h3>🎯 告警状态</h3>{% for key,label in [('cpu','🔥 CPU'),('mem','🧠 内存'),('disk','💾 硬盘')] %}{% set st=s.states.get(key) %}<p>{{label}}：{{'🚨 告警中' if st and st.active else '✅ 正常'}}{% if st %}｜上次 {{st.last_value|round(0)}}%｜{{st.last_sent_at}}{% endif %}</p>{% endfor %}</div></div>'''
FORM='''<div class=top><h1>{{'➕' if is_add else '✏️'}} {{action}}</h1><a class=btn href="/servers">📋 返回</a></div><form method=post class=card><div class=formgrid><div><label>🏷️ 名称</label><input name=name value="{{s.name or ''}}" required></div><div><label>🌐 IP/主机</label><input name=host value="{{s.host or ''}}" required placeholder="1.2.3.4 或 example.com"></div><div><label>🔌 端口</label><input name=check_port type=number min=1 max=65535 value="{{s.check_port or 22}}"></div><div><label>🧬 系统</label><input name=os_name value="{{s.os_name or ''}}" placeholder="Ubuntu 22.04"></div><div><label>🔁 周期</label><select name=cycle><option value=monthly {{'selected' if s.cycle=='monthly' else ''}}>月付</option><option value=quarterly {{'selected' if s.cycle=='quarterly' else ''}}>季付</option><option value=yearly {{'selected' if s.cycle=='yearly' else ''}}>年付</option></select></div><div><label>📆 到期</label><input name=expire_at value="{{s.expire_at or ''}}" placeholder="2027-05-01"></div><div><label>💰 价格</label><input name=price type=number step=.01 value="{{s.price if s.price is not none else 0}}"></div><div><label>💱 币种</label><select name=currency>{% for c in ['CNY','USD','EUR','GBP'] %}<option value={{c}} {{'selected' if (s.currency or 'USD')==c else ''}}>{{c}}</option>{% endfor %}</select></div><div><label>🔥 CPU 阈值 %</label><input name=cpu_alert type=number min=1 max=100 value="{{s.cpu_alert or 90}}"></div><div><label>🧠 内存阈值 %</label><input name=mem_alert type=number min=1 max=100 value="{{s.mem_alert or 90}}"></div><div><label>💾 硬盘阈值 %</label><input name=disk_alert type=number min=1 max=100 value="{{s.disk_alert or 90}}"></div><div><label>📝 备注</label><textarea name=note rows=4>{{s.note or ''}}</textarea></div></div><hr><label><input type=checkbox name=free_forever style="width:auto" {{'checked' if s.free_forever else ''}}> 🎁 永久免费</label><label><input type=checkbox name=auto_renew style="width:auto" {{'checked' if s.auto_renew else ''}}> 🔁 自动续费</label><div class=btns style="margin-top:18px"><button class=primary type=submit>💾 保存</button><a class=btn href="/servers">取消</a></div></form>'''
LOCAL='''<div class=top><h1>🏠 本机面板</h1><a class=btn href="/">📊 总览</a></div><div class=grid3><div class="card kpi"><div>🌐 公网IP</div><div class=value style="font-size:22px">{{local.public_ip or '未知'}}</div></div><div class="card kpi"><div>⏱️ 运行</div><div class=value style="font-size:22px">{{local.uptime or '未知'}}</div></div><div class="card kpi"><div>🧩 CPU</div><div class=value>{{local.cpu_count or 0}} 核</div></div></div><div class=grid2 style="margin-top:16px"><div class=card><h2>📊 资源</h2>🔥 CPU {{'%.0f'|format(local.cpu or 0)}}%<div class=progress><div class=bar style="width:{{local.cpu or 0}}%"></div></div><br>🧠 内存 {{fmt(local.mem_used or 0)}} / {{fmt(local.mem_total or 0)}} ({{'%.0f'|format(local.mem_percent or 0)}}%)<div class=progress><div class=bar style="width:{{local.mem_percent or 0}}%"></div></div><br>💾 磁盘 {{fmt(local.disk_used or 0)}} / {{fmt(local.disk_total or 0)}} ({{'%.0f'|format(local.disk_percent or 0)}}%)<div class=progress><div class=bar style="width:{{local.disk_percent or 0}}%"></div></div></div><form class=card method=post><h2>✏️ 编辑本机资料</h2><label>名称</label><input name=name value="{{profile.name}}"><label>备注</label><input name=note value="{{profile.note}}"><label>周期</label><select name=cycle><option value=monthly {{'selected' if profile.cycle=='monthly' else ''}}>月付</option><option value=quarterly {{'selected' if profile.cycle=='quarterly' else ''}}>季付</option><option value=yearly {{'selected' if profile.cycle=='yearly' else ''}}>年付</option></select><label>价格</label><input name=price value="{{profile.price}}"><label>币种</label><select name=currency>{% for c in ['CNY','USD','EUR','GBP'] %}<option value={{c}} {{'selected' if profile.currency==c else ''}}>{{c}}</option>{% endfor %}</select><label>到期</label><input name=expire_at value="{{profile.expire_at}}"><button class=primary>💾 保存</button></form></div>'''
EVENTS='''<div class=top><h1>🧾✨ 事件记录</h1><a class=btn href="/">📊 总览</a></div><div class=card><p class=small>📜 记录区域已开启滚轮浏览，鼠标放在表格内即可上下滑动查看更多历史事件。</p><div class=scrollbox><table class=table><thead><tr><th>时间</th><th>类型</th><th>标题</th><th>内容</th></tr></thead><tbody>{% for e in events %}<tr><td>{{e.created_at}}</td><td><span class=badge>{{e.event_type}}</span></td><td><b>{{e.title}}</b></td><td>{{e.content}}</td></tr>{% else %}<tr><td colspan=4>暂无事件</td></tr>{% endfor %}</tbody></table></div></div>'''
app.jinja_env.globals.update(fmt=fmt,dur=dur,exptext=exptext,pricet=pricet,cycle=cycle,fresh=fresh)
if __name__=='__main__': init_db(); app.run(host=WEB_HOST,port=WEB_PORT,threaded=True)
