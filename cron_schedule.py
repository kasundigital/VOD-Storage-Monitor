"""Read-only host crontab discovery for SnapRAID sync/scrub schedules.

Reads only bind-mounted Ubuntu cron files, never invokes `crontab`, shell
commands, host utilities, or SnapRAID. Cron job commands are never exposed in
API responses (the original cron file might contain secrets).
"""
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import os
import re
import shlex
import time

from croniter import croniter

ROOT_CRONTAB = Path('/host/cron/spool/root')
SYSTEM_CRONTAB = Path('/host/cron/etc-crontab')
SYSTEM_CRON_D = Path('/host/cron/cron.d')
VALID_FILENAME = re.compile(r'^[A-Za-z0-9_-]+$')
CRON_MACROS = {'@hourly', '@daily', '@midnight', '@weekly', '@monthly',
               '@yearly', '@annually'}
VALID_TZ = re.compile(r'^[A-Za-z0-9_+./-]+$')


def _time_zone(value):
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return ZoneInfo('UTC')


def _operation(command):
    """Recognize explicit SnapRAID executable plus sync/scrub operation only."""
    try:
        args = shlex.split(command, comments=False)
    except ValueError:
        return None
    if not args:
        return None
    # Do not mistake an unrelated program such as echo/grep for a scheduled
    # SnapRAID job merely because its arguments contain "snapraid sync".
    first = Path(args[0]).name
    allowed = {'snapraid-maintenance.sh', 'snapraid'}
    wrappers = {'flock', 'nice', 'ionice', 'timeout', 'env', 'bash', 'sh'}
    if first not in allowed | wrappers:
        return None
    for i, token in enumerate(args):
        if Path(token).name not in ('snapraid-maintenance.sh', 'snapraid'):
            continue
        # Allow optional SnapRAID flags before the operation, but do not
        # interpret an arbitrary command containing the word 'snapraid'.
        for arg in args[i + 1:i + 8]:
            if arg in ('sync', 'scrub'):
                return arg
            if arg.startswith('scrub') and Path(token).name == 'snapraid-maintenance.sh':
                return 'scrub'
            if arg in (';', '&&', '||', '|'):
                break
    return None


def _parse_line(line, system_crontab):
    parts = line.split(None, 6 if system_crontab else 5)
    if not parts:
        return None
    if parts[0].startswith('@'):
        if parts[0].lower() not in CRON_MACROS:
            return None
        if system_crontab:
            if len(parts) < 3:
                return None
            expression, command = parts[0], ' '.join(parts[2:])
        else:
            if len(parts) < 2:
                return None
            expression, command = parts[0], ' '.join(parts[1:])
    else:
        required = 7 if system_crontab else 6
        if len(parts) < required:
            return None
        expression = ' '.join(parts[:5])
        command = parts[6] if system_crontab else parts[5]
    operation = _operation(command)
    if not operation or not croniter.is_valid(expression):
        return None
    return expression, operation


def _read_jobs(path, name, system, default_tz, now):
    try:
        contents = path.read_text(encoding='utf-8', errors='replace')
    except FileNotFoundError:
        return [], None
    except OSError as exc:
        return [], f'Cannot read {name}: {exc.strerror or exc}'
    zone = default_tz
    jobs = []
    for line in contents.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('CRON_TZ='):
            value = line.partition('=')[2].strip().strip('"\'')
            if VALID_TZ.fullmatch(value):
                zone = value
            continue
        if '=' in line.split(None, 1)[0]:
            continue
        found = _parse_line(line, system)
        if not found:
            continue
        expression, operation = found
        tz = _time_zone(zone)
        try:
            nxt = croniter(expression, datetime.fromtimestamp(now, tz)).get_next(datetime)
        except (ValueError, OverflowError, TypeError):
            continue
        jobs.append({
            'operation': operation, 'expression': expression, 'source': name,
            'timezone': zone, 'next_epoch': nxt.timestamp(),
            'next_display': nxt.strftime('%a %d %b %Y, %I:%M %p %Z'),
            'next_iso': nxt.isoformat()
        })
    return jobs, None


def host_cron_schedules(now=None):
    """Find next root/system SnapRAID cron runs from mounted host files."""
    now = time.time() if now is None else now
    timezone = os.environ.get('TZ') or 'UTC'
    files = [(ROOT_CRONTAB, 'root crontab', False),
             (SYSTEM_CRONTAB, '/etc/crontab', True)]
    warnings = []
    if SYSTEM_CRON_D.is_dir():
        try:
            files.extend((p, '/etc/cron.d/' + p.name, True)
                         for p in sorted(SYSTEM_CRON_D.iterdir())[:100]
                         if p.is_file() and VALID_FILENAME.fullmatch(p.name))
        except OSError:
            warnings.append('Cannot list /etc/cron.d')
    jobs = []
    readable = 0
    for path, label, system in files:
        if path.is_file():
            readable += 1
        found, error = _read_jobs(path, label, system, timezone, now)
        jobs.extend(found)
        if error:
            warnings.append(error)
    jobs.sort(key=lambda job: job['next_epoch'])
    next_by_operation = {}
    for job in jobs:
        next_by_operation.setdefault(job['operation'], job)
    if not readable:
        warnings.append('Host cron files are unavailable (check read-only Docker mounts).')
    return {
        'available': readable > 0,
        'timezone': timezone,
        'jobs': jobs,
        'next_by_operation': next_by_operation,
        'warnings': warnings,
        'note': 'Read-only host crontab view. Times are estimates; cron daemon state is not verified.'
    }
