import os, re, json, time, threading, subprocess, sqlite3
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from flask import Flask, render_template, jsonify, request, redirect, url_for, session, flash

APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv('DB_PATH', '/data/vodmon.db'))
SNAPRAID_CONFIG = os.getenv('SNAPRAID_CONFIG', '/etc/snapraid.conf')
REFRESH_SECONDS = int(os.getenv('REFRESH_SECONDS', '60'))
ENABLE_ACTIONS = os.getenv('ENABLE_ACTIONS', 'false').lower() == 'true'
ENABLE_DOCKER = os.getenv('ENABLE_DOCKER_MONITOR', 'false').lower() == 'true'
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'change-me')
SECRET_KEY = os.getenv('SECRET_KEY', 'change-this-secret')
APP_VERSION = os.getenv('APP_VERSION', '0.2.0')
BUILD_SHA = os.getenv('BUILD_SHA', 'dev')

app = Flask(__name__)
app.secret_key = SECRET_KEY
cache = {'data': {}, 'updated': None, 'collecting': False}
lock = threading.Lock()


def run(cmd, timeout=20):
    try:
        p = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, check=False)
        return p.returncode, p.stdout.strip()
    except Exception as e:
        return 999, str(e)


def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.execute('''CREATE TABLE IF NOT EXISTS events(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
      message TEXT NOT NULL, duration REAL DEFAULT 0
    )''')
    c.commit()
    return c


def log_event(kind, status, message, duration=0):
    c = db(); c.execute('INSERT INTO events(ts,kind,status,message,duration) VALUES(?,?,?,?,?)',
        (datetime.now(timezone.utc).isoformat(), kind, status, message[-12000:], duration)); c.commit(); c.close()


def auth_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get('auth'):
            return redirect(url_for('login', next=request.path))
        return fn(*args, **kwargs)
    return wrapped


def human_bytes(n):
    try: n=float(n)
    except: return '-'
    units=['B','KB','MB','GB','TB','PB']
    i=0
    while n>=1000 and i<len(units)-1:
        n/=1000; i+=1
    return f'{n:.1f} {units[i]}'


def get_df(path):
    rc,out=run(['df','-B1','--output=size,used,avail,pcent,target',path])
    lines=out.splitlines()
    if rc or len(lines)<2: return {'path':path,'error':out}
    parts=lines[-1].split()
    size,used,avail,pct,target=parts[0],parts[1],parts[2],parts[3],parts[4]
    return {'path':target,'size':int(size),'used':int(used),'avail':int(avail),'pct':int(pct.rstrip('%')),
            'size_h':human_bytes(size),'used_h':human_bytes(used),'avail_h':human_bytes(avail)}


def parse_mdstat():
    text=Path('/proc/mdstat').read_text(errors='ignore') if Path('/proc/mdstat').exists() else ''
    arrays=[]
    blocks=re.split(r'\n(?=md\d+\s*:)', text)
    for b in blocks:
        m=re.match(r'(md\d+)\s*:\s*(\w+)\s+(\w+)\s+(.+)', b)
        if not m: continue
        name,state,level,members=m.groups()
        healthy='[UU]' in b or ('[U' in b and '_' not in b)
        progress=None
        pm=re.search(r'(resync|recovery|reshape|check)\s*=\s*([0-9.]+)%', b)
        if pm: progress={'type':pm.group(1),'percent':float(pm.group(2))}
        arrays.append({'name':name,'state':state,'level':level,'members':members.split('\n')[0].strip(),
                       'healthy':healthy,'progress':progress,'raw':b.strip()})
    return arrays


def snapraid_content_paths():
    paths=[]
    try:
        for line in Path(SNAPRAID_CONFIG).read_text(errors='ignore').splitlines():
            line=line.strip()
            if line.startswith('content '): paths.append(line.split(None,1)[1].strip())
    except: pass
    return paths


def snapraid_last_sync():
    mtimes=[]
    for p in snapraid_content_paths():
        try: mtimes.append(Path(p).stat().st_mtime)
        except: pass
    if not mtimes: return None
    ts=max(mtimes)
    return {'epoch':ts,'iso':datetime.fromtimestamp(ts).astimezone().isoformat(),
            'display':datetime.fromtimestamp(ts).astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}


def snapraid_summary():
    rc,status=run(['snapraid','-c',SNAPRAID_CONFIG,'status'],timeout=60)
    rc2,diff=run(['snapraid','-c',SNAPRAID_CONFIG,'diff'],timeout=120)
    status_ok = (rc == 0 and bool(status.strip()))
    diff_ok = (rc2 == 0 and bool(diff.strip()))
    pending={}
    for key in ['equal','added','removed','updated','moved','copied','restored']:
        m=re.search(rf'^\s*([0-9]+)\s+{key}\s*$', diff, re.M)
        pending[key]=int(m.group(1)) if m else 0
    warning=[x.strip() for x in (status+'\n'+diff).splitlines() if 'WARNING!' in x]
    pending_total=sum(v for k,v in pending.items() if k!='equal')
    if not status_ok:
        protection='Unavailable'
    elif not diff_ok:
        protection='Status OK / Diff unavailable'
    elif pending_total == 0:
        protection='Protected'
    else:
        protection='Changes Pending'
    return {'status_rc':rc,'diff_rc':rc2,'status_ok':status_ok,'diff_ok':diff_ok,
            'status_raw':status,'diff_raw':diff,'changes':pending,
            'pending_total':pending_total,'protection':protection,'warnings':warning,
            'last_sync':snapraid_last_sync()}


def list_disks():
    rc,out=run(['lsblk','-J','-b','-d','-o','NAME,SIZE,MODEL,SERIAL,TYPE'])
    if rc: return []
    try: items=json.loads(out).get('blockdevices',[])
    except: return []
    disks=[]
    for d in items:
        if d.get('type')!='disk': continue
        name=d['name']; dev='/dev/'+name
        smart={'health':'Unknown','temp':None,'power_hours':None,'reallocated':None,'pending':None,'error':None}
        rc2,sout=run(['smartctl','-x','-j',dev],timeout=25)
        smart_serial=''
        j=None
        try:
            j=json.loads(sout)
        except Exception:
            j=None

        # SAS disks can need explicit SCSI mode depending on the HBA/expander.
        if (not j or not j.get('device')) and name.startswith('sd'):
            rc3,sout3=run(['smartctl','-x','-j','-d','scsi',dev],timeout=25)
            try:
                j3=json.loads(sout3)
                if j3.get('device'):
                    j=j3; rc2=rc3; sout=sout3
            except Exception:
                pass

        try:
            if not j:
                raise ValueError('No valid smartctl JSON returned')

            passed=j.get('smart_status',{}).get('passed')
            protocol=(j.get('device',{}).get('protocol') or '').upper()
            nvme_health=j.get('nvme_smart_health_information_log',{})
            nvme_critical=nvme_health.get('critical_warning')

            if passed is True:
                smart['health']='Healthy'
            elif passed is False:
                smart['health']='FAILED'
            elif protocol == 'NVME' and nvme_critical == 0:
                smart['health']='Healthy'
            elif j.get('smart_support',{}).get('available') is False:
                smart['health']='Unsupported'
            elif j.get('device',{}):
                smart['health']='Available'

            t=j.get('temperature',{}).get('current')
            if t in (None, 0):
                t=(j.get('scsi_environmental_reports',{})
                    .get('temperature_1',{})
                    .get('current'))
            if t in (None, 0) and protocol == 'NVME':
                t=nvme_health.get('temperature')
            smart['temp']=t if t not in (0,) else None

            smart['power_hours']=j.get('power_on_time',{}).get('hours')
            smart_serial=(j.get('serial_number') or '').strip()

            attrs=j.get('ata_smart_attributes',{}).get('table',[])
            amap={a.get('name'):a.get('raw',{}).get('value') for a in attrs}
            smart['reallocated']=amap.get('Reallocated_Sector_Ct')
            smart['pending']=amap.get('Current_Pending_Sector')

            # SAS/SCSI drives expose grown defects instead of ATA pending/reallocated counters.
            if smart['reallocated'] is None:
                smart['reallocated']=j.get('scsi_grown_defect_list')

            msgs=j.get('smartctl',{}).get('messages',[])
            if msgs:
                smart['error']='; '.join(str(m.get('string','')) for m in msgs if m.get('string'))[-700:] or None

            # smartctl uses bitmask exit codes; JSON can still contain valid SMART data.
            if rc2 and smart['health'] in ('Unknown','Available') and not smart['error']:
                smart['error']=f'smartctl exit code {rc2}'
        except Exception as e:
            smart['error']=(sout[-700:] if sout else str(e)) or f'smartctl exit code {rc2}'

        disks.append({'name':name,'dev':dev,'size':int(d.get('size') or 0),'size_h':human_bytes(d.get('size') or 0),
                      'model':(d.get('model') or '').strip(),'serial':((d.get('serial') or '').strip() or smart_serial),'smart':smart})
    return disks


def docker_status():
    if not ENABLE_DOCKER: return {'enabled':False,'containers':[]}
    rc,out=run(['docker','ps','-a','--format','{{json .}}'],timeout=15)
    containers=[]
    if rc==0:
        for line in out.splitlines():
            try:
                j=json.loads(line); containers.append(j)
            except: pass
    return {'enabled':True,'rc':rc,'containers':containers,'error':out if rc else None}


def collect():
    with lock:
        if cache['collecting']: return
        cache['collecting']=True
    try:
        data={
            'storage': {'vod':get_df('/mnt/vod'),'docker':get_df('/docker')},
            'raid': parse_mdstat(),
            'snapraid': snapraid_summary(),
            'disks': list_disks(),
            'docker': docker_status(),
            'hostname': (Path('/host/etc/hostname').read_text().strip() if Path('/host/etc/hostname').exists() else os.uname().nodename),
            'app_version': APP_VERSION,
            'build_sha': BUILD_SHA[:7] if BUILD_SHA else 'dev',
        }
        data['disk_health']={
            'total':len(data['disks']),
            'healthy':sum(1 for d in data['disks'] if d['smart']['health']=='Healthy'),
            'failed':sum(1 for d in data['disks'] if d['smart']['health']=='FAILED'),
            'warning':sum(1 for d in data['disks'] if (d['smart']['temp'] or 0)>=50 or (d['smart']['pending'] or 0)>0),
            'max_temp':max([d['smart']['temp'] for d in data['disks'] if d['smart']['temp'] is not None] or [None])
        }
        cache['data']=data; cache['updated']=datetime.now().astimezone().isoformat()
    finally:
        cache['collecting']=False


def collector_loop():
    while True:
        collect(); time.sleep(max(20,REFRESH_SECONDS))

@app.route('/login',methods=['GET','POST'])
def login():
    if request.method=='POST':
        if request.form.get('password')==ADMIN_PASSWORD:
            session['auth']=True; return redirect(request.args.get('next') or url_for('index'))
        flash('Invalid password','error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear(); return redirect(url_for('login'))

@app.route('/')
@auth_required
def index():
    if not cache['updated']: collect()
    c=db(); events=c.execute('SELECT ts,kind,status,message,duration FROM events ORDER BY id DESC LIMIT 20').fetchall(); c.close()
    return render_template('index.html', data=cache['data'], updated=cache['updated'], actions=ENABLE_ACTIONS, events=events)

@app.route('/api/status')
@auth_required
def api_status():
    return jsonify({'updated':cache['updated'], **cache['data']})

@app.route('/api/refresh',methods=['POST'])
@auth_required
def api_refresh():
    threading.Thread(target=collect,daemon=True).start()
    return jsonify({'ok':True})

@app.route('/api/action/<name>',methods=['POST'])
@auth_required
def action(name):
    if not ENABLE_ACTIONS: return jsonify({'ok':False,'error':'Actions disabled'}),403
    allowed={
        'sync':['snapraid','-c',SNAPRAID_CONFIG,'sync'],
        'scrub':['snapraid','-c',SNAPRAID_CONFIG,'scrub','-p','10'],
        'diff':['snapraid','-c',SNAPRAID_CONFIG,'diff'],
        'status':['snapraid','-c',SNAPRAID_CONFIG,'status'],
    }
    if name not in allowed: return jsonify({'ok':False,'error':'Unknown action'}),400
    def task():
        start=time.time(); rc,out=run(allowed[name],timeout=86400)
        log_event(name,'success' if rc==0 else 'failed',out,time.time()-start); collect()
    threading.Thread(target=task,daemon=True).start()
    log_event(name,'started',f'{name} started from dashboard',0)
    return jsonify({'ok':True,'message':f'{name} started'})

if __name__=='__main__':
    db()
    threading.Thread(target=collector_loop,daemon=True).start()
    app.run(host='0.0.0.0',port=int(os.getenv('PORT','8099')),debug=False)
