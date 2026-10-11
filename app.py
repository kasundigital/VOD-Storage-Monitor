import os, re, json, glob, hmac, time, threading, subprocess, sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from flask import Flask, render_template, jsonify, request, redirect, url_for, session, flash

APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv('DB_PATH', '/data/vodmon.db'))
SNAPRAID_CONFIG = os.getenv('SNAPRAID_CONFIG', '/etc/snapraid.conf')
SNAPRAID_LOG_PATH = os.getenv('SNAPRAID_LOG_PATH', '/docker/snapraid-recovery-backup/resync-final.log')
SNAPRAID_SNAPSHOT_PATH = os.getenv('SNAPRAID_SNAPSHOT_PATH', '/docker/vod-monitor/data/snapraid-snapshot.json')
SNAPRAID_SCRUB_LOG_PATH = os.getenv('SNAPRAID_SCRUB_LOG_PATH', '')
REFRESH_SECONDS = int(os.getenv('REFRESH_SECONDS', '60'))
ENABLE_DOCKER = os.getenv('ENABLE_DOCKER_MONITOR', 'false').lower() == 'true'
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'change-me')
SECRET_KEY = os.getenv('SECRET_KEY', 'change-this-secret')
SYNC_STALE_HOURS = float(os.getenv('SYNC_STALE_HOURS', '26'))
SCRUB_STALE_DAYS = float(os.getenv('SCRUB_STALE_DAYS', '14'))
APP_VERSION = os.getenv('APP_VERSION', '0.6.0')
BUILD_SHA = os.getenv('BUILD_SHA', 'dev')

app = Flask(__name__)
app.secret_key = SECRET_KEY
def fmt_time(epoch):
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch).astimezone().strftime('%Y-%m-%d %H:%M')


def fmt_duration(seconds):
    if seconds is None:
        return '-'
    seconds = int(max(0, seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f'{days}d {hours}h'
    if hours:
        return f'{hours}h {minutes}m'
    if minutes:
        return f'{minutes}m'
    return f'{secs}s'


def fmt_age(epoch):
    if epoch is None:
        return None
    return fmt_duration(time.time() - epoch) + ' ago'


def parse_status(text):
    """Extract the health facts SnapRAID prints in `snapraid status`."""
    text = text or ''
    info = {'scrub_oldest_days': None, 'scrub_median_days': None, 'scrub_newest_days': None,
            'unscrubbed_pct': None, 'sync_in_progress': None, 'errors': None}
    m = re.search(r'oldest block was scrubbed (\d+) days? ago, the median (\d+), the newest (\d+)', text)
    if m:
        info['scrub_oldest_days'], info['scrub_median_days'], info['scrub_newest_days'] = map(int, m.groups())
    m = re.search(r'(\d+)% of the array is not scrubbed', text)
    if m:
        info['unscrubbed_pct'] = int(m.group(1))
    elif 'full array was scrubbed' in text:
        info['unscrubbed_pct'] = 0
    m = re.search(r'sync in progress at (\d+)%', text)
    if m:
        info['sync_in_progress'] = int(m.group(1))
    elif 'No sync is in progress' in text:
        info['sync_in_progress'] = False
    m = re.search(r'there (?:are|is) (\d+) errors?', text)
    if m:
        info['errors'] = int(m.group(1))
    elif 'No error detected' in text:
        info['errors'] = 0
    return info


def empty_dashboard():
    return {
        'storage': {
            'vod': {'path':'/mnt/vod','size':0,'used':0,'avail':0,'pct':0,'size_h':'-','used_h':'-','avail_h':'-'},
            'docker': {'path':'/docker','size':0,'used':0,'avail':0,'pct':0,'size_h':'-','used_h':'-','avail_h':'-'}
        },
        'raid': [],
        'snapraid': {
            'status_rc':None,'diff_rc':None,'status_ok':None,'diff_ok':None,
            'status_raw':'','diff_raw':'',
            'changes':{'equal':0,'added':0,'removed':0,'updated':0,'moved':0,'copied':0,'restored':0},
            'pending_total':None,'protection':'Initializing','warnings':[],'last_sync':None,
            'snapshot_time':None,'snapshot_error':None,'scrub_coverage':None,
            'last_log_result':'Unknown','health':parse_status(''),'scrub_stale':False,
            'maintenance_running':False,'job':{'name':None,'progress':None},
            'config':{'data_disks':0,'parity_levels':0,'content_copies':0,'parity_paths':[]}
        },
        'disks': [],
        'docker': {'enabled':ENABLE_DOCKER,'containers':[]},
        'hostname': (Path('/host/etc/hostname').read_text().strip() if Path('/host/etc/hostname').exists() else os.uname().nodename),
        'app_version': APP_VERSION,
        'build_sha': BUILD_SHA[:7] if BUILD_SHA else 'dev',
        'disk_health': {'total':0,'healthy':0,'failed':0,'warning':0,'other':0,'standby':0,'max_temp':None}
    }

cache = {'data': empty_dashboard(), 'updated': None, 'collecting': False}
lock = threading.Lock()
# The web application NEVER invokes snapraid (including status or diff).
# All SnapRAID readings come from host processes, existing logs, and optional
# manually captured snapshots; no dashboard refresh can acquire the content lock.
SNAPRAID_OPERATIONS = ('sync', 'scrub', 'check', 'fix', 'status', 'diff', 'touch', 'list', 'dup', 'smart')


def host_boot_time():
    try:
        for line in Path('/host/proc/stat').read_text().splitlines():
            if line.startswith('btime '):
                return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return None


def process_start_time(entry, boot):
    """Wall-clock start time of a host process, from /proc/<pid>/stat."""
    if boot is None:
        return None
    try:
        stat = (entry / 'stat').read_text()
        ticks = int(stat.rsplit(')', 1)[1].split()[19])
        return boot + ticks / os.sysconf('SC_CLK_TCK')
    except (OSError, ValueError, IndexError):
        return None


def snapraid_processes():
    processes = []
    proc = Path('/host/proc')
    boot = host_boot_time()
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
                # Ignore processes sharing the monitor's PID namespace.
                if own_pidns and os.readlink(entry / 'ns/pid') == own_pidns:
                    continue
                argv = [p.decode('utf-8', errors='replace') for p in
                        (entry / 'cmdline').read_bytes().split(b'\0') if p]
                if not argv or Path(argv[0]).name != 'snapraid':
                    continue
                operation = next((a for a in reversed(argv) if a in SNAPRAID_OPERATIONS), 'snapraid')
                started = process_start_time(entry, boot)
                processes.append({'pid': int(entry.name), 'name': operation, 'started': started,
                                  'started_display': fmt_time(started),
                                  'elapsed': fmt_duration(time.time() - started) if started else None})
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return sorted(processes, key=lambda p: p['pid'])


def resolve_log_path(path):
    """A log path may be a glob (e.g. /docker/snapraid/logs/*.log); use the newest match."""
    if path and glob.has_magic(path):
        matches = [m for m in glob.glob(path) if os.path.isfile(m)]
        return max(matches, key=os.path.getmtime) if matches else path
    return path


def log_path_for(operation):
    if operation == 'scrub' and SNAPRAID_SCRUB_LOG_PATH:
        return SNAPRAID_SCRUB_LOG_PATH
    return SNAPRAID_LOG_PATH


def read_snapraid_log(path=None, since=0):
    """Tail of a log. With `since` (a byte offset), only text appended after it is returned,
    unless the file shrank (rotated/truncated), in which case it is all new."""
    path = resolve_log_path(SNAPRAID_LOG_PATH if path is None else path)
    if not path:
        return {'path': '', 'updated': None, 'mtime': None, 'size': None, 'text': '',
                'error': 'Set SNAPRAID_LOG_PATH to an existing, mounted log file.'}
    try:
        with open(path, 'rb') as stream:
            stream.seek(0, os.SEEK_END)
            total = stream.tell()
            start = since if 0 < since <= total else 0
            offset = max(start, total - 98304)
            stream.seek(offset)
            chunk = stream.read().decode('utf-8', errors='replace')
        if offset > start and '\n' in chunk:
            chunk = chunk.split('\n', 1)[1]
        text = '\n'.join(chunk.splitlines()[-220:])
        mtime = Path(path).stat().st_mtime
        return {'path': path, 'updated': fmt_time(mtime), 'mtime': mtime, 'size': total,
                'text': text, 'error': None}
    except OSError as exc:
        return {'path': path, 'updated': None, 'mtime': None, 'size': None, 'text': '',
                'error': f'Log unavailable: {exc.strerror or str(exc)}'}


def log_result(text):
    """Outcome of the run recorded in a SnapRAID log (-l log or captured console output)."""
    text = text or ''
    errors = {}
    for kind in ('error_file', 'error_io', 'error_data'):
        found = re.findall(r'^summary:' + kind + r':(\d+)', text, re.M)
        if found:
            errors[kind] = int(found[-1])
    exits = re.findall(r'^summary:exit:(\w+)', text, re.M)
    # https://www.snapraid.it/manual-log : recovered = all errors were fixed.
    exit_results = {'ok': 'OK', 'recovered': 'OK', 'warning': 'Warning',
                    'error': 'Error', 'recoverable': 'Error', 'unrecoverable': 'Error'}
    exit_status = exits[-1] if exits and exits[-1] in exit_results else None
    if exit_status:
        result = exit_results[exit_status]
    elif any(errors.values()) or re.search(r'^DANGER!|^\d+ (?:file|io|data) errors?', text, re.M | re.I):
        result = 'Error'
    elif re.search(r'^Everything OK|^Nothing to do|^No error detected', text, re.M):
        result = 'OK'
    elif re.search(r'^WARNING!', text, re.M):
        result = 'Warning'
    else:
        result = None
    detail = ', '.join(f"{k.split('_', 1)[1]} errors: {v}" for k, v in errors.items() if v)
    if exit_status in ('recovered', 'recoverable', 'unrecoverable'):
        detail = exit_status + (f' ({detail})' if detail else '')
    detail = detail or None
    return {'result': result, 'detail': detail}


def latest_progress(text):
    # SnapRAID's -l structured logs may have no percentage; do not invent one.
    for line in reversed((text or '').splitlines()):
        m = re.search(r'(?<!\d)(\d{1,3})%\s*,\s*([0-9.]+)\s*(MB|GB|TB)', line, re.I)
        if m:
            return {'percent': min(100, int(m.group(1))), 'processed': f'{m.group(2)} {m.group(3).upper()}'}
    return None


def snapraid_observation():
    processes = snapraid_processes()
    log = read_snapraid_log(log_path_for(processes[0]['name'] if processes else None))
    progress = latest_progress(log['text']) if processes else None
    outcome = log_result(log['text'])
    return {'running': bool(processes), 'processes': processes,
            'name': processes[0]['name'] if processes else None,
            'started': processes[0].get('started_display') if processes else None,
            'elapsed': processes[0].get('elapsed') if processes else None,
            'progress': progress,
            'log': {'path': log['path'], 'updated': log['updated'], 'mtime': log['mtime'],
                    'error': log['error'], 'result': outcome['result'], 'detail': outcome['detail']},
            'last_run': last_run_summary()}


# Operations that are worth keeping in the run history (status/diff/list are just queries).
TRACKED_OPERATIONS = ('sync', 'scrub', 'check', 'fix', 'touch', 'snapraid')


def watch_jobs_once():
    """Record start/end/result of host SnapRAID jobs into the history table."""
    c = db()
    try:
        row = c.execute("SELECT value FROM settings WHERE key='active_jobs'").fetchone()
        active = json.loads(row[0]) if row else {}
    except (ValueError, sqlite3.Error):
        active = {}
    current = {}
    for p in snapraid_processes():
        if p['name'] in TRACKED_OPERATIONS:
            started = p.get('started')
            key = f"{p['pid']}:{int(started or 0)}"
            if key in active:
                current[key] = active[key]
            else:
                # Remember where the log ended when the job was first seen, so an
                # older summary already in an append-style log is never credited to it.
                log = read_snapraid_log(log_path_for(p['name']))
                current[key] = {'name': p['name'], 'started': started,
                                'log_path': log['path'], 'log_size': log['size'] or 0}
    finished = [job for key, job in active.items() if key not in current]
    if finished:
        for job in finished:
            path = resolve_log_path(log_path_for(job['name']))
            since = job.get('log_size', 0) if path == job.get('log_path') else 0
            log = read_snapraid_log(path, since=since)
            started = job.get('started') or time.time()
            outcome = {'result': None, 'detail': None}
            # Only trust log text this run appended (or a new log file written during it).
            if log['mtime'] and log['mtime'] >= started - 5:
                outcome = log_result(log['text'])
            status = outcome['result'] or 'Finished'
            message = outcome['detail'] or ('Result from ' + log['path'] if outcome['result']
                                            else 'Process ended; no result found in the log')
            c.execute('INSERT INTO events(ts,kind,status,message,duration) VALUES(?,?,?,?,?)',
                      (datetime.fromtimestamp(started, timezone.utc).isoformat(), job['name'], status,
                       message, time.time() - started))
    if finished or set(active) != set(current):
        c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('active_jobs',?)", (json.dumps(current),))
    c.commit()
    c.close()


def job_watch_loop():
    while True:
        try:
            watch_jobs_once()
        except Exception:
            pass
        time.sleep(10)


def history(limit=20, where='', params=()):
    # Older versions also stored a 'started' marker per run; those are not results.
    c = db()
    rows = c.execute('SELECT ts,kind,status,message,duration FROM events '
                     "WHERE status != 'started' " + where + ' ORDER BY ts DESC, id DESC LIMIT ?',
                     (*params, limit)).fetchall()
    c.close()
    events = []
    for ts, kind, status, message, duration in rows:
        try:
            start = datetime.fromisoformat(ts).timestamp()
        except ValueError:
            start = None
        end = start + (duration or 0) if start is not None else None
        events.append({'kind': kind, 'status': status, 'message': message,
                       'started': fmt_time(start) or ts, 'ended': fmt_time(end),
                       'ended_epoch': end, 'ended_age': fmt_age(end),
                       'duration': fmt_duration(duration) if duration else '-',
                       'level': status_level(status)})
    return events


def status_level(status):
    s = (status or '').lower()
    if s in OK_STATUSES:
        return 'ok'
    if s in NON_FAILURE_STATUSES:
        return 'warn'
    return 'bad'


OK_STATUSES = ('ok', 'success', 'succeeded', 'completed')
NON_FAILURE_STATUSES = OK_STATUSES + ('warning', 'finished', 'unknown', 'cancelled')


def last_run_summary():
    # Query the whole table, not just the visible page, so old results are never lost.
    def first(where='', params=()):
        found = history(1, where, params)
        return found[0] if found else None
    marks = lambda values: ','.join('?' * len(values))
    return {
        'last': first(),
        'last_failed': first(f'AND lower(status) NOT IN ({marks(NON_FAILURE_STATUSES)})', NON_FAILURE_STATUSES),
        'last_sync_ok': first(f"AND kind='sync' AND lower(status) IN ({marks(OK_STATUSES)})", OK_STATUSES),
        'last_scrub': first("AND kind='scrub'"),
    }


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
        threading.Thread(target=job_watch_loop,daemon=True,name='job-watch').start()


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
        member_map=re.search(r'\[([U_]+)\]', b)
        healthy=('_' not in member_map.group(1)) if member_map else state=='active'
        if state!='active' or '(F)' in b:
            healthy=False
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
    # Prefer a sync this monitor saw finish successfully. The content file mtime is only a
    # fallback: scrub, fix and touch rewrite it too, and sync saves it periodically mid-run.
    synced = last_run_summary().get('last_sync_ok')
    if synced and synced.get('ended_epoch'):
        ts, source = synced['ended_epoch'], 'run history'
    else:
        mtimes = []
        for p in snapraid_content_paths():
            try: mtimes.append(Path(p).stat().st_mtime)
            except OSError: pass
        if not mtimes: return None
        ts, source = max(mtimes), 'content file'
    age_hours = (time.time() - ts) / 3600
    return {'epoch': ts, 'iso': datetime.fromtimestamp(ts).astimezone().isoformat(),
            'display': fmt_time(ts), 'age': fmt_age(ts), 'source': source,
            'stale': age_hours > SYNC_STALE_HOURS}


def read_snapraid_snapshot():
    """Read a host-produced snapshot. Never execute SnapRAID from the monitor."""
    try:
        data = json.loads(Path(SNAPRAID_SNAPSHOT_PATH).read_text(errors='replace'))
        if not isinstance(data, dict) or data.get('schema') != 1:
            raise ValueError('Unsupported snapshot format')
        if not isinstance(data.get('status'), str) or not isinstance(data.get('diff'), str):
            raise ValueError('Missing status/diff text')
        if not isinstance(data.get('captured_at'), str):
            raise ValueError('Missing capture timestamp')
        if not isinstance(data.get('status_rc'), int) or not isinstance(data.get('diff_rc'), int):
            raise ValueError('Missing status/diff exit codes')
        return data, None
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError, TypeError) as exc:
        return None, str(exc)


def snapraid_summary():
    observation = snapraid_observation()
    running = observation['running']
    snapshot, snapshot_error = read_snapraid_snapshot()
    status = snapshot['status'] if snapshot else ''
    diff = snapshot['diff'] if snapshot else ''
    rc = snapshot['status_rc'] if snapshot else None
    rc2 = snapshot['diff_rc'] if snapshot else None
    status_ok = (rc == 0) if snapshot else None
    diff_ok = (rc2 in (0, 2)) if snapshot else None
    pending = {key: None for key in
               ('equal', 'added', 'removed', 'updated', 'moved', 'copied', 'restored')}
    pending_total = None
    if diff_ok:
        for key in pending:
            match = re.search(r'^\s*([0-9]+)\s+' + re.escape(key) + r'\s*$', diff, re.M)
            if match:
                pending[key] = int(match.group(1))
        # Do not present 0 when the diff format could not be parsed.
        if all(v is not None for v in pending.values()):
            pending_total = sum(v for k, v in pending.items() if k != 'equal')

    log = observation['log']
    last_log_result = log.get('result') or log_result(log.get('text', ''))['result'] or 'Unknown'
    if last_log_result == 'Error':
        last_log_result = 'Failed'
    config = snapraid_config_summary()
    warnings = list(dict.fromkeys(
        line.strip() for line in status.splitlines()
        if 'WARNING!' in line
    ))
    if config.get('data_disks', 0) >= 30 and config.get('parity_levels', 0) < 5:
        recommendation = 'WARNING! For 30 data disks, SnapRAID recommends five parity levels.'
        if not any('five parity levels' in w for w in warnings):
            warnings.append(recommendation)

    coverage = None
    if status_ok:
        match = re.search(r'(\d+)% of the array is not scrubbed', status)
        if match:
            coverage = 100 - int(match.group(1))

    # Scrub age, errors and interrupted sync come from the host snapshot only.
    health = parse_status(status if status_ok else '')

    if running:
        protection = {'sync': 'Syncing', 'scrub': 'Scrubbing', 'fix': 'Repair in progress',
                      'check': 'Checking'}.get(observation['name'], 'SnapRAID busy')
    elif health['errors']:
        protection = 'Errors in snapshot'
    elif health['sync_in_progress']:
        protection = 'Sync interrupted'
    elif last_log_result == 'OK':
        protection = 'Last log OK'
    elif last_log_result == 'Failed':
        protection = 'Last log failed'
    elif last_log_result == 'Warning':
        protection = 'Last log warning'
    else:
        protection = 'Not verified'

    scrub_stale = (health['scrub_newest_days'] is not None and health['scrub_newest_days'] > SCRUB_STALE_DAYS)

    return {
        'status_rc': rc, 'diff_rc': rc2, 'status_ok': status_ok,
        'diff_ok': diff_ok, 'status_raw': status, 'diff_raw': diff,
        'changes': pending, 'pending_total': pending_total,
        'protection': protection, 'warnings': warnings, 'health': health,
        'scrub_stale': scrub_stale,
        'last_sync': snapraid_last_sync(), 'config': config,
        'snapshot_time': snapshot['captured_at'] if snapshot else None,
        'snapshot_error': snapshot_error,
        'scrub_coverage': coverage, 'last_log_result': last_log_result,
        'maintenance_running': running,
        'job': {'name': observation['name'], 'progress': observation['progress'],
                'started': observation.get('started'), 'elapsed': observation.get('elapsed'),
                'pids': [p['pid'] for p in observation['processes']]},
        'last_run': observation.get('last_run') or {},
    }


def smart_for_disk(d):
    name=d['name']; dev='/dev/'+name
    smart={'health':'Unknown','health_detail':None,'temp':None,'power_hours':None,'reallocated':None,
           'pending':None,'uncorrectable':None,'error':None,'standby':False,'checked':fmt_time(time.time())}
    # -n standby: never spin up a sleeping disk just to read SMART.
    rc2,sout=run(['smartctl','-x','-j','-n','standby',dev],timeout=25)
    smart_serial=''
    j=None
    try:
        j=json.loads(sout)
    except Exception:
        j=None

    if (not j or not j.get('device')) and name.startswith('sd'):
        rc3,sout3=run(['smartctl','-x','-j','-n','standby','-d','scsi',dev],timeout=25)
        try:
            j3=json.loads(sout3)
            if j3.get('device'):
                j=j3; rc2=rc3; sout=sout3
        except Exception:
            pass

    msgs_text=' '.join(str(m.get('string','')) for m in ((j or {}).get('smartctl',{}).get('messages') or []))
    if re.search(r'STANDBY|SLEEP', msgs_text + (sout if not j else ''), re.I):
        # Keep the last reading taken while the disk was awake.
        prev=next((x for x in cache.get('data',{}).get('disks',[]) if x.get('name')==name), None)
        if prev:
            smart=dict(prev['smart'])
        else:
            smart['health']='Standby'
        smart['standby']=True
        return {'name':name,'dev':dev,'size':int(d.get('size') or 0),'size_h':human_bytes(d.get('size') or 0),
                'model':(d.get('model') or '').strip(),'serial':(d.get('serial') or '').strip() or (prev or {}).get('serial',''),
                'smart':smart}

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
        smart['uncorrectable']=amap.get('Offline_Uncorrectable')
        if smart['reallocated'] is None:
            smart['reallocated']=j.get('scsi_grown_defect_list')
        # SMART "PASSED" only checks vendor thresholds; pending/uncorrectable sectors are
        # the usual early sign of a failing HDD, so surface them.
        if smart['health']=='Healthy' and ((smart['pending'] or 0) > 0 or (smart['uncorrectable'] or 0) > 0):
            smart['health']='Warning'
            smart['health_detail']=f"{smart['pending'] or 0} pending, {smart['uncorrectable'] or 0} uncorrectable sector(s)"

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
            'standby':sum(1 for d in data['disks'] if d['smart'].get('standby')),
            'max_temp':max([d['smart']['temp'] for d in data['disks'] if d['smart']['temp'] is not None] or [None])
        }
        h=data['disk_health']
        h['other']=h['total']-h['healthy']-h['failed']-h['warning']
        cache['data']=data; cache['updated']=fmt_time(time.time())
        return True
    finally:
        cache['collecting']=False


def collector_loop():
    while True:
        collect(); time.sleep(max(20,REFRESH_SECONDS))

@app.route('/login',methods=['GET','POST'])
def login():
    if request.method=='POST':
        if hmac.compare_digest(request.form.get('password','').encode(), ADMIN_PASSWORD.encode()):
            session['auth']=True
            target=request.args.get('next') or ''
            if not target.startswith('/') or target.startswith('//'):
                target=url_for('index')
            return redirect(target)
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
    return render_template('index.html', data=data, updated=cache['updated'] or 'initializing',
                           events=history(20), last_run=last_run_summary(), stale_hours=SYNC_STALE_HOURS)

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
