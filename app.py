import os, re, json, time, threading, subprocess, sqlite3
from concurrent.futures import ThreadPoolExecutor
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
APP_VERSION = os.getenv('APP_VERSION', '0.3.6')
BUILD_SHA = os.getenv('BUILD_SHA', 'dev')

app = Flask(__name__)
app.secret_key = SECRET_KEY
def empty_dashboard():
    return {
        'storage': {
            'vod': {'path':'/mnt/vod','size':0,'used':0,'avail':0,'pct':0,'size_h':'-','used_h':'-','avail_h':'-'},
            'docker': {'path':'/docker','size':0,'used':0,'avail':0,'pct':0,'size_h':'-','used_h':'-','avail_h':'-'}
        },
        'raid': [],
        'snapraid': {
            'status_rc':None,'diff_rc':None,'status_ok':False,'diff_ok':False,
            'status_raw':'','diff_raw':'',
            'changes':{'equal':0,'added':0,'removed':0,'updated':0,'moved':0,'copied':0,'restored':0},
            'pending_total':0,'protection':'Initializing','warnings':[],'last_sync':None,
            'config':{'data_disks':0,'parity_levels':0,'content_copies':0,'parity_paths':[]}
        },
        'disks': [],
        'docker': {'enabled':ENABLE_DOCKER,'containers':[]},
        'hostname': (Path('/host/etc/hostname').read_text().strip() if Path('/host/etc/hostname').exists() else os.uname().nodename),
        'app_version': APP_VERSION,
        'build_sha': BUILD_SHA[:7] if BUILD_SHA else 'dev',
        'disk_health': {'total':0,'healthy':0,'failed':0,'warning':0,'max_temp':None}
    }

cache = {'data': empty_dashboard(), 'updated': None, 'collecting': False}
lock = threading.Lock()
job_lock = threading.Lock()
job_state = {
    'running': False, 'name': None, 'started': None, 'finished': None,
    'returncode': None, 'status': 'idle', 'output': [], 'process': None,
    'cancel_requested': False
}

def public_job_state():
    with job_lock:
        started = job_state.get('started')
        elapsed = 0
        if started:
            end = time.time() if job_state.get('running') else (job_state.get('finished') or time.time())
            elapsed = max(0, end - started)
        lines = job_state.get('output', [])[-200:]
        progress = None
        for line in reversed(lines):
            m = re.search(r'(?<!\d)(\d{1,3})%\s*,\s*([0-9.]+)\s*(MB|GB|TB)', line, re.I)
            if m:
                progress = {
                    'percent': min(100, int(m.group(1))),
                    'processed': f"{m.group(2)} {m.group(3).upper()}"
                }
                break
        return {
            'running': job_state.get('running', False),
            'name': job_state.get('name'),
            'started': started,
            'finished': job_state.get('finished'),
            'returncode': job_state.get('returncode'),
            'status': job_state.get('status', 'idle'),
            'elapsed': round(elapsed, 1),
            'progress': progress,
            'output': '\n'.join(lines)
        }

def run_job_command(cmd):
    with job_lock:
        job_state['process'] = None
    try:
        p = subprocess.Popen(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             bufsize=1)
        with job_lock:
            job_state['process'] = p
        lines = []
        if p.stdout:
            for line in p.stdout:
                line = line.rstrip()
                lines.append(line)
                with job_lock:
                    job_state['output'] = lines[-200:]
        rc = p.wait()
        return rc, '\n'.join(lines)
    except Exception as e:
        return 999, str(e)
    finally:
        with job_lock:
            job_state['process'] = None


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


def snapraid_config_summary():
    summary={'data_disks':0,'parity_levels':0,'content_copies':0,'parity_paths':[]}
    try:
        for line in Path(SNAPRAID_CONFIG).read_text(errors='ignore').splitlines():
            line=line.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('data '):
                summary['data_disks'] += 1
            elif line.startswith('content '):
                summary['content_copies'] += 1
            elif re.match(r'^(?:[2-6]-)?parity\s+', line):
                summary['parity_levels'] += 1
                summary['parity_paths'].append(line.split(None,1)[1].strip())
    except Exception:
        pass
    return summary


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
    job = public_job_state()
    maintenance_running = job.get('running') and job.get('name') in ('sync', 'scrub10', 'scrub_full')

    rc, status = run(['snapraid', '-c', SNAPRAID_CONFIG, 'status'], timeout=60)
    status_ok = (rc == 0 and bool(status.strip()))

    if maintenance_running:
        # SnapRAID holds its lock during sync/scrub, so do not run diff in parallel.
        rc2 = None
        diff = ''
        diff_ok = None
    else:
        rc2, diff = run(['snapraid', '-c', SNAPRAID_CONFIG, 'diff'], timeout=120)
        diff_ok = (rc2 in (0, 2) and bool(diff.strip()))

    pending = {}
    for key in ['equal', 'added', 'removed', 'updated', 'moved', 'copied', 'restored']:
        match = re.search(r'^\\s*([0-9]+)\\s+' + re.escape(key) + r'\\s*$', diff, re.M) if diff else None
        pending[key] = int(match.group(1)) if match else 0

    warning_source = status + ('\\n' + diff if diff else '')
    warning = list(dict.fromkeys(
        line.strip() for line in warning_source.splitlines() if 'WARNING!' in line
    ))
    pending_total = sum(value for key, value in pending.items() if key != 'equal')

    if maintenance_running:
        protection = 'Syncing' if job.get('name') == 'sync' else 'Scrubbing'
    elif not status_ok:
        protection = 'Unavailable'
    elif diff_ok is False:
        protection = 'Status OK / Diff unavailable'
    elif pending_total == 0:
        protection = 'Protected'
    else:
        protection = 'Changes Pending'

    return {
        'status_rc': rc,
        'diff_rc': rc2,
        'status_ok': status_ok,
        'diff_ok': diff_ok,
        'status_raw': status,
        'diff_raw': diff,
        'changes': pending,
        'pending_total': pending_total,
        'protection': protection,
        'warnings': warning,
        'last_sync': snapraid_last_sync(),
        'config': snapraid_config_summary(),
        'maintenance_running': maintenance_running,
        'job': job
    }


def smart_for_disk(d):
    name=d['name']; dev='/dev/'+name
    smart={'health':'Unknown','temp':None,'power_hours':None,'reallocated':None,'pending':None,'error':None}
    rc2,sout=run(['smartctl','-x','-j',dev],timeout=25)
    smart_serial=''
    j=None
    try:
        j=json.loads(sout)
    except Exception:
        j=None

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
            t=(j.get('scsi_environmental_reports',{}).get('temperature_1',{}).get('current'))
        if t in (None, 0) and protocol == 'NVME':
            t=nvme_health.get('temperature')
        smart['temp']=t if t not in (0,) else None

        smart['power_hours']=j.get('power_on_time',{}).get('hours')
        smart_serial=(j.get('serial_number') or '').strip()

        attrs=j.get('ata_smart_attributes',{}).get('table',[])
        amap={a.get('name'):a.get('raw',{}).get('value') for a in attrs}
        smart['reallocated']=amap.get('Reallocated_Sector_Ct')
        smart['pending']=amap.get('Current_Pending_Sector')
        if smart['reallocated'] is None:
            smart['reallocated']=j.get('scsi_grown_defect_list')

        msgs=j.get('smartctl',{}).get('messages',[])
        if msgs:
            smart['error']='; '.join(str(m.get('string','')) for m in msgs if m.get('string'))[-700:] or None
        if rc2 and smart['health'] in ('Unknown','Available') and not smart['error']:
            smart['error']=f'smartctl exit code {rc2}'
    except Exception as e:
        smart['error']=(sout[-700:] if sout else str(e)) or f'smartctl exit code {rc2}'

    return {'name':name,'dev':dev,'size':int(d.get('size') or 0),'size_h':human_bytes(d.get('size') or 0),
            'model':(d.get('model') or '').strip(),'serial':((d.get('serial') or '').strip() or smart_serial),'smart':smart}


def list_disks():
    rc,out=run(['lsblk','-J','-b','-d','-o','NAME,SIZE,MODEL,SERIAL,TYPE'])
    if rc: return []
    try: items=[d for d in json.loads(out).get('blockdevices',[]) if d.get('type')=='disk']
    except: return []
    workers=min(8, max(1, len(items)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        disks=list(pool.map(smart_for_disk, items))
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


def collect(force=False):
    with lock:
        if cache['collecting'] and not force:
            return False
        if cache['collecting'] and force:
            return False
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
        return True
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
    if not cache.get('updated') and not cache.get('collecting'):
        threading.Thread(target=collect,daemon=True).start()
    data=cache.get('data') or empty_dashboard()
    c=db(); events=c.execute('SELECT ts,kind,status,message,duration FROM events ORDER BY id DESC LIMIT 20').fetchall(); c.close()
    return render_template('index.html', data=data, updated=cache['updated'] or 'initializing', actions=ENABLE_ACTIONS, events=events)

@app.route('/api/status')
@auth_required
def api_status():
    return jsonify({'updated':cache['updated'], **cache['data']})

@app.route('/api/refresh',methods=['POST'])
@auth_required
def api_refresh():
    threading.Thread(target=collect,daemon=True).start()
    return jsonify({'ok':True})

@app.route('/api/job')
@auth_required
def api_job():
    return jsonify(public_job_state())

@app.route('/api/action/<name>',methods=['POST'])
@auth_required
def action(name):
    if not ENABLE_ACTIONS:
        return jsonify({'ok':False,'error':'Actions disabled. Set ENABLE_ACTIONS=true.'}),403

    allowed={
        'status': ['snapraid','-c',SNAPRAID_CONFIG,'status'],
        'diff': ['snapraid','-c',SNAPRAID_CONFIG,'diff'],
        'sync': ['snapraid','-c',SNAPRAID_CONFIG,'sync'],
        'scrub10': ['snapraid','-c',SNAPRAID_CONFIG,'scrub','-p','10'],
        'scrub_full': ['snapraid','-c',SNAPRAID_CONFIG,'scrub','-p','100'],
    }
    if name not in set(allowed) | {'check'}:
        return jsonify({'ok':False,'error':'Unknown action'}),400

    with job_lock:
        if job_state['running']:
            return jsonify({'ok':False,'error':f"SnapRAID {job_state['name']} is already running"}),409
        job_state.update({
            'running':True,'name':name,'started':time.time(),'finished':None,
            'returncode':None,'status':'running','output':[],'process':None,
            'cancel_requested':False
        })

    def task():
        start=time.time()
        try:
            if name == 'check':
                rc1,out1=run_job_command(['snapraid','-c',SNAPRAID_CONFIG,'status'])
                if rc1 == 0:
                    rc2,out2=run_job_command(['snapraid','-c',SNAPRAID_CONFIG,'diff'])
                else:
                    rc2,out2=rc1,'Diff skipped because status failed.'
                rc = rc1 if rc1 else (0 if rc2 in (0, 2) else rc2)
                out = '=== STATUS ===\n'+out1+'\n\n=== DIFF ===\n'+out2
            else:
                rc,out=run_job_command(allowed[name])

            with job_lock:
                cancelled=job_state.get('cancel_requested',False)
                job_state['running']=False
                job_state['finished']=time.time()
                job_state['returncode']=rc
                job_state['status']='cancelled' if cancelled else ('success' if rc==0 else 'failed')
                if out:
                    job_state['output']=out.splitlines()[-200:]
            log_event(name,'cancelled' if cancelled else ('success' if rc==0 else 'failed'),
                      out,time.time()-start)
        except Exception as e:
            with job_lock:
                job_state['running']=False
                job_state['finished']=time.time()
                job_state['returncode']=999
                job_state['status']='failed'
                job_state['output']=[str(e)]
            log_event(name,'failed',str(e),time.time()-start)
        finally:
            collect()

    threading.Thread(target=task,daemon=True).start()
    log_event(name,'started',f'{name} started from dashboard',0)
    return jsonify({'ok':True,'message':f'SnapRAID {name} started'})

@app.route('/api/action/cancel',methods=['POST'])
@auth_required
def cancel_action():
    if not ENABLE_ACTIONS:
        return jsonify({'ok':False,'error':'Actions disabled'}),403
    with job_lock:
        if not job_state.get('running'):
            return jsonify({'ok':False,'error':'No SnapRAID job is running'}),409
        p=job_state.get('process')
        job_state['cancel_requested']=True
        job_state['status']='cancelling'
    try:
        if p and p.poll() is None:
            p.terminate()
        return jsonify({'ok':True,'message':'Cancel requested'})
    except Exception as e:
        return jsonify({'ok':False,'error':str(e)}),500

if __name__=='__main__':
    db()
    threading.Thread(target=collector_loop,daemon=True).start()
    app.run(host='0.0.0.0',port=int(os.getenv('PORT','8099')),debug=False)
