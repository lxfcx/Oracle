#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, time, socket, sqlite3, ipaddress, secrets, html, zipfile, shutil, glob, re
from datetime import datetime
from functools import wraps

import requests
from flask import Flask, request, redirect, url_for, session, render_template_string, jsonify, flash, make_response, send_file, send_from_directory, abort
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
ADMIN_IDS=[x.strip() for x in os.getenv('ADMIN_IDS','').split(',') if x.strip()]
METRICS_PORT=int(os.getenv('METRICS_PORT','8765'))
METRICS_SECRET=os.getenv('METRICS_SECRET') or (BOT_TOKEN[-16:] if BOT_TOKEN else 'server-monitor-secret')
app=Flask(__name__); app.secret_key=WEB_SECRET_KEY
METRIC_RATE_CACHE={}

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
    c.execute("""CREATE TABLE IF NOT EXISTS server_metrics(server_id INTEGER PRIMARY KEY,name TEXT DEFAULT '',hostname TEXT DEFAULT '',public_ip TEXT DEFAULT '',uptime_seconds INTEGER DEFAULT 0,boot_time TEXT DEFAULT '',cpu_percent REAL DEFAULT 0,mem_percent REAL DEFAULT 0,disk_percent REAL DEFAULT 0,rx_bytes INTEGER DEFAULT 0,tx_bytes INTEGER DEFAULT 0,cpu_cores INTEGER DEFAULT 0,mem_total INTEGER DEFAULT 0,disk_total INTEGER DEFAULT 0,disk_used INTEGER DEFAULT 0,mem_used INTEGER DEFAULT 0,swap_total INTEGER DEFAULT 0,swap_used INTEGER DEFAULT 0,swap_percent REAL DEFAULT 0,load1 REAL DEFAULT 0,load5 REAL DEFAULT 0,load15 REAL DEFAULT 0,updated_at TEXT DEFAULT '',raw TEXT DEFAULT '')""")
    for col,d in [('swap_total','INTEGER DEFAULT 0'),('swap_used','INTEGER DEFAULT 0'),('swap_percent','REAL DEFAULT 0'),('load1','REAL DEFAULT 0'),('load5','REAL DEFAULT 0'),('load15','REAL DEFAULT 0'),('load1','REAL DEFAULT 0'),('load5','REAL DEFAULT 0'),('load15','REAL DEFAULT 0')]: ensure_col(c,'server_metrics',col,d)
    c.execute("""CREATE TABLE IF NOT EXISTS metric_alert_state(server_id INTEGER,metric TEXT,active INTEGER DEFAULT 0,last_value REAL DEFAULT 0,threshold REAL DEFAULT 0,last_sent_at TEXT DEFAULT '',PRIMARY KEY(server_id,metric))""")
    c.execute("""CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,event_type TEXT DEFAULT '',title TEXT DEFAULT '',content TEXT DEFAULT '',created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS reminders(server_id INTEGER,remind_key TEXT,sent_at TEXT,PRIMARY KEY(server_id,remind_key))""")
    c.execute("""CREATE TABLE IF NOT EXISTS local_profile(key TEXT PRIMARY KEY,value TEXT DEFAULT '')""")
    for k,v in {'name':socket.gethostname(),'note':'','cycle':'monthly','price':'0','currency':'USD','expire_at':'','site_name':'服务器监控'}.items(): c.execute('INSERT OR IGNORE INTO local_profile(key,value) VALUES(?,?)',(k,v))
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

def local_or_login(fn):
    @wraps(fn)
    def w(*a, **kw):
        ip = request.remote_addr or ''
        if ip in ('127.0.0.1', '::1', 'localhost'):
            return fn(*a, **kw)
        if not session.get('ok'):
            return redirect(url_for('login', next=request.path))
        return fn(*a, **kw)
    return w

def login_required(fn):
    @wraps(fn)
    def w(*a,**kw):
        if not session.get('ok'): return redirect(url_for('login',next=request.path))
        return fn(*a,**kw)
    return w


@app.before_request
def persist_settings_form_patch():
    if request.method != 'POST':
        return None
    p=(request.path or '').lower()
    if 'setting' not in p and 'config' not in p:
        return None
    try:
        f=request.form
        if 'site_name' in f or 'platform_name' in f or 'name' in f:
            if 'site_name' in f:
                set_setting_value('site_name', f.get('site_name','').strip())
            elif 'platform_name' in f:
                set_setting_value('site_name', f.get('platform_name','').strip())
        if 'BOT_TOKEN' in f:
            set_setting_value('bot_token', f.get('BOT_TOKEN','').strip())
        if 'bot_token' in f:
            set_setting_value('bot_token', f.get('bot_token','').strip())
        if 'ADMIN_IDS' in f:
            set_setting_value('admin_ids', f.get('ADMIN_IDS','').strip())
        if 'admin_ids' in f:
            set_setting_value('admin_ids', f.get('admin_ids','').strip())
    except Exception as e:
        print('[settings persist patch]', e, flush=True)
    return None


@app.route('/site-meta')
def site_meta():
    return jsonify({'site_name':site_name() if 'site_name' in globals() else '服务器监控','has_favicon':bool(favicon_exists()) if 'favicon_exists' in globals() else False})

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
    mem=psutil.virtual_memory(); sw=psutil.swap_memory(); disk=psutil.disk_usage('/')
    return {'hostname':socket.gethostname(),'public_ip':public_ip(),'uptime':dur(time.time()-psutil.boot_time()),'cpu':psutil.cpu_percent(interval=0.1),'cpu_count':psutil.cpu_count() or 0,'mem_used':mem.used,'mem_total':mem.total,'mem_percent':mem.percent,'swap_used':sw.used,'swap_total':sw.total,'swap_percent':sw.percent,'disk_used':disk.used,'disk_total':disk.total,'disk_percent':disk.percent}
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
        s.update({'status':st,'metrics':m,'online':st.get('last_status')=='online','probe_fresh':fresh(m),'flag':server_flag(s),'location':' '.join([x for x in [s.get('country'),s.get('region'),s.get('city')] if x]) or '未知','location_cn':server_location_cn(s),'expire_label':exptext(s.get('expire_at'),s.get('free_forever')),'price_label':pricet(s)})
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

def render(active,body,**ctx): init_db(); return render_template_string(BASE,active=active,body=render_template_string(body,**ctx),now=now(),username=session.get('username',WEB_USERNAME),theme_css=active_theme_css() if 'active_theme_css' in globals() else '',site_name=site_name() if 'site_name' in globals() else '服务器监控',site_subtitle='',has_favicon=bool(favicon_exists()) if 'favicon_exists' in globals() else False)
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
            expire='永久' if free else normalize_datetime_value(request.form.get('expire_at','')).strip(); price=0 if free else float(request.form.get('price') or 0)
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
    s.update({'status':get_status(sid),'metrics':get_metrics(sid),'states':states(sid),'flag':server_flag(s),'location':' '.join([x for x in [s.get('country'),s.get('region'),s.get('city')] if x]) or '未知','location_cn':server_location_cn(s)}); s['agent_cmd']=agent_cmd(s)
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
            expire='永久' if free else normalize_datetime_value(request.form.get('expire_at','')).strip(); price=0 if free else float(request.form.get('price') or 0)
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


# ===== WEB V3 FIX HELPERS =====
COUNTRY_NAME_CODE={'中国':'CN','China':'CN','香港':'HK','Hong Kong':'HK','台湾':'TW','Taiwan':'TW','美国':'US','United States':'US','USA':'US','日本':'JP','Japan':'JP','新加坡':'SG','Singapore':'SG','韩国':'KR','South Korea':'KR','英国':'GB','United Kingdom':'GB','德国':'DE','Germany':'DE','法国':'FR','France':'FR','荷兰':'NL','Netherlands':'NL','加拿大':'CA','Canada':'CA','澳大利亚':'AU','Australia':'AU','印度':'IN','India':'IN','俄罗斯':'RU','Russia':'RU','巴西':'BR','Brazil':'BR','土耳其':'TR','Turkey':'TR','泰国':'TH','Thailand':'TH','越南':'VN','Vietnam':'VN','马来西亚':'MY','Malaysia':'MY','菲律宾':'PH','Philippines':'PH','印尼':'ID','Indonesia':'ID','阿联酋':'AE','United Arab Emirates':'AE','迪拜':'AE','意大利':'IT','Italy':'IT','西班牙':'ES','Spain':'ES'}
def country_code_guess(country):
    c=str(country or '').strip()
    if c in COUNTRY_NAME_CODE: return COUNTRY_NAME_CODE[c]
    low=c.lower()
    for name,code in COUNTRY_NAME_CODE.items():
        if name.lower() in low or low in name.lower(): return code
    return ''
def server_flag(s):
    if not s: return '🌐'
    code=(s.get('country_code') or '').strip() or country_code_guess(s.get('country') or '')
    return flag(code)
def theme_bg_exists():
    for fn in ['web_bg.jpg','web_bg.png','web_bg.webp','web_bg.jpeg']:
        p=os.path.join(APP_DIR,fn)
        if os.path.exists(p):
            return p
    theme_dir=os.path.join(APP_DIR,'web_theme')
    for pat in ['*.jpg','*.jpeg','*.png','*.webp','*.gif']:
        files=glob.glob(os.path.join(theme_dir,'**',pat), recursive=True)
        if files:
            return files[0]
    return ''
@app.route('/theme-bg')
def theme_bg():
    from flask import send_file, abort, make_response
    fn=theme_bg_exists()
    if not fn:
        abort(404)
    resp = make_response(send_file(fn if os.path.isabs(str(fn)) else os.path.join(APP_DIR,fn)))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    return resp
def quote_env(v): return '"'+str(v or '').replace('\\','\\\\').replace('"','\\"')+'"'
def update_env_file(path,data):
    lines=[]
    if os.path.exists(path):
        for line in open(path,'r',encoding='utf-8',errors='ignore').read().splitlines():
            if '=' in line and not line.strip().startswith('#') and line.split('=',1)[0].strip() in data: continue
            lines.append(line)
    for k,v in data.items():
        lines.append(f'{k}={quote_env(v)}'); os.environ[k]=str(v)
    os.makedirs(os.path.dirname(path),exist_ok=True)
    open(path,'w',encoding='utf-8').write('\n'.join(lines)+'\n')
@app.route('/settings',methods=['GET','POST'])
@login_required
def settings_page():
    if request.method=='POST':
        act=request.form.get('action','')
        try:
            if act=='site':
                name_value=request.form.get('site_name','').strip() or '服务器监控'
                set_setting('site_name', name_value)
                flash('平台名字已保存，刷新页面后生效','success')
            elif act=='favicon':
                save_uploaded_favicon(request.files.get('favicon'))
                flash('浏览器标签图标已上传，强制刷新后生效','success')
            elif act=='password':
                user=request.form.get('username','admin').strip() or 'admin'
                p1=request.form.get('password',''); p2=request.form.get('password2','')
                if not p1 or p1!=p2: raise ValueError('两次密码不一致或为空')
                from werkzeug.security import generate_password_hash
                update_env_file(os.path.join(APP_DIR,'.env.web'),{'WEB_USERNAME':user,'WEB_PASSWORD_HASH':generate_password_hash(p1),'WEB_SECRET_KEY':secrets.token_hex(32)})
                flash('登录账号密码已修改，重新登录后生效','success')
            elif act=='tg':
                update_env_file(os.path.join(APP_DIR,'.env'),{'BOT_TOKEN':request.form.get('bot_token','').strip(),'ADMIN_IDS':request.form.get('admin_ids','').strip()})
                flash('TG 接口配置已保存，请重启 server-monitor-bot 和 server-monitor-web','success')
            elif act=='upload_bg':
                f=request.files.get('bg')
                if not f or not f.filename: raise ValueError('请选择图片')
                ext=f.filename.rsplit('.',1)[-1].lower() if '.' in f.filename else 'jpg'
                if ext not in ['jpg','jpeg','png','webp']: raise ValueError('只支持 jpg/png/webp')
                for old in ['web_bg.jpg','web_bg.png','web_bg.webp','web_bg.jpeg']:
                    try: os.remove(os.path.join(APP_DIR,old))
                    except Exception: pass
                f.save(os.path.join(APP_DIR,'web_bg.'+ext))
                flash('全站背景图已上传，刷新页面即可看到','success')
            elif act=='upload_theme_zip':
                f=request.files.get('theme_zip')
                if not f or not f.filename: raise ValueError('请选择主题 zip 压缩包')
                if not f.filename.lower().endswith('.zip'): raise ValueError('只支持 zip 主题包')
                tmp=os.path.join(APP_DIR,'_theme_upload.zip')
                f.save(tmp)
                safe_extract_zip(tmp, THEME_DIR)
                try: os.remove(tmp)
                except Exception: pass
                # Komari 主题通常包含 komari-theme.json 和 dist/，这里兼容读取其中 css / 图片资源作为本面板皮肤。
                flash('主题 zip 已上传并应用；如果主题包含 CSS/图片，会自动作为当前 Web 皮肤资源加载','success')
            elif act=='clear_bg':
                for old in ['web_bg.jpg','web_bg.png','web_bg.webp','web_bg.jpeg']:
                    try: os.remove(os.path.join(APP_DIR,old))
                    except Exception: pass
                shutil.rmtree(THEME_DIR, ignore_errors=True)
                flash('已恢复默认星空背景和默认主题','success')
        except Exception as e:
            flash('操作失败：'+str(e),'error')
        return redirect(url_for('settings_page'))
    return render('settings',SETTINGS,web_user=WEB_USERNAME,bot_token=BOT_TOKEN,admin_ids=','.join(ADMIN_IDS) if 'ADMIN_IDS' in globals() and isinstance(ADMIN_IDS,list) else os.getenv('ADMIN_IDS',''),has_bg=bool(theme_bg_exists()),theme_css=active_theme_css())

@app.route('/api/local-live')
@local_or_login
def api_local_live():
    resp=jsonify(_local_live_json())
    resp.headers['Cache-Control']='no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma']='no-cache'
    resp.headers['Expires']='0'
    return resp


@app.route('/api/servers-live')
@login_required
def api_servers_live():
    ss=all_servers()
    resp=jsonify({'time':now(),'servers':[metric_json(x) for x in ss]})
    resp.headers['Cache-Control']='no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma']='no-cache'
    resp.headers['Expires']='0'
    return resp

@app.route('/api/metrics-debug')
@login_required
def api_metrics_debug():
    c=db()
    try:
        rows=[rd(r) for r in c.execute("""
        SELECT s.id,s.name,s.host,
               m.name AS metric_name,m.hostname,m.public_ip,
               m.cpu_cores,m.mem_total,m.mem_used,m.mem_percent,
               m.swap_total,m.swap_used,m.swap_percent,
               m.disk_total,m.disk_used,m.disk_percent,
               m.cpu_percent,m.load1,m.load5,m.load15,m.rx_bytes,m.tx_bytes,m.updated_at
        FROM servers s
        LEFT JOIN server_metrics m ON s.id=m.server_id
        ORDER BY s.id ASC
        """).fetchall()]
    finally:
        c.close()
    resp=jsonify({'time':now(),'rows':rows})
    resp.headers['Cache-Control']='no-store, no-cache, must-revalidate, max-age=0'
    return resp


# ===== END WEB V3 FIX HELPERS =====




# ===== FINAL PATCH FROM CURRENT CODE: UI + progress + color =====
def _num(v):
    try:
        return float(v or 0)
    except Exception:
        return 0.0

def expire_days_for_row(row):
    try:
        if truth(row.get('free_forever')):
            return 999999
        exp=str(row.get('expire_at') or '').strip()
        if exp in ['永久','永久免费']:
            return 999999
        return (pdt(exp).date()-datetime.now().date()).days
    except Exception:
        return None

def status_color_class_by_days(row):
    d=expire_days_for_row(row)
    if d is None:
        return 'muted-text'
    if d == 999999:
        return 'ok-text'
    if d < 0 or d <= 7:
        return 'danger-text'
    if d <= 30:
        return 'warn-text'
    return 'ok-text'

def progress_row(label, sid, key, value, limit=90):
    v=max(0, min(100, _num(value)))
    try:
        lim=float(limit or 90)
    except Exception:
        lim=90
    cls='bad' if v>=80 else 'warn' if v>=50 else ''
    return f'''<div class="progressrow"><span>{html.escape(str(label))}</span><div class="progress"><div class="bar {cls}" data-{key}="{sid}" data-limit="{lim:g}" style="width:{v:g}%"></div></div><b data-{key}txt="{sid}">{v:.0f}%</b></div>'''
# ===== END FINAL PATCH =====


# ===== SITE CUSTOM PATCH: title/favicon/theme transparency =====
def get_setting(key, default=''):
    try:
        c=db()
        try:
            r=c.execute('SELECT value FROM local_profile WHERE key=?',(key,)).fetchone()
            return (r['value'] if r else default) or default
        finally:
            c.close()
    except Exception:
        return default

def set_setting(key, value):
    c=db()
    try:
        c.execute('CREATE TABLE IF NOT EXISTS local_profile(key TEXT PRIMARY KEY,value TEXT DEFAULT "")')
        c.execute('INSERT OR REPLACE INTO local_profile(key,value) VALUES(?,?)',(key, str(value or '')))
        c.commit()
    finally:
        c.close()

def site_name():
    return get_setting('site_name','服务器监控')

def site_subtitle():
    return ''

def favicon_exists():
    for fn in ['favicon.ico','favicon.png','favicon.jpg','favicon.jpeg','favicon.webp']:
        p=os.path.join(APP_DIR,fn)
        if os.path.exists(p):
            return p
    return ''

@app.route('/favicon.ico')
def favicon():
    from flask import send_file, abort, make_response
    p=favicon_exists()
    if not p:
        abort(404)
    resp=make_response(send_file(p))
    resp.headers['Cache-Control']='no-store, no-cache, must-revalidate, max-age=0'
    return resp

def save_uploaded_favicon(fileobj):
    if not fileobj or not fileobj.filename:
        raise ValueError('请选择图标文件')
    ext=fileobj.filename.rsplit('.',1)[-1].lower() if '.' in fileobj.filename else 'png'
    if ext not in ['ico','png','jpg','jpeg','webp']:
        raise ValueError('图标只支持 ico/png/jpg/webp')
    for old in ['favicon.ico','favicon.png','favicon.jpg','favicon.jpeg','favicon.webp']:
        try: os.remove(os.path.join(APP_DIR,old))
        except Exception: pass
    target=os.path.join(APP_DIR,'favicon.'+ext)
    fileobj.save(target)
    return target
# ===== END SITE CUSTOM PATCH =====


# ===== EVENT DISPLAY PATCH =====
def clean_event_html(v):
    txt=str(v or '')
    txt=re.sub(r'<br\s*/?>','\n',txt,flags=re.I)
    txt=re.sub(r'</?(b|code|i|strong|em)>','',txt,flags=re.I)
    txt=re.sub(r'<[^>]+>','',txt)
    return html.escape(txt).replace('\n','<br>')

def event_context(e):
    content=str((e or {}).get('content') or '')
    title=str((e or {}).get('title') or '')
    raw=content+' '+title
    # 提取 “主机：xxx”
    host=''
    m=re.search(r'主机[:：]\s*(?:<code>)?([^<\s]+)', raw)
    if m:
        host=m.group(1).strip()
    c=db()
    try:
        servers=[rd(r) for r in c.execute('SELECT * FROM servers ORDER BY id ASC').fetchall()]
        metrics=[rd(r) for r in c.execute('SELECT * FROM server_metrics').fetchall()]
    finally:
        c.close()
    # 先用 metrics 的 hostname/public_ip 关联
    for mm in metrics:
        sid=mm.get('server_id')
        srv=next((x for x in servers if x.get('id')==sid), None)
        if not srv:
            continue
        keys=[str(mm.get('hostname') or ''), str(mm.get('public_ip') or ''), str(srv.get('name') or ''), str(srv.get('host') or '')]
        if any(k and k in raw for k in keys):
            return f'关联服务器：#{srv.get("id")} {html.escape(str(srv.get("name") or ""))} ｜ IP/主机：{html.escape(str(srv.get("host") or ""))}'
    # 再用 servers 表的 name/host 关联
    for srv in servers:
        keys=[str(srv.get('name') or ''), str(srv.get('host') or '')]
        if any(k and k in raw for k in keys):
            return f'关联服务器：#{srv.get("id")} {html.escape(str(srv.get("name") or ""))} ｜ IP/主机：{html.escape(str(srv.get("host") or ""))}'
    # 主机名等于本机 hostname
    try:
        if host and host == socket.gethostname():
            return f'关联对象：本机 ｜ 主机名：{html.escape(host)} ｜ IP：{html.escape(public_ip() or "未知")}'
    except Exception:
        pass
    if host:
        return f'关联对象：未知服务器 ｜ 主机名：{html.escape(host)}'
    return ''
# ===== END EVENT DISPLAY PATCH =====





# ===== FREE LABEL FIX PATCH =====
def display_price_label(row):
    if truth((row or {}).get('free_forever')):
        return '🎁 免费'
    return pricet(row or {})

def display_expire_label(row):
    if truth((row or {}).get('free_forever')) or str((row or {}).get('expire_at') or '').strip() in ['永久','永久免费']:
        return '♾️ 永久'
    return exptext((row or {}).get('expire_at'), (row or {}).get('free_forever'))

def display_price_expire(row):
    return display_price_label(row), display_expire_label(row)
# ===== END FREE LABEL FIX PATCH =====








# ===== FINAL FULL LINK REALTIME PATCH =====
def metric_config_html(m):
    m=m or {}
    def iv(v):
        try: return int(float(v or 0))
        except Exception: return 0
    cpu=iv(m.get('cpu_cores'))
    mem=iv(m.get('mem_total'))
    swap=iv(m.get('swap_total'))
    disk=iv(m.get('disk_total'))
    return f"🧩 {cpu or '?'}C ｜ 🧠 {fmt(mem) if mem else '未知'} ｜ 🔄 {fmt(swap) if swap else '无'} ｜ 💾 {fmt(disk) if disk else '未知'}"

def _float(v):
    try: return float(v or 0)
    except Exception: return 0.0

def _int(v):
    try: return int(float(v or 0))
    except Exception: return 0

def metric_json(x):
    m=x.get('metrics') or {}
    sid=x.get('id')
    rx=_int(m.get('rx_bytes'))
    tx=_int(m.get('tx_bytes'))
    ts=time.time()
    down_speed=0
    up_speed=0
    old=METRIC_RATE_CACHE.get(sid)
    if old:
        dt=max(0.5, ts-old.get('ts',ts))
        down_speed=max(0, int((rx-old.get('rx',rx))/dt))
        up_speed=max(0, int((tx-old.get('tx',tx))/dt))
    METRIC_RATE_CACHE[sid]={'rx':rx,'tx':tx,'ts':ts}
    return {
        'id':sid,
        'name':x.get('name') or '',
        'online':bool(x.get('online')),
        'status':'在线' if x.get('online') else '离线' if x.get('status',{}).get('last_status')=='offline' else '未知',
        'uptime':dur(m.get('uptime_seconds') or 0),
        'cpu':_float(m.get('cpu_percent')),
        'mem':_float(m.get('mem_percent')),
        'swap':_float(m.get('swap_percent')),
        'disk':_float(m.get('disk_percent')),
        'cpu_cores':_int(m.get('cpu_cores')),
        'mem_total':_int(m.get('mem_total')),
        'mem_used':_int(m.get('mem_used')),
        'swap_total':_int(m.get('swap_total')),
        'swap_used':_int(m.get('swap_used')),
        'disk_total':_int(m.get('disk_total')),
        'disk_used':_int(m.get('disk_used')),
        'rx_bytes':rx,
        'tx_bytes':tx,
        'down_speed':down_speed,
        'up_speed':up_speed,
        'load1':_float(m.get('load1')),
        'load5':_float(m.get('load5')),
        'load15':_float(m.get('load15')),
        'updated_at':m.get('updated_at') or '',
        'config_html':metric_config_html(m),
        'net_speed_html':f"↑ {fmt(up_speed)}/s&nbsp;&nbsp;↓ {fmt(down_speed)}/s",
        'traffic_html':f"↑ {fmt(tx)}&nbsp;&nbsp;↓ {fmt(rx)}",
        'load_html':f"{_float(m.get('load1')):.2f} ｜ {_float(m.get('load5')):.2f} ｜ {_float(m.get('load15')):.2f}",
    }
# ===== END FINAL FULL LINK REALTIME PATCH =====


# ===== SETTINGS PERSISTENCE PATCH =====
def ensure_settings_table():
    c=db()
    try:
        c.execute("""CREATE TABLE IF NOT EXISTS app_settings(
            key TEXT PRIMARY KEY,
            value TEXT DEFAULT '',
            updated_at TEXT DEFAULT ''
        )""")
        c.commit()
    finally:
        c.close()

def get_setting(key, default=''):
    try:
        ensure_settings_table()
        c=db()
        try:
            r=c.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
            if r and r[0] is not None:
                return str(r[0])
        finally:
            c.close()
    except Exception:
        pass
    return str(default or '')

def set_setting_value(key, value):
    ensure_settings_table()
    c=db()
    try:
        c.execute("INSERT OR REPLACE INTO app_settings(key,value,updated_at) VALUES(?,?,?)", (key, str(value or ''), now()))
        c.commit()
    finally:
        c.close()

def site_name_value():
    v=get_setting('site_name', '')
    if v:
        return v
    try:
        return str(site_name() if callable(site_name) else site_name)
    except Exception:
        return os.getenv('SITE_NAME','路西法的VPS监控')

def bot_token_value():
    return get_setting('bot_token', os.getenv('BOT_TOKEN',''))

def admin_ids_value():
    return get_setting('admin_ids', os.getenv('ADMIN_IDS',''))
# ===== END SETTINGS PERSISTENCE PATCH =====


# ===== OS DISPLAY AND DATETIME PICKER PATCH =====
def datetime_input_value(v):
    v=str(v or '').strip()
    if not v:
        return ''
    v=v.replace(' ', 'T')
    # datetime-local wants YYYY-MM-DDTHH:MM, date-only is also okay but normalize to T00:00
    if re.match(r'^\d{4}-\d{2}-\d{2}$', v):
        return v + 'T00:00'
    if re.match(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}', v):
        return v[:16]
    return v[:16]

def normalize_datetime_value(v):
    v=str(v or '').strip()
    if not v:
        return ''
    v=v.replace('T', ' ')
    if re.match(r'^\d{4}-\d{2}-\d{2}$', v):
        return v + ' 00:00'
    if re.match(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}', v):
        return v[:16]
    return v

def probe_os_name_for_server(s):
    try:
        manual=str((s.get('os_name') if hasattr(s,'get') else s.os_name) or '').strip()
        if manual and manual not in ('未知','未知系统'):
            return manual
        sid=(s.get('id') if hasattr(s,'get') else s.id)
        c=db()
        try:
            r=c.execute("SELECT raw FROM server_metrics WHERE server_id=?", (sid,)).fetchone()
        finally:
            c.close()
        if r and r[0]:
            import json
            data=json.loads(r[0])
            v=str(data.get('os_name') or data.get('os') or '').strip()
            if v:
                return v
    except Exception:
        pass
    return ''
# ===== END OS DISPLAY AND DATETIME PICKER PATCH =====


# ===== LOCAL LIVE PATCH =====
LOCAL_RATE_CACHE = {}

def _local_meminfo():
    data={}
    try:
        with open('/proc/meminfo','r',encoding='utf-8',errors='ignore') as f:
            for line in f:
                if ':' in line:
                    k,v=line.split(':',1)
                    data[k]=int(v.strip().split()[0])*1024
    except Exception:
        pass
    return data

def _local_cpu_percent():
    def read():
        vals=list(map(int, open('/proc/stat').readline().split()[1:]))
        idle=vals[3]+vals[4]
        total=sum(vals)
        return idle,total
    try:
        i1,t1=read()
        time.sleep(0.16)
        i2,t2=read()
        total=t2-t1
        idle=i2-i1
        return round((1-idle/total)*100,1) if total else 0
    except Exception:
        return 0

def _local_net_bytes():
    rx=tx=0
    try:
        for line in open('/proc/net/dev','r',encoding='utf-8',errors='ignore').read().splitlines()[2:]:
            iface,rest=line.split(':',1)
            iface=iface.strip()
            if iface=='lo':
                continue
            vals=rest.split()
            rx+=int(vals[0]); tx+=int(vals[8])
    except Exception:
        pass
    return rx,tx

def _local_live_json():
    import shutil as _shutil
    ts=time.time()
    rx,tx=_local_net_bytes()
    old=LOCAL_RATE_CACHE.get('local')
    down=up=0
    if old:
        dt=max(0.5,ts-old.get('ts',ts))
        down=max(0,int((rx-old.get('rx',rx))/dt))
        up=max(0,int((tx-old.get('tx',tx))/dt))
    LOCAL_RATE_CACHE['local']={'rx':rx,'tx':tx,'ts':ts}

    mi=_local_meminfo()
    mem_total=mi.get('MemTotal',0)
    mem_avail=mi.get('MemAvailable',0)
    mem_used=max(0,mem_total-mem_avail)
    mem_percent=round(mem_used*100/mem_total,1) if mem_total else 0

    swap_total=mi.get('SwapTotal',0)
    swap_free=mi.get('SwapFree',0)
    swap_used=max(0,swap_total-swap_free)
    swap_percent=round(swap_used*100/swap_total,1) if swap_total else 0

    du=_shutil.disk_usage('/')
    disk_total=du.total
    disk_used=du.used
    disk_percent=round(disk_used*100/disk_total,1) if disk_total else 0

    try:
        load1,load5,load15=os.getloadavg()
    except Exception:
        load1=load5=load15=0

    upsec=0
    try:
        upsec=int(float(open('/proc/uptime').read().split()[0]))
    except Exception:
        pass

    return {
        'cpu':_local_cpu_percent(),
        'mem':mem_percent,
        'swap':swap_percent,
        'disk':disk_percent,
        'mem_total':mem_total,
        'mem_used':mem_used,
        'swap_total':swap_total,
        'swap_used':swap_used,
        'disk_total':disk_total,
        'disk_used':disk_used,
        'rx_bytes':rx,
        'tx_bytes':tx,
        'down_speed':down,
        'up_speed':up,
        'net_speed_html':f"↑ {fmt(up)}/s&nbsp;&nbsp;↓ {fmt(down)}/s",
        'traffic_html':f"↑ {fmt(tx)}&nbsp;&nbsp;↓ {fmt(rx)}",
        'load1':round(load1,2),
        'load5':round(load5,2),
        'load15':round(load15,2),
        'load_html':f"{load1:.2f} ｜ {load5:.2f} ｜ {load15:.2f}",
        'uptime':dur(upsec),
        'time':now(),
    }
# ===== END LOCAL LIVE PATCH =====

LOGIN='''<!doctype html><html><head><meta charset=utf-8><script>(function(){let t=localStorage.getItem('theme')||'dark',g=localStorage.getItem('glass')||'glass';document.documentElement.classList.toggle('light',t==='light');document.documentElement.classList.toggle('solid',g==='solid')})();</script><meta name=viewport content="width=device-width,initial-scale=1"><title>登录</title><link id="favLink" rel="icon" href="/favicon.ico?v=login"><style>
:root{--text:#edf5ff;--muted:#9fb0c7;--line:#ffffff28;--glass:#ffffff16;--glass2:#ffffff28;--a:#6ee7ff;--b:#a78bfa;--shadow:#0008}
body.light{--text:#0f172a;--muted:#475569;--line:#0f172a22;--glass:#ffffff55;--glass2:#ffffff88;--shadow:#64748b44}
body.solid{--glass:#1d2b3faa;--glass2:#263950cc}body.light.solid{--glass:#ffffff;--glass2:#f8fafc}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;overflow:hidden;background:#050914;transition:.3s}
body:before{content:"";position:fixed;inset:0;z-index:-3;background-image:linear-gradient(rgba(0,0,0,.18),rgba(0,0,0,.35)),url('/theme-bg?v=login'),
radial-gradient(1px 1px at 8% 12%,#fff,transparent),radial-gradient(2px 2px at 18% 80%,#dbeafe,transparent),radial-gradient(1px 1px at 29% 35%,#fff,transparent),radial-gradient(1.5px 1.5px at 42% 18%,#cffafe,transparent),radial-gradient(2px 2px at 55% 70%,#fff,transparent),radial-gradient(1px 1px at 72% 28%,#e0e7ff,transparent),radial-gradient(1.8px 1.8px at 86% 62%,#fff,transparent),radial-gradient(circle at 18% 18%,#1d4ed875,transparent 34%),radial-gradient(circle at 82% 4%,#7c3aed70,transparent 38%),linear-gradient(180deg,#07111f,#020617 72%);animation:twinkle 16s ease-in-out infinite}
body:after{content:"";position:fixed;inset:-40%;z-index:-2;background:radial-gradient(circle,#ffffff12 0 1px,transparent 2px);background-size:82px 82px;animation:drift 65s linear infinite}
body.light:before{background-image:linear-gradient(rgba(255,255,255,.10),rgba(255,255,255,.18)),var(--custom-bg,none),radial-gradient(circle at 20% 12%,#93c5fd99,transparent 32%),radial-gradient(circle at 82% 0,#c4b5fd99,transparent 36%),linear-gradient(180deg,#eff6ff,#dbeafe 62%,#f8fafc);background-size:cover,cover,auto,auto,auto;background-position:center}body.light:after{background:radial-gradient(circle,#33415526 0 1px,transparent 2px);background-size:86px 86px}
@keyframes twinkle{0%,100%{filter:brightness(1)}50%{filter:brightness(1.32)}}@keyframes drift{from{transform:translate3d(0,0,0)}to{transform:translate3d(82px,82px,0)}}
.card{position:relative;z-index:2;width:min(460px,92vw);padding:34px;border:1px solid var(--line);background:linear-gradient(180deg,var(--glass2),var(--glass));border-radius:32px;box-shadow:0 30px 100px var(--shadow);backdrop-filter:blur(26px);-webkit-backdrop-filter:blur(26px)}
h1{margin:0 0 8px;font-size:31px}.sub{color:var(--muted);margin-bottom:20px}.controls{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:12px 0 18px}.ctl{padding:10px 12px;border:1px solid var(--line);background:var(--glass);border-radius:15px;color:var(--text);font-weight:900;cursor:pointer}
input,.loginbtn{width:100%;padding:15px;border-radius:16px;margin:9px 0;border:1px solid var(--line);font-size:16px}input{background:#00000035;color:var(--text)}body.light input{background:#ffffffc7}.loginbtn{background:linear-gradient(135deg,var(--a),var(--b));color:#07111f;font-weight:950;border:0;cursor:pointer}.flash{background:#fb718522;border:1px solid #fb718577;padding:12px;border-radius:14px}.tip{color:var(--muted);font-size:13px;line-height:1.7;margin-top:10px}
.top h1{margin:0;font-size:clamp(28px,3vw,44px);font-weight:1000;line-height:1.2;color:var(--text)!important;text-shadow:0 2px 14px rgba(0,0,0,.28)}html.light .top h1{color:#0f172a!important;text-shadow:0 2px 10px rgba(255,255,255,.55)}
/* ===== all page title readability final patch ===== */
/* 所有页面顶部大标题：不再使用透明渐变，强制高对比清晰显示 */
main .top h1,
.layout .main .top h1,
.top > h1,
body .top h1 {
  color: #f8fbff !important;
  background: none !important;
  -webkit-background-clip: initial !important;
  background-clip: initial !important;
  -webkit-text-fill-color: #f8fbff !important;
  text-shadow:
    0 2px 4px rgba(0,0,0,.75),
    0 0 18px rgba(15,23,42,.55) !important;
  font-weight: 1000 !important;
  letter-spacing: .2px !important;
}

/* 日间模式标题：深色清晰 */
html.light main .top h1,
html.light .layout .main .top h1,
html.light .top > h1,
html.light body .top h1 {
  color: #0f172a !important;
  background: none !important;
  -webkit-background-clip: initial !important;
  background-clip: initial !important;
  -webkit-text-fill-color: #0f172a !important;
  text-shadow:
    0 1px 0 rgba(255,255,255,.85),
    0 2px 10px rgba(255,255,255,.55) !important;
}

/* 夜间透明背景时再加一点描边感，避免雪山/亮背景吃字 */
html:not(.light) body:not(.solid) main .top h1,
html:not(.light) body:not(.solid) .layout .main .top h1,
html:not(.light) body:not(.solid) .top > h1 {
  color: #ffffff !important;
  -webkit-text-fill-color: #ffffff !important;
  text-shadow:
    0 2px 4px rgba(0,0,0,.88),
    0 0 3px rgba(0,0,0,.85),
    0 0 22px rgba(37,99,235,.35) !important;
}

/* 标题里的 emoji/icon 保持原色，不做透明渐变 */
main .top h1 *,
.layout .main .top h1 * {
  background: none !important;
  -webkit-background-clip: initial !important;
  background-clip: initial !important;
  -webkit-text-fill-color: currentColor !important;
}
/* ===== end all page title readability final patch ===== */


/* ===== bright page title readability patch ===== */
/* 所有页面大标题：清楚但不发黑，不使用暗沉重阴影 */
main .top h1,
.layout .main .top h1,
.top > h1,
body .top h1 {
  background: linear-gradient(90deg, #ffffff 0%, #93e8ff 45%, #c7b8ff 100%) !important;
  -webkit-background-clip: text !important;
  background-clip: text !important;
  color: transparent !important;
  -webkit-text-fill-color: transparent !important;
  text-shadow:
    0 0 1px rgba(255,255,255,.85),
    0 0 10px rgba(125,211,252,.42),
    0 0 22px rgba(167,139,250,.28) !important;
  font-weight: 1000 !important;
  letter-spacing: .3px !important;
}

/* 夜间透明背景：亮一点，阴影轻一点，不要黑 */
html:not(.light) body:not(.solid) main .top h1,
html:not(.light) body:not(.solid) .layout .main .top h1,
html:not(.light) body:not(.solid) .top > h1 {
  background: linear-gradient(90deg, #ffffff 0%, #b9f3ff 46%, #d9ccff 100%) !important;
  -webkit-background-clip: text !important;
  background-clip: text !important;
  color: transparent !important;
  -webkit-text-fill-color: transparent !important;
  text-shadow:
    0 0 2px rgba(255,255,255,.95),
    0 0 12px rgba(186,230,253,.55),
    0 0 26px rgba(129,140,248,.32) !important;
}

/* 夜间实色背景：保持亮白蓝紫 */
html:not(.light) body.solid main .top h1,
html:not(.light) body.solid .layout .main .top h1,
html:not(.light) body.solid .top > h1 {
  background: linear-gradient(90deg, #f8fbff 0%, #8be9ff 45%, #c4b5fd 100%) !important;
  -webkit-background-clip: text !important;
  background-clip: text !important;
  color: transparent !important;
  -webkit-text-fill-color: transparent !important;
  text-shadow:
    0 0 1px rgba(255,255,255,.8),
    0 0 10px rgba(56,189,248,.32) !important;
}

/* 日间：亮蓝紫，但保持可读，不发暗 */
html.light main .top h1,
html.light .layout .main .top h1,
html.light .top > h1,
html.light body .top h1 {
  background: linear-gradient(90deg, #2563eb 0%, #7c3aed 48%, #0891b2 100%) !important;
  -webkit-background-clip: text !important;
  background-clip: text !important;
  color: transparent !important;
  -webkit-text-fill-color: transparent !important;
  text-shadow:
    0 1px 0 rgba(255,255,255,.55),
    0 0 8px rgba(255,255,255,.45) !important;
}

/* 标题图标跟文字一起亮，不要黑 */
main .top h1 *,
.layout .main .top h1 * {
  text-shadow: inherit !important;
}

/* ===== end bright page title readability patch ===== */


/* ===== date picker and os hint patch ===== */
input[type="date"] {
  color-scheme: dark;
  cursor: pointer;
}
html.light input[type="date"] {
  color-scheme: light;
}
input[type="date"]::-webkit-calendar-picker-indicator {
  cursor: pointer;
  opacity: .95;
  filter: invert(1) drop-shadow(0 1px 2px rgba(0,0,0,.35));
}
html.light input[type="date"]::-webkit-calendar-picker-indicator {
  filter: none;
}
/* ===== end date picker and os hint patch ===== */


/* ===== datetime picker visual patch ===== */
input[type="datetime-local"] {
  color-scheme: dark;
  cursor: pointer;
}
html.light input[type="datetime-local"] {
  color-scheme: light;
}
input[type="datetime-local"]::-webkit-calendar-picker-indicator {
  cursor: pointer;
  opacity: .96;
  filter: invert(1) drop-shadow(0 1px 2px rgba(0,0,0,.35));
}
html.light input[type="datetime-local"]::-webkit-calendar-picker-indicator {
  filter: none;
}
/* ===== end datetime picker visual patch ===== */

</style><script>
function apply(){let theme=localStorage.getItem('theme')||'dark', glass=localStorage.getItem('glass')||'glass';document.body.classList.toggle('light',theme==='light');document.body.classList.toggle('solid',glass==='solid');let a=document.getElementById('themeText'),b=document.getElementById('glassText');if(a)a.textContent=theme==='light'?'☀️ 当前：日间明亮':'🌙 当前：夜间星空';if(b)b.textContent=glass==='solid'?'⬛ 当前：实色背景':'🧊 当前：透明玻璃'}
function toggleTheme(){localStorage.setItem('theme',(localStorage.getItem('theme')||'dark')==='dark'?'light':'dark');apply()}
function toggleGlass(){localStorage.setItem('glass',(localStorage.getItem('glass')||'glass')==='glass'?'solid':'glass');apply()}
document.addEventListener('DOMContentLoaded',()=>{apply();fetch('/site-meta').then(r=>r.json()).then(j=>{document.title=(j.site_name||'服务器监控')+' 登录';let a=document.getElementById('loginTitle'),b=document.getElementById('loginSub');if(a)a.textContent='🛡️✨ '+(j.site_name||'服务器监控')+' 登录';if(b)b.textContent='服务器监控 Web 控制台';}).catch(()=>{});})

</script></head><body><script>let __bgv=Date.now();document.body.style.setProperty('--custom-bg',"url('/theme-bg?v="+__bgv+"')");fetch('/theme-bg?v='+__bgv,{cache:'no-store'}).then(r=>{if(r.ok)document.body.classList.add('has-custom-bg','login-bg')}).catch(()=>{});</script><form class=card method=post><h1 id="loginTitle">🛡️✨ Web 面板登录</h1><div class=sub id="loginSub">服务器监控 星空磨砂玻璃控制台</div><div class=controls><button type=button class=ctl onclick=toggleTheme()><span id=themeText>🌙 当前：夜间星空</span></button><button type=button class=ctl onclick=toggleGlass()><span id=glassText>🧊 当前：透明玻璃</span></button></div>{% with messages=get_flashed_messages(with_categories=true) %}{% for c,m in messages %}<div class=flash>{{m}}</div>{% endfor %}{% endwith %}<input name=username placeholder=账号 required><input name=password type=password placeholder=密码 required><button class=loginbtn>🚀 登录控制台</button><div class=tip>登录后可查看大屏统计、服务器资源、国旗地区、探针、阈值告警、事件记录。</div></form>
<script>
document.addEventListener('click', function(e){
  const el = e.target;
  if (el && el.matches && el.matches('input[type="date"]') && el.showPicker) {
    try { el.showPicker(); } catch(_) {}
  }
}, true);
</script>
</body></html>'''
BASE='''<!doctype html><html lang=zh-CN><head><meta charset=utf-8><script>(function(){let t=localStorage.getItem('theme')||'dark',g=localStorage.getItem('glass')||'glass';document.documentElement.classList.toggle('light',t==='light');document.documentElement.classList.toggle('solid',g==='solid')})();</script><meta name=viewport content="width=device-width,initial-scale=1"><title>{{site_name_value()}} Web</title>{% if has_favicon %}<link rel="icon" href="/favicon.ico?v={{now}}">{% endif %}{% if theme_css %}<link rel="stylesheet" href="{{theme_css}}?v={{now}}">{% endif %}<style>
:root{--bg:#07111f;--card:#ffffff14;--card2:#ffffff24;--line:#ffffff28;--text:#edf5ff;--muted:#9fb0c7;--blue:#6ee7ff;--purple:#a78bfa;--green:#34d399;--red:#fb7185;--yellow:#fbbf24;--shadow:#0007}
html.light,body.light{--bg:#dbeafe;--card:#ffffff55;--card2:#ffffff88;--line:#0f172a22;--text:#0f172a;--muted:#475569;--shadow:#64748b42}
html.solid,body.solid{--card:#1d2b3faa;--card2:#263950cc}html.light.solid,body.light.solid{--card:#fffffff2;--card2:#f8fafcff}
*{box-sizing:border-box}body{margin:0;min-height:100vh;color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;background:var(--bg);transition:background .35s,color .35s;overflow-x:hidden}
body:before{content:"";position:fixed;inset:0;z-index:-3;background-image:linear-gradient(rgba(0,0,0,.16),rgba(0,0,0,.30)),url('/theme-bg?v={{now}}'),
radial-gradient(1px 1px at 7% 14%,#fff,transparent),radial-gradient(1.6px 1.6px at 17% 73%,#bfdbfe,transparent),radial-gradient(1px 1px at 28% 33%,#fff,transparent),radial-gradient(2px 2px at 41% 17%,#e0e7ff,transparent),radial-gradient(1px 1px at 58% 80%,#fff,transparent),radial-gradient(1.8px 1.8px at 76% 28%,#cffafe,transparent),radial-gradient(1px 1px at 92% 64%,#fff,transparent),
radial-gradient(circle at 15% 8%,#1d4e8975,transparent 32%),radial-gradient(circle at 95% 0,#7c3aed70,transparent 35%),radial-gradient(circle at 50% 110%,#06b6d455,transparent 34%),linear-gradient(180deg,#07111f,#020617 72%);animation:twinkle 16s ease-in-out infinite}
body:after{content:"";position:fixed;inset:-35%;z-index:-2;background:radial-gradient(circle,#ffffff12 0 1px,transparent 2px);background-size:86px 86px;animation:starDrift 70s linear infinite}
body.light:before{background-image:linear-gradient(rgba(255,255,255,.10),rgba(255,255,255,.18)),var(--custom-bg,none),radial-gradient(circle at 20% 12%,#93c5fd99,transparent 30%),radial-gradient(circle at 90% 0,#c4b5fd99,transparent 34%),radial-gradient(circle at 45% 110%,#67e8f980,transparent 35%),linear-gradient(180deg,#eff6ff,#dbeafe 62%,#f8fafc);background-size:cover,cover,auto,auto,auto,auto;background-position:center}body.light:after{background:radial-gradient(circle,#33415526 0 1px,transparent 2px);background-size:86px 86px}
@keyframes twinkle{0%,100%{filter:brightness(1)}50%{filter:brightness(1.32)}}@keyframes starDrift{from{transform:translate3d(0,0,0)}to{transform:translate3d(86px,86px,0)}}
a{color:inherit;text-decoration:none}.layout{display:grid;grid-template-columns:270px 1fr;min-height:100vh}.side{position:sticky;top:0;height:100vh;padding:22px;background:linear-gradient(180deg,var(--card2),var(--card));border-right:1px solid var(--line);backdrop-filter:blur(26px);-webkit-backdrop-filter:blur(26px);box-shadow:20px 0 70px var(--shadow)}
.brand{display:flex;gap:12px;align-items:center;margin-bottom:20px}.brand .ico{width:50px;height:50px;border-radius:19px;display:grid;place-items:center;font-size:25px;background:linear-gradient(135deg,var(--blue),var(--purple));box-shadow:0 16px 40px #67e8f944}.brand b{display:block;font-size:28px;font-weight:1000;color:var(--text);text-shadow:0 2px 10px rgba(0,0,0,.22)}.brand span,.muted,.small{color:var(--muted)}.switches{display:grid;grid-template-columns:1fr;gap:8px;margin-bottom:18px}.themebtn{width:100%;padding:11px 12px;border-radius:16px;border:1px solid var(--line);background:var(--card);color:var(--text);font-weight:900;cursor:pointer;backdrop-filter:blur(16px)}
.nav a{display:flex;gap:10px;padding:12px;margin:8px 0;border:1px solid transparent;border-radius:16px}.nav a.active,.nav a:hover{background:var(--card2);border-color:var(--line);box-shadow:0 12px 30px var(--shadow)}
.main{padding:24px 28px 60px;max-width:1720px;width:100%;margin:auto}.top{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:20px}.top h1{margin:0;font-size:31px}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:16px}.grid2{display:grid;grid-template-columns:repeat(2,1fr);gap:16px}.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}.cardgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:16px}
.card{border:1px solid var(--line);background:linear-gradient(180deg,var(--card2),var(--card));border-radius:26px;padding:20px;box-shadow:0 24px 70px var(--shadow);backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px)}.kpi{position:relative;overflow:hidden}.kpi:after{content:"";position:absolute;right:-35px;top:-45px;width:145px;height:145px;border-radius:50%;background:#ffffff12}.kpi .label{color:var(--muted)}.kpi .value{font-size:36px;font-weight:950;margin-top:8px}.ok{color:var(--green)}.bad{color:var(--red)}.warn{color:var(--yellow)}
.badge{display:inline-flex;border:1px solid var(--line);border-radius:999px;padding:6px 10px;background:var(--card);font-size:13px}.table{width:100%;border-collapse:separate;border-spacing:0 10px}.table th{text-align:left;color:var(--muted);padding:0 12px}.table td{padding:14px 12px;background:var(--card);border-top:1px solid var(--line);border-bottom:1px solid var(--line);vertical-align:middle}.table td:first-child{border-left:1px solid var(--line);border-radius:16px 0 0 16px}.table td:last-child{border-right:1px solid var(--line);border-radius:0 16px 16px 0}
.progress{height:10px;background:#ffffff18;border-radius:99px;overflow:hidden;margin-top:7px}.bar{height:100%;background:linear-gradient(90deg,var(--green),var(--blue));border-radius:99px}.bar.warn{background:linear-gradient(90deg,var(--yellow),#fb923c)}.bar.bad{background:linear-gradient(90deg,#fb923c,var(--red))}
.btns{display:flex;gap:8px;flex-wrap:wrap}.btn,button{border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:13px;padding:9px 12px;font-weight:850;cursor:pointer}.btn.primary,button.primary{background:linear-gradient(135deg,var(--blue),var(--purple));color:#07111f;border:0}.btn.danger,button.danger{background:#fb718522;border-color:#fb718577;color:#fecdd3}
input,select,textarea{width:100%;padding:12px;border-radius:14px;border:1px solid var(--line);background:#00000030;color:var(--text)}body.light input,body.light select,body.light textarea{background:#ffffffb8}label{display:block;margin:13px 0 7px;color:var(--text);font-weight:850}.formgrid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px 16px}.flash{padding:12px 14px;border-radius:16px;margin-bottom:14px;border:1px solid var(--line);background:var(--card)}.flash.success{background:#34d39922;border-color:#34d39966}.flash.error{background:#fb718522;border-color:#fb718577}pre{white-space:pre-wrap;word-break:break-all;background:#00000038;border:1px solid var(--line);padding:16px;border-radius:18px}.small{font-size:13px;line-height:1.65}hr{border:0;border-top:1px solid var(--line);margin:18px 0}
.scrollbox{max-height:560px;overflow-y:auto;overflow-x:auto;padding-right:8px;border-radius:18px}.scrollbox.compact{max-height:360px}.scrollbox::-webkit-scrollbar{width:10px;height:10px}.scrollbox::-webkit-scrollbar-track{background:#ffffff12;border-radius:99px}.scrollbox::-webkit-scrollbar-thumb{background:linear-gradient(180deg,var(--blue),var(--purple));border-radius:99px}.hidden{display:none!important}
.servercard h3{margin:0 0 8px}.servercard .meta{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0}.flagimg{width:24px;height:18px;object-fit:cover;border-radius:4px;box-shadow:0 0 0 1px var(--line);vertical-align:-3px;margin-right:5px}.copyok{color:var(--green);font-size:13px;margin-left:8px}.toast{position:fixed;left:50%;bottom:32px;transform:translateX(-50%) translateY(30px);background:linear-gradient(135deg,var(--blue),var(--purple));color:#07111f;padding:13px 18px;border-radius:999px;font-weight:950;box-shadow:0 20px 60px var(--shadow);opacity:0;pointer-events:none;transition:.25s;z-index:9999}.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}

.progressrow{display:grid;grid-template-columns:56px 1fr 48px;align-items:center;gap:10px;margin:10px 0;font-size:15px;font-weight:850}.progressrow b{text-align:right}.bar{transition:width .6s ease}.ok-text{color:var(--green)!important;font-weight:950}.warn-text{color:var(--yellow)!important;font-weight:950}.danger-text{color:var(--red)!important;font-weight:950}.muted-text{color:var(--muted)!important}.servercard h3{font-size:19px;font-weight:950}.servercard,.table td{font-size:15px}.badge{font-weight:850}.kpi .value{font-weight:950}.themebtn,.btn,button{font-weight:950}

.server-title{display:flex!important;align-items:center!important;gap:7px!important;flex-wrap:nowrap!important;white-space:nowrap!important;overflow:hidden!important;text-overflow:ellipsis!important}
.server-title .dot{width:14px;height:14px;border-radius:50%;display:inline-block;flex:0 0 auto;box-shadow:0 0 0 2px rgba(0,0,0,.25)}
.server-title .dot.online{background:#16c75f}.server-title .dot.offline{background:#ff4d5e}.server-title .name{overflow:hidden;text-overflow:ellipsis}
.flagimg{flex:0 0 auto}
body.has-custom-bg:before{background-image:linear-gradient(rgba(0,0,0,.12),rgba(0,0,0,.25)),var(--custom-bg),radial-gradient(1px 1px at 7% 14%,#fff,transparent),radial-gradient(circle at 15% 8%,#1d4e8975,transparent 32%),radial-gradient(circle at 95% 0,#7c3aed70,transparent 35%),linear-gradient(180deg,#07111f,#020617 72%)!important;background-size:cover,cover,auto,auto,auto,auto!important;background-position:center!important}
body.has-custom-bg.login-bg:before{background-image:linear-gradient(rgba(0,0,0,.12),rgba(0,0,0,.25)),var(--custom-bg),radial-gradient(1px 1px at 8% 12%,#fff,transparent),radial-gradient(circle at 18% 18%,#1d4ed875,transparent 34%),radial-gradient(circle at 82% 4%,#7c3aed70,transparent 38%),linear-gradient(180deg,#07111f,#020617 72%)!important;background-size:cover,cover,auto,auto,auto,auto!important;background-position:center!important}


/* readable text patch */
body{font-size:16px!important;font-weight:720!important;text-shadow:0 1px 2px rgba(0,0,0,.22)}
.muted,.small,.label,.table th{color:rgba(237,245,255,.88)!important;font-weight:760!important}
html.light .muted,html.light .small,html.light .label,html.light .table th{color:rgba(15,23,42,.78)!important}
.card,.table td,.badge,input,select,textarea,pre{font-size:15.5px!important}
.table td{line-height:1.68!important}
.scrollbox{background:rgba(0,0,0,.05);padding:10px}
.event-content{line-height:1.8!important;word-break:break-word}
.event-context{margin-top:8px;padding:8px 10px;border-radius:12px;background:rgba(52,211,153,.13);border:1px solid rgba(52,211,153,.35);color:var(--text);font-weight:900}







/* realtime komari-like refresh patch */
.bar{transition:width .85s cubic-bezier(.22,.61,.36,1), background .25s ease!important;position:relative;overflow:hidden}
.bar:after{content:"";position:absolute;inset:0;background:linear-gradient(90deg,transparent,rgba(255,255,255,.45),transparent);transform:translateX(-120%);animation:barShine 1.8s linear infinite}
@keyframes barShine{to{transform:translateX(120%)}}
.server-title .dot.online{animation:livePulse 1.6s ease-in-out infinite}
@keyframes livePulse{0%,100%{box-shadow:0 0 0 2px rgba(0,0,0,.25),0 0 0 0 rgba(52,211,153,.55)}50%{box-shadow:0 0 0 2px rgba(0,0,0,.25),0 0 0 8px rgba(52,211,153,0)}}
.live-updated{font-size:12px;color:var(--muted);font-weight:850;margin-top:6px}
.realtime-chip{display:inline-flex;align-items:center;gap:5px;border:1px solid var(--line);background:var(--card);border-radius:999px;padding:4px 8px;font-size:12px;font-weight:900;color:var(--muted)}

.layout.{grid-template-columns:1fr}.layout. .side .small,.layout. .switches,.layout. .brand b{display:block!important}.layout. .nav a{font-size:inherit;justify-content:flex-start}}


/* realtime komari-like refresh patch */
.bar{transition:width .85s cubic-bezier(.22,.61,.36,1), background .25s ease!important;position:relative;overflow:hidden}
.bar:after{content:"";position:absolute;inset:0;background:linear-gradient(90deg,transparent,rgba(255,255,255,.45),transparent);transform:translateX(-120%);animation:barShine 1.8s linear infinite}
@keyframes barShine{to{transform:translateX(120%)}}
.server-title .dot.online{animation:livePulse 1.6s ease-in-out infinite}
@keyframes livePulse{0%,100%{box-shadow:0 0 0 2px rgba(0,0,0,.25),0 0 0 0 rgba(52,211,153,.55)}50%{box-shadow:0 0 0 2px rgba(0,0,0,.25),0 0 0 8px rgba(52,211,153,0)}}
.live-updated{font-size:12px;color:var(--muted);font-weight:850;margin-top:6px}
.realtime-chip{display:inline-flex;align-items:center;gap:5px;border:1px solid var(--line);background:var(--card);border-radius:999px;padding:4px 8px;font-size:12px;font-weight:900;color:var(--muted)}


/* Komari-like realtime bars */
.progress{position:relative}
.bar{transition:width .75s cubic-bezier(.22,.61,.36,1),background .2s ease!important;position:relative;overflow:hidden}
.bar:after{content:"";position:absolute;top:0;bottom:0;width:45%;left:-55%;background:linear-gradient(90deg,transparent,rgba(255,255,255,.42),transparent);animation:barFlow 1.4s linear infinite}
@keyframes barFlow{to{left:110%}}
.server-title .dot.online{animation:dotPulse 1.45s ease-in-out infinite}
@keyframes dotPulse{0%,100%{box-shadow:0 0 0 2px rgba(0,0,0,.25),0 0 0 0 rgba(52,211,153,.55)}50%{box-shadow:0 0 0 2px rgba(0,0,0,.25),0 0 0 8px rgba(52,211,153,0)}}


/* accurate realtime data patch */
.bar{transition:width .75s cubic-bezier(.22,.61,.36,1),background .2s ease!important;position:relative;overflow:hidden}
.bar:after{content:"";position:absolute;top:0;bottom:0;width:45%;left:-55%;background:linear-gradient(90deg,transparent,rgba(255,255,255,.42),transparent);animation:barFlow 1.4s linear infinite}
@keyframes barFlow{to{left:110%}}
.server-title .dot.online{animation:dotPulse 1.45s ease-in-out infinite}
@keyframes dotPulse{0%,100%{box-shadow:0 0 0 2px rgba(0,0,0,.25),0 0 0 0 rgba(52,211,153,.55)}50%{box-shadow:0 0 0 2px rgba(0,0,0,.25),0 0 0 8px rgba(52,211,153,0)}}
.metric-source{margin-top:7px;font-size:12px;color:var(--muted)!important;font-weight:850;line-height:1.55}
.metric-source.stale{color:var(--muted)!important}


/* fullchain realtime metric rows */
.metric-extra{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:10px}
.metric-extra .mini{border:1px solid var(--line);background:var(--card);border-radius:13px;padding:7px 9px;font-size:12px;line-height:1.55;font-weight:850;color:var(--text)}
.metric-extra .mini b{display:block;color:var(--muted);font-size:12px;margin-bottom:2px}
.metric-source{margin-top:7px;font-size:12px;color:var(--muted)!important;font-weight:850;line-height:1.55}
.bar{transition:width .75s cubic-bezier(.22,.61,.36,1),background .2s ease!important;position:relative;overflow:hidden}
.bar:after{content:"";position:absolute;top:0;bottom:0;width:45%;left:-55%;background:linear-gradient(90deg,transparent,rgba(255,255,255,.42),transparent);animation:barFlow 1.4s linear infinite}
@keyframes barFlow{to{left:110%}}
.server-title .dot.online{animation:dotPulse 1.45s ease-in-out infinite}
@keyframes dotPulse{0%,100%{box-shadow:0 0 0 2px rgba(0,0,0,.25),0 0 0 0 rgba(52,211,153,.55)}50%{box-shadow:0 0 0 2px rgba(0,0,0,.25),0 0 0 8px rgba(52,211,153,0)}}
@media(max-width:760px){.metric-extra{grid-template-columns:1fr}}


/* readability contrast fix */
html:not(.light) body,
html:not(.light) .card,
html:not(.light) .table td,
html:not(.light) .badge,
html:not(.light) .small,
html:not(.light) .muted,
html:not(.light) .metric-extra .mini,
html:not(.light) .metric-extra .mini b{
  color:#f8fbff!important;
  text-shadow:0 1px 3px rgba(0,0,0,.75)!important;
}
html:not(.light) .card{
  background:linear-gradient(180deg,rgba(25,38,55,.72),rgba(18,30,45,.62))!important;
  border-color:rgba(255,255,255,.24)!important;
}
html:not(.light) .metric-extra .mini,
html:not(.light) .badge{
  background:rgba(15,25,38,.55)!important;
}
html:not(.light) .progress{
  background:rgba(255,255,255,.15)!important;
}
html.light body,
html.light .card,
html.light .table td,
html.light .badge,
html.light .small,
html.light .muted,
html.light .metric-extra .mini,
html.light .metric-extra .mini b{
  text-shadow:none!important;
}
.servercard,.card{font-weight:850}
.metric-extra .mini{min-height:58px}
.metric-extra .mini b{opacity:.88}


/* final overview readability + realtime */
.metric-extra{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:10px}
.metric-extra .mini{border:1px solid var(--line);background:rgba(15,25,38,.50);border-radius:13px;padding:7px 9px;font-size:12px;line-height:1.55;font-weight:850;color:#f8fbff;text-shadow:0 1px 3px rgba(0,0,0,.65)}
.metric-extra .mini b{display:block;color:#eaf3ff;font-size:12px;margin-bottom:2px;opacity:.95}
html.light .metric-extra .mini{background:rgba(255,255,255,.78);color:#0f172a;text-shadow:none}
html.light .metric-extra .mini b{color:#334155}
.bar{transition:width .75s cubic-bezier(.22,.61,.36,1),background .2s ease!important;position:relative;overflow:hidden}
.bar:after{content:"";position:absolute;top:0;bottom:0;width:45%;left:-55%;background:linear-gradient(90deg,transparent,rgba(255,255,255,.42),transparent);animation:barFlow 1.4s linear infinite}
@keyframes barFlow{to{left:110%}}
.server-title .dot.online{animation:dotPulse 1.45s ease-in-out infinite}
@keyframes dotPulse{0%,100%{box-shadow:0 0 0 2px rgba(0,0,0,.25),0 0 0 0 rgba(52,211,153,.55)}50%{box-shadow:0 0 0 2px rgba(0,0,0,.25),0 0 0 8px rgba(52,211,153,0)}}
@media(max-width:760px){.metric-extra{grid-template-columns:1fr}}


/* ===== glass transparency restore patch: only visual, no logic changes ===== */

/* 夜间透明：恢复玻璃感，不能太实 */
html:not(.light) body:not(.solid) .card,
html:not(.light) body:not(.solid) .servercard,
html:not(.light) body:not(.solid) .metric-extra .mini,
html:not(.light) body:not(.solid) .badge,
html:not(.light) body:not(.solid) input,
html:not(.light) body:not(.solid) select,
html:not(.light) body:not(.solid) textarea,
html:not(.light) body:not(.solid) pre {
  background: rgba(18, 32, 48, .38) !important;
  border-color: rgba(255,255,255,.22) !important;
  backdrop-filter: blur(20px) saturate(145%) !important;
  -webkit-backdrop-filter: blur(20px) saturate(145%) !important;
}

/* 夜间实色：仍保持较深，但不影响透明模式 */
html:not(.light) body.solid .card,
html:not(.light) body.solid .servercard,
html:not(.light) body.solid .metric-extra .mini,
html:not(.light) body.solid .badge {
  background: rgba(23, 36, 54, .88) !important;
  border-color: rgba(255,255,255,.16) !important;
  backdrop-filter: none !important;
  -webkit-backdrop-filter: none !important;
}

/* 日间透明：更通透，但文字保持深色 */
html.light body:not(.solid) .card,
html.light body:not(.solid) .servercard,
html.light body:not(.solid) .metric-extra .mini,
html.light body:not(.solid) .badge,
html.light body:not(.solid) input,
html.light body:not(.solid) select,
html.light body:not(.solid) textarea,
html.light body:not(.solid) pre {
  background: rgba(255,255,255,.46) !important;
  border-color: rgba(15,23,42,.16) !important;
  backdrop-filter: blur(18px) saturate(145%) !important;
  -webkit-backdrop-filter: blur(18px) saturate(145%) !important;
}

/* 日间实色 */
html.light body.solid .card,
html.light body.solid .servercard,
html.light body.solid .metric-extra .mini,
html.light body.solid .badge {
  background: rgba(255,255,255,.90) !important;
  border-color: rgba(15,23,42,.14) !important;
}

/* 夜间字体增强：透明背景下仍然清楚 */
html:not(.light) body,
html:not(.light) .card,
html:not(.light) .servercard,
html:not(.light) .table td,
html:not(.light) .table th,
html:not(.light) .small,
html:not(.light) .muted,
html:not(.light) .metric-extra .mini,
html:not(.light) .metric-extra .mini b,
html:not(.light) label,
html:not(.light) p,
html:not(.light) h1,
html:not(.light) h2,
html:not(.light) h3 {
  color: #f7fbff !important;
  text-shadow: 0 1px 3px rgba(0,0,0,.72) !important;
}

html:not(.light) .muted,
html:not(.light) .small,
html:not(.light) .metric-extra .mini b {
  color: rgba(238,246,255,.86) !important;
}

/* 日间字体：使用深色，不吃背景 */
html.light body,
html.light .card,
html.light .servercard,
html.light .table td,
html.light .table th,
html.light .small,
html.light .muted,
html.light .metric-extra .mini,
html.light .metric-extra .mini b,
html.light label,
html.light p,
html.light h1,
html.light h2,
html.light h3 {
  color: #0f172a !important;
  text-shadow: none !important;
}

html.light .muted,
html.light .small,
html.light .metric-extra .mini b {
  color: rgba(15,23,42,.72) !important;
}

/* 价格/到期/永久免费 badge：夜间自动高对比 */
html:not(.light) .badge,
html:not(.light) .price,
html:not(.light) [class*="price"],
html:not(.light) [class*="expire"] {
  color: #f8fbff !important;
  text-shadow: 0 1px 3px rgba(0,0,0,.75) !important;
}

/* 日间价格/到期/永久免费 badge：深色清晰 */
html.light .badge,
html.light .price,
html.light [class*="price"],
html.light [class*="expire"] {
  color: #0f172a !important;
  text-shadow: none !important;
}

/* 强调色保留：免费、金额、剩余天数在两种模式都看得清楚 */
html:not(.light) .badge b,
html:not(.light) .badge strong {
  color: #ffffff !important;
}
html.light .badge b,
html.light .badge strong {
  color: #0f172a !important;
}

/* 绿色/黄色/红色告警文字在玻璃上清楚 */
html:not(.light) .good,
html:not(.light) .ok {
  color: #55f0a8 !important;
}
html:not(.light) .warn {
  color: #ffd166 !important;
}
html:not(.light) .bad,
html:not(.light) .danger {
  color: #ff8a8a !important;
}
html.light .good,
html.light .ok {
  color: #047857 !important;
}
html.light .warn {
  color: #b7791f !important;
}
html.light .bad,
html.light .danger {
  color: #b91c1c !important;
}

/* 进度条底色在透明模式下不要太淡 */
html:not(.light) .progress {
  background: rgba(255,255,255,.16) !important;
}
html.light .progress {
  background: rgba(15,23,42,.13) !important;
}

/* 服务器卡片不要被之前的深色补丁压成实色 */
html:not(.light) body:not(.solid) .servercard.card,
html:not(.light) body:not(.solid) .card.servercard {
  background: linear-gradient(180deg, rgba(30,45,65,.42), rgba(18,30,45,.34)) !important;
}

/* 日间透明服务器卡片 */
html.light body:not(.solid) .servercard.card,
html.light body:not(.solid) .card.servercard {
  background: linear-gradient(180deg, rgba(255,255,255,.52), rgba(255,255,255,.36)) !important;
}

/* ===== end glass transparency restore patch ===== */


/* ===== colorful readable badge/price/status restore patch ===== */

/* 不再让所有 badge/价格都变黑白：恢复多彩视觉 */
.badge,
.price,
[class*="price"],
[class*="expire"],
.good,
.ok,
.warn,
.bad,
.danger,
.server-status,
.status,
.event-context {
  text-shadow: none;
}

/* 夜间：基础文字仍清楚，但允许彩色元素有自己的颜色 */
html:not(.light) .badge {
  background: rgba(18,32,48,.48) !important;
  border: 1px solid rgba(255,255,255,.22) !important;
  color: #eef7ff !important;
  text-shadow: 0 1px 3px rgba(0,0,0,.55) !important;
}

/* 日间 badge */
html.light .badge {
  background: rgba(255,255,255,.62) !important;
  border: 1px solid rgba(15,23,42,.16) !important;
  color: #0f172a !important;
  text-shadow: none !important;
}

/* 价格：蓝紫渐变，夜间日间都清楚 */
.badge.price,
.price,
[class*="price"] {
  background: linear-gradient(135deg, rgba(96,165,250,.32), rgba(168,85,247,.28)) !important;
  border-color: rgba(147,197,253,.45) !important;
  color: #dbeafe !important;
  font-weight: 950 !important;
}
html.light .badge.price,
html.light .price,
html.light [class*="price"] {
  background: linear-gradient(135deg, rgba(59,130,246,.18), rgba(168,85,247,.15)) !important;
  border-color: rgba(59,130,246,.32) !important;
  color: #1d4ed8 !important;
}

/* 免费 / 永久免费：绿色 */
.badge.free,
.badge.forever {
  background: linear-gradient(135deg, rgba(34,197,94,.30), rgba(16,185,129,.24)) !important;
  border-color: rgba(74,222,128,.45) !important;
  color: #bbf7d0 !important;
}
html.light .badge.free,
html.light .badge.forever {
  background: linear-gradient(135deg, rgba(34,197,94,.17), rgba(16,185,129,.13)) !important;
  border-color: rgba(22,163,74,.30) !important;
  color: #047857 !important;
}

/* 到期/续费：紫色 */
.badge.expire,
[class*="expire"] {
  background: linear-gradient(135deg, rgba(168,85,247,.30), rgba(236,72,153,.22)) !important;
  border-color: rgba(216,180,254,.42) !important;
  color: #f3e8ff !important;
  font-weight: 950 !important;
}
html.light .badge.expire,
html.light [class*="expire"] {
  background: linear-gradient(135deg, rgba(168,85,247,.15), rgba(236,72,153,.12)) !important;
  border-color: rgba(168,85,247,.28) !important;
  color: #7e22ce !important;
}

/* 在线/正常：绿色 */
.good,
.ok,
.online,
.badge.good,
.badge.ok {
  color: #4ade80 !important;
}
html.light .good,
html.light .ok,
html.light .online,
html.light .badge.good,
html.light .badge.ok {
  color: #047857 !important;
}

/* 警告：黄色/橙色 */
.warn,
.badge.warn {
  color: #facc15 !important;
}
html.light .warn,
html.light .badge.warn {
  color: #b45309 !important;
}

/* 危险/离线/过期：红色 */
.bad,
.danger,
.offline,
.badge.bad,
.badge.danger {
  color: #fb7185 !important;
}
html.light .bad,
html.light .danger,
html.light .offline,
html.light .badge.bad,
html.light .badge.danger {
  color: #b91c1c !important;
}

/* 进度条恢复多彩：绿/黄/红 */
.bar {
  background: linear-gradient(90deg, #22c55e, #14b8a6) !important;
}
.bar.warn {
  background: linear-gradient(90deg, #f59e0b, #facc15) !important;
}
.bar.bad {
  background: linear-gradient(90deg, #ef4444, #fb7185) !important;
}

/* 实时小卡片恢复彩色边框和文字 */
.metric-extra .mini:nth-child(1) {
  border-color: rgba(96,165,250,.35) !important;
}
.metric-extra .mini:nth-child(1) b {
  color: #93c5fd !important;
}
.metric-extra .mini:nth-child(2) {
  border-color: rgba(34,197,94,.35) !important;
}
.metric-extra .mini:nth-child(2) b {
  color: #86efac !important;
}
.metric-extra .mini:nth-child(3) {
  border-color: rgba(168,85,247,.35) !important;
}
.metric-extra .mini:nth-child(3) b {
  color: #d8b4fe !important;
}
html.light .metric-extra .mini:nth-child(1) b {
  color: #2563eb !important;
}
html.light .metric-extra .mini:nth-child(2) b {
  color: #047857 !important;
}
html.light .metric-extra .mini:nth-child(3) b {
  color: #7e22ce !important;
}

/* 表格/事件里的类型颜色 */
.event-context {
  background: linear-gradient(135deg, rgba(14,165,233,.18), rgba(34,197,94,.12)) !important;
  border-color: rgba(56,189,248,.35) !important;
  color: #e0f2fe !important;
}
html.light .event-context {
  background: linear-gradient(135deg, rgba(14,165,233,.12), rgba(34,197,94,.09)) !important;
  border-color: rgba(14,165,233,.26) !important;
  color: #075985 !important;
}

/* 图标和标题恢复层次 */
.server-title b,
.brand b,
h1,
h2,
h3 {
  background: linear-gradient(90deg, #7dd3fc, #c4b5fd, #86efac);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent !important;
  text-shadow: none !important;
}
html.light .server-title b,
html.light .brand b,
html.light h1,
html.light h2,
html.light h3 {
  background: linear-gradient(90deg, #2563eb, #7e22ce, #047857);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent !important;
}

/* 但按钮文字不要透明 */
button,
.btn,
.nav a {
  color: inherit;
  -webkit-background-clip: initial;
  background-clip: initial;
}

/* 如果没有 class，常见价格/永久文本所在 badge 也给一点彩色阴影 */
.badge {
  box-shadow: 0 8px 24px rgba(56,189,248,.08) !important;
}

/* ===== end colorful readable badge/price/status restore patch ===== */

@media(max-width:1100px){.layout{grid-template-columns:1fr}.side{height:auto;position:relative}.nav{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}.nav a{margin:0}.grid{grid-template-columns:repeat(2,1fr)}.grid2,.grid3,.formgrid{grid-template-columns:1fr}}@media(max-width:640px){.main{padding:16px}.grid{grid-template-columns:1fr}.top{display:block}.cardgrid{grid-template-columns:1fr}}
</style><script>



function applyTheme(){
  let theme=localStorage.getItem('theme')||'dark';
  let glass=localStorage.getItem('glass')||'glass';
  document.documentElement.classList.toggle('light',theme==='light');
  document.documentElement.classList.toggle('solid',glass==='solid');
  document.body.classList.toggle('light',theme==='light');
  document.body.classList.toggle('solid',glass==='solid');
  let a=document.getElementById('themeText'),b=document.getElementById('glassText');
  if(a)a.textContent=theme==='light'?'☀️ 当前：日间明亮':'🌙 当前：夜间星空';
  if(b)b.textContent=glass==='solid'?'⬛ 当前：实色背景':'🧊 当前：透明玻璃';
}
function toggleTheme(){
  localStorage.setItem('theme',(localStorage.getItem('theme')||'dark')==='dark'?'light':'dark');
  applyTheme();
}
function toggleGlass(){
  localStorage.setItem('glass',(localStorage.getItem('glass')||'glass')==='glass'?'solid':'glass');
  applyTheme();
}
function setView(v){
  localStorage.setItem('serverView',v);
  document.querySelectorAll('[data-view-table]').forEach(e=>e.classList.toggle('hidden',v!=='table'));
  document.querySelectorAll('[data-view-card]').forEach(e=>e.classList.toggle('hidden',v!=='card'));
}
function initView(){setView(localStorage.getItem('serverView')||'card');}
async function copyText(id){
  let t=document.getElementById(id);
  if(!t)return;
  let txt=t.innerText||t.textContent||'';
  let ok=false;
  try{
    if(navigator.clipboard && window.isSecureContext){
      await navigator.clipboard.writeText(txt);
      ok=true;
    }
  }catch(e){}
  if(!ok){
    try{
      let a=document.createElement('textarea');
      a.value=txt;
      a.setAttribute('readonly','');
      a.style.position='fixed';
      a.style.left='-9999px';
      a.style.top='0';
      document.body.appendChild(a);
      a.focus();
      a.select();
      ok=document.execCommand('copy');
      document.body.removeChild(a);
    }catch(e){ok=false;}
  }
  let o=document.getElementById(id+'ok');
  if(o){
    o.textContent=ok?'✅ 已复制成功':'⚠️ 自动复制失败，请手动复制';
    setTimeout(()=>o.textContent='',2600);
  }
  showToast(ok?'✅ 探针命令已复制成功':'⚠️ 自动复制失败，请手动选择命令复制');
}
function showToast(msg){
  let x=document.getElementById('toast');
  if(!x){
    x=document.createElement('div');
    x.id='toast';
    x.className='toast';
    document.body.appendChild(x);
  }
  x.textContent=msg;
  x.classList.add('show');
  setTimeout(()=>x.classList.remove('show'),2600);
}
function fmtBytes(n){
  n=Number(n||0);
  if(!n)return '0B';
  let u=['B','KB','MB','GB','TB','PB'];
  let i=0;
  while(n>=1024&&i<u.length-1){n/=1024;i++;}
  return n.toFixed(1)+u[i];
}
function qsAll(sel){return Array.from(document.querySelectorAll(sel));}
function setText(sel,txt){qsAll(sel).forEach(e=>e.textContent=txt);}
function setHtml(sel,txt){qsAll(sel).forEach(e=>e.innerHTML=txt);}
function setBar(id,key,val){
  let v=Number(val||0);
  qsAll('[data-'+key+'="'+id+'"]').forEach(e=>{
    e.style.width=Math.max(0,Math.min(100,v))+'%';
    e.classList.toggle('bad',v>=80);
    e.classList.toggle('warn',v>=50&&v<80);
  });
  setText('[data-'+key+'txt="'+id+'"]',Math.round(v)+'%');
}
function paintServer(s){
  setBar(s.id,'cpu',s.cpu);
  setBar(s.id,'mem',s.mem);
  setBar(s.id,'swap',s.swap);
  setBar(s.id,'disk',s.disk);
  setText('[data-uptime="'+s.id+'"]',s.uptime||'未知');
  setText('[data-status="'+s.id+'"]',(s.online?'🟢 ':'🔴 ')+(s.status||'未知'));
  setHtml('[data-hw="'+s.id+'"]',s.config_html||'');
  setHtml('[data-netspeed="'+s.id+'"]',s.net_speed_html||'↑ 0B/s&nbsp;&nbsp;↓ 0B/s');
  setHtml('[data-traffic="'+s.id+'"]',s.traffic_html||'↑ 0B&nbsp;&nbsp;↓ 0B');
  setHtml('[data-load="'+s.id+'"]',s.load_html||'0.00 ｜ 0.00 ｜ 0.00');
  setText('[data-cpucores="'+s.id+'"]',(s.cpu_cores||'?')+' Cores');
  setText('[data-memused="'+s.id+'"]',fmtBytes(s.mem_used));
  setText('[data-memtotal="'+s.id+'"]',fmtBytes(s.mem_total));
  setText('[data-swapused="'+s.id+'"]',fmtBytes(s.swap_used));
  setText('[data-swaptotal="'+s.id+'"]',s.swap_total?fmtBytes(s.swap_total):'无');
  setText('[data-diskused="'+s.id+'"]',fmtBytes(s.disk_used));
  setText('[data-disktotal="'+s.id+'"]',fmtBytes(s.disk_total));
  setText('[data-updated="'+s.id+'"]',s.updated_at||'未知');
  qsAll('[data-live-dot="'+s.id+'"]').forEach(e=>{
    e.classList.toggle('online',!!s.online);
    e.classList.toggle('offline',!s.online);
  });
}
async function live(){
  try{
    let r=await fetch('/api/servers-live?t='+Date.now(),{cache:'no-store',headers:{'Cache-Control':'no-cache'}});
    let j=await r.json();
    (j.servers||[]).forEach(paintServer);
  }catch(e){}
}
async function refreshKpi(){
  try{
    let j=await(await fetch('/api/summary')).json();
    for(let k of ['total','online','offline','probes','expiring','expired']){
      let e=document.querySelector('[data-kpi="'+k+'"]');
      if(e)e.textContent=j[k];
    }
    let t=document.querySelector('[data-now]');
    if(t)t.textContent=j.time;
  }catch(e){}
}
function delok(){return confirm('确认删除服务器？');}
document.addEventListener('DOMContentLoaded',()=>{
  applyTheme();
  initView();
  live();
  refreshKpi();
  setInterval(live,1000);
  setInterval(refreshKpi,3000);
});


function setLocalText(sel,txt){document.querySelectorAll(sel).forEach(e=>e.textContent=txt)}
function setLocalHtml(sel,txt){document.querySelectorAll(sel).forEach(e=>e.innerHTML=txt)}
function setLocalBar(sel,val){
  let v=Number(val||0);
  document.querySelectorAll(sel).forEach(e=>{
    e.style.width=Math.max(0,Math.min(100,v))+'%';
    e.classList.toggle('bad',v>=80);
    e.classList.toggle('warn',v>=50&&v<80);
  });
}
async function localLive(){
  if(!document.querySelector('[data-localcpu],[data-localmem],[data-localswap],[data-localdisk],[data-local-netspeed]')) return;
  try{
    let r=await fetch('/api/local-live?t='+Date.now(),{cache:'no-store',headers:{'Cache-Control':'no-cache'}});
    let j=await r.json();

    setLocalBar('[data-localcpu]',j.cpu);
    setLocalText('[data-localcputxt]',Math.round(j.cpu||0)+'%');

    setLocalBar('[data-localmem]',j.mem);
    setLocalText('[data-localmemtxt]',Math.round(j.mem||0)+'%');

    setLocalBar('[data-localswap]',j.swap);
    setLocalText('[data-localswaptxt]',Math.round(j.swap||0)+'%');

    setLocalBar('[data-localdisk]',j.disk);
    setLocalText('[data-localdisktxt]',Math.round(j.disk||0)+'%');

    setLocalHtml('[data-local-netspeed]',j.net_speed_html||'↑ 0B/s&nbsp;&nbsp;↓ 0B/s');
    setLocalHtml('[data-local-traffic]',j.traffic_html||'↑ 0B&nbsp;&nbsp;↓ 0B');
    setLocalHtml('[data-local-load]',j.load_html||'0.00 ｜ 0.00 ｜ 0.00');
    setLocalText('[data-local-uptime]',j.uptime||'未知');
  }catch(e){}
}
setInterval(localLive,1000);
localLive();

</script></head><body><script>document.body.style.setProperty('--custom-bg',"url('/theme-bg?v={{now}}')");fetch('/theme-bg?v={{now}}',{cache:'no-store'}).then(r=>{if(r.ok)document.body.classList.add('has-custom-bg')}).catch(()=>{});</script><div class=layout><aside class=side><div class=brand><div class=ico>🛡️</div><div><b>{{site_name_value()}}</b></div></div><div class=switches><button class=themebtn onclick="toggleTheme()" type=button><span id=themeText>🌙 当前：夜间星空</span></button><button class=themebtn onclick="toggleGlass()" type=button><span id=glassText>🧊 当前：透明玻璃</span></button></div><nav class=nav><a class="{{'active' if active=='dashboard' else ''}}" href="/">📊 总览大屏</a><a class="{{'active' if active=='servers' else ''}}" href="/servers">🖥️ 服务器</a><a class="{{'active' if active=='add' else ''}}" href="/servers/add">➕ 添加服务器</a><a class="{{'active' if active=='local' else ''}}" href="/local">🏠 本机</a><a class="{{'active' if active=='events' else ''}}" href="/events">🧾 事件</a><a class="{{'active' if active=='settings' else ''}}" href="/settings">⚙️ 设置</a><a href="/logout">🚪 退出</a></nav><div class=small style="margin-top:22px">👤 {{username}}<br>🕒 <span data-now>{{now}}</span><br>🌌 星空 · 🧊 玻璃 · 🇺🇳 国旗</div></aside><main class=main>{% with messages=get_flashed_messages(with_categories=true) %}{% for c,m in messages %}<div class="flash {{c}}">{{m}}</div>{% endfor %}{% endwith %}{{body|safe}}</main></div></body></html>'''
DASH='''<div class=top><h1>📊✨ 服务器总览大屏</h1><div class=btns><button onclick="setView('card')">🔳 卡片视图</button><button onclick="setView('table')">📋 表格视图</button><a class="btn primary" href="/servers/add">➕ 添加服务器</a></div></div>
<div class=grid><div class="card kpi"><div class=label>📦 总数</div><div class=value data-kpi=total>{{data.total}}</div></div><div class="card kpi"><div class=label>🟢 在线</div><div class="value ok" data-kpi=online>{{data.online}}</div></div><div class="card kpi"><div class=label>🔴 离线</div><div class="value bad" data-kpi=offline>{{data.offline}}</div></div><div class="card kpi"><div class=label>📡 探针在线</div><div class=value data-kpi=probes>{{data.probes}}</div></div></div>
<div class=grid2 style="margin-top:16px"><div class=card><h2>🏠 本机状态</h2><p><span class=badge>🌐 {{local.public_ip or '未知'}}</span> <span class=badge>🧩 {{local.cpu_count or 0}} 核</span> <span class=badge>⏱️ <span data-local-uptime>{{local.uptime or '未知'}}</span></span></p><hr>{{progress_row('CPU',0,'localcpu',local.cpu or 0,90)|safe}}{{progress_row('内存',0,'localmem',local.mem_percent or 0,90)|safe}}{{progress_row('SWAP',0,'localswap',local.swap_percent or 0,80)|safe}}{{progress_row('硬盘',0,'localdisk',local.disk_percent or 0,90)|safe}}<div class="metric-extra" style="margin-top:14px">
  <div class="mini"><b>网络</b><span data-local-netspeed>↑ 0B/s&nbsp;&nbsp;↓ 0B/s</span></div>
  <div class="mini"><b>流量</b><span data-local-traffic>↑ 0B&nbsp;&nbsp;↓ 0B</span></div>
  <div class="mini"><b>负载</b><span data-local-load>0.00 ｜ 0.00 ｜ 0.00</span></div>
</div></div><div class=card><h2>⏰ 到期和风险</h2><div class=grid3><div class=card><div class=label>⚠️ 7天内到期</div><div class="value warn" data-kpi=expiring>{{data.expiring}}</div></div><div class=card><div class=label>🚨 已过期</div><div class="value bad" data-kpi=expired>{{data.expired}}</div></div><div class=card><div class=label>⚪ 未知</div><div class=value data-kpi=unknown>{{data.unknown}}</div></div></div></div></div>
<div class=card style="margin-top:16px"><h2>🖥️ 所有服务器</h2>
<div data-view-card class=cardgrid>{% for s in data.servers %}{% set m=s.metrics %}
<div class="card servercard"><h3 class="server-title"><span data-live-dot="{{s.id}}" class="dot {{'online' if s.online else 'offline'}}"></span>{{flag_icon(s)|safe}}<span class="name">{{s.name}}</span></h3><div class=meta><span class=badge>ID{{s.id}}</span><span class=badge>{{s.host}}:{{s.check_port}}</span><span class=badge>{{s.location_cn or s.location}}</span></div><div style="display:flex;gap:8px;flex-wrap:wrap;margin:8px 0"><span class="badge {{status_color_class_by_days(s)}}">{{display_price_label(s)}}</span><span class="badge {{status_color_class_by_days(s)}}">{{display_expire_label(s)}}</span><span class=badge>⏱️ <span data-uptime="{{s.id}}">{{duration(m.uptime_seconds or 0)}}</span></span></div><div class=small data-hw="{{s.id}}">{{metric_config_html(m)|safe}}</div><div class="metric-extra"><div class="mini"><b>网络</b><span data-netspeed="{{s.id}}">↑ 0B/s&nbsp;&nbsp;↓ 0B/s</span></div><div class="mini"><b>流量</b><span data-traffic="{{s.id}}">↑ 0B&nbsp;&nbsp;↓ 0B</span></div><div class="mini"><b>负载</b><span data-load="{{s.id}}">0.00 ｜ 0.00 ｜ 0.00</span></div></div><hr>{{progress_row('CPU',s.id,'cpu',m.cpu_percent or 0,s.cpu_alert or 90)|safe}}{{progress_row('内存',s.id,'mem',m.mem_percent or 0,s.mem_alert or 90)|safe}}{{progress_row('SWAP',s.id,'swap',m.swap_percent or 0,80)|safe}}{{progress_row('硬盘',s.id,'disk',m.disk_percent or 0,s.disk_alert or 90)|safe}}<br><a class=btn href="/servers/{{s.id}}">详情</a></div>
{% else %}<div class=card>📭 暂无服务器</div>{% endfor %}</div>
<div data-view-table class=hidden><table class=table><thead><tr><th>服务器</th><th>状态/在线时长</th><th>资源进度</th><th>费用/到期</th><th>操作</th></tr></thead><tbody>{% for s in data.servers %}{% set m=s.metrics %}<tr><td><b>{{flag_icon(s)|safe}} {{s.name}}</b><br><span class=muted>ID{{s.id}}｜{{s.host}}:{{s.check_port}}｜{{s.location_cn or s.location}}</span></td><td><span data-status="{{s.id}}">{{'🟢 在线' if s.online else '🔴 离线'}}</span><br>⏱️ <span data-uptime="{{s.id}}">{{duration(m.uptime_seconds or 0)}}</span></td><td>{{progress_row('CPU',s.id,'cpu',m.cpu_percent or 0,s.cpu_alert or 90)|safe}}{{progress_row('内存',s.id,'mem',m.mem_percent or 0,s.mem_alert or 90)|safe}}{{progress_row('SWAP',s.id,'swap',m.swap_percent or 0,80)|safe}}{{progress_row('硬盘',s.id,'disk',m.disk_percent or 0,s.disk_alert or 90)|safe}}</td><td><span class="{{status_color_class_by_days(s)}}">{{display_price_label(s)}}</span><br><span class="{{status_color_class_by_days(s)}}">{{display_expire_label(s)}}</span></td><td><a class=btn href="/servers/{{s.id}}">详情</a></td></tr>{% else %}<tr><td colspan=5>📭 暂无服务器</td></tr>{% endfor %}</tbody></table></div></div>
<div class=card style="margin-top:16px"><h2>🧾 最新事件</h2><div class="scrollbox compact"><table class=table>{% for e in events %}<tr><td><b>{{e.title}}</b><br><span class=muted>{{e.created_at}}｜{{e.event_type}}</span></td><td class="event-content">{{clean_event_html(e.content)|safe}}{% set ctx=event_context(e) %}{% if ctx %}<div class="event-context">{{ctx|safe}}</div>{% endif %}</td></tr>{% else %}<tr><td>暂无事件</td></tr>{% endfor %}</table></div></div>'''

SERVERS='''<div class=top><h1>🖥️✨ 所有服务器</h1><div class=btns><button onclick="setView('card')">🔳 卡片视图</button><button onclick="setView('table')">📋 表格视图</button><a class="btn primary" href="/servers/add">➕ 添加服务器</a><a class=btn href="/">📊 总览</a></div></div>
<div class=grid><div class="card kpi"><div class=label>📦 总数</div><div class=value>{{data.total}}</div></div><div class="card kpi"><div class=label>🟢 在线</div><div class="value ok">{{data.online}}</div></div><div class="card kpi"><div class=label>🔴 离线</div><div class="value bad">{{data.offline}}</div></div><div class="card kpi"><div class=label>📡 探针</div><div class=value>{{data.probes}}</div></div></div>
<div class=card style="margin-top:16px">
<div data-view-card class=cardgrid>
{% for s in data.servers %}{% set m=s.metrics %}
<div class="card servercard">
<h3 class="server-title"><span data-live-dot="{{s.id}}" class="dot {{'online' if s.online else 'offline'}}"></span>{{flag_icon(s)|safe}}<span class="name">{{s.name}}</span></h3>
<div class=meta><span class=badge>#{{s.id}}</span><span class=badge>{{s.host}}:{{s.check_port}}</span><span class=badge>{{s.location_cn or s.location}}</span></div>
<div style="display:flex;gap:8px;flex-wrap:wrap;margin:8px 0"><span class="badge {{status_color_class_by_days(s)}}">{{display_price_label(s)}}</span><span class="badge {{status_color_class_by_days(s)}}">{{display_expire_label(s)}}</span><span class=badge data-status="{{s.id}}">{{'🟢 在线' if s.online else '🔴 离线'}}</span><span class=badge>⏱️ <span data-uptime="{{s.id}}">{{duration(m.uptime_seconds or 0)}}</span></span></div>
<div class=small data-hw="{{s.id}}">{{metric_config_html(m)|safe}}</div><div class="metric-extra"><div class="mini"><b>网络</b><span data-netspeed="{{s.id}}">↑ 0B/s&nbsp;&nbsp;↓ 0B/s</span></div><div class="mini"><b>流量</b><span data-traffic="{{s.id}}">↑ 0B&nbsp;&nbsp;↓ 0B</span></div><div class="mini"><b>负载</b><span data-load="{{s.id}}">0.00 ｜ 0.00 ｜ 0.00</span></div></div>
<hr>
{{progress_row('CPU',s.id,'cpu',m.cpu_percent or 0,s.cpu_alert or 90)|safe}}
{{progress_row('内存',s.id,'mem',m.mem_percent or 0,s.mem_alert or 90)|safe}}
{{progress_row('SWAP',s.id,'swap',m.swap_percent or 0,80)|safe}}
{{progress_row('硬盘',s.id,'disk',m.disk_percent or 0,s.disk_alert or 90)|safe}}
<br><div class=btns><a class="btn primary" href="/servers/{{s.id}}">查看</a><a class=btn href="/servers/{{s.id}}/edit">编辑</a></div>
</div>
{% else %}<div class=card>📭 暂无服务器</div>{% endfor %}
</div>
<div data-view-table class=hidden>
<table class=table><thead><tr><th>ID</th><th>服务器</th><th>状态/在线时长</th><th>资源进度</th><th>费用/到期</th><th>操作</th></tr></thead><tbody>
{% for s in data.servers %}{% set m=s.metrics %}
<tr><td><b>#{{s.id}}</b></td><td><b>{{flag_icon(s)|safe}} {{s.name}}</b><br><span class=muted>{{s.host}}:{{s.check_port}}｜{{s.location_cn or s.location}}</span><br><span class=small>{{s.note or '无备注'}}</span></td><td><span data-status="{{s.id}}">{{'🟢 在线' if s.online else '🔴 离线'}}</span><br>⏱️ <span data-uptime="{{s.id}}">{{duration(m.uptime_seconds or 0)}}</span></td><td>{{progress_row('CPU',s.id,'cpu',m.cpu_percent or 0,s.cpu_alert or 90)|safe}}{{progress_row('内存',s.id,'mem',m.mem_percent or 0,s.mem_alert or 90)|safe}}{{progress_row('SWAP',s.id,'swap',m.swap_percent or 0,80)|safe}}{{progress_row('硬盘',s.id,'disk',m.disk_percent or 0,s.disk_alert or 90)|safe}}</td><td><span class="{{status_color_class_by_days(s)}}">{{display_price_label(s)}}</span><br><span class="{{status_color_class_by_days(s)}}">{{display_expire_label(s)}}</span></td><td><div class=btns><a class="btn primary" href="/servers/{{s.id}}">查看</a><a class=btn href="/servers/{{s.id}}/edit">编辑</a></div></td></tr>
{% else %}<tr><td colspan=6>📭 暂无服务器</td></tr>{% endfor %}
</tbody></table>
</div></div>'''

DETAIL='''<div class=top><h1>🖥️ {{flag_icon(s)|safe}} {{s.name}}</h1><div class=btns><a class=btn href="/servers">📋 返回</a><a class="btn primary" href="/servers/{{s.id}}/edit">✏️ 编辑</a></div></div>{% set m=s.metrics %}<div class=grid3><div class="card kpi"><div class=label>📡 状态</div><div class="value {{'ok' if s.status.last_status=='online' else 'bad' if s.status.last_status=='offline' else ''}}">{{'🟢 在线' if s.status.last_status=='online' else '🔴 离线' if s.status.last_status=='offline' else '⚪ 未知'}}</div><div class=small>{{s.status.last_checked_at or '未知'}}</div></div><div class="card kpi"><div class=label>⏱️ 运行时长</div><div class=value><span data-uptime='{{s.id}}'>{{duration(m.uptime_seconds or 0)}}</span></div><div class=small>开机：{{m.boot_time or '未知'}}</div></div><div class="card kpi"><div class=label>⏰ 到期</div><div class=value style="font-size:22px">{{expire_text(s.expire_at,s.free_forever)}}</div><div class=small>{{price_text(s)}}｜{{cycle_cn(s.cycle)}}</div></div></div><div class=grid2 style="margin-top:16px"><div class=card><h2>⚙️ 服务器配置</h2><p><span class=badge>🆔 ID {{s.id}}</span> <span class=badge>{{flag_icon(s)|safe}} {{s.location_cn or s.location}}</span></p><p>🌐 主机：<code>{{s.host}}:{{s.check_port}}</code></p><p>🏢 运营商：{{s.isp or '未知'}}</p><p>🧬 系统：{{s.os_name or '未知系统'}}</p><p>📝 备注：{{s.note or '无'}}</p><hr><div class=grid3><div class=card>🧩 CPU<br><b>{{m.cpu_cores or '?'}} Cores</b></div><div class=card>🧠 内存<br><b>{{fmt_size(m.mem_total or 0) if m.mem_total else '未知'}}</b></div><div class=card>💾 硬盘<br><b>{{fmt_size(m.disk_total or 0) if m.disk_total else '未知'}}</b></div></div></div><div class=card><h2>📊 资源使用</h2>🔥 CPU <span data-cputxt='{{s.id}}'>{{'%.0f'|format(m.cpu_percent or 0)}}%</span> / {{'%.0f'|format(s.cpu_alert or 90)}}%<div class=progress><div class="bar {{bar_class(m.cpu_percent or 0,70,s.cpu_alert or 90)}}" data-cpu="{{s.id}}" data-limit="{{s.cpu_alert or 90}}" style="width:{{m.cpu_percent or 0}}%"></div></div><br>🧠 内存 {{fmt_size(m.mem_used or 0) if m.mem_used else '未知'}} / {{fmt_size(m.mem_total or 0) if m.mem_total else '未知'}} (<span data-memtxt='{{s.id}}'>{{'%.0f'|format(m.mem_percent or 0)}}%</span>) / {{'%.0f'|format(s.mem_alert or 90)}}%<div class=progress><div class="bar {{bar_class(m.mem_percent or 0,70,s.mem_alert or 90)}}" data-mem="{{s.id}}" data-limit="{{s.mem_alert or 90}}" style="width:{{m.mem_percent or 0}}%"></div></div><br>💾 硬盘 {{fmt_size(m.disk_used or 0) if m.disk_used else '未知'}} / {{fmt_size(m.disk_total or 0) if m.disk_total else '未知'}} (<span data-disktxt='{{s.id}}'>{{'%.0f'|format(m.disk_percent or 0)}}%</span>) / {{'%.0f'|format(s.disk_alert or 90)}}%<div class=progress><div class="bar {{bar_class(m.disk_percent or 0,70,s.disk_alert or 90)}}" data-disk="{{s.id}}" data-limit="{{s.disk_alert or 90}}" style="width:{{m.disk_percent or 0}}%"></div></div><hr>🌐 流量：⬇️ {{fmt_size(m.rx_bytes or 0)}} / ⬆️ {{fmt_size(m.tx_bytes or 0)}}<br>📡 数据源：{{'🟢 在线' if fresh(m) else '🟠 超时/未上报'}}｜{{m.updated_at or '未知'}}</div></div><div class=grid2 style="margin-top:16px"><div class=card><h2>📡 一键部署探针</h2><p class=small>复制到这台服务器 SSH 执行，探针静默上报，离线/恢复由主机器人统一推送。</p><pre id="agentcmd">{{s.agent_cmd}}</pre><button class=primary type=button onclick="copyText('agentcmd')">📋 复制探针命令</button><span id="agentcmdok" class=copyok></span></div><div class=card><h2>🛠️ 操作</h2><div class=btns><form method=post action="/servers/{{s.id}}/check"><button class=primary>📡 立即检测</button></form><form method=post action="/servers/{{s.id}}/refresh"><button>🌍 刷新地区</button></form><a class=btn href="/servers/{{s.id}}/edit">✏️ 编辑资料/阈值</a><form method=post action="/servers/{{s.id}}/delete" onsubmit="return delok()"><button class=danger>🗑️ 删除</button></form></div><hr><h3>🎯 告警状态</h3>{% for key,label in [('cpu','🔥 CPU'),('mem','🧠 内存'),('disk','💾 硬盘')] %}{% set st=s.states.get(key) %}<p>{{label}}：{{'🚨 告警中' if st and st.active else '✅ 正常'}}{% if st %}｜上次 {{st.last_value|round(0)}}%｜{{st.last_sent_at}}{% endif %}</p>{% endfor %}</div></div>'''
FORM='''<div class=top><h1>{{'➕' if is_add else '✏️'}} {{action}}</h1><a class=btn href="/servers">📋 返回</a></div><form method=post class=card><div class=formgrid><div><label>🏷️ 名称</label><input name=name value="{{s.name or ''}}" required></div><div><label>🌐 IP/主机</label><input name=host value="{{s.host or ''}}" required placeholder="1.2.3.4 或 example.com"></div><div><label>🔌 端口</label><input name=check_port type=number min=1 max=65535 value="{{s.check_port or 22}}"></div><div><label>🧬 系统</label><input name=os_name value="{{probe_os_name_for_server(s)}}" placeholder="探针安装上报后自动识别，也可手动填写"><div class=small>安装探针并成功上报后会自动显示系统；也可以手动填写覆盖。</div></div><div><label>🔁 周期</label><select name=cycle><option value=monthly {{'selected' if s.cycle=='monthly' else ''}}>月付</option><option value=quarterly {{'selected' if s.cycle=='quarterly' else ''}}>季付</option><option value=yearly {{'selected' if s.cycle=='yearly' else ''}}>年付</option></select></div><div><label>📆 到期</label><input name=expire_at type=date value="{{datetime_input_value(s.expire_at or '')}}" placeholder="请选择到期日期"><div class=date-help>只选择年月日，不再保存时间。</div></div><div><label>💰 价格</label><input name=price type=number step=.01 value="{{s.price if s.price is not none else 0}}"></div><div><label>💱 币种</label><select name=currency>{% for c in ['CNY','USD','EUR','GBP'] %}<option value={{c}} {{'selected' if (s.currency or 'USD')==c else ''}}>{{c}}</option>{% endfor %}</select></div><div><label>🔥 CPU 阈值 %</label><input name=cpu_alert type=number min=1 max=100 value="{{s.cpu_alert or 90}}"></div><div><label>🧠 内存阈值 %</label><input name=mem_alert type=number min=1 max=100 value="{{s.mem_alert or 90}}"></div><div><label>💾 硬盘阈值 %</label><input name=disk_alert type=number min=1 max=100 value="{{s.disk_alert or 90}}"></div><div><label>📝 备注</label><textarea name=note rows=4>{{s.note or ''}}</textarea></div></div><hr><label><input type=checkbox name=free_forever style="width:auto" {{'checked' if s.free_forever else ''}}> 🎁 永久免费</label><label><input type=checkbox name=auto_renew style="width:auto" {{'checked' if s.auto_renew else ''}}> 🔁 自动续费</label><div class=btns style="margin-top:18px"><button class=primary type=submit>💾 保存</button><a class=btn href="/servers">取消</a></div></form>'''
LOCAL='''<div class=top><h1>🏠 本机面板</h1><a class=btn href="/">📊 总览</a></div><div class=grid3><div class="card kpi"><div>🌐 公网IP</div><div class=value style="font-size:22px">{{local.public_ip or '未知'}}</div></div><div class="card kpi"><div>⏱️ 运行</div><div class=value style="font-size:22px" data-local-uptime>{{local.uptime or '未知'}}</div></div><div class="card kpi"><div>🧩 CPU</div><div class=value>{{local.cpu_count or 0}} 核</div></div></div><div class=grid2 style="margin-top:16px"><div class=card><h2>📊 资源</h2>🔥 CPU <span data-local-cputxt>{{'%.0f'|format(local.cpu or 0)}}%</span><div class=progress><div class=bar data-local-cpu style="width:{{local.cpu or 0}}%"></div></div><br>🧠 内存 <span data-local-memused>{{fmt(local.mem_used or 0)}}</span> / <span data-local-memtotal>{{fmt(local.mem_total or 0)}}</span> (<span data-local-memtxt>{{'%.0f'|format(local.mem_percent or 0)}}%</span>)<div class=progress><div class=bar data-local-mem style="width:{{local.mem_percent or 0}}%"></div></div><br>🔄 SWAP <span data-local-swapused>0B</span> / <span data-local-swaptotal>无</span> (<span data-local-swaptxt>0%</span>)<div class=progress><div class=bar data-local-swap style="width:0%"></div></div><br>💾 磁盘 <span data-local-diskused>{{fmt(local.disk_used or 0)}}</span> / <span data-local-disktotal>{{fmt(local.disk_total or 0)}}</span> (<span data-local-disktxt>{{'%.0f'|format(local.disk_percent or 0)}}%</span>)<div class=progress><div class=bar data-local-disk style="width:{{local.disk_percent or 0}}%"></div></div><div class="metric-extra" style="margin-top:14px"><div class="mini"><b>网络</b><span data-local-netspeed>↑ 0B/s&nbsp;&nbsp;↓ 0B/s</span></div><div class="mini"><b>流量</b><span data-local-traffic>↑ 0B&nbsp;&nbsp;↓ 0B</span></div><div class="mini"><b>负载</b><span data-local-load>0.00 ｜ 0.00 ｜ 0.00</span></div></div></div><form class=card method=post><h2>✏️ 编辑本机资料</h2><label>名称</label><input name=name value="{{profile.name}}"><label>备注</label><input name=note value="{{profile.note}}"><label>周期</label><select name=cycle><option value=monthly {{'selected' if profile.cycle=='monthly' else ''}}>月付</option><option value=quarterly {{'selected' if profile.cycle=='quarterly' else ''}}>季付</option><option value=yearly {{'selected' if profile.cycle=='yearly' else ''}}>年付</option></select><label>价格</label><input name=price value="{{profile.price}}"><label>币种</label><select name=currency>{% for c in ['CNY','USD','EUR','GBP'] %}<option value={{c}} {{'selected' if profile.currency==c else ''}}>{{c}}</option>{% endfor %}</select><label>到期</label><input name=expire_at type=date value="{{datetime_input_value(profile.expire_at)}}"><button class=primary>💾 保存</button></form></div>'''
EVENTS='''<div class=top><h1>🧾✨ 事件记录</h1><a class=btn href="/">📊 总览</a></div><div class=card><p class=small>📜 记录区域已开启滚轮浏览，鼠标放在表格内即可上下滑动查看更多历史事件。</p><div class=scrollbox><table class=table><thead><tr><th>时间</th><th>类型</th><th>标题</th><th>内容</th></tr></thead><tbody>{% for e in events %}<tr><td>{{e.created_at}}</td><td><span class=badge>{{e.event_type}}</span></td><td><b>{{e.title}}</b></td><td class="event-content">{{clean_event_html(e.content)|safe}}{% set ctx=event_context(e) %}{% if ctx %}<div class="event-context">{{ctx|safe}}</div>{% endif %}</td></tr>{% else %}<tr><td colspan=4>暂无事件</td></tr>{% endfor %}</tbody></table></div></div>'''
SETTINGS='''<div class=top><h1>⚙️✨ 系统设置</h1><a class=btn href="/">📊 返回总览</a></div>
<div class=grid2 style="margin-top:16px">
<form class=card method=post accept-charset="UTF-8"><h2>🏷️ 平台名字</h2><input type=hidden name=action value=site><label>平台名称</label><input name=site_name value="{{site_name_value()}}" placeholder="服务器监控"><button class=primary>💾 保存平台名字</button><p class=small>只保留一个平台名称；保存后侧栏名称和浏览器标题同步变化。</p></form>
<form class=card method=post enctype=multipart/form-data><h2>🌐 浏览器标签图标</h2><input type=hidden name=action value=favicon><p class=small>支持 ico / png / jpg / webp。当前：{{'✅ 已上传' if has_favicon else '❌ 默认图标'}}</p><input type=file name=favicon accept=".ico,image/*"><button class=primary>🌐 上传标签图标</button><p class=small>上传后 Ctrl+F5 强制刷新，浏览器标签页图标会更新。</p></form>
</div>
<div class=grid2><form class=card method=post><h2>🔐 修改网页登录密码</h2><input type=hidden name=action value=password><label>账号</label><input name=username value="{{web_user}}"><label>新密码</label><input name=password placeholder="明文输入新密码"><label>再次输入</label><input name=password2 placeholder="再次输入新密码"><button class=primary>💾 保存账号密码</button></form><form class=card method=post enctype=multipart/form-data><h2>🖼️ 自定义全站主题背景</h2><input type=hidden name=action value=upload_bg><p class=small>支持 jpg / png / webp，上传后登录页和后台页面都会使用这张图。</p><input type=file name=bg accept="image/*"><button class=primary>🌌 上传背景图</button></form></div><div class=grid2 style="margin-top:16px"><form class=card method=post><h2>🤖 TG 接口对接</h2><input type=hidden name=action value=tg><label>BOT_TOKEN</label><input name=bot_token value="{{bot_token}}" placeholder="123456:ABC"><label>ADMIN_IDS</label><input name=admin_ids value="{{admin_ids}}" placeholder="123456789,987654321"><button class=primary>💾 保存 TG 配置</button><p class=small>保存后建议执行：<code>systemctl restart server-monitor-bot server-monitor-web</code></p></form><form class=card method=post enctype=multipart/form-data><h2>🎨 上传 Komari 风格主题 ZIP</h2><input type=hidden name=action value=upload_theme_zip><p class=small>兼容含 komari-theme.json / dist / CSS / 图片的 zip 主题包，会自动读取 CSS 和图片作为当前面板皮肤。</p><input type=file name=theme_zip accept='.zip'><button class=primary>🎨 上传并应用主题包</button></form><form class=card method=post action="/api/test-tg"><h2>📨 TG 测试推送</h2><label>测试内容</label><textarea name=test_text rows=5>✅ Web 面板 TG 测试推送成功
如果你收到这条消息，说明 BOT_TOKEN 和 ADMIN_IDS 正常。</textarea><input type=hidden name=bot_token value="{{bot_token}}"><input type=hidden name=admin_ids value="{{admin_ids}}"><button class=primary>🚀 发送测试推送</button><p class=small>先保存 TG 配置后再测试；测试会推送到 ADMIN_IDS。</p></form></div><div class=grid2 style="margin-top:16px"><form class=card method=post><h2>🧹 背景管理</h2><input type=hidden name=action value=clear_bg><p>当前自定义背景：{{'✅ 已上传' if has_bg else '❌ 未上传，使用默认星空'}}</p><button class=danger>恢复默认星空背景</button></form><div class=card><h2>📌 说明</h2><p class=small>复制按钮已兼容 HTTP 页面：优先使用剪贴板 API，失败时自动使用备用复制方案，并弹出复制成功提示。</p><p class=small>资源进度条每 10 秒刷新一次：绿色正常，黄色较高，红色超过阈值或接近危险。</p></div></div>'''




# ===== WEB V6 FINAL UI FIX HELPERS =====
COUNTRY_NAME_CODE.update({
    'England':'GB','英格兰':'GB','Scotland':'GB','苏格兰':'GB','Wales':'GB','威尔士':'GB',
    'Northern Ireland':'GB','北爱尔兰':'GB','UK':'GB','Great Britain':'GB','Czechia':'CZ','Czech Republic':'CZ','捷克':'CZ',
    'Bosnia and Herzegovina':'BA','波黑':'BA','Ivory Coast':'CI','Côte d’Ivoire':'CI','科特迪瓦':'CI',
    'Curacao':'CW','库拉索':'CW','Cape Verde':'CV','佛得角':'CV','DR Congo':'CD','刚果金':'CD',
    'Saudi Arabia':'SA','沙特阿拉伯':'SA','New Zealand':'NZ','新西兰':'NZ','South Africa':'ZA','南非':'ZA',
    'Qatar':'QA','卡塔尔':'QA','Switzerland':'CH','瑞士':'CH','Mexico':'MX','墨西哥':'MX',
    'Morocco':'MA','摩洛哥':'MA','Haiti':'HT','海地':'HT','Paraguay':'PY','巴拉圭':'PY',
    'Tunisia':'TN','突尼斯':'TN','Egypt':'EG','埃及':'EG','Iran':'IR','伊朗':'IR',
    'Uruguay':'UY','乌拉圭':'UY','Senegal':'SN','塞内加尔':'SN','Iraq':'IQ','伊拉克':'IQ',
    'Argentina':'AR','阿根廷':'AR','Algeria':'DZ','阿尔及利亚':'DZ','Austria':'AT','奥地利':'AT',
    'Jordan':'JO','约旦':'JO','Portugal':'PT','葡萄牙':'PT','Uzbekistan':'UZ','乌兹别克斯坦':'UZ',
    'Colombia':'CO','哥伦比亚':'CO','Croatia':'HR','克罗地亚':'HR','Ghana':'GH','加纳':'GH','Panama':'PA','巴拿马':'PA',
})
COUNTRY_CN = {
    'United Kingdom':'英国','England':'英格兰','Scotland':'苏格兰','Wales':'威尔士','Northern Ireland':'北爱尔兰','UK':'英国','Great Britain':'英国',
    'China':'中国','Hong Kong':'香港','Taiwan':'台湾','Macau':'澳门','United States':'美国','USA':'美国',
    'Japan':'日本','Singapore':'新加坡','South Korea':'韩国','Korea':'韩国','Germany':'德国','France':'法国',
    'Netherlands':'荷兰','Canada':'加拿大','Australia':'澳大利亚','India':'印度','Russia':'俄罗斯','Brazil':'巴西',
    'Turkey':'土耳其','Thailand':'泰国','Vietnam':'越南','Malaysia':'马来西亚','Philippines':'菲律宾','Indonesia':'印尼',
    'United Arab Emirates':'阿联酋','Italy':'意大利','Spain':'西班牙','Sweden':'瑞典','Norway':'挪威','Finland':'芬兰',
    'Poland':'波兰','Czechia':'捷克','Czech Republic':'捷克','Bosnia and Herzegovina':'波黑','Qatar':'卡塔尔',
    'Switzerland':'瑞士','Mexico':'墨西哥','South Africa':'南非','Morocco':'摩洛哥','Haiti':'海地','Paraguay':'巴拉圭',
    'Ivory Coast':'科特迪瓦','Curacao':'库拉索','Tunisia':'突尼斯','Belgium':'比利时','Egypt':'埃及','Iran':'伊朗',
    'New Zealand':'新西兰','Cape Verde':'佛得角','Saudi Arabia':'沙特阿拉伯','Uruguay':'乌拉圭','Senegal':'塞内加尔',
    'Iraq':'伊拉克','Argentina':'阿根廷','Algeria':'阿尔及利亚','Austria':'奥地利','Jordan':'约旦','Portugal':'葡萄牙',
    'DR Congo':'刚果金','Uzbekistan':'乌兹别克斯坦','Colombia':'哥伦比亚','Croatia':'克罗地亚','Ghana':'加纳','Panama':'巴拿马'
}
REGION_CN = {'England':'英格兰','Scotland':'苏格兰','Wales':'威尔士','Northern Ireland':'北爱尔兰'}
CITY_CN = {'London':'伦敦','Tokyo':'东京','Singapore':'新加坡','Hong Kong':'香港','Los Angeles':'洛杉矶','New York':'纽约','Frankfurt':'法兰克福','Paris':'巴黎','Amsterdam':'阿姆斯特丹','Seoul':'首尔','Sydney':'悉尼','Toronto':'多伦多','Dubai':'迪拜'}

def cn_name(v, mapping):
    raw=str(v or '').strip()
    return mapping.get(raw, raw)

def server_country_code(s):
    if not s: return ''
    code=(s.get('country_code') or '').strip().upper()
    if not code:
        code=country_code_guess(s.get('country') or '').upper()
    if code == 'UK': code = 'GB'
    return code if len(code)==2 and code.isalpha() else ''

def flag_icon(s):
    code=server_country_code(s)
    if not code:
        return '<span title="未知地区">🌐</span>'
    return f'<img class="flagimg" src="https://flagcdn.com/w40/{code.lower()}.png" srcset="https://flagcdn.com/w80/{code.lower()}.png 2x" width="24" height="18" alt="{code}" title="{code}">'

def server_location_cn(s):
    if not s: return '未知'
    country=cn_name(s.get('country') or '', COUNTRY_CN)
    region=cn_name(s.get('region') or '', REGION_CN)
    city=cn_name(s.get('city') or '', CITY_CN)
    parts=[]
    for x in [country, region, city]:
        if x and x not in parts and x not in ['None','未知']:
            parts.append(x)
    return ' '.join(parts) if parts else '未知'

def expire_days_value(s):
    try:
        if truth(s.get('free_forever')): return 999999
        exp=str(s.get('expire_at') or '').strip()
        if exp in ['永久','永久免费']: return 999999
        return (pdt(exp).date()-datetime.now().date()).days
    except Exception:
        return None

def status_color_class_by_days(s):
    d=expire_days_value(s)
    if d is None: return 'muted'
    if d < 0 or d <= 7: return 'danger-text'
    if d <= 30: return 'warn-text'
    return 'ok-text'

def bar_class(value, warn=70, bad=90):
    try: v=float(value or 0)
    except Exception: return ''
    return 'bad' if v>=bad else 'warn' if v>=warn else ''

THEME_DIR=os.path.join(APP_DIR,'web_theme')
def theme_bg_exists():
    for fn in ['web_bg.jpg','web_bg.png','web_bg.webp','web_bg.jpeg']:
        if os.path.exists(os.path.join(APP_DIR,fn)): return os.path.join(APP_DIR,fn)
    for pat in ['*.jpg','*.jpeg','*.png','*.webp']:
        files=glob.glob(os.path.join(THEME_DIR,'**',pat), recursive=True)
        if files: return files[0]
    return ''

@app.route('/theme-assets/<path:name>')
@login_required
def theme_assets(name):
    from flask import send_from_directory, abort
    root=os.path.abspath(THEME_DIR)
    target=os.path.abspath(os.path.join(root,name))
    if not target.startswith(root) or not os.path.exists(target): abort(404)
    return send_from_directory(root, name)

def active_theme_css():
    theme_dir=os.path.join(APP_DIR,'web_theme')
    for pat in ['*.css']:
        files=glob.glob(os.path.join(theme_dir,'**',pat), recursive=True)
        if files:
            rel=os.path.relpath(files[0], theme_dir).replace(os.sep,'/')
            return '/theme-assets/'+rel
    return ''
    rel=os.path.relpath(css_files[0], THEME_DIR).replace(os.sep,'/')
    return f'/theme-assets/{rel}'

def safe_extract_zip(zip_path, dest):
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        for member in z.infolist():
            name=member.filename.replace('\\','/')
            if name.startswith('/') or '..' in name.split('/'):
                continue
            z.extract(member, dest)

@app.route('/api/test-tg', methods=['POST'])
@login_required
def api_test_tg():
    token=(request.form.get('bot_token') or os.getenv('BOT_TOKEN') or BOT_TOKEN or '').strip()
    admins=(request.form.get('admin_ids') or os.getenv('ADMIN_IDS') or (','.join(ADMIN_IDS) if isinstance(ADMIN_IDS,list) else '')).strip()
    text=(request.form.get('test_text') or '✅ Web 面板 TG 测试推送成功').strip()
    if not token or not admins:
        flash('请先填写 BOT_TOKEN 和 ADMIN_IDS','error')
        return redirect(url_for('settings_page'))
    ok_count=0; err=[]
    for chat_id in [x.strip() for x in admins.split(',') if x.strip()]:
        try:
            r=requests.post(f'https://api.telegram.org/bot{token}/sendMessage', json={'chat_id':chat_id,'text':text,'parse_mode':'HTML'}, timeout=10)
            if r.ok and r.json().get('ok'): ok_count+=1
            else: err.append(f'{chat_id}: {r.text[:120]}')
        except Exception as e:
            err.append(f'{chat_id}: {e}')
    if ok_count: flash(f'TG 测试推送成功：{ok_count} 个接收人','success')
    if err: flash('TG 测试失败：'+'；'.join(err[:3]),'error')
    return redirect(url_for('settings_page'))
# ===== END WEB V6 FINAL UI FIX HELPERS =====

# ===== RUNTIME FIX: age helper =====
def age(v):
    try:
        return dur((datetime.now() - pdt(v)).total_seconds()) + '前'
    except Exception:
        return '未知'
# ===== END RUNTIME FIX =====


# ===== USER REQUEST PATCH: date-only expiry + neon dashboard + realtime channel + extra ops metrics =====
# 只处理本次提出的 Web 表单/视觉/实时/监控增强，不改动服务器增删改查等原有业务流程。
import json as _json
import subprocess as _subprocess
try:
    from flask import Response as _Response, stream_with_context as _stream_with_context
except Exception:
    _Response = None
    _stream_with_context = None

# 1) 到期日只保留年月日：表单用 date，入库不再补 00:00 / 时间。
def datetime_input_value(v):
    v = str(v or '').strip()
    if not v or v in ('永久', '永久免费'):
        return ''
    v = v.replace('T', ' ')
    m = re.search(r'(\d{4}-\d{2}-\d{2})', v)
    if m:
        return m.group(1)
    try:
        return pdt(v).strftime('%Y-%m-%d')
    except Exception:
        return v[:10]

def normalize_datetime_value(v):
    v = str(v or '').strip()
    if not v:
        return ''
    v = v.replace('T', ' ')
    m = re.search(r'(\d{4}-\d{2}-\d{2})', v)
    return m.group(1) if m else v[:10]

# 2) 扩展指标列：兼容旧数据库，缺什么补什么；没有探针上报时显示“未上报”，不会影响旧功能。
_BASE_INIT_DB_FOR_NEON = init_db
def init_db():
    _BASE_INIT_DB_FOR_NEON()
    c = db()
    try:
        extra_cols = [
            ('gpu_count', 'INTEGER DEFAULT 0'), ('gpu_name', "TEXT DEFAULT ''"),
            ('gpu_util', 'REAL DEFAULT 0'), ('gpu_mem_total', 'INTEGER DEFAULT 0'),
            ('gpu_mem_used', 'INTEGER DEFAULT 0'), ('gpu_mem_percent', 'REAL DEFAULT 0'),
            ('io_read_bytes', 'INTEGER DEFAULT 0'), ('io_write_bytes', 'INTEGER DEFAULT 0'),
            ('io_read_speed', 'INTEGER DEFAULT 0'), ('io_write_speed', 'INTEGER DEFAULT 0'),
            ('tcp_established', 'INTEGER DEFAULT 0'), ('tcp_listen', 'INTEGER DEFAULT 0'),
            ('tcp_time_wait', 'INTEGER DEFAULT 0')
        ]
        for col, ddl in extra_cols:
            ensure_col(c, 'server_metrics', col, ddl)
        c.commit()
    finally:
        c.close()

_NEON_IO_RATE_CACHE = {}

def _safe_json_loads(v):
    try:
        if not v:
            return {}
        obj = _json.loads(str(v))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}

def _deep_find(obj, keys):
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and obj.get(k) not in (None, ''):
                return obj.get(k)
        for val in obj.values():
            got = _deep_find(val, keys)
            if got not in (None, ''):
                return got
    elif isinstance(obj, list):
        for val in obj:
            got = _deep_find(val, keys)
            if got not in (None, ''):
                return got
    return None

def _metric_pick(m, keys, default=0):
    m = m or {}
    for k in keys:
        try:
            if k in m and m.get(k) not in (None, ''):
                return m.get(k)
        except Exception:
            pass
    raw = _safe_json_loads((m or {}).get('raw') if hasattr(m, 'get') else '')
    got = _deep_find(raw, keys)
    return default if got in (None, '') else got

def _fmt_pct(v):
    try:
        return f'{float(v or 0):.0f}%'
    except Exception:
        return '0%'

def _metric_gpu_values(m):
    name = str(_metric_pick(m, ['gpu_name', 'gpu_model', 'gpu_title'], '') or '').strip()
    count = _int(_metric_pick(m, ['gpu_count', 'gpus'], 0))
    util = _float(_metric_pick(m, ['gpu_util', 'gpu_percent', 'gpu_usage', 'utilization_gpu'], 0))
    mem_total = _int(_metric_pick(m, ['gpu_mem_total', 'gpu_memory_total', 'gpu_total'], 0))
    mem_used = _int(_metric_pick(m, ['gpu_mem_used', 'gpu_memory_used', 'gpu_used'], 0))
    mem_pct = _float(_metric_pick(m, ['gpu_mem_percent', 'gpu_memory_percent'], 0))
    if not mem_pct and mem_total:
        mem_pct = round(mem_used * 100 / mem_total, 1)
    return name, count, util, mem_used, mem_total, mem_pct

def metric_gpu_html(m):
    name, count, util, mem_used, mem_total, mem_pct = _metric_gpu_values(m)
    if not (name or count or util or mem_total):
        return '未上报'
    head = name or (f'{count} GPU' if count else 'GPU')
    mem = f' ｜ 显存 {fmt(mem_used)}/{fmt(mem_total)}' if mem_total else ''
    return f'{html.escape(head)} ｜ {_fmt_pct(util)}{mem}'

def metric_io_html(m):
    rb = _int(_metric_pick(m, ['io_read_bytes', 'disk_read_bytes', 'read_bytes'], 0))
    wb = _int(_metric_pick(m, ['io_write_bytes', 'disk_write_bytes', 'write_bytes'], 0))
    rs = _int(_metric_pick(m, ['io_read_speed', 'disk_read_speed', 'read_speed'], 0))
    ws = _int(_metric_pick(m, ['io_write_speed', 'disk_write_speed', 'write_speed'], 0))
    if rs or ws:
        return f'R {fmt(rs)}/s ｜ W {fmt(ws)}/s'
    if rb or wb:
        return f'R {fmt(rb)} ｜ W {fmt(wb)}'
    return '未上报'

def metric_tcp_html(m):
    est = _int(_metric_pick(m, ['tcp_established', 'tcp_est', 'connections_established', 'tcp_connections'], 0))
    listen = _int(_metric_pick(m, ['tcp_listen', 'listen'], 0))
    tw = _int(_metric_pick(m, ['tcp_time_wait', 'time_wait'], 0))
    if est or listen or tw:
        return f'EST {est} ｜ LISTEN {listen} ｜ TW {tw}'
    return '未上报'

def expire_cycle_days(s):
    c = str((s or {}).get('cycle') or '').lower()
    if c == 'yearly':
        return 365
    if c == 'quarterly':
        return 90
    return 30

def expire_progress_info(s):
    d = expire_days_value(s)
    if d is None:
        return {'days': None, 'percent': 0, 'class': 'unknown', 'text': '未设置到期日'}
    if d == 999999:
        return {'days': d, 'percent': 100, 'class': 'forever', 'text': '♾️ 永久 / 免费'}
    if d < 0:
        return {'days': d, 'percent': 100, 'class': 'danger', 'text': f'🚨 已过期 {abs(d)} 天'}
    total = max(1, expire_cycle_days(s))
    pct = max(0, min(100, d * 100 / total))
    cls = 'danger' if d <= 7 else 'warn' if d <= 30 else 'ok'
    return {'days': d, 'percent': pct, 'class': cls, 'text': f'📆 剩余 {d} 天'}

def expire_progress_html(s):
    info = expire_progress_info(s)
    sid = html.escape(str((s or {}).get('id') or '0'))
    cls = info['class']
    pct = float(info['percent'] or 0)
    text = html.escape(info['text'])
    return f'''<div class="expire-progress-wrap {cls}" data-expwrap="{sid}"><div class="expire-progress-title"><span data-exptext="{sid}">{text}</span><b>{pct:.0f}%</b></div><div class="expire-progress"><div class="expire-bar {cls}" data-expbar="{sid}" style="width:{pct:.0f}%"></div></div></div>'''

def server_card_class(s):
    info = expire_progress_info(s)
    cls = info.get('class') or 'unknown'
    return f'expire-{cls}'

def server_card_style(s):
    try:
        sid = int((s or {}).get('id') or 0)
    except Exception:
        sid = 0
    h1 = (sid * 47 + 15) % 360
    h2 = (h1 + 78) % 360
    h3 = (h1 + 155) % 360
    return f'--h1:{h1};--h2:{h2};--h3:{h3};'

def ai_fault_reason(s):
    s = s or {}
    st = s.get('status') or {}
    m = s.get('metrics') or {}
    reasons = []
    if st.get('last_status') == 'offline':
        return '离线：优先检查主机端口、防火墙、安全组、探针进程和上游网络。'
    if not fresh(m):
        reasons.append('探针数据超时，可能是 Agent 未运行或上报链路异常')
    cpu = _float(m.get('cpu_percent'))
    mem = _float(m.get('mem_percent'))
    disk = _float(m.get('disk_percent'))
    load1 = _float(m.get('load1'))
    cores = max(1, _int(m.get('cpu_cores')))
    gpu_name, gpu_count, gpu_util, gpu_mu, gpu_mt, gpu_mp = _metric_gpu_values(m)
    tcp_est = _int(_metric_pick(m, ['tcp_established', 'tcp_est', 'connections_established', 'tcp_connections'], 0))
    if cpu >= _float(s.get('cpu_alert') or 90):
        reasons.append('CPU 超过阈值，疑似计算任务或异常进程占用')
    if mem >= _float(s.get('mem_alert') or 90):
        reasons.append('内存接近耗尽，可能触发 OOM 或 Swap 抖动')
    if disk >= _float(s.get('disk_alert') or 90):
        reasons.append('磁盘空间高危，日志/缓存/备份可能堆积')
    if load1 > cores * 2:
        reasons.append('系统负载偏高，可能存在 IO 等待或进程排队')
    if gpu_util >= 90 or gpu_mp >= 90:
        reasons.append('GPU/显存高占用，检查推理/训练任务')
    if tcp_est >= 1000:
        reasons.append('TCP 连接数异常偏高，注意攻击流量或连接泄漏')
    ed = expire_days_value(s)
    if ed is not None and ed != 999999 and 0 <= ed <= 7:
        reasons.append('即将到期，请提前续费避免停机')
    return '；'.join(reasons[:3]) if reasons else '运行稳定：暂未发现明显故障特征。'

def ai_fault_html(s):
    return html.escape(ai_fault_reason(s))

# 本机 GPU / IO / TCP 采集：没有 GPU 或权限不足时自动显示未检测到。
def _local_gpu_snapshot():
    try:
        cmd = ['nvidia-smi', '--query-gpu=name,utilization.gpu,memory.used,memory.total', '--format=csv,noheader,nounits']
        out = _subprocess.check_output(cmd, timeout=1.5, stderr=_subprocess.DEVNULL).decode('utf-8', 'ignore').strip()
        if not out:
            return {'gpu_html': '未检测到 GPU', 'gpu_count': 0, 'gpu_util': 0, 'gpu_mem_percent': 0}
        rows = [x.strip() for x in out.splitlines() if x.strip()]
        utils=[]; used=[]; total=[]; names=[]
        for line in rows:
            parts=[p.strip() for p in line.split(',')]
            if len(parts)>=4:
                names.append(parts[0]); utils.append(_float(parts[1])); used.append(_float(parts[2])*1024*1024); total.append(_float(parts[3])*1024*1024)
        util = sum(utils)/len(utils) if utils else 0
        mem_used = sum(used); mem_total = sum(total)
        mem_pct = round(mem_used*100/mem_total,1) if mem_total else 0
        title = names[0] if names else f'{len(rows)} GPU'
        return {'gpu_html': f'{html.escape(title)} ｜ {_fmt_pct(util)} ｜ 显存 {fmt(mem_used)}/{fmt(mem_total)}', 'gpu_count': len(rows), 'gpu_util': util, 'gpu_mem_percent': mem_pct}
    except Exception:
        return {'gpu_html': '未检测到 GPU', 'gpu_count': 0, 'gpu_util': 0, 'gpu_mem_percent': 0}

def _local_disk_io_bytes():
    read_b = write_b = 0
    try:
        with open('/proc/diskstats', 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                parts=line.split()
                if len(parts)<14:
                    continue
                name=parts[2]
                if name.startswith(('loop','ram','fd')):
                    continue
                read_b += int(parts[5]) * 512
                write_b += int(parts[9]) * 512
    except Exception:
        pass
    return read_b, write_b

def _local_tcp_counts():
    states = {'01':0, '0A':0, '06':0}
    for fn in ['/proc/net/tcp', '/proc/net/tcp6']:
        try:
            with open(fn, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f.read().splitlines()[1:]:
                    parts=line.split()
                    if len(parts)>3 and parts[3] in states:
                        states[parts[3]] += 1
        except Exception:
            pass
    return states['01'], states['0A'], states['06']

_BASE_LOCAL_LIVE_JSON_FOR_NEON = _local_live_json
def _local_live_json():
    j = _BASE_LOCAL_LIVE_JSON_FOR_NEON()
    ts = time.time()
    rb, wb = _local_disk_io_bytes()
    old = _NEON_IO_RATE_CACHE.get('local')
    rs = ws = 0
    if old:
        dt = max(0.5, ts - old.get('ts', ts))
        rs = max(0, int((rb - old.get('rb', rb))/dt))
        ws = max(0, int((wb - old.get('wb', wb))/dt))
    _NEON_IO_RATE_CACHE['local'] = {'rb': rb, 'wb': wb, 'ts': ts}
    est, listen, tw = _local_tcp_counts()
    gpu = _local_gpu_snapshot()
    j.update(gpu)
    j.update({
        'io_read_bytes': rb, 'io_write_bytes': wb, 'io_read_speed': rs, 'io_write_speed': ws,
        'io_html': f'R {fmt(rs)}/s ｜ W {fmt(ws)}/s',
        'tcp_established': est, 'tcp_listen': listen, 'tcp_time_wait': tw,
        'tcp_html': f'EST {est} ｜ LISTEN {listen} ｜ TW {tw}',
        'ai_html': '本机实时：CPU/内存/磁盘/IO/TCP 正在动态判断。'
    })
    return j

_BASE_METRIC_JSON_FOR_NEON = metric_json
def metric_json(x):
    j = _BASE_METRIC_JSON_FOR_NEON(x)
    m = (x or {}).get('metrics') or {}
    sid = (x or {}).get('id')
    rb = _int(_metric_pick(m, ['io_read_bytes', 'disk_read_bytes', 'read_bytes'], 0))
    wb = _int(_metric_pick(m, ['io_write_bytes', 'disk_write_bytes', 'write_bytes'], 0))
    ts = time.time()
    old = _NEON_IO_RATE_CACHE.get(('srv', sid))
    rs = _int(_metric_pick(m, ['io_read_speed', 'disk_read_speed', 'read_speed'], 0))
    ws = _int(_metric_pick(m, ['io_write_speed', 'disk_write_speed', 'write_speed'], 0))
    if old and (rb or wb):
        dt = max(0.5, ts - old.get('ts', ts))
        rs = max(rs, int(max(0, rb - old.get('rb', rb))/dt))
        ws = max(ws, int(max(0, wb - old.get('wb', wb))/dt))
    _NEON_IO_RATE_CACHE[('srv', sid)] = {'rb': rb, 'wb': wb, 'ts': ts}
    info = expire_progress_info(x)
    j.update({
        'gpu_html': metric_gpu_html(m),
        'io_html': f'R {fmt(rs)}/s ｜ W {fmt(ws)}/s' if (rs or ws) else metric_io_html(m),
        'tcp_html': metric_tcp_html(m),
        'ai_html': ai_fault_reason(x),
        'expire_text': info['text'],
        'expire_percent': round(float(info['percent'] or 0), 1),
        'expire_class': info['class'],
        'card_class': server_card_class(x),
    })
    return j

def _pin_xy_for_server(s, i=0):
    code = server_country_code(s)
    city = str((s or {}).get('city') or (s or {}).get('region') or '').lower()
    mapping = {'CN': (72, 48), 'HK': (76, 58), 'TW': (79, 57), 'JP': (84, 44), 'SG': (73, 72), 'KR': (81, 43), 'US': (20, 45), 'CA': (18, 30), 'GB': (46, 34), 'DE': (51, 38), 'FR': (48, 42), 'NL': (49, 37), 'RU': (66, 27), 'AU': (82, 82), 'IN': (65, 60)}
    if 'beijing' in city or '北京' in city: return (73, 43)
    if 'shanghai' in city or '上海' in city: return (75, 50)
    if 'guangzhou' in city or '深圳' in city or '广州' in city: return (74, 58)
    return mapping.get(code, ((18 + i*13) % 86 + 6, (32 + i*17) % 48 + 24))

def node_topology_html(servers):
    servers = list(servers or [])
    pins = []
    for i, s in enumerate(servers[:28]):
        x, y = _pin_xy_for_server(s, i)
        name = html.escape(str(s.get('name') or f'节点{i+1}'))
        loc = html.escape(str(s.get('location_cn') or s.get('location') or '未知'))
        online = 'online' if s.get('online') else 'offline'
        pins.append(f'<span class="map-pin {online}" style="left:{x:.1f}%;top:{y:.1f}%" title="{name}｜{loc}"></span>')
    if not pins:
        pins.append('<span class="map-empty">暂无节点</span>')
    return '<div class="card ops-card topology-card"><h2>🗺️ 全国节点地图拓扑</h2><div class="topology-map"><div class="map-core">主控</div>' + ''.join(pins) + '</div><p class="small">按服务器地区自动生成拓扑点位，绿色在线、红色离线。</p></div>'

def ops_radar_html(servers):
    servers = list(servers or [])
    total = max(1, len(servers))
    offline = sum(1 for s in servers if not s.get('online'))
    cpu = max([_float((s.get('metrics') or {}).get('cpu_percent')) for s in servers] or [0])
    mem = max([_float((s.get('metrics') or {}).get('mem_percent')) for s in servers] or [0])
    disk = max([_float((s.get('metrics') or {}).get('disk_percent')) for s in servers] or [0])
    tcp = max([_int(_metric_pick(s.get('metrics') or {}, ['tcp_established', 'tcp_est', 'connections_established'], 0)) for s in servers] or [0])
    risk = max(offline*100/total, cpu, mem, disk, min(100, tcp/20))
    cls = 'danger' if risk >= 75 else 'warn' if risk >= 45 else 'ok'
    return f'''<div class="card ops-card radar-card {cls}"><h2>🛰️ 运维攻击流量雷达图</h2><div class="radar" style="--risk:{risk:.0f}%"><span>{risk:.0f}</span></div><div class="radar-legend"><b>离线 {offline}/{total}</b><b>CPU {cpu:.0f}%</b><b>内存 {mem:.0f}%</b><b>TCP {tcp}</b></div><p class="small">基于离线率、资源峰值、TCP 连接数做运维风险雷达判断。</p></div>'''

def ops_feature_panel(servers):
    return '''<div class="card ops-card feature-card"><h2>🔥 Komari 级霓虹监控能力</h2><div class="feature-grid"><span>1. WebSocket / SSE 0刷新</span><span>2. GPU / IO / TCP 连接监控</span><span>3. 全国节点地图拓扑</span><span>4. 攻击流量雷达图</span><span>5. AI 自动判断故障原因</span></div></div>'''

def _live_payload():
    init_db()
    d = summary()
    return {
        'type': 'live',
        'time': now(),
        'summary': {k: d[k] for k in ['total','online','offline','unknown','probes','expiring','expired']},
        'servers': [metric_json(x) for x in d.get('servers', [])],
        'local': _local_live_json(),
    }

@app.route('/api/live-stream')
@login_required
def api_live_stream():
    if _Response is None or _stream_with_context is None:
        abort(500)
    def gen():
        while True:
            try:
                yield 'data: ' + _json.dumps(_live_payload(), ensure_ascii=False) + '\n\n'
            except GeneratorExit:
                break
            except Exception as e:
                yield 'data: ' + _json.dumps({'type':'error','message':str(e)}, ensure_ascii=False) + '\n\n'
            time.sleep(1)
    resp = _Response(_stream_with_context(gen()), mimetype='text/event-stream')
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['X-Accel-Buffering'] = 'no'
    return resp

try:
    from flask_sock import Sock as _Sock
    _sock = _Sock(app)
    @_sock.route('/ws/live')
    def ws_live(ws):
        try:
            if not session.get('ok'):
                ws.close()
                return
        except Exception:
            pass
        while True:
            ws.send(_json.dumps(_live_payload(), ensure_ascii=False))
            time.sleep(1)
except Exception:
    _sock = None

_NEON_REQUEST_CSS = r'''

/* ===== user requested neon colorful clear UI patch ===== */
:root{--neonText:#f8fcff;--neonShadow:0 1px 2px rgba(0,0,0,.38),0 0 18px rgba(14,165,233,.20)}
html:not(.light) body{background:#15345f!important;color:#f8fcff!important}
html:not(.light) body:before{background-image:linear-gradient(rgba(255,255,255,.08),rgba(255,255,255,.10)),var(--custom-bg,none),radial-gradient(circle at 13% 10%,rgba(125,211,252,.72),transparent 30%),radial-gradient(circle at 85% 0%,rgba(244,114,182,.55),transparent 34%),radial-gradient(circle at 45% 112%,rgba(52,211,153,.50),transparent 36%),linear-gradient(135deg,#1d4ed8 0%,#7c3aed 38%,#0891b2 70%,#22c55e 112%)!important;filter:saturate(1.12) brightness(1.12)!important}
html.light body:before{background-image:linear-gradient(rgba(255,255,255,.20),rgba(255,255,255,.28)),var(--custom-bg,none),radial-gradient(circle at 12% 12%,rgba(56,189,248,.45),transparent 30%),radial-gradient(circle at 88% 4%,rgba(236,72,153,.32),transparent 34%),radial-gradient(circle at 50% 108%,rgba(34,197,94,.30),transparent 36%),linear-gradient(135deg,#e0f2fe,#f5d0fe 44%,#dcfce7 100%)!important}
.top h1,.brand b,h1,h2,h3{background:linear-gradient(90deg,#fff,#7dd3fc,#f0abfc,#86efac,#fde68a)!important;-webkit-background-clip:text!important;background-clip:text!important;color:transparent!important;-webkit-text-fill-color:transparent!important;text-shadow:0 0 1px rgba(255,255,255,.80),0 0 18px rgba(56,189,248,.24)!important;letter-spacing:.2px!important}
html.light .top h1,html.light .brand b,html.light h1,html.light h2,html.light h3{background:linear-gradient(90deg,#1d4ed8,#7e22ce,#0891b2,#16a34a)!important;-webkit-background-clip:text!important;background-clip:text!important;color:transparent!important;-webkit-text-fill-color:transparent!important;text-shadow:0 1px 0 rgba(255,255,255,.55)!important}
.card{background:linear-gradient(145deg,rgba(255,255,255,.30),rgba(255,255,255,.14))!important;border-color:rgba(255,255,255,.34)!important;box-shadow:0 18px 54px rgba(30,64,175,.22),inset 0 1px 0 rgba(255,255,255,.20)!important;color:var(--neonText)!important;text-shadow:var(--neonShadow)!important}
html.light .card{background:linear-gradient(145deg,rgba(255,255,255,.78),rgba(255,255,255,.54))!important;color:#0f172a!important;text-shadow:none!important}
.servercard{position:relative;overflow:hidden;background:linear-gradient(145deg,hsla(var(--h1),92%,62%,.30),hsla(var(--h2),92%,58%,.22) 52%,hsla(var(--h3),92%,58%,.18))!important;border-color:hsla(var(--h2),92%,76%,.46)!important}.servercard:before{content:"";position:absolute;inset:0;background:radial-gradient(circle at 10% 0,rgba(255,255,255,.22),transparent 34%),linear-gradient(120deg,transparent,rgba(255,255,255,.12),transparent);pointer-events:none}.servercard.expire-danger{animation:expireCardPulse 1.25s ease-in-out infinite;border-color:rgba(251,113,133,.75)!important;box-shadow:0 0 0 1px rgba(251,113,133,.24),0 18px 60px rgba(244,63,94,.28)!important}.servercard.expire-warn{box-shadow:0 18px 58px rgba(251,191,36,.18)!important;border-color:rgba(251,191,36,.50)!important}@keyframes expireCardPulse{0%,100%{filter:saturate(1) brightness(1)}50%{filter:saturate(1.25) brightness(1.16)}}
.expire-progress-wrap{margin:10px 0 8px}.expire-progress-title{display:flex;justify-content:space-between;gap:10px;font-size:12px;font-weight:950;color:#f8fbff;text-shadow:0 1px 2px rgba(0,0,0,.45)}html.light .expire-progress-title{color:#0f172a;text-shadow:none}.expire-progress{height:12px;border-radius:999px;background:rgba(255,255,255,.20);overflow:hidden;border:1px solid rgba(255,255,255,.22)}html.light .expire-progress{background:rgba(15,23,42,.12);border-color:rgba(15,23,42,.12)}.expire-bar{height:100%;border-radius:999px;background:linear-gradient(90deg,#22c55e,#06b6d4,#8b5cf6);position:relative;transition:width .8s cubic-bezier(.22,.61,.36,1)}.expire-bar:after{content:"";position:absolute;inset:0;background:linear-gradient(90deg,transparent,rgba(255,255,255,.55),transparent);animation:barFlow 1.35s linear infinite}.expire-bar.warn{background:linear-gradient(90deg,#f59e0b,#facc15,#fb923c)}.expire-bar.danger{background:linear-gradient(90deg,#ef4444,#fb7185,#f97316)}.expire-bar.forever{background:linear-gradient(90deg,#10b981,#22c55e,#84cc16)}
.metric-extra{grid-template-columns:repeat(auto-fit,minmax(130px,1fr))!important}.metric-extra .mini{background:linear-gradient(145deg,rgba(255,255,255,.18),rgba(255,255,255,.08))!important;border-color:rgba(255,255,255,.28)!important}.metric-extra .mini b{font-weight:1000!important}.ai-mini{grid-column:1/-1}.ai-mini span{line-height:1.55}.ops-grid{display:grid;grid-template-columns:1.1fr .9fr;gap:16px;margin-top:16px}.ops-card{min-height:270px}.feature-card{min-height:auto;margin-top:16px}.feature-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px}.feature-grid span{padding:12px;border-radius:16px;border:1px solid rgba(255,255,255,.24);background:linear-gradient(135deg,rgba(14,165,233,.22),rgba(168,85,247,.16));font-weight:950}.topology-map{position:relative;height:220px;border-radius:24px;overflow:hidden;border:1px solid rgba(255,255,255,.26);background:radial-gradient(circle at 50% 50%,rgba(255,255,255,.18),transparent 6%),linear-gradient(135deg,rgba(14,165,233,.18),rgba(34,197,94,.12)),repeating-linear-gradient(0deg,rgba(255,255,255,.08) 0 1px,transparent 1px 28px),repeating-linear-gradient(90deg,rgba(255,255,255,.08) 0 1px,transparent 1px 28px)}.map-core{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);padding:8px 12px;border-radius:999px;background:linear-gradient(90deg,#06b6d4,#8b5cf6);font-weight:1000;box-shadow:0 0 28px rgba(34,211,238,.45)}.map-pin{position:absolute;width:14px;height:14px;border-radius:50%;transform:translate(-50%,-50%);background:#22c55e;box-shadow:0 0 0 4px rgba(34,197,94,.20),0 0 20px rgba(34,197,94,.78);animation:pinPulse 1.6s ease-in-out infinite}.map-pin.offline{background:#fb7185;box-shadow:0 0 0 4px rgba(251,113,133,.22),0 0 20px rgba(251,113,133,.78)}@keyframes pinPulse{50%{transform:translate(-50%,-50%) scale(1.28)}}.map-empty{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);font-weight:950}.radar{width:180px;height:180px;margin:8px auto 14px;border-radius:50%;display:grid;place-items:center;background:conic-gradient(#22c55e var(--risk),rgba(255,255,255,.14) 0),repeating-radial-gradient(circle,transparent 0 21px,rgba(255,255,255,.16) 22px 23px);box-shadow:0 0 34px rgba(34,197,94,.28)}.radar-card.warn .radar{background:conic-gradient(#facc15 var(--risk),rgba(255,255,255,.14) 0),repeating-radial-gradient(circle,transparent 0 21px,rgba(255,255,255,.16) 22px 23px)}.radar-card.danger .radar{background:conic-gradient(#fb7185 var(--risk),rgba(255,255,255,.14) 0),repeating-radial-gradient(circle,transparent 0 21px,rgba(255,255,255,.16) 22px 23px)}.radar span{width:88px;height:88px;border-radius:50%;display:grid;place-items:center;background:rgba(8,17,35,.68);font-size:30px;font-weight:1000}.radar-legend{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}.radar-legend b{padding:8px;border-radius:12px;background:rgba(255,255,255,.12);font-size:12px}.neon-live-chip{display:inline-flex;align-items:center;gap:6px;margin-left:10px;padding:6px 10px;border-radius:999px;border:1px solid rgba(255,255,255,.28);background:linear-gradient(90deg,rgba(34,197,94,.24),rgba(14,165,233,.20));font-size:12px;font-weight:1000}.neon-live-chip:before{content:"";width:8px;height:8px;border-radius:50%;background:#22c55e;box-shadow:0 0 14px #22c55e;animation:dotPulse 1.2s infinite}.date-help{font-size:12px;color:var(--muted);margin-top:5px;font-weight:850}
@media(max-width:900px){.ops-grid{grid-template-columns:1fr}.radar{width:150px;height:150px}.metric-extra{grid-template-columns:1fr!important}}
/* ===== end user requested neon colorful clear UI patch ===== */
'''

_NEON_REQUEST_JS = r'''

/* ===== user requested websocket/sse 0-refresh frontend patch ===== */
(function(){
  window.__neonLiveOk=false;
  function applyKpi(sum,time){if(sum){['total','online','offline','probes','expiring','expired','unknown'].forEach(k=>{let e=document.querySelector('[data-kpi="'+k+'"]');if(e&&sum[k]!==undefined)e.textContent=sum[k];});}if(time){document.querySelectorAll('[data-now]').forEach(e=>e.textContent=time);}}
  function paintExtraServer(s){if(!s||!s.id)return;setHtml('[data-gpu="'+s.id+'"]',s.gpu_html||'未上报');setHtml('[data-io="'+s.id+'"]',s.io_html||'未上报');setHtml('[data-tcp="'+s.id+'"]',s.tcp_html||'未上报');setText('[data-ai="'+s.id+'"]',s.ai_html||'运行稳定');setText('[data-exptext="'+s.id+'"]',s.expire_text||'');document.querySelectorAll('[data-expbar="'+s.id+'"]').forEach(e=>{let v=Number(s.expire_percent||0);e.style.width=Math.max(0,Math.min(100,v))+'%';e.classList.remove('ok','warn','danger','forever','unknown');e.classList.add(s.expire_class||'unknown');});document.querySelectorAll('[data-expwrap="'+s.id+'"]').forEach(e=>{e.classList.remove('ok','warn','danger','forever','unknown');e.classList.add(s.expire_class||'unknown');});document.querySelectorAll('[data-server-card="'+s.id+'"]').forEach(e=>{e.classList.remove('expire-ok','expire-warn','expire-danger','expire-forever','expire-unknown');if(s.card_class)e.classList.add(s.card_class);});}
  if(window.paintServer){const oldPaint=window.paintServer;window.paintServer=function(s){oldPaint(s);paintExtraServer(s);};}
  function paintLocalExtra(j){if(!j)return;setLocalHtml('[data-local-gpu]',j.gpu_html||'未检测到 GPU');setLocalHtml('[data-local-io]',j.io_html||'R 0B/s ｜ W 0B/s');setLocalHtml('[data-local-tcp]',j.tcp_html||'EST 0 ｜ LISTEN 0 ｜ TW 0');setLocalText('[data-local-ai]',j.ai_html||'本机实时判断中');}
  window.neonApplyPacket=function(j){if(!j)return;window.__neonLiveOk=true;applyKpi(j.summary,j.time);(j.servers||[]).forEach(s=>{if(window.paintServer)window.paintServer(s);else paintExtraServer(s);});if(j.local){paintLocalExtra(j.local);}let chip=document.querySelector('[data-neon-live]');if(chip)chip.textContent='WebSocket/SSE 0刷新已连接';};
  const oldLive=window.live;if(oldLive){window.live=async function(){if(window.__neonLiveOk)return;return oldLive();};}const oldRefreshKpi=window.refreshKpi;if(oldRefreshKpi){window.refreshKpi=async function(){if(window.__neonLiveOk)return;return oldRefreshKpi();};}
  async function localExtraOnce(){if(window.__neonLiveOk)return;try{let j=await(await fetch('/api/local-live?t='+Date.now(),{cache:'no-store'})).json();paintLocalExtra(j);}catch(e){}}
  setInterval(localExtraOnce,1000);
  document.addEventListener('DOMContentLoaded',function(){let opened=false;try{const proto=location.protocol==='https:'?'wss':'ws';const ws=new WebSocket(proto+'://'+location.host+'/ws/live');ws.onopen=()=>{opened=true;window.__neonLiveOk=true;let chip=document.querySelector('[data-neon-live]');if(chip)chip.textContent='WebSocket 0刷新已连接';};ws.onmessage=(ev)=>{try{window.neonApplyPacket(JSON.parse(ev.data));}catch(e){}};ws.onclose=()=>{if(opened)return;startSSE();};ws.onerror=()=>{if(!opened)startSSE();};setTimeout(()=>{if(!opened)startSSE();},1200);}catch(e){startSSE();}});
  function startSSE(){if(window.__neonSSE)return;window.__neonSSE=true;try{const es=new EventSource('/api/live-stream');es.onopen=()=>{window.__neonLiveOk=true;let chip=document.querySelector('[data-neon-live]');if(chip)chip.textContent='SSE 0刷新已连接';};es.onmessage=(ev)=>{try{window.neonApplyPacket(JSON.parse(ev.data));}catch(e){}};es.onerror=()=>{window.__neonLiveOk=false;let chip=document.querySelector('[data-neon-live]');if(chip)chip.textContent='回退到轮询刷新';};}catch(e){window.__neonLiveOk=false;}}
})();
/* ===== end user requested websocket/sse 0-refresh frontend patch ===== */
'''

def _apply_requested_template_patch():
    global BASE, LOGIN, DASH, SERVERS, DETAIL, FORM, LOCAL
    if 'user requested neon colorful clear UI patch' not in BASE:
        BASE = BASE.replace('</style><script>', _NEON_REQUEST_CSS + '</style><script>')
    if 'user requested websocket/sse 0-refresh frontend patch' not in BASE:
        BASE = BASE.replace('</script></head><body>', _NEON_REQUEST_JS + '</script></head><body>')
    if 'user requested neon colorful clear UI patch' not in LOGIN:
        LOGIN = LOGIN.replace('</style><script>', _NEON_REQUEST_CSS + '</style><script>')
    FORM = FORM.replace('type=datetime-local value="{{datetime_input_value(s.expire_at or \'\')}}" placeholder="请选择到期日期和时间"', 'type=date value="{{datetime_input_value(s.expire_at or \'\')}}" placeholder="请选择到期日期"><div class=date-help>只选择年月日，不再保存时间。</div')
    LOCAL = LOCAL.replace('type=date value="{{datetime_input_value(profile.expire_at)}}"', 'type=date value="{{datetime_input_value(profile.expire_at)}}"')
    DASH = DASH.replace('📊✨ 服务器总览大屏', '📊🌈 霓虹风监控大屏 <span class="neon-live-chip" data-neon-live>正在连接0刷新</span>')
    for name in ('DASH','SERVERS'):
        val = globals()[name]
        val = val.replace('<div class="card servercard">', '<div class="card servercard {{server_card_class(s)}}" data-server-card="{{s.id}}" style="{{server_card_style(s)}}">')
        val = val.replace('<div class="card servercard"><h3', '<div class="card servercard {{server_card_class(s)}}" data-server-card="{{s.id}}" style="{{server_card_style(s)}}"><h3')
        val = val.replace('</span></div><div class=small data-hw="{{s.id}}">', '</span></div>{{expire_progress_html(s)|safe}}<div class=small data-hw="{{s.id}}">')
        val = val.replace('<br><span class="{{status_color_class_by_days(s)}}">{{display_expire_label(s)}}</span></td>', '<br><span class="{{status_color_class_by_days(s)}}">{{display_expire_label(s)}}</span>{{expire_progress_html(s)|safe}}</td>')
        val = val.replace('<div class="mini"><b>负载</b><span data-load="{{s.id}}">0.00 ｜ 0.00 ｜ 0.00</span></div></div>', '<div class="mini"><b>负载</b><span data-load="{{s.id}}">0.00 ｜ 0.00 ｜ 0.00</span></div><div class="mini"><b>GPU</b><span data-gpu="{{s.id}}">{{metric_gpu_html(m)|safe}}</span></div><div class="mini"><b>IO</b><span data-io="{{s.id}}">{{metric_io_html(m)|safe}}</span></div><div class="mini"><b>TCP</b><span data-tcp="{{s.id}}">{{metric_tcp_html(m)|safe}}</span></div><div class="mini ai-mini"><b>AI 故障判断</b><span data-ai="{{s.id}}">{{ai_fault_html(s)}}</span></div></div>')
        globals()[name] = val
    DASH = DASH.replace('<div class=card style="margin-top:16px"><h2>🖥️ 所有服务器</h2>', '<div class="ops-grid">{{node_topology_html(data.servers)|safe}}{{ops_radar_html(data.servers)|safe}}</div>{{ops_feature_panel(data.servers)|safe}}<div class=card style="margin-top:16px"><h2>🖥️ 所有服务器</h2>')
    DASH = DASH.replace('<div class="mini"><b>负载</b><span data-local-load>0.00 ｜ 0.00 ｜ 0.00</span></div>\n</div>', '<div class="mini"><b>负载</b><span data-local-load>0.00 ｜ 0.00 ｜ 0.00</span></div>\n  <div class="mini"><b>GPU</b><span data-local-gpu>未检测到 GPU</span></div>\n  <div class="mini"><b>IO</b><span data-local-io>R 0B/s ｜ W 0B/s</span></div>\n  <div class="mini"><b>TCP</b><span data-local-tcp>EST 0 ｜ LISTEN 0 ｜ TW 0</span></div>\n  <div class="mini ai-mini"><b>AI 故障判断</b><span data-local-ai>本机实时判断中</span></div>\n</div>')
    LOCAL = LOCAL.replace('<div class="mini"><b>负载</b><span data-local-load>0.00 ｜ 0.00 ｜ 0.00</span></div></div>', '<div class="mini"><b>负载</b><span data-local-load>0.00 ｜ 0.00 ｜ 0.00</span></div><div class="mini"><b>GPU</b><span data-local-gpu>未检测到 GPU</span></div><div class="mini"><b>IO</b><span data-local-io>R 0B/s ｜ W 0B/s</span></div><div class="mini"><b>TCP</b><span data-local-tcp>EST 0 ｜ LISTEN 0 ｜ TW 0</span></div><div class="mini ai-mini"><b>AI 故障判断</b><span data-local-ai>本机实时判断中</span></div></div>')
    DETAIL = DETAIL.replace('<div class=small>{{price_text(s)}}｜{{cycle_cn(s.cycle)}}</div></div></div>', '<div class=small>{{price_text(s)}}｜{{cycle_cn(s.cycle)}}</div>{{expire_progress_html(s)|safe}}</div></div>')

_apply_requested_template_patch()

app.jinja_env.globals.update(
    expire_progress_html=expire_progress_html,
    expire_progress_info=expire_progress_info,
    server_card_class=server_card_class,
    server_card_style=server_card_style,
    metric_gpu_html=metric_gpu_html,
    metric_io_html=metric_io_html,
    metric_tcp_html=metric_tcp_html,
    ai_fault_html=ai_fault_html,
    node_topology_html=node_topology_html,
    ops_radar_html=ops_radar_html,
    ops_feature_panel=ops_feature_panel,
    datetime_input_value=datetime_input_value,
)
# ===== END USER REQUEST PATCH =====


# ===== USER COMPACT READABILITY PATCH: smaller cards + readable text + colorful expiry bars =====
def expire_progress_info(s):
    d = expire_days_value(s)
    if d is None:
        return {'days': None, 'percent': 0, 'class': 'unknown', 'text': '未设置到期日'}
    if d == 999999:
        return {'days': d, 'percent': 100, 'class': 'forever', 'text': '♾️ 永久 / 免费'}
    if d < 0:
        return {'days': d, 'percent': 100, 'class': 'danger', 'text': f'🚨 已过期 {abs(d)} 天'}
    total = max(1, expire_cycle_days(s))
    # 倒计时进度：按当前付费周期剩余天数计算，越接近到期越短。
    pct = max(4, min(100, d * 100 / total)) if d > 0 else 100
    # 不再把 7 天内全部渲染成红色：今天/3天内红色，4-15天黄色，15天以上彩色安全条。
    cls = 'danger' if d <= 3 else 'warn' if d <= 15 else 'ok'
    label = '🚨 今天到期' if d == 0 else f'📆 剩余 {d} 天'
    return {'days': d, 'percent': pct, 'class': cls, 'text': label}

def expire_progress_html(s):
    info = expire_progress_info(s)
    sid = html.escape(str((s or {}).get('id') or '0'))
    cls = info['class']
    pct = float(info['percent'] or 0)
    text = html.escape(info['text'])
    return f'''<div class="expire-progress-wrap {cls}" data-expwrap="{sid}"><div class="expire-progress-title"><span data-exptext="{sid}">{text}</span><b>{pct:.0f}%</b></div><div class="expire-progress"><div class="expire-bar {cls}" data-expbar="{sid}" style="width:{pct:.0f}%"></div></div></div>'''

def server_card_class(s):
    info = expire_progress_info(s)
    cls = info.get('class') or 'unknown'
    return f'expire-{cls}'

def overview_compact_risk_html(data):
    data = data or {}
    ss = list(data.get('servers') or [])
    total = int(data.get('total') or len(ss) or 0)
    probes = int(data.get('probes') or 0)
    expiring = int(data.get('expiring') or 0)
    expired = int(data.get('expired') or 0)
    unknown = int(data.get('unknown') or 0)
    offline = int(data.get('offline') or 0)
    auto = sum(1 for s in ss if truth(s.get('auto_renew')))
    free = sum(1 for s in ss if truth(s.get('free_forever')))
    stale = sum(1 for s in ss if not fresh(s.get('metrics') or {}))
    healthy = max(0, total - offline - expired)
    health_pct = round(healthy * 100 / total) if total else 100
    def chip(cls, label, val, k=''):
        attr = f' data-kpi={html.escape(k)}' if k else ''
        return f'<div class="risk-chip {cls}"><span>{html.escape(label)}</span><b{attr}>{html.escape(str(val))}</b></div>'
    chips = ''.join([
        chip('warn','7天内到期',expiring,'expiring'),
        chip('bad','已过期',expired,'expired'),
        chip('muted','未知状态',unknown,'unknown'),
        chip('ok','健康率',f'{health_pct}%'),
    ])
    tools = ''.join([
        f'<span>📡 探针覆盖 <b>{probes}/{total}</b></span>',
        f'<span>🔁 自动续费 <b>{auto}</b></span>',
        f'<span>♾️ 永久/免费 <b>{free}</b></span>',
        f'<span>🧭 数据超时 <b>{stale}</b></span>',
        '<span>🧠 AI故障判断 <b>实时</b></span>',
        '<span>🔌 WebSocket/SSE <b>0刷新</b></span>',
    ])
    return f'''<div class="card compact-risk-card"><h2>⏰ 到期与风险概览</h2><div class="risk-chip-grid">{chips}</div><div class="risk-tool-grid">{tools}</div></div>'''

_COMPACT_READABILITY_CSS = r'''

/* ===== compact readability patch requested by user ===== */
html:not(.light) body:before{background-image:linear-gradient(rgba(255,255,255,.10),rgba(255,255,255,.14)),var(--custom-bg,none),radial-gradient(circle at 12% 8%,rgba(56,189,248,.64),transparent 28%),radial-gradient(circle at 86% 2%,rgba(244,114,182,.48),transparent 32%),radial-gradient(circle at 46% 110%,rgba(34,197,94,.42),transparent 34%),linear-gradient(135deg,#2563eb 0%,#8b5cf6 38%,#06b6d4 72%,#22c55e 112%)!important;filter:saturate(1.18) brightness(1.18)!important}
body{font-size:14px!important;line-height:1.5!important;font-weight:680!important}.layout{grid-template-columns:220px 1fr!important}.side{padding:16px!important}.main{padding:16px!important}.top{gap:10px!important;margin-bottom:12px!important}.top h1{font-size:clamp(23px,2.4vw,34px)!important}.brand{gap:9px!important}.brand .ico{width:36px!important;height:36px!important;border-radius:14px!important}.nav a{padding:9px 10px!important;border-radius:13px!important;font-size:13px!important}.switches{gap:7px!important}.themebtn,.btn,button{padding:8px 10px!important;border-radius:12px!important;font-size:13px!important}.card{padding:13px!important;border-radius:18px!important;box-shadow:0 12px 36px rgba(30,64,175,.18),inset 0 1px 0 rgba(255,255,255,.16)!important}.grid,.grid2,.grid3,.cardgrid{gap:10px!important}.grid{grid-template-columns:repeat(auto-fit,minmax(145px,1fr))!important}.grid2{grid-template-columns:minmax(0,1.15fr) minmax(300px,.85fr)!important}.grid3{grid-template-columns:repeat(auto-fit,minmax(90px,1fr))!important}.cardgrid{grid-template-columns:repeat(auto-fit,minmax(245px,1fr))!important}.value,.kpi .value{font-size:28px!important;line-height:1.05!important}.label{font-size:12px!important}.badge{padding:5px 8px!important;border-radius:999px!important;font-size:12px!important}.small,.muted{font-size:12px!important}h2{font-size:17px!important;margin:0 0 9px!important}h3{font-size:15.5px!important;margin:0 0 8px!important}hr{margin:10px 0!important}.table th,.table td{padding:8px!important;font-size:13px!important;line-height:1.5!important}.scrollbox{padding:6px!important}input,select,textarea,pre{font-size:13px!important;padding:9px 10px!important;border-radius:12px!important}.progressrow{gap:7px!important;margin:6px 0!important;font-size:12px!important}.progress{height:9px!important}.bar{height:100%!important}.metric-extra{gap:6px!important;margin-top:8px!important;grid-template-columns:repeat(auto-fit,minmax(112px,1fr))!important}.metric-extra .mini{padding:6px 7px!important;min-height:auto!important;border-radius:11px!important;font-size:11.5px!important}.metric-extra .mini b{font-size:11px!important}.ai-mini span{line-height:1.4!important}.ops-grid{gap:10px!important;margin-top:10px!important}.ops-card{min-height:190px!important}.topology-map{height:160px!important;border-radius:18px!important}.radar{width:128px!important;height:128px!important;margin:4px auto 8px!important}.radar span{width:62px!important;height:62px!important;font-size:21px!important}.radar-legend{gap:6px!important}.radar-legend b{padding:6px!important;font-size:11px!important}.feature-card{margin-top:10px!important}.feature-grid{grid-template-columns:repeat(auto-fit,minmax(150px,1fr))!important;gap:7px!important}.feature-grid span{padding:8px 9px!important;border-radius:12px!important;font-size:12px!important}.neon-live-chip{padding:4px 8px!important;font-size:11px!important}.server-title,.server-title .name,.servercard h3,.servercard h3 .name{background:none!important;-webkit-background-clip:initial!important;background-clip:initial!important;color:#ffffff!important;-webkit-text-fill-color:#ffffff!important;text-shadow:0 1px 3px rgba(0,0,0,.82),0 0 13px rgba(14,165,233,.32)!important;font-weight:1000!important}.server-title .name{display:inline-block;max-width:100%;word-break:break-word}html.light .server-title,html.light .server-title .name,html.light .servercard h3,html.light .servercard h3 .name{color:#0f172a!important;-webkit-text-fill-color:#0f172a!important;text-shadow:0 1px 0 rgba(255,255,255,.72)!important}.card h2,.card h3:not(.server-title),.table td b,.metric-extra .mini b,.risk-tool-grid b{background:none!important;-webkit-background-clip:initial!important;background-clip:initial!important;color:#f8fbff!important;-webkit-text-fill-color:#f8fbff!important;text-shadow:0 1px 3px rgba(0,0,0,.70)!important}.top h1{background:linear-gradient(90deg,#fff,#7dd3fc,#f0abfc,#86efac,#fde68a)!important;-webkit-background-clip:text!important;background-clip:text!important;color:transparent!important;-webkit-text-fill-color:transparent!important;text-shadow:0 0 2px rgba(255,255,255,.95),0 0 14px rgba(56,189,248,.34)!important}html.light .card h2,html.light .card h3:not(.server-title),html.light .table td b,html.light .metric-extra .mini b,html.light .risk-tool-grid b{color:#0f172a!important;-webkit-text-fill-color:#0f172a!important;text-shadow:none!important}.card p,.card span,.card div,.table td{color:inherit}.expire-progress-wrap{margin:7px 0 6px!important}.expire-progress-title{font-size:11px!important}.expire-progress{height:10px!important;background:rgba(255,255,255,.20)!important}.expire-bar{background:linear-gradient(90deg,#22c55e,#06b6d4,#8b5cf6,#ec4899)!important}.expire-bar.warn{background:linear-gradient(90deg,#facc15,#fb923c,#f472b6)!important}.expire-bar.danger{background:linear-gradient(90deg,#ef4444,#f97316,#facc15)!important}.expire-bar.forever{background:linear-gradient(90deg,#10b981,#22c55e,#84cc16,#06b6d4)!important}.servercard.expire-danger{animation:expireCardPulse 1.8s ease-in-out infinite!important}.servercard.expire-warn{box-shadow:0 12px 36px rgba(251,191,36,.14)!important}.compact-risk-card{min-height:0!important}.risk-chip-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:7px}.risk-chip{padding:8px;border-radius:14px;border:1px solid rgba(255,255,255,.22);background:linear-gradient(145deg,rgba(255,255,255,.16),rgba(255,255,255,.07));display:grid;gap:2px}.risk-chip span{font-size:11px;color:rgba(247,251,255,.88);font-weight:850}.risk-chip b{font-size:22px;line-height:1;font-weight:1000}.risk-chip.ok b{color:#86efac}.risk-chip.warn b{color:#fde68a}.risk-chip.bad b{color:#fb7185}.risk-chip.muted b{color:#c4b5fd}.risk-tool-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:7px;margin-top:9px}.risk-tool-grid span{display:flex;align-items:center;justify-content:space-between;gap:6px;padding:7px 8px;border-radius:12px;border:1px solid rgba(255,255,255,.20);background:rgba(255,255,255,.10);font-size:11.5px;font-weight:850}html.light .risk-chip span{color:#334155}html.light .risk-tool-grid span{background:rgba(255,255,255,.68);border-color:rgba(15,23,42,.12)}@media(max-width:980px){.layout{grid-template-columns:1fr!important}.side{position:relative!important;height:auto!important}.grid2{grid-template-columns:1fr!important}.risk-chip-grid{grid-template-columns:repeat(2,1fr)}.risk-tool-grid{grid-template-columns:1fr}.ops-grid{grid-template-columns:1fr!important}.cardgrid{grid-template-columns:1fr!important}}
/* ===== end compact readability patch requested by user ===== */
'''

def _apply_compact_readability_patch():
    global BASE, LOGIN, DASH, SERVERS, DETAIL, FORM, LOCAL
    if 'compact readability patch requested by user' not in BASE:
        BASE = BASE.replace('</style><script>', _COMPACT_READABILITY_CSS + '</style><script>')
    if 'compact readability patch requested by user' not in LOGIN:
        LOGIN = LOGIN.replace('</style><script>', _COMPACT_READABILITY_CSS + '</style><script>')
    old = '''<div class=card><h2>⏰ 到期和风险</h2><div class=grid3><div class=card><div class=label>⚠️ 7天内到期</div><div class="value warn" data-kpi=expiring>{{data.expiring}}</div></div><div class=card><div class=label>🚨 已过期</div><div class="value bad" data-kpi=expired>{{data.expired}}</div></div><div class=card><div class=label>⚪ 未知</div><div class=value data-kpi=unknown>{{data.unknown}}</div></div></div></div>'''
    new = '{{overview_compact_risk_html(data)|safe}}'
    if old in DASH:
        DASH = DASH.replace(old, new)
    app.jinja_env.globals.update(
        expire_progress_html=expire_progress_html,
        expire_progress_info=expire_progress_info,
        server_card_class=server_card_class,
        overview_compact_risk_html=overview_compact_risk_html,
    )

_apply_compact_readability_patch()
# ===== END USER COMPACT READABILITY PATCH =====

app.jinja_env.globals.update(fmt=fmt,dur=dur,duration=dur,exptext=exptext,expire_text=exptext,pricet=pricet,price_text=pricet,cycle=cycle,cycle_cn=cycle,fresh=fresh,flag=flag,server_flag=server_flag,fmt_size=fmt,age=age,server_location_cn=server_location_cn,bar_class=bar_class,flag_icon=flag_icon,server_country_code=server_country_code,status_color_class_by_days=status_color_class_by_days,active_theme_css=active_theme_css,progress_row=progress_row,site_name=site_name,favicon_exists=favicon_exists,clean_event_html=clean_event_html,event_context=event_context,display_price_label=display_price_label,display_expire_label=display_expire_label,metric_config_html=metric_config_html,site_name_value=site_name_value,bot_token_value=bot_token_value,admin_ids_value=admin_ids_value,datetime_input_value=datetime_input_value,probe_os_name_for_server=probe_os_name_for_server)
# ===== FINAL USER PATCH: readable main titles + per-server expiry gradients + global radar world map =====
def page_title_html(emoji, text, chip=''):
    chip_html = f'<span class="title-chip">{html.escape(str(chip))}</span>' if chip else ''
    return f'<span class="title-emoji">{emoji}</span><span class="title-text">{html.escape(str(text))}</span>{chip_html}'


def _expire_gradient_for_days(days):
    # 不同剩余天数给不同颜色，而不是整批同色
    if days is None:
        return 'linear-gradient(90deg,#94a3b8,#cbd5e1,#e2e8f0)'
    if days == 999999:
        return 'linear-gradient(90deg,#10b981,#22c55e,#2dd4bf,#60a5fa)'
    if days < 0:
        return 'linear-gradient(90deg,#ef4444,#fb7185,#f97316,#facc15)'
    if days == 0:
        return 'linear-gradient(90deg,#dc2626,#f97316,#facc15)'
    d = max(1, min(120, int(days)))
    hue = int(d / 120 * 160)
    h1 = hue
    h2 = min(195, hue + 28)
    h3 = min(245, hue + 56)
    return f'linear-gradient(90deg,hsl({h1} 84% 58%),hsl({h2} 92% 62%),hsl({h3} 96% 68%))'


def expire_progress_info(s):
    d = expire_days_value(s)
    if d is None:
        return {'days': None, 'percent': 0, 'class': 'unknown', 'text': '未设置到期日', 'gradient': _expire_gradient_for_days(None)}
    if d == 999999:
        return {'days': d, 'percent': 100, 'class': 'forever', 'text': '♾️ 永久 / 免费', 'gradient': _expire_gradient_for_days(d)}
    if d < 0:
        return {'days': d, 'percent': 100, 'class': 'danger', 'text': f'🚨 已过期 {abs(d)} 天', 'gradient': _expire_gradient_for_days(d)}
    total = max(1, expire_cycle_days(s))
    pct = max(4, min(100, d * 100 / total)) if d > 0 else 100
    cls = 'danger' if d <= 3 else 'warn' if d <= 15 else 'ok'
    label = '🚨 今天到期' if d == 0 else f'📆 剩余 {d} 天'
    return {'days': d, 'percent': pct, 'class': cls, 'text': label, 'gradient': _expire_gradient_for_days(d)}


def expire_progress_html(s):
    info = expire_progress_info(s)
    sid = html.escape(str((s or {}).get('id') or '0'))
    cls = info['class']
    pct = float(info['percent'] or 0)
    text = html.escape(info['text'])
    grad = html.escape(info.get('gradient') or _expire_gradient_for_days(info.get('days')))
    return f"<div class=\"expire-progress-wrap {cls}\" data-expwrap=\"{sid}\" style=\"--exp-grad:{grad}\"><div class=\"expire-progress-title\"><span data-exptext=\"{sid}\">{text}</span><b>{pct:.0f}%</b></div><div class=\"expire-progress\"><div class=\"expire-bar {cls}\" data-expbar=\"{sid}\" style=\"width:{pct:.0f}%\"></div></div></div>"


def _global_pin_xy_for_server(s, i=0):
    code = server_country_code(s)
    city = str((s or {}).get('city') or (s or {}).get('region') or '').lower()
    mapping = {
        'CN': (73, 43), 'HK': (76, 50), 'TW': (79, 49), 'JP': (84, 42), 'KR': (81, 40),
        'SG': (74, 63), 'IN': (64, 53), 'TH': (71, 57), 'VN': (74, 56), 'MY': (73, 61),
        'PH': (80, 58), 'ID': (79, 67), 'AE': (58, 49), 'TR': (54, 41), 'RU': (64, 23),
        'DE': (50, 33), 'FR': (48, 36), 'GB': (45, 29), 'NL': (49, 31), 'ES': (45, 40),
        'IT': (52, 39), 'US': (21, 37), 'CA': (20, 25), 'MX': (18, 48), 'BR': (31, 70),
        'AR': (29, 83), 'CL': (24, 78), 'AU': (83, 77), 'NZ': (92, 82), 'ZA': (54, 81)
    }
    if 'beijing' in city or '北京' in city:
        return (74, 39)
    if 'shanghai' in city or '上海' in city:
        return (76, 45)
    if 'guangzhou' in city or '深圳' in city or '广州' in city:
        return (75, 52)
    return mapping.get(code, ((14 + i*11) % 82 + 8, (18 + i*13) % 60 + 18))


def _world_map_svg():
    return """<svg class=\"world-svg\" viewBox=\"0 0 1000 520\" preserveAspectRatio=\"none\" aria-hidden=\"true\">\n    <g class=\"world-outline\">\n      <path d=\"M88 118l42-25 58 8 54 24 26 33 2 37-31 26-39-5-38 18-29 46-49 11-38-34 14-49 30-27-8-38z\"/>\n      <path d=\"M244 322l51 30 23 54-17 62-30 41-35-10-19-57 7-62 20-58z\"/>\n      <path d=\"M447 122l43-18 42 10 8 23-34 18-39-12z\"/>\n      <path d=\"M472 168l48-11 61 12 40 25 23 20 22 1 14-13 34 4 16 19-6 22-26 16-34 10-11 30-24 13-18 30-31 5-37-14-26 23-31-11-22-37-42-34-29-37 6-37 34-24 20-42z\"/>\n      <path d=\"M744 330l49 15 62 34 45 38-5 43-37 26-63-5-41-25-26-45 2-37 14-44z\"/>\n      <path d=\"M825 193l35-8 34 7 16 17-11 18-34 7-24 23-30 9-26-10-12-20 18-16 18-27z\"/>\n      <path d=\"M515 388l39 7 32 20-7 29-33 10-35-12-15-22 19-32z\"/>\n    </g></svg>"""


def node_topology_html(servers):
    servers = list(servers or [])
    pins = []
    lines = []
    hub_x, hub_y = 73, 43
    for i, s in enumerate(servers[:36]):
        x, y = _global_pin_xy_for_server(s, i)
        name = html.escape(str(s.get('name') or f'节点{i+1}'))
        loc = html.escape(str(s.get('location_cn') or s.get('location') or '未知'))
        online = 'online' if s.get('online') else 'offline'
        angle = (1 if x >= hub_x else -1) * max(4, abs(hub_y - y) * 1.18)
        lines.append(f'<span class="world-link {online}" style="left:{min(x,hub_x):.1f}%;top:{min(y,hub_y):.1f}%;width:{abs(hub_x-x):.1f}%;transform:rotate({angle:.1f}deg)"></span>')
        pins.append(f'<span class="world-pin {online}" style="left:{x:.1f}%;top:{y:.1f}%" title="{name}｜{loc}"><i></i><b>{name}</b></span>')
    if not pins:
        pins.append('<span class="map-empty">暂无节点</span>')
    return '<div class="card ops-card topology-card"><h2>🗺️ 全球节点雷达地图</h2><div class="world-radar"><div class="world-grid"></div>' + _world_map_svg() + '<div class="scan-ring ring1"></div><div class="scan-ring ring2"></div><div class="scan-ring ring3"></div><div class="scan-beam"></div><div class="radar-hub">主控</div>' + ''.join(lines) + ''.join(pins) + '</div><p class="small">全球节点以雷达地图方式显示：绿色在线、红色离线，中心为主控侧汇聚点。</p></div>'


def ops_feature_panel(servers):
    return '<div class="card ops-card feature-card"><h2>🔥 Komari 级霓虹监控能力</h2><div class="feature-grid"><span>1. WebSocket / SSE 0刷新</span><span>2. GPU / IO / TCP 连接监控</span><span>3. 全球节点雷达地图</span><span>4. 攻击流量雷达图</span><span>5. AI 自动判断故障原因</span></div></div>'


_FINAL_TITLE_MAP_CSS = r'''
/* ===== final readability + world radar patch ===== */
.top h1{display:flex!important;align-items:center!important;gap:10px!important;flex-wrap:wrap!important;background:none!important;color:#f8fbff!important;-webkit-text-fill-color:#f8fbff!important;text-shadow:0 2px 8px rgba(0,0,0,.74),0 0 18px rgba(56,189,248,.26)!important;font-weight:1000!important;letter-spacing:.2px!important}
.top h1 .title-emoji{display:inline-flex!important;align-items:center!important;line-height:1!important;font-size:1.05em!important;background:none!important;color:initial!important;-webkit-text-fill-color:initial!important;text-shadow:0 2px 6px rgba(0,0,0,.32)!important;filter:saturate(1.08) drop-shadow(0 2px 6px rgba(255,255,255,.12))!important}
.top h1 .title-text{display:inline-block!important;background:none!important;color:#ffffff!important;-webkit-text-fill-color:#ffffff!important;text-shadow:0 2px 8px rgba(0,0,0,.78),0 0 14px rgba(125,211,252,.24)!important}
.top h1 .title-chip{display:inline-flex!important;align-items:center!important;gap:6px!important;padding:4px 10px!important;border-radius:999px!important;border:1px solid rgba(255,255,255,.24)!important;background:linear-gradient(135deg,rgba(34,197,94,.18),rgba(56,189,248,.14),rgba(168,85,247,.16))!important;color:#f8fbff!important;-webkit-text-fill-color:#f8fbff!important;font-size:12px!important;font-weight:900!important;text-shadow:none!important;box-shadow:0 0 18px rgba(34,197,94,.12)!important}
html.light .top h1,html.light .top h1 .title-text{color:#0f172a!important;-webkit-text-fill-color:#0f172a!important;text-shadow:0 1px 0 rgba(255,255,255,.72),0 0 10px rgba(255,255,255,.28)!important}
html.light .top h1 .title-chip{color:#0f172a!important;-webkit-text-fill-color:#0f172a!important;background:linear-gradient(135deg,rgba(186,230,253,.86),rgba(233,213,255,.88),rgba(254,240,138,.84))!important;border-color:rgba(15,23,42,.10)!important}

.expire-progress-wrap{--exp-grad:linear-gradient(90deg,#22c55e,#06b6d4,#8b5cf6)!important;border:1px solid rgba(255,255,255,.14)!important;border-radius:14px!important;padding:7px 8px!important;background:linear-gradient(180deg,rgba(255,255,255,.10),rgba(255,255,255,.04))!important}
.expire-progress-wrap .expire-progress{height:10px!important;border-radius:999px!important;overflow:hidden!important;background:rgba(255,255,255,.12)!important}
.expire-progress-wrap .expire-bar{background:var(--exp-grad)!important;box-shadow:0 0 15px rgba(255,255,255,.12),0 0 22px rgba(56,189,248,.14)!important;border-radius:999px!important}
.expire-progress-wrap.forever{box-shadow:0 0 18px rgba(16,185,129,.10)!important}
.expire-progress-wrap.warn{box-shadow:0 0 18px rgba(250,204,21,.08)!important}
.expire-progress-wrap.danger{box-shadow:0 0 18px rgba(239,68,68,.12)!important}

.ops-grid{grid-template-columns:minmax(0,1.18fr) minmax(300px,.82fr)!important;align-items:stretch!important}
.topology-card{overflow:hidden!important}
.world-radar{position:relative!important;height:300px!important;border-radius:22px!important;overflow:hidden!important;border:1px solid rgba(255,255,255,.16)!important;background:radial-gradient(circle at 72% 43%,rgba(34,197,94,.10),transparent 12%),radial-gradient(circle at 50% 50%,rgba(56,189,248,.07),transparent 38%),linear-gradient(180deg,rgba(2,6,23,.62),rgba(15,23,42,.82))!important;box-shadow:inset 0 0 0 1px rgba(255,255,255,.04),0 14px 38px rgba(2,6,23,.25)!important}
.world-grid{position:absolute!important;inset:0!important;background-image:linear-gradient(rgba(148,163,184,.08) 1px,transparent 1px),linear-gradient(90deg,rgba(148,163,184,.08) 1px,transparent 1px)!important;background-size:28px 28px!important;opacity:.7!important}
.world-svg{position:absolute!important;inset:0!important;width:100%!important;height:100%!important;opacity:.95!important}
.world-outline path{fill:rgba(59,130,246,.14)!important;stroke:rgba(148,163,184,.42)!important;stroke-width:2.2!important}
.scan-ring{position:absolute!important;left:73%!important;top:43%!important;transform:translate(-50%,-50%)!important;border-radius:50%!important;border:1px solid rgba(34,197,94,.22)!important;box-shadow:0 0 18px rgba(34,197,94,.12)!important}
.scan-ring.ring1{width:70px!important;height:70px!important}.scan-ring.ring2{width:150px!important;height:150px!important}.scan-ring.ring3{width:250px!important;height:250px!important}
.scan-beam{position:absolute!important;left:73%!important;top:43%!important;width:260px!important;height:260px!important;transform:translate(-50%,-50%)!important;border-radius:50%!important;background:conic-gradient(from 0deg,rgba(34,197,94,0) 0deg,rgba(34,197,94,0) 295deg,rgba(74,222,128,.24) 328deg,rgba(190,242,100,.52) 352deg,rgba(34,197,94,0) 360deg)!important;mix-blend-mode:screen!important;animation:worldSweep 5.8s linear infinite!important;pointer-events:none!important}
.radar-hub{position:absolute!important;left:73%!important;top:43%!important;transform:translate(-50%,-50%)!important;min-width:40px!important;height:40px!important;padding:0 10px!important;border-radius:999px!important;display:flex!important;align-items:center!important;justify-content:center!important;background:linear-gradient(135deg,rgba(34,197,94,.86),rgba(6,182,212,.88))!important;color:#05202f!important;font-size:12px!important;font-weight:1000!important;box-shadow:0 0 0 4px rgba(34,197,94,.10),0 0 26px rgba(34,197,94,.24)!important;z-index:3!important}
.world-link{position:absolute!important;height:2px!important;transform-origin:left center!important;background:linear-gradient(90deg,rgba(34,197,94,.55),rgba(56,189,248,.10))!important;opacity:.45!important;z-index:1!important}.world-link.offline{background:linear-gradient(90deg,rgba(239,68,68,.58),rgba(244,114,182,.12))!important}
.world-pin{position:absolute!important;transform:translate(-50%,-50%)!important;display:flex!important;align-items:center!important;gap:6px!important;z-index:4!important}
.world-pin i{display:block!important;width:10px!important;height:10px!important;border-radius:50%!important;background:#22c55e!important;box-shadow:0 0 0 4px rgba(34,197,94,.12),0 0 18px rgba(34,197,94,.34)!important;animation:nodePulse 1.8s ease-in-out infinite!important}.world-pin.offline i{background:#fb7185!important;box-shadow:0 0 0 4px rgba(251,113,133,.12),0 0 18px rgba(251,113,133,.34)!important}
.world-pin b{display:none!important;white-space:nowrap!important;font-size:10.5px!important;padding:3px 6px!important;border-radius:999px!important;background:rgba(2,6,23,.72)!important;color:#f8fafc!important;border:1px solid rgba(255,255,255,.14)!important}
.world-pin:hover b{display:inline-block!important}
.map-empty{position:absolute!important;left:50%!important;top:50%!important;transform:translate(-50%,-50%)!important;padding:10px 14px!important;border-radius:12px!important;background:rgba(255,255,255,.12)!important;color:#fff!important;font-weight:900!important}
@keyframes worldSweep{from{transform:translate(-50%,-50%) rotate(0deg)}to{transform:translate(-50%,-50%) rotate(360deg)}}
@keyframes nodePulse{0%,100%{transform:scale(1)}50%{transform:scale(1.22)}}
@media(max-width:980px){.world-radar{height:250px!important}.scan-ring.ring3,.scan-beam{width:200px!important;height:200px!important}.scan-ring.ring2{width:120px!important;height:120px!important}}
/* ===== end final readability + world radar patch ===== */
'''


def _apply_final_title_map_patch():
    global BASE, DASH, SERVERS, FORM, LOCAL, EVENTS, SETTINGS, DETAIL
    if 'final readability + world radar patch' not in BASE:
        BASE = BASE.replace('</style><script>', _FINAL_TITLE_MAP_CSS + '</style><script>')
    DASH = re.sub(r'<div class=top><h1>.*?</h1><div class=btns>', '<div class=top><h1><span class="title-emoji">📊🌈</span><span class="title-text">霓虹风监控大屏</span><span class="title-chip" data-neon-live>正在连接0刷新</span></h1><div class=btns>', DASH, count=1)
    SERVERS = re.sub(r'<div class=top><h1>.*?</h1><div class=btns>', '<div class=top><h1><span class="title-emoji">🖥️✨</span><span class="title-text">所有服务器</span></h1><div class=btns>', SERVERS, count=1)
    FORM = FORM.replace("<div class=top><h1>{{'➕' if is_add else '✏️'}} {{action}}</h1>", '<div class=top><h1><span class="title-emoji">{{\'➕\' if is_add else \'✏️\'}}</span><span class="title-text">{{action}}</span></h1>')
    LOCAL = LOCAL.replace('<div class=top><h1>🏠 本机面板</h1>', '<div class=top><h1><span class="title-emoji">🏠</span><span class="title-text">本机面板</span></h1>')
    EVENTS = EVENTS.replace('<div class=top><h1>🧾✨ 事件记录</h1>', '<div class=top><h1><span class="title-emoji">🧾✨</span><span class="title-text">事件记录</span></h1>')
    SETTINGS = SETTINGS.replace('<div class=top><h1>⚙️✨ 系统设置</h1>', '<div class=top><h1><span class="title-emoji">⚙️✨</span><span class="title-text">系统设置</span></h1>')
    DETAIL = DETAIL.replace('<div class=top><h1>🖥️ {{flag_icon(s)|safe}} {{s.name}}</h1>', '<div class=top><h1><span class="title-emoji">🖥️</span><span class="title-text">{{flag_icon(s)|safe}} {{s.name}}</span></h1>')
    app.jinja_env.globals.update(expire_progress_html=expire_progress_html, expire_progress_info=expire_progress_info, node_topology_html=node_topology_html, ops_feature_panel=ops_feature_panel)


_apply_final_title_map_patch()
# ===== END FINAL USER PATCH =====

# ===== SCREENSHOT FEEDBACK PATCH: light expiry visibility + better world map + stable network boxes =====
_SCREENSHOT_FIX_CSS = r'''
/* ===== screenshot feedback patch ===== */
.expire-progress-wrap{position:relative!important;overflow:hidden!important}
.expire-progress-wrap .expire-progress{position:relative!important}
.expire-progress-wrap .expire-bar{min-width:12px!important;position:relative!important}
.expire-progress-wrap .expire-bar::after{content:""!important;position:absolute!important;inset:0!important;background:linear-gradient(90deg,rgba(255,255,255,.00),rgba(255,255,255,.28),rgba(255,255,255,.00))!important;mix-blend-mode:screen!important;animation:expSheen 2.7s linear infinite!important}
html.light .expire-progress-wrap{background:linear-gradient(180deg,rgba(255,255,255,.96),rgba(250,245,255,.92))!important;border-color:rgba(168,85,247,.18)!important;box-shadow:0 8px 18px rgba(168,85,247,.08), inset 0 1px 0 rgba(255,255,255,.95)!important}
html.light .expire-progress-wrap .expire-progress{background:linear-gradient(180deg,rgba(192,132,252,.12),rgba(244,114,182,.08))!important;box-shadow:inset 0 0 0 1px rgba(168,85,247,.10)!important}
html.light .expire-progress-wrap .expire-progress-title span,
html.light .expire-progress-wrap .expire-progress-title b{color:#7c3aed!important;-webkit-text-fill-color:#7c3aed!important;text-shadow:none!important;font-weight:1000!important}
html.light .expire-progress-wrap .expire-bar{filter:saturate(1.42) brightness(1.08)!important;box-shadow:0 0 0 1px rgba(255,255,255,.65),0 0 12px rgba(168,85,247,.16)!important}
html.light .expire-progress-wrap.forever .expire-bar{box-shadow:0 0 0 1px rgba(255,255,255,.62),0 0 12px rgba(16,185,129,.18)!important}
html.light .expire-progress-wrap.warn .expire-bar{box-shadow:0 0 0 1px rgba(255,255,255,.62),0 0 12px rgba(251,191,36,.18)!important}
html.light .expire-progress-wrap.danger .expire-bar{box-shadow:0 0 0 1px rgba(255,255,255,.62),0 0 12px rgba(239,68,68,.18)!important}

.metric-extra{align-items:stretch!important}
.metric-extra .mini{display:flex!important;flex-direction:column!important;justify-content:space-between!important;align-items:flex-start!important;min-height:60px!important;overflow:hidden!important}
.metric-extra .mini span,
[data-netspeed],[data-traffic],[data-load],
[data-local-netspeed],[data-local-traffic],[data-local-load]{display:block!important;white-space:nowrap!important;overflow:hidden!important;text-overflow:ellipsis!important;min-height:1.45em!important;line-height:1.45!important;font-variant-numeric:tabular-nums!important;font-feature-settings:"tnum" 1!important;font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,"Liberation Mono","Courier New",monospace!important}

.world-radar{background:radial-gradient(circle at 73% 43%,rgba(34,197,94,.11),transparent 11%),radial-gradient(circle at 44% 48%,rgba(56,189,248,.06),transparent 34%),linear-gradient(180deg,rgba(4,15,38,.76),rgba(15,23,42,.90))!important}
.world-outline .landmass{fill:rgba(114,151,216,.20)!important;stroke:rgba(158,184,224,.50)!important;stroke-width:2.1!important;stroke-linejoin:round!important}
.world-outline .island{fill:rgba(114,151,216,.18)!important;stroke:rgba(158,184,224,.42)!important;stroke-width:1.7!important;stroke-linejoin:round!important}
.world-link{opacity:.52!important}
@keyframes expSheen{0%{transform:translateX(-110%)}100%{transform:translateX(110%)}}
/* ===== end screenshot feedback patch ===== */
'''


def _world_map_svg():
    return """<svg class=\"world-svg\" viewBox=\"0 0 1000 520\" preserveAspectRatio=\"none\" aria-hidden=\"true\">\n    <g class=\"world-outline\">\n      <polygon class=\"landmass\" points=\"46,232 60,182 88,150 80,120 126,92 186,100 242,126 274,160 275,196 238,220 190,214 158,214 130,224 108,256\" />\n      <polygon class=\"landmass\" points=\"242,290 294,320 318,374 300,436 270,478 234,468 214,410 220,348\" />\n      <polygon class=\"island\" points=\"444,118 486,103 528,110 534,138 492,154 456,146\" />\n      <polygon class=\"landmass\" points=\"406,160 450,148 500,152 548,166 604,172 646,188 676,214 720,216 738,200 778,198 838,186 886,192 910,210 896,228 858,234 834,250 800,260 772,252 760,234 734,232 708,246 688,280 654,292 626,332 578,336 546,322 512,346 478,336 454,312 430,294 400,276 376,252 392,198\" />\n      <polygon class=\"landmass\" points=\"520,354 560,360 594,380 584,410 548,420 506,408 494,384\" />\n      <polygon class=\"landmass\" points=\"736,298 784,308 836,334 898,382 892,426 846,450 784,444 742,420 720,378 722,334\" />\n      <polygon class=\"landmass\" points=\"822,226 862,220 900,224 930,244 918,264 884,272 850,266 818,252 806,236\" />\n      <polygon class=\"landmass\" points=\"728,220 754,188 792,176 820,182 800,204 786,224 758,238\" />\n      <polygon class=\"landmass\" points=\"896,434 926,430 956,444 952,472 922,484 892,470 886,452\" />\n      <polygon class=\"island\" points=\"520,126 548,116 572,126 566,142 540,148\" />\n      <polygon class=\"island\" points=\"796,128 820,122 842,132 836,146 814,148\" />\n    </g></svg>"""


def _apply_screenshot_feedback_patch():
    global BASE
    if 'screenshot feedback patch' not in BASE:
        BASE = BASE.replace('</style><script>', _SCREENSHOT_FIX_CSS + '</style><script>')


_apply_screenshot_feedback_patch()
# ===== END SCREENSHOT FEEDBACK PATCH =====

# ===== FINAL UI FEEDBACK PATCH: light bars + live lamp + stable metrics + clean status dots =====
_UI_FEEDBACK_CSS = r'''
/* ===== final ui feedback patch ===== */
html.light .expire-progress-wrap{background:linear-gradient(180deg,#ffffff,#faf5ff)!important;border:1px solid rgba(147,51,234,.16)!important;box-shadow:0 8px 18px rgba(124,58,237,.07),inset 0 1px 0 rgba(255,255,255,.95)!important}
html.light .expire-progress-wrap .expire-progress{background:linear-gradient(180deg,rgba(99,102,241,.10),rgba(168,85,247,.05))!important;box-shadow:inset 0 0 0 1px rgba(124,58,237,.08)!important}
html.light .expire-progress-wrap .expire-progress-title span,
html.light .expire-progress-wrap .expire-progress-title b{color:#6d28d9!important;-webkit-text-fill-color:#6d28d9!important;text-shadow:none!important;font-weight:1000!important}
html.light .expire-progress-wrap .expire-bar,
html.light .expire-progress-wrap.ok .expire-bar{background:linear-gradient(90deg,#16a34a,#06b6d4,#2563eb)!important;filter:saturate(1.45) brightness(1.08)!important;box-shadow:0 0 0 1px rgba(255,255,255,.82),0 0 12px rgba(37,99,235,.20)!important}
html.light .expire-progress-wrap.warn .expire-bar{background:linear-gradient(90deg,#f59e0b,#eab308,#f97316)!important;filter:saturate(1.38) brightness(1.06)!important;box-shadow:0 0 0 1px rgba(255,255,255,.82),0 0 12px rgba(245,158,11,.22)!important}
html.light .expire-progress-wrap.danger .expire-bar{background:linear-gradient(90deg,#dc2626,#ef4444,#f97316)!important;filter:saturate(1.34) brightness(1.05)!important;box-shadow:0 0 0 1px rgba(255,255,255,.82),0 0 12px rgba(220,38,38,.22)!important}
html.light .expire-progress-wrap.forever .expire-bar{background:linear-gradient(90deg,#059669,#10b981,#0ea5e9)!important;filter:saturate(1.38) brightness(1.06)!important;box-shadow:0 0 0 1px rgba(255,255,255,.82),0 0 12px rgba(16,185,129,.22)!important}

.neon-live-chip{position:relative!important;display:inline-flex!important;align-items:center!important;gap:8px!important;padding:6px 12px!important;padding-left:12px!important;border-radius:999px!important;font-weight:1000!important;letter-spacing:.2px!important}
.neon-live-chip::before{content:""!important;display:inline-block!important;width:10px!important;height:10px!important;border-radius:50%!important;flex:0 0 10px!important;background:#fbbf24!important;box-shadow:0 0 12px rgba(251,191,36,.90),0 0 22px rgba(251,191,36,.36)!important;animation:lampPulseAmber 1.8s ease-in-out infinite!important}
.neon-live-chip.is-live::before{background:#22c55e!important;box-shadow:0 0 12px rgba(34,197,94,.95),0 0 22px rgba(34,197,94,.40)!important;animation:lampPulseGreen 1.6s ease-in-out infinite!important}
.neon-live-chip.is-sse::before{background:#facc15!important;box-shadow:0 0 12px rgba(250,204,21,.95),0 0 22px rgba(250,204,21,.40)!important;animation:lampPulseAmber 1.7s ease-in-out infinite!important}
.neon-live-chip.is-poll::before,.neon-live-chip.is-down::before{background:#ef4444!important;box-shadow:0 0 12px rgba(239,68,68,.95),0 0 22px rgba(239,68,68,.42)!important;animation:lampPulseRed 1.7s ease-in-out infinite!important}
.neon-live-chip.is-connecting::before{background:#f59e0b!important;box-shadow:0 0 12px rgba(245,158,11,.95),0 0 22px rgba(245,158,11,.42)!important;animation:lampPulseAmber 1.8s ease-in-out infinite!important}
@keyframes lampPulseGreen{0%,100%{opacity:1;box-shadow:0 0 10px rgba(34,197,94,.90),0 0 22px rgba(34,197,94,.34)}50%{opacity:.98;box-shadow:0 0 14px rgba(34,197,94,1),0 0 30px rgba(34,197,94,.46)}}
@keyframes lampPulseAmber{0%,100%{opacity:1;box-shadow:0 0 10px rgba(250,204,21,.90),0 0 22px rgba(250,204,21,.34)}50%{opacity:.98;box-shadow:0 0 14px rgba(250,204,21,1),0 0 30px rgba(250,204,21,.46)}}
@keyframes lampPulseRed{0%,100%{opacity:1;box-shadow:0 0 10px rgba(239,68,68,.90),0 0 22px rgba(239,68,68,.34)}50%{opacity:.98;box-shadow:0 0 14px rgba(239,68,68,1),0 0 30px rgba(239,68,68,.46)}}

.metric-extra{display:grid!important;grid-template-columns:repeat(3,minmax(0,1fr))!important;gap:8px!important;align-items:stretch!important}
.metric-extra .mini{min-width:0!important;min-height:68px!important;overflow:hidden!important;padding:8px 8px!important}
.metric-extra .mini b{margin-bottom:4px!important}
.metric-extra .mini span,
[data-netspeed],[data-traffic],[data-load],
[data-local-netspeed],[data-local-traffic],[data-local-load]{display:-webkit-box!important;-webkit-line-clamp:2!important;-webkit-box-orient:vertical!important;white-space:normal!important;word-break:break-word!important;overflow:hidden!important;line-height:1.25!important;min-height:2.5em!important;height:2.5em!important;font-size:10.5px!important;font-variant-numeric:tabular-nums!important;font-feature-settings:"tnum" 1!important;font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,"Liberation Mono","Courier New",monospace!important}

.server-title{overflow:visible!important;align-items:center!important}
.server-title .dot{width:14px!important;height:14px!important;min-width:14px!important;min-height:14px!important;display:inline-block!important;border-radius:999px!important;overflow:visible!important;flex:0 0 14px!important;position:relative!important;top:0!important;margin-right:3px!important;background:#22c55e!important;border:none!important;outline:none!important;box-shadow:none!important}
.server-title .dot.online{background:#22c55e!important;animation:serverLampGreen 1.6s ease-in-out infinite!important;box-shadow:0 0 10px rgba(34,197,94,.96),0 0 18px rgba(34,197,94,.55)!important}
.server-title .dot.offline{background:#ef4444!important;animation:serverLampRed 1.6s ease-in-out infinite!important;box-shadow:0 0 10px rgba(239,68,68,.96),0 0 18px rgba(239,68,68,.55)!important}
@keyframes serverLampGreen{0%,100%{opacity:1;box-shadow:0 0 10px rgba(34,197,94,.94),0 0 18px rgba(34,197,94,.52)}50%{opacity:.98;box-shadow:0 0 14px rgba(34,197,94,1),0 0 24px rgba(34,197,94,.65)}}
@keyframes serverLampRed{0%,100%{opacity:1;box-shadow:0 0 10px rgba(239,68,68,.94),0 0 18px rgba(239,68,68,.52)}50%{opacity:.98;box-shadow:0 0 14px rgba(239,68,68,1),0 0 24px rgba(239,68,68,.65)}}
html.light .server-title .dot.online{box-shadow:0 0 10px rgba(34,197,94,.95),0 0 18px rgba(34,197,94,.54)!important}
html.light .server-title .dot.offline{box-shadow:0 0 10px rgba(239,68,68,.95),0 0 18px rgba(239,68,68,.54)!important}

@media(max-width:980px){.metric-extra{grid-template-columns:1fr!important}}
/* ===== end final ui feedback patch ===== */
'''

_UI_FEEDBACK_JS = r'''
<script>
(function(){
  function syncLiveChip(){
    var chip=document.querySelector('[data-neon-live]');
    if(!chip) return;
    var t=(chip.textContent||'').trim();
    chip.classList.remove('is-live','is-sse','is-poll','is-down','is-connecting');
    if(/WebSocket/i.test(t)) chip.classList.add('is-live');
    else if(/\bSSE\b/i.test(t)) chip.classList.add('is-sse');
    else if(/轮询|回退|失败|断开|错误/i.test(t)) chip.classList.add('is-poll');
    else chip.classList.add('is-connecting');
  }
  document.addEventListener('DOMContentLoaded',function(){
    var chip=document.querySelector('[data-neon-live]');
    if(chip){
      syncLiveChip();
      try{ new MutationObserver(syncLiveChip).observe(chip,{childList:true,characterData:true,subtree:true}); }catch(e){}
    }
  });
})();
</script>
'''


def _apply_ui_feedback_patch():
    global BASE
    if 'final ui feedback patch' not in BASE:
        BASE = BASE.replace('</style><script>', _UI_FEEDBACK_CSS + '</style><script>')
    if '_UI_FEEDBACK_JS' not in BASE:
        BASE = BASE.replace('</script></head><body>', '</script>' + _UI_FEEDBACK_JS + '</head><body>')


_apply_ui_feedback_patch()
# ===== END FINAL UI FEEDBACK PATCH =====


# ===== FINAL CLEAN PATCH: inline metrics, no inner boxes, compact Komari card, animated radar =====
def _fmt_inline_bytes(n, suffix=''):
    try:
        n = float(n or 0)
    except Exception:
        return '0 B' + suffix
    units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
    i = 0
    while abs(n) >= 1024 and i < len(units) - 1:
        n /= 1024.0
        i += 1
    if i == 0:
        s = f'{n:.0f} {units[i]}'
    else:
        s = f'{n:.2f} {units[i]}'
    return s + suffix

def _inline_up_down(up_value, down_value, suffix=''):
    return (
        f'<span class="m-up">↑ {_fmt_inline_bytes(up_value, suffix)}</span>'
        f'<span class="m-down">↓ {_fmt_inline_bytes(down_value, suffix)}</span>'
    )

def _inline_load(load1, load5, load15):
    try: l1 = float(load1 or 0)
    except Exception: l1 = 0.0
    try: l5 = float(load5 or 0)
    except Exception: l5 = 0.0
    try: l15 = float(load15 or 0)
    except Exception: l15 = 0.0
    return f'<span class="m-load">{l1:.2f}</span><span class="m-sep"> | </span><span class="m-load">{l5:.2f}</span><span class="m-sep"> | </span><span class="m-load">{l15:.2f}</span>'

def _inline_state(v):
    raw = str(v or '未上报').strip()
    cls = 'm-warn' if ('未上报' in raw or '未检测' in raw or '未知' in raw or raw == '无') else 'm-ok'
    return f'<span class="{cls}">{html.escape(raw)}</span>'

_PREV_METRIC_JSON_FINAL_INLINE = metric_json
def metric_json(x):
    j = _PREV_METRIC_JSON_FINAL_INLINE(x)
    j['net_speed_html'] = _inline_up_down(j.get('up_speed', 0), j.get('down_speed', 0), '/s')
    j['traffic_html'] = _inline_up_down(j.get('tx_bytes', 0), j.get('rx_bytes', 0), '')
    j['load_html'] = _inline_load(j.get('load1', 0), j.get('load5', 0), j.get('load15', 0))
    for k in ('gpu_html', 'io_html', 'tcp_html'):
        j[k] = _inline_state(j.get(k) or '未上报')
    return j

_PREV_LOCAL_JSON_FINAL_INLINE = _local_live_json
def _local_live_json():
    j = _PREV_LOCAL_JSON_FINAL_INLINE()
    j['net_speed_html'] = _inline_up_down(j.get('up_speed', 0), j.get('down_speed', 0), '/s')
    j['traffic_html'] = _inline_up_down(j.get('tx_bytes', 0), j.get('rx_bytes', 0), '')
    j['load_html'] = _inline_load(j.get('load1', 0), j.get('load5', 0), j.get('load15', 0))
    for k in ('gpu_html', 'io_html', 'tcp_html'):
        j[k] = _inline_state(j.get(k) or '未上报')
    return j

_FINAL_INLINE_CLEAN_CSS = r"""
/* ===== final inline metrics: left label + right data, transparent only ===== */
html body .servercard .metric-extra,
html body .card .metric-extra,
html body .metric-extra{
  display:block!important;
  margin:8px 0 0!important;
  padding:0!important;
  border:0!important;
  border-radius:0!important;
  background:transparent!important;
  background-color:transparent!important;
  box-shadow:none!important;
  overflow:visible!important;
}
html body .servercard .metric-extra .mini,
html body .card .metric-extra .mini,
html body .metric-extra .mini,
html body .metric-extra .mini:nth-child(1),
html body .metric-extra .mini:nth-child(2),
html body .metric-extra .mini:nth-child(3),
html body .metric-extra .mini:nth-child(4),
html body .metric-extra .mini:nth-child(5),
html body .metric-extra .mini:nth-child(6),
html body .metric-extra .mini:nth-child(7){
  display:grid!important;
  grid-template-columns:48px minmax(0,1fr)!important;
  align-items:center!important;
  column-gap:8px!important;
  width:100%!important;
  min-width:0!important;
  min-height:0!important;
  height:auto!important;
  margin:0 0 5px!important;
  padding:0!important;
  border:0!important;
  border-radius:0!important;
  background:transparent!important;
  background-color:transparent!important;
  box-shadow:none!important;
  overflow:visible!important;
  text-shadow:none!important;
}
html body .metric-extra .mini b{
  grid-column:1!important;
  display:block!important;
  margin:0!important;
  padding:0!important;
  border:0!important;
  background:transparent!important;
  color:#f8fbff!important;
  -webkit-text-fill-color:#f8fbff!important;
  font-size:12px!important;
  line-height:1.25!important;
  font-weight:1000!important;
  white-space:nowrap!important;
  text-shadow:0 1px 3px rgba(0,0,0,.70)!important;
}
html body .metric-extra .mini > span{
  grid-column:2!important;
  display:block!important;
  width:100%!important;
  min-width:0!important;
  margin:0!important;
  padding:0!important;
  height:auto!important;
  min-height:0!important;
  white-space:nowrap!important;
  overflow:visible!important;
  text-overflow:clip!important;
  line-height:1.28!important;
  font-size:10.8px!important;
  font-weight:950!important;
  letter-spacing:-.45px!important;
  text-align:left!important;
  font-variant-numeric:tabular-nums!important;
  font-feature-settings:"tnum" 1!important;
  font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,"Liberation Mono","Courier New",monospace!important;
  background:transparent!important;
  background-color:transparent!important;
  box-shadow:none!important;
}
html body .metric-extra .mini .m-up{display:inline!important;color:#34d399!important;-webkit-text-fill-color:#34d399!important;margin-right:8px!important}
html body .metric-extra .mini .m-down{display:inline!important;color:#38bdf8!important;-webkit-text-fill-color:#38bdf8!important}
html body .metric-extra .mini .m-load{display:inline!important;color:#a78bfa!important;-webkit-text-fill-color:#a78bfa!important}
html body .metric-extra .mini .m-sep{display:inline!important;color:#fbbf24!important;-webkit-text-fill-color:#fbbf24!important}
html body .metric-extra .mini .m-ok{color:#34d399!important;-webkit-text-fill-color:#34d399!important}
html body .metric-extra .mini .m-warn{color:#fbbf24!important;-webkit-text-fill-color:#fbbf24!important}
html body .metric-extra .ai-mini{
  grid-template-columns:86px minmax(0,1fr)!important;
  margin-top:7px!important;
  padding-top:6px!important;
  border-top:1px solid rgba(255,255,255,.14)!important;
}
html body .metric-extra .ai-mini > span,
html body [data-ai],
html body [data-local-ai]{
  white-space:normal!important;
  overflow:visible!important;
  line-height:1.34!important;
  font-size:11px!important;
  font-weight:950!important;
  color:transparent!important;
  -webkit-text-fill-color:transparent!important;
  background:linear-gradient(90deg,#22d3ee,#60a5fa,#a78bfa,#34d399)!important;
  -webkit-background-clip:text!important;
  background-clip:text!important;
  text-shadow:none!important;
}
html.light body .metric-extra .mini b{
  color:#0f172a!important;
  -webkit-text-fill-color:#0f172a!important;
  text-shadow:none!important;
}
html.light body .metric-extra .mini .m-up{color:#15803d!important;-webkit-text-fill-color:#15803d!important}
html.light body .metric-extra .mini .m-down{color:#0369a1!important;-webkit-text-fill-color:#0369a1!important}
html.light body .metric-extra .mini .m-load{color:#6d28d9!important;-webkit-text-fill-color:#6d28d9!important}
html.light body .metric-extra .mini .m-sep{color:#ca8a04!important;-webkit-text-fill-color:#ca8a04!important}
html.light body .metric-extra .mini .m-ok{color:#15803d!important;-webkit-text-fill-color:#15803d!important}
html.light body .metric-extra .mini .m-warn{color:#b45309!important;-webkit-text-fill-color:#b45309!important}
html.light body .metric-extra .ai-mini{border-top-color:rgba(15,23,42,.12)!important}

/* Komari 能力卡片缩小 */
html body .feature-card{
  min-height:auto!important;
  margin-top:8px!important;
  padding:10px 12px!important;
}
html body .feature-card h2{
  font-size:15px!important;
  margin:0 0 7px!important;
}
html body .feature-grid{
  grid-template-columns:repeat(auto-fit,minmax(130px,1fr))!important;
  gap:6px!important;
}
html body .feature-grid span{
  padding:6px 8px!important;
  border-radius:10px!important;
  font-size:11px!important;
  line-height:1.25!important;
}

/* 运维雷达圆圈扫描动画 */
html body .radar{
  position:relative!important;
  overflow:hidden!important;
  animation:radarBreathRun 2.4s ease-in-out infinite!important;
}
html body .radar::after{
  content:""!important;
  position:absolute!important;
  inset:6px!important;
  border-radius:50%!important;
  background:conic-gradient(from 0deg,rgba(34,211,238,0) 0deg,rgba(34,211,238,0) 270deg,rgba(34,211,238,.26) 312deg,rgba(134,239,172,.62) 354deg,rgba(34,211,238,0) 360deg)!important;
  animation:radarSweepRun 1.8s linear infinite!important;
  mix-blend-mode:screen!important;
  pointer-events:none!important;
}
html body .radar span{position:relative!important;z-index:2!important}
@keyframes radarSweepRun{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}
@keyframes radarBreathRun{0%,100%{filter:brightness(1)}50%{filter:brightness(1.18)}}

@media(max-width:420px){
  html body .metric-extra .mini{grid-template-columns:42px minmax(0,1fr)!important;column-gap:6px!important}
  html body .metric-extra .ai-mini{grid-template-columns:74px minmax(0,1fr)!important}
  html body .metric-extra .mini > span{font-size:10px!important;letter-spacing:-.65px!important}
  html body .metric-extra .mini .m-up{margin-right:5px!important}
}
/* ===== end final inline metrics ===== */
"""

def _apply_final_inline_clean_patch():
    global BASE
    if 'final inline metrics: left label + right data' not in BASE:
        BASE = BASE.replace('</style><script>', _FINAL_INLINE_CLEAN_CSS + '</style><script>')

_apply_final_inline_clean_patch()
# ===== END FINAL CLEAN PATCH =====

if __name__=='__main__': init_db(); app.run(host=WEB_HOST,port=WEB_PORT,threaded=True)
