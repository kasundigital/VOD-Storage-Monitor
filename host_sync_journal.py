"""Read-only evidence of completed host SnapRAID sync systemd units.

Unlike SnapRAID content timestamps, a systemd unit's successful completion
proves that the specific host-run sync command exited successfully. The
dashboard never executes SnapRAID or acquires its content lock.
"""
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

# Only dedicated sync services, not generic maintenance units which might scrub.
_SYNC_OK = re.compile(
    r'^snapraid-[A-Za-z0-9_.-]*sync[A-Za-z0-9_.-]*\.service: Deactivated successfully\.$'
)
DEFAULT_JOURNAL_DIRS = '/host/var/log/journal:/host/run/log/journal'


# journalctl -o short-unix includes the original host service identity and an
# unambiguous UNIX epoch. It avoids depending on optional JSON journal fields.
_SHORT_UNIX = re.compile(
    r'^\s*(?P<timestamp>\d+(?:\.\d+)?)\s+\S+\s+systemd\[1\]:\s+(?P<message>.+)$'
)


def parse_sync_completions(journal_lines, now=None):
    """Latest verified host PID 1 sync-service completion timestamp.

    Accepts short-unix host journal records and JSON lines for compatibility.
    Never interprets completion of a scrub/fix unit as a successful sync.
    """
    now = time.time() if now is None else now
    latest = None
    for line in journal_lines.splitlines():
        try:
            match = _SHORT_UNIX.fullmatch(line)
            if match:
                message = match.group('message')
                epoch = float(match.group('timestamp'))
            else:
                item = json.loads(line)
                if not isinstance(item, dict) or str(item.get('_PID')) != '1':
                    continue
                message = item.get('MESSAGE', '')
                epoch = int(item['__REALTIME_TIMESTAMP']) / 1_000_000
            if not _SYNC_OK.fullmatch(message):
                continue
            if not 0 < epoch <= now + 300:
                continue
        except (ValueError, TypeError, KeyError):
            continue
        latest = epoch if latest is None else max(latest, epoch)
    return latest


# Query the recent host journal first: searching 180 days of log data on large
# media servers can take longer than the previous hard-coded 8-second timeout.
# Expand only if there is no confirmed success in the recent window.
SEARCH_WINDOWS = ((2, 30), (7, 45), (30, 60), (180, 90))
SYNC_CACHE_TTL_SECONDS = 300
_sync_cache = {'expires': 0.0, 'value': None}
_sync_cache_lock = threading.Lock()
logger = logging.getLogger(__name__)


def _scan_successful_host_sync():
    """Return the latest matching successful host sync completion, or None.

    This only reads host journals. The monitor never launches SnapRAID or takes
    its content lock. Every search checks all configured journal directories.
    """
    if not shutil.which('journalctl'):
        return None
    journal_dirs = tuple(dict.fromkeys(
        d for d in os.getenv('HOST_JOURNAL_DIRS', DEFAULT_JOURNAL_DIRS).split(':')
        if d and Path(d).is_dir()
    ))
    if not journal_dirs:
        return None

    for days, timeout in SEARCH_WINDOWS:
        cutoff = datetime.fromtimestamp(time.time() - days * 86400).strftime('%Y-%m-%d %H:%M:%S')
        latest = None
        for directory in journal_dirs:
            try:
                result = subprocess.run(
                    ['journalctl', '--directory=' + directory, '--no-pager',
                     '--output=short-unix', '--since=' + cutoff,
                     '--grep=snapraid-', '--lines=300'],
                    capture_output=True, text=True, check=False, timeout=timeout,
                )
                if result.returncode:
                    # Volatile journal mounts legitimately may have no entries.
                    continue
                found = parse_sync_completions(result.stdout)
                if found is not None:
                    latest = found if latest is None else max(latest, found)
            except subprocess.TimeoutExpired:
                logger.warning(
                    'Host SnapRAID journal query timed out: path=%s days=%d timeout=%ds',
                    directory, days, timeout,
                )
            except OSError as exc:
                logger.warning('Host SnapRAID journal unavailable at %s: %s', directory, exc)
        if latest is not None:
            return latest
    return None


def last_successful_host_sync():
    """Cached successful sync from host journal, safe for frequent UI polling.

    Cache even a negative result for five minutes to avoid repeatedly scanning
    busy journals. A new successful sync may take up to five minutes to display.
    """
    with _sync_cache_lock:
        if time.monotonic() < _sync_cache['expires']:
            return _sync_cache['value']
        value = _scan_successful_host_sync()
        _sync_cache['value'] = value
        _sync_cache['expires'] = time.monotonic() + SYNC_CACHE_TTL_SECONDS
        return value
