import os, re, json, time, threading, subprocess, sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from flask import Flask, render_template, jsonify, request, redirect, url_for, session, flash

APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv('DB_PATH', '/data/vodmon.db'))
SNAPRAID_CONFIG = os.getenv('SNAPRAID_CONFIG', '/etc/snapraid.conf')
SNAPRAID_LOG_PATH = os.getenv('SNAPRAID_LOG_PATH', '/docker/snapraid-recovery-backup/resync-final.log')
REFRESH_SECONDS = int(os.getenv('REFRESH_SECONDS', '60'))
ENABLE_DOCKER = os.getenv('ENABLE_DOCKER_MONITOR', 'false').lower() == 'true'
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'change-me')
SECRET_KEY = os.getenv('SECRET_KEY', 'change-this-secret')
APP_VERSION = os.getenv('APP_VERSION', '0.4.3')
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
# This application NEVER launches maintenance jobs. Host processes and logs are observed
# through read-only mounts, including jobs started via SSH or systemd.
def snapraid_processes():
    processes = []
    proc = Path('/host/proc')
    try:
        own_pidns = os.readlink('/proc/self/ns/pid')
    except OSError:
        own_pidns = None
    try:
        entries = proc.iterdir()
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                # Exclude this monitor's own status/diff subprocesses.
                if own_pidns and os.readlink(entry / 'ns/pid') == own_pidns:
                    continue
                argv = [p.decode('utf-8', errors='replace') for p in
                        (entry / 'cmdline').read_bytes().split(b'\0') if p]
                if not argv or Path(argv[0]).name != 'snapraid':
                    continue
                operation = next((a for a in reversed(argv)
                                  if a in ('sync','scrub','check','fix','status','diff','touch','list')),
                                 'snapraid')
                processes.append({'pid': int(entry.name), 'name': operation})
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return sorted(processes, key=lambda p: p['pid'])


def read_snapraid_log():
    path = SNAPRAID_LOG_PATH
    if not path:
        return {'path': '', 'updated': None, 'text': '',
                'error': 'Set SNAPRAID_LOG_PATH to an existing, mounted log file.'}
    try:
        with open(path, 'rb') as stream:
            stream.seek(0, os.SEEK_END)
            total = stream.tell()
            stream.seek(max(0, total - 98304))
            chunk = stream.read().decode('utf-8', errors='replace')
        if total > 98304 and '\n' in chunk:
            chunk = chunk.split('\n', 1)[1]
        text = '\n'.join(chunk.splitlines()[-220:])
        updated = datetime.fromtimestamp(Path(path).stat().st_mtime).astimezone().isoformat()
        return {'path': path, 'updated': updated, 'text': text, 'error': None}
    except OSError as exc:
        return {'path': path, 'updated': None, 'text': '',
                'error': f'Log unavailable: {exc.strerror or str(exc)}'}


def snapraid_observation():
    processes = snapraid_processes()
    log = read_snapraid_log()
    progress = None
    # SnapRAID's -l structured logs may have no percentage; do not invent one.
    for line in reversed(log['text'].splitlines()):
        m = re.search(r'(?<!\d)(\d{1,3})%\s*,\s*([0-9.]+)\s*(MB|GB|TB)', line, re.I)
        if m:
            progress = {'percent': min(100, int(m.group(1))),
                        'processed': f'{m.group(2)} {m.group(3).upper()}'}
            break
    return {'running': bool(processes), 'processes': processes,
            'name': processes[0]['name'] if processes else None,
            'progress': progress if processes else None, 'log': log}


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
    c.execute('''CREATE TABLE IF NOT EXISTS settings(
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL
    )''')
    c.commit()
    return c


def log_event(kind, status, message, duration=0):
    c = db(); c.execute('INSERT INTO events(ts,kind,status,message,duration) VALUES(?,?,?,?,?)',
        (datetime.now(timezone.utc).isoformat(), kind, status, message[-12000:], duration)); c.commit(); c.close()


background_lock = threading.Lock()
background_started = False

def start_background_threads():
    global background_started
    with background_lock:
        if background_started:
            return
        background_started = True
        threading.Thread(target=collector_loop,daemon=True,name='collector').start()


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
    observation = snapraid_observation()
    running = observation['running']
    previous = cache.get('data', {}).get('snapraid', {})

    if running:
        # An externally managed SnapRAID operation owns the content lock.
        # Don't run status/diff during maintenance or misreport lock errors as failures.
        rc = None
        rc2 = None
        status = previous.get('status_raw', '')
        diff = previous.get('diff_raw', '')
        status_ok = None
        diff_ok = None
        pending = dict(previous.get('changes', {}))
    else:
        rc, status = run(['snapraid', '-c', SNAPRAID_CONFIG, 'status'], timeout=60)
        status_ok = (rc == 0 and bool(status.strip()))
        rc2, diff = run(['snapraid', '-c', SNAPRAID_CONFIG, 'diff'], timeout=120)
        diff_ok = (rc2 in (0, 2) and bool(diff.strip()))
        pending = {}
        for key in ['equal', 'added', 'removed', 'updated', 'moved', 'copied', 'restored']:
            match = re.search(r'^\s*([0-9]+)\s+' + re.escape(key) + r'\s*$', diff, re.M) if diff else None
            pending[key] = int(match.group(1)) if match else 0

    warning_source = status + ('\n' + diff if diff else '')
    hidden_warning = "WARNING! For 30 disks, it's recommended to use five parity levels."
    warnings = list(dict.fromkeys(
        line.strip() for line in warning_source.splitlines()
        if 'WARNING!' in line and line.strip() != hidden_warning
    ))
    pending_total = sum(value for key, value in pending.items() if key != 'equal')

    if running:
        name = observation['name']
        protection = {'sync':'Syncing', 'scrub':'Scrubbing', 'fix':'Repair in progress',
                      'check':'Checking'}.get(name, 'SnapRAID busy')
    elif not status_ok:
        protection = 'Unavailable'
    elif diff_ok is False:
        protection = 'Status OK / Diff unavailable'
    elif pending_total == 0:
        protection = 'Protected'
    else:
        protection = 'Changes Pending'

    return {
        'status_rc': rc, 'diff_rc': rc2, 'status_ok': status_ok,
        'diff_ok': diff_ok, 'status_raw': status, 'diff_raw': diff,
        'changes': pending, 'pending_total': pending_total,
        'protection': protection, 'warnings': warnings,
        'last_sync': snapraid_last_sync(), 'config': snapraid_config_summary(),
        'maintenance_running': running,
        'job': {'name': observation['name'], 'progress': observation['progress']}
    }


def smart_for_disk(d):
    name=d['name']; dev='/dev/'+name
    smart={'health':'Unknown','health_detail':None,'temp':None,'power_hours':None,'reallocated':None,'pending':None,'error':None}
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

        if protocol == 'NVME':
            # NVMe critical_warning is a bitmask. smartctl may report
            # smart_status.passed=false for a temperature-only warning, which
            # does not mean the drive has failed.
            try:
                nvme_critical = int(nvme_critical or 0)
            except (TypeError, ValueError):
                nvme_critical = 0

            media_errors = int(nvme_health.get('media_errors') or 0)
            reasons = []
            if nvme_critical & 0x01:
                reasons.append('Available spare below threshold')
            if nvme_critical & 0x02:
                reasons.append('Temperature above/below warning threshold')
            if nvme_critical & 0x04:
                reasons.append('NVM subsystem reliability degraded')
            if nvme_critical & 0x08:
                reasons.append('Media placed in read-only mode')
            if nvme_critical & 0x10:
                reasons.append('Volatile memory backup failed')
            if nvme_critical & 0x20:
                reasons.append('Persistent memory read-only/unreliable')

            if nvme_critical == 0 and media_errors == 0:
                smart['health'] = 'Healthy'
            elif nvme_critical == 0x02 and media_errors == 0:
                smart['health'] = 'Temp Warning'
            elif nvme_critical:
                smart['health'] = 'Critical' if (nvme_critical & (0x04 | 0x08 | 0x10 | 0x20)) else 'Warning'
            elif media_errors > 0:
                smart['health'] = 'Warning'

            if reasons:
                smart['health_detail'] = '; '.join(reasons)
            elif media_errors > 0:
                smart['health_detail'] = f'{media_errors} media/data integrity error(s)'
        elif passed is True:
            smart['health']='Healthy'
        elif passed is False:
            smart['health']='FAILED'
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


def disk_kind(d):
    name = str(d.get('name') or '')
    if name.startswith('nvme'):
        return 'NVMe'
    rota = d.get('rota')
    if rota in (1, True, '1'):
        return 'HDD'
    return 'SSD'


def disk_descendants(name):
    rc,out=run(['lsblk','-nrpo','NAME','/dev/'+name],timeout=10)
    if rc:
        return {name}
    return {Path(line.strip()).name for line in out.splitlines() if line.strip()}


def usage_from_mount(mountpoint, source=None):
    if not mountpoint or mountpoint == '[SWAP]':
        return None
    info=get_df(mountpoint)
    if info.get('error'):
        return None
    return {
        'mount': mountpoint,
        'source': source or mountpoint,
        'pct': info.get('pct',0),
        'used_h': info.get('used_h','-'),
        'avail_h': info.get('avail_h','-'),
        'size_h': info.get('size_h','-')
    }


def disk_usage(name):
    # First prefer filesystems mounted directly from this physical disk/partition.
    rc,out=run(['lsblk','-nrpo','NAME,MOUNTPOINT','/dev/'+name],timeout=10)
    direct=[]
    if rc == 0:
        for line in out.splitlines():
            parts=line.split(None,1)
            if len(parts)==2 and parts[1] and parts[1] != '[SWAP]':
                u=usage_from_mount(parts[1], Path(parts[0]).name)
                if u:
                    direct.append(u)
    if direct:
        # Prefer the main filesystem over small boot mounts.
        direct.sort(key=lambda x: (x['mount'] not in ('/docker','/'), x['mount']))
        return direct[0]

    # RAID members do not have their own mountpoint. Show the usage of the
    # mounted md array they belong to so every physical SSD/NVMe still has
    # a meaningful usage bar.
    members=disk_descendants(name)
    mdtext=Path('/proc/mdstat').read_text(errors='ignore') if Path('/proc/mdstat').exists() else ''
    candidates=[]
    for block in re.split(r'\n(?=md\d+\s*:)', mdtext):
        m=re.match(r'(md\d+)\s*:', block)
        if not m:
            continue
        md=m.group(1)
        if not any(re.search(r'(?<![A-Za-z0-9])'+re.escape(member)+r'\[', block) for member in members):
            continue
        rc2,target=run(['findmnt','-n','-o','TARGET','/dev/'+md],timeout=10)
        target=(target or '').strip()
        if rc2 == 0 and target:
            u=usage_from_mount(target, md)
            if u:
                candidates.append(u)
    if candidates:
        candidates.sort(key=lambda x: (x['mount'] not in ('/docker','/'), x['mount']))
        return candidates[0]

    return {'mount':None,'source':None,'pct':None,'used_h':'-','avail_h':'-','size_h':'-'}


def list_disks():
    rc,out=run(['lsblk','-J','-b','-d','-o','NAME,SIZE,MODEL,SERIAL,TYPE,ROTA'])
    if rc: return []
    try: items=[d for d in json.loads(out).get('blockdevices',[]) if d.get('type')=='disk']
    except: return []
    workers=min(8, max(1, len(items)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        disks=list(pool.map(smart_for_disk, items))
    source={d.get('name'):d for d in items}
    for disk in disks:
        raw=source.get(disk['name'],{})
        disk['kind']=disk_kind(raw)
        disk['usage']=disk_usage(disk['name'])
    order={'HDD':0,'SSD':1,'NVMe':2}
    disks.sort(key=lambda d:(order.get(d.get('kind'),9), d.get('name','')))
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
            'failed':sum(1 for d in data['disks'] if d['smart']['health'] in ('FAILED','Critical')),
            'warning':sum(1 for d in data['disks'] if d['smart']['health'] in ('Warning','Temp Warning')),
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
    return render_template('index.html', data=data, updated=cache['updated'] or 'initializing', events=events)

@app.route('/api/status')
@auth_required
def api_status():
    return jsonify({'updated':cache['updated'], **cache['data']})

@app.route('/api/observation')
@auth_required
def api_observation():
    return jsonify(snapraid_observation())


db()
start_background_threads()

if __name__=='__main__':
    app.run(host='0.0.0.0',port=int(os.getenv('PORT','8099')),debug=False)
