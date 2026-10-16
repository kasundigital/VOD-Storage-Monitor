"""Read-only evidence of completed host SnapRAID sync systemd units.

Unlike SnapRAID content timestamps, a systemd unit's successful completion
proves that the specific host-run sync command exited successfully. The
dashboard never executes SnapRAID or acquires its content lock.
"""
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

# Only dedicated sync services, not generic maintenance units which might scrub.
_SYNC_OK = re.compile(
    r'^snapraid-[A-Za-z0-9_.-]*sync[A-Za-z0-9_.-]*\.service: Deactivated successfully\.$'
)
DEFAULT_JOURNAL_DIRS = '/host/var/log/journal:/host/run/log/journal'


def parse_sync_completions(journal_json_lines, now=None):
    """Latest trusted PID 1 completion timestamp from journalctl JSON lines."""
    now = time.time() if now is None else now
    latest = None
    for line in journal_json_lines.splitlines():
        try:
            item = json.loads(line)
            if not isinstance(item, dict) or item.get('_PID') != '1':
                continue
            if not _SYNC_OK.fullmatch(item.get('MESSAGE', '')):
                continue
            epoch = int(item['__REALTIME_TIMESTAMP']) / 1_000_000
            if not 0 < epoch <= now + 300:
                continue
        except (ValueError, TypeError, KeyError):
            continue
        latest = epoch if latest is None else max(latest, epoch)
    return latest


def last_successful_host_sync():
    """Find recent successful systemd-run sync completions in mounted host journals.

    Returns None when unavailable; never assumes success from content-file mtime
    or from a log that merely contains 'Everything OK' before an eventual failure.
    """
    if not shutil.which('journalctl'):
        return None
    latest = None
    journal_dirs = os.getenv('HOST_JOURNAL_DIRS', DEFAULT_JOURNAL_DIRS).split(':')
    cutoff = int(time.time() - 180 * 86400)
    for directory in dict.fromkeys(journal_dirs):
        if not directory or not Path(directory).is_dir():
            continue
        try:
            result = subprocess.run(
                ['journalctl', '--directory=' + directory, '--no-pager',
                 '--output=json', '--since=@' + str(cutoff),
                 '--grep=snapraid-.*sync.*\\.service: Deactivated successfully',
                 '--lines=100'],
                capture_output=True, text=True, check=False, timeout=8,
            )
            if result.returncode:
                continue
            found = parse_sync_completions(result.stdout)
            if found is not None:
                latest = found if latest is None else max(latest, found)
        except (OSError, subprocess.TimeoutExpired):
            continue
    return latest
