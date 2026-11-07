"""Read completed host maintenance logs without running or modifying SnapRAID."""
import glob
import os
import re
import time

_FOOTER = re.compile(
    r'^=== COMPLETED SUCCESSFULLY ===\n'
    r'(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) [^\n]+ \d{4}\s*\Z', re.M)


def completed_sync_log(path, now=None):
    """Return completion-log mtime only for the known successful sync wrapper.

    A bare 'Everything OK' is insufficient: saving/verifying content may fail
    afterwards. Require the sync header and final wrapper success footer. On
    Sundays the footer follows the scrub, so this is a conservative completion
    time for the whole maintenance run, not the exact sync-process end time.
    """
    now = time.time() if now is None else now
    candidates = []
    for name in glob.glob(path) if path else []:
        try:
            if os.path.isfile(name):
                candidates.append((os.path.getmtime(name), name))
        except OSError:
            continue
    for modified, name in sorted(candidates, reverse=True)[:20]:
        if not 0 < modified <= now + 300:
            continue
        try:
            with open(name, 'rb') as stream:
                header = stream.read(4096).decode('utf-8', errors='replace')
                stream.seek(0, os.SEEK_END)
                stream.seek(max(0, stream.tell() - 98304))
                tail = stream.read().decode('utf-8', errors='replace')
            if (header.startswith('=== SnapRAID sync ===\n')
                    and re.search(r'^Starting SnapRAID sync$', header, re.M)
                    and _FOOTER.search(tail)):
                return modified
        except OSError:
            continue
    return None
