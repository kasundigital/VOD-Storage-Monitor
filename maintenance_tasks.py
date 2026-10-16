"""Evidence-aware maintenance to-do list for the read-only VOD dashboard.

Everything here is advisory. Neither this module nor the dashboard executes
SnapRAID commands, schedules work, or modifies the host.
"""
from datetime import datetime
import time

SNAPSHOT_MAX_AGE_SECONDS = 3600


def _snapshot_epoch(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        date = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if date.tzinfo is None:
            return None
        return date.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _task(id, title, status, label, detail, next_step, source, priority):
    return dict(id=id, title=title, status=status, label=label, detail=detail,
                next_step=next_step, source=source, priority=priority)


def pending_maintenance(data, now=None, cron=None):
    """Build the currently observed to-do list from collected monitor data.

    Statuses: due, waiting, running, blocked, unknown, done.
    A snapshot is historical; tasks always identify its source and never
    promise live protection or a current pending-change count.
    """
    now = time.time() if now is None else now
    snap = data.get('snapraid') or {}
    inventory = data.get('disk_inventory') or {}
    smart = data.get('disk_health') or {}
    health = snap.get('health') or {}
    last_sync = snap.get('last_sync') or {}
    job = snap.get('job') or {}
    operation = job.get('name') if snap.get('maintenance_running') else None
    snapshot_epoch = _snapshot_epoch(snap.get('snapshot_time'))
    snapshot_fresh = (snapshot_epoch is not None
                      and -300 <= now - snapshot_epoch <= SNAPSHOT_MAX_AGE_SECONDS)
    after_sync = (last_sync.get('epoch') is not None
                  and snapshot_epoch is not None
                  and snapshot_epoch >= last_sync['epoch'])
    evidence_ok = (snapshot_fresh and after_sync
                   and snap.get('status_ok') is True and snap.get('diff_ok') is True)
    pending = snap.get('pending_total') if snap.get('diff_ok') is True else None
    historical = (f"Host snapshot {snap['snapshot_time']}" if snapshot_epoch else
                  'Host snapshot not collected')
    tasks = []

    failed = smart.get('failed') or 0
    missing = inventory.get('missing') or 0
    warnings = smart.get('warning') or 0
    inventory_warnings = inventory.get('warnings') or 0
    if missing or failed:
        tasks.append(_task('disks', 'Check disk health and mounts', 'blocked', 'Critical',
            f'{missing} missing configured mount(s) and {failed} SMART failure(s) reported.',
            'Investigate affected disks and mount paths before parity maintenance.',
            'Current disk inventory and SMART', 0))
    elif warnings or inventory_warnings:
        tasks.append(_task('disks', 'Review disk warnings', 'due', 'Review',
            f'{warnings} SMART warning(s) and {inventory_warnings} inventory warning(s).',
            'Inspect affected SMART readings and physical disk identities.',
            'Current disk inventory and SMART', 1))

    if operation == 'sync':
        sync = _task('sync', 'SnapRAID sync', 'running', 'In progress',
                     'A host SnapRAID sync process is currently running.',
                     'Wait until the sync finishes. Do not start another.',
                     'Host process monitor', 2)
    elif health.get('errors') or health.get('sync_in_progress'):
        sync = _task('sync', 'Investigate interrupted sync', 'blocked', 'Investigate',
                     'The saved host status reports errors or an incomplete synchronization.',
                     'Review host SnapRAID logs and confirm disk availability before retrying.',
                     historical, 0)
    elif missing or failed:
        sync = _task('sync', 'SnapRAID sync', 'waiting', 'Blocked by disk issue',
                     'Disk health or mount issues require attention before syncing.',
                     'Resolve disk warnings above; then review the SnapRAID state.',
                     'Disk checks and '+historical, 1)
    elif pending is not None and pending > 0:
        sync = _task('sync', 'SnapRAID sync', 'due', 'Sync needed',
                     f'{pending:,} added, changed, or removed file(s) at the last snapshot.',
                     'Let downloads and imports finish, then run a controlled host sync.',
                     historical, 2)
    elif last_sync.get('stale') and last_sync.get('epoch') is not None:
        sync = _task('sync', 'Review sync schedule', 'due', 'Sync overdue',
                     f"Last successful sync: {last_sync.get('age') or 'older than threshold'}.",
                     'Check for pending files on the host before deciding to sync.',
                     'Last verified sync history', 2)
    elif evidence_ok and pending == 0 and health.get('errors') == 0 and health.get('sync_in_progress') is False:
        sync = _task('sync', 'SnapRAID sync', 'done', 'No changes at snapshot',
                     'The recent host snapshot reported zero pending changes.',
                     'No immediate sync required based on this saved snapshot.',
                     historical, 5)
    else:
        sync = _task('sync', 'Verify pending sync changes', 'unknown', 'Not verified',
                     'Current unsynced file changes cannot be confirmed from available evidence.',
                     'When no SnapRAID job is running, capture a fresh host status/diff snapshot.',
                     historical, 3)
    tasks.append(sync)

    unscrubbed = health.get('unscrubbed_pct') if snap.get('status_ok') is True else None
    oldest = health.get('scrub_oldest_days') if snap.get('status_ok') is True else None
    if operation == 'scrub':
        scrub = _task('scrub', 'SnapRAID scrub', 'running', 'In progress',
                      'A host SnapRAID scrub process is running.',
                      'Let the current scrub finish; refresh the snapshot after it ends.',
                      'Host process monitor', 3)
    elif missing or failed or health.get('errors') or health.get('sync_in_progress'):
        scrub = _task('scrub', 'SnapRAID scrub', 'waiting', 'Resolve faults first',
                      'Stored checks report an array or disk issue needing investigation.',
                      'Address the disk / sync issue before starting a scrub.',
                      historical, 3)
    elif unscrubbed is None and not snap.get('scrub_stale'):
        scrub = _task('scrub', 'Verify scrub coverage', 'unknown', 'Not verified',
                      'No valid host snapshot of scrub coverage is available.',
                      'Capture a host status snapshot after maintenance has finished.',
                      historical, 4)
    elif unscrubbed and unscrubbed > 0 or snap.get('scrub_stale'):
        needs_sync = sync['status'] in ('due', 'blocked', 'running', 'waiting')
        extra = f'{unscrubbed}% of blocks not yet scrubbed.' if unscrubbed is not None else 'Last scrub is overdue.'
        if oldest is not None:
            extra += f' Oldest block checked {oldest}d ago.'
        scrub = _task('scrub', 'SnapRAID scrub', 'waiting' if needs_sync else 'due',
                      'After sync' if needs_sync else 'Scrub recommended', extra,
                      ('Complete the pending sync first, then plan a partial scrub.' if needs_sync
                       else 'Plan a partial host scrub when disks are idle.'),
                      historical, 3)
    else:
        scrub = _task('scrub', 'SnapRAID scrub', 'done', 'Coverage current',
                      'The last captured status reported no un-scrubbed blocks and no overdue scrub.',
                      'Continue periodic host scrub maintenance as normal.',
                      historical, 5)
    tasks.append(scrub)

    if operation:
        snapshot_task = _task('snapshot', 'Refresh status snapshot', 'waiting', 'After maintenance',
                     'The saved status/diff snapshot may be outdated during active maintenance.',
                     'Capture a new status/diff snapshot after the host job finishes.',
                     historical, 4)
    elif sync['status'] in ('due', 'running', 'blocked', 'waiting') and (pending or 0) > 0:
        snapshot_task = _task('snapshot', 'Refresh status snapshot', 'waiting', 'After sync',
                     'The current snapshot describes file changes awaiting sync.',
                     'Capture a fresh host status/diff snapshot once sync has completed.',
                     historical, 4)
    elif not evidence_ok:
        snapshot_task = _task('snapshot', 'Capture status/diff snapshot', 'due', 'Refresh needed',
                     'Status/diff evidence is missing, stale, or predates the last verified sync.',
                     'Run the existing host snapshot script while SnapRAID is idle.',
                     historical, 4)
    else:
        snapshot_task = _task('snapshot', 'Status/diff snapshot', 'done', 'Recently captured',
                     'Recent status/diff evidence is available from the host.',
                     'Refresh after the next host maintenance operation.',
                     historical, 5)
    tasks.append(snapshot_task)

    order = {'blocked': 0, 'due': 1, 'running': 2, 'waiting': 3, 'unknown': 4, 'done': 5}
    # Link advisory Sync/Scrub items to the host's existing cron schedule.
    # No schedule is installed, changed, or executed by the monitor.
    cron = cron or {}
    next_jobs = cron.get('next_by_operation') or {}
    for task in tasks:
        if task['id'] not in ('sync', 'scrub'):
            continue
        scheduled = next_jobs.get(task['id'])
        if scheduled:
            task['schedule'] = {
                'status': 'configured', 'label': 'Next cron run',
                'next_display': scheduled['next_display'],
                'next_iso': scheduled['next_iso'],
                'expression': scheduled['expression'],
                'timezone': scheduled['timezone'],
                'source': scheduled['source']
            }
        elif cron.get('available'):
            task['schedule'] = {'status': 'none', 'label': 'No matching cron job',
                                'next_display': 'Not scheduled', 'source': 'Host crontab'}
        else:
            task['schedule'] = {'status': 'unknown', 'label': 'Schedule unavailable',
                                'next_display': 'Could not read host cron',
                                'source': 'Host crontab'}
    tasks.sort(key=lambda item: (order[item['status']], item['priority'], item['id']))
    counts = {state: sum(t['status'] == state for t in tasks) for state in order}
    actionable = counts['blocked'] + counts['due']
    if actionable:
        title = f"{actionable} maintenance item{'s' if actionable != 1 else ''} need attention"
    elif counts['running']:
        title = 'Maintenance in progress'
    elif counts['waiting']:
        title = 'Follow-up tasks after maintenance'
    elif counts['unknown']:
        title = 'Maintenance status needs verification'
    else:
        title = 'No immediate maintenance tasks'
    return {'title': title, 'attention': actionable, 'counts': counts,
            'items': tasks, 'source_note': 'Read-only advice. Next-run dates are calculated from mounted host crontabs; actual execution is not guaranteed.'}
