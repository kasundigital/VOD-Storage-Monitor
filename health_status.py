"""Conservative display-only health assessment for VOD Storage Monitor.

These labels do not claim live SnapRAID verification. A recent host-captured
snapshot plus recent sync history is required for a green SnapRAID indicator.
"""
from datetime import datetime, timezone
import time

_LEVEL_ORDER = {'ok': 0, 'unknown': 1, 'busy': 2, 'warn': 3, 'bad': 4}


def _item(level, label, detail):
    return {'level': level, 'label': label, 'detail': detail}


def _worst(*items):
    return max(items, key=lambda x: _LEVEL_ORDER[x['level']])


def _recent_snapshot(value, now):
    if not value:
        return False
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            return False
        age = now - stamp.timestamp()
        return -300 <= age <= 3600  # host snapshot is at most an hour old
    except (TypeError, ValueError, OverflowError):
        return False


def assess_health(data, now=None):
    """Return source-aware presentation states for banner and four existing cards."""
    now = time.time() if now is None else now
    storage = data.get('storage') or {}
    vod = storage.get('vod') or {}
    if vod.get('error') or not vod.get('size'):
        vod_state = _item('bad' if vod.get('error') else 'unknown',
                          'Unavailable' if vod.get('error') else 'Not collected',
                          'VOD filesystem usage could not be read' if vod.get('error') else 'Waiting for storage data')
    else:
        pct = vod.get('pct') or 0
        if pct >= 95:
            vod_state = _item('bad', 'Almost full', f'VOD pool is {pct}% full')
        elif pct >= 90:
            vod_state = _item('warn', 'Space getting low', f'VOD pool is {pct}% full')
        else:
            vod_state = _item('ok', 'Healthy', f'{pct}% used · plenty of free capacity')

    raids = data.get('raid') or []
    raid_storage = storage.get('docker') or {}
    if raid_storage.get('error'):
        raid_state = _item('bad', 'Storage unavailable', 'Docker filesystem could not be read')
    elif not raids:
        raid_state = _item('unknown', 'Not verified', 'No mdadm RAID status was collected')
    elif any(not a.get('healthy') for a in raids):
        raid_state = _item('bad', 'Degraded array', 'One or more mdadm arrays need attention')
    elif any(a.get('progress') for a in raids):
        raid_state = _item('busy', 'RAID maintenance', 'An mdadm array is rebuilding or checking')
    elif not raid_storage.get('size'):
        raid_state = _item('unknown', 'Storage unknown', 'Docker filesystem usage was not collected')
    else:
        raid_state = _item('ok', 'Healthy', f'{len(raids)} mdadm array(s) reporting healthy')

    inventory = data.get('disk_inventory') or {}
    smart = data.get('disk_health') or {}
    total = smart.get('total', 0)
    failed = smart.get('failed', 0)
    warning = smart.get('warning', 0)
    other = smart.get('other', 0)
    expected = inventory.get('expected', 0)
    online = inventory.get('online', 0)
    missing = inventory.get('missing', 0)
    alerts = inventory.get('alerts') or []
    if failed or missing:
        disk_state = _item('bad', 'Disk attention', f'{failed} SMART failed · {missing} configured locations missing')
    elif warning or inventory.get('warnings', 0) or alerts:
        disk_state = _item('warn', 'Check disks', f'{warning} SMART warning(s) · {len(alerts)} inventory alert(s)')
    elif not total or not expected or inventory.get('health') != 'ok':
        disk_state = _item('unknown', 'Not verified', 'Waiting for SMART and expected disk inventory')
    elif other or online != expected:
        disk_state = _item('unknown', 'Partially verified', f'{other} SMART result(s) unknown · {online}/{expected} mounts online')
    else:
        disk_state = _item('ok', 'Healthy', f'{total}/{total} SMART healthy · {online}/{expected} mounts verified')

    snap = data.get('snapraid') or {}
    health = snap.get('health') or {}
    synced = snap.get('last_sync') or {}
    pending = snap.get('pending_total')
    fresh = _recent_snapshot(snap.get('snapshot_time'), now)
    if snap.get('maintenance_running'):
        name = (snap.get('job') or {}).get('name') or 'operation'
        snap_state = _item('busy', 'Maintenance running', f'Host SnapRAID {name} is running')
    elif (snap.get('status_ok') is False or snap.get('diff_ok') is False
          or health.get('errors') or health.get('sync_in_progress')):
        snap_state = _item('bad', 'Check SnapRAID', 'Snapshot reports an error or unfinished synchronization')
    elif pending is not None and pending > 0:
        snap_state = _item('warn', 'Changes pending', f'{pending} change(s) at last snapshot')
    elif synced.get('stale') and synced.get('epoch') is not None:
        snap_state = _item('warn', 'Sync overdue', f"Last verified sync: {synced.get('age', 'older than expected')}")
    elif not synced.get('epoch'):
        snap_state = _item('unknown', 'Sync unverified', 'No successful sync timestamp is available')
    elif not fresh or snap.get('status_ok') is not True or snap.get('diff_ok') is not True or pending is None:
        snap_state = _item('unknown', 'Verification needed', 'Capture a current host status/diff snapshot to verify parity')
    elif health.get('errors') is None or health.get('sync_in_progress') is not False:
        snap_state = _item('unknown', 'Incomplete snapshot', 'Host status did not confirm zero errors and a finished sync')
    elif snap.get('scrub_stale') or health.get('unscrubbed_pct') == 100:
        snap_state = _item('warn', 'Scrub needs attention', 'Latest snapshot indicates scrub maintenance is needed')
    else:
        snap_state = _item('ok', 'Verified at snapshot', 'No pending changes or reported errors in recent host snapshot')

    cards = {'vod': vod_state, 'raid': raid_state, 'snapraid': snap_state, 'disks': disk_state}
    worst = _worst(*cards.values())
    counts = {level: sum(1 for item in cards.values() if item['level'] == level)
              for level in _LEVEL_ORDER}
    if worst['level'] == 'bad':
        title = 'Action required'
    elif worst['level'] == 'warn':
        title = 'Review recommended'
    elif worst['level'] == 'busy':
        title = 'Maintenance in progress'
    elif worst['level'] == 'unknown':
        title = 'Verification needed'
    else:
        title = 'All monitored checks healthy'
    summary_parts = []
    for level, word in (('bad', 'critical'), ('warn', 'warning'), ('busy', 'in progress'),
                        ('unknown', 'unverified'), ('ok', 'healthy')):
        if counts[level]:
            summary_parts.append(f"{counts[level]} {word}")
    detail = ' · '.join(summary_parts) + ' · SnapRAID checks are snapshot-based, not live.'
    return {'level': worst['level'], 'label': title, 'detail': detail, 'cards': cards}
