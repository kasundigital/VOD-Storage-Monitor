"""Shared freshness rules for saved SnapRAID observations."""
from datetime import datetime
import time


def snapshot_current(snap, now=None):
    now = time.time() if now is None else now
    try:
        stamp = datetime.fromisoformat(snap['snapshot_time'].replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            return False
        epoch = stamp.timestamp()
        last_sync = (snap.get('last_sync') or {}).get('epoch')
        return -300 <= now - epoch <= 3600 and (last_sync is None or epoch >= last_sync)
    except (KeyError, AttributeError, TypeError, ValueError, OverflowError):
        return False
