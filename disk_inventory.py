"""Read-only validation of expected SnapRAID disks versus host mounts and block IDs.

This module never mounts, unmounts, repairs, or invokes SnapRAID.
"""
import json
import os
import re
import tempfile
from pathlib import Path
from datetime import datetime, timezone


def expected_from_config(config_path):
    expected = []
    text = Path(config_path).read_text(errors='replace')
    for original in text.splitlines():
        line = original.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split()
        if parts[0] == 'data' and len(parts) >= 3:
            name, raw = parts[1], parts[2]
            role, path = 'Data', os.path.normpath(raw)
        elif re.fullmatch(r'(?:[2-6]-)?parity', parts[0]) and len(parts) >= 2:
            name = parts[0]
            role, path = 'Parity', os.path.dirname(os.path.normpath(parts[1]))
        else:
            continue
        if not os.path.isabs(path):
            continue
        expected.append({'id': name, 'role': role, 'path': path})
    return expected


def _decode_mount(s):
    # /proc/<pid>/mountinfo uses octal escapes for spaces, tabs and backslashes.
    return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m.group(1), 8)), s)


def host_mounts(path='/host/proc/1/mountinfo'):
    mounts = []
    for line in Path(path).read_text().splitlines():
        try:
            left, right = line.split(' - ', 1)
            l, r = left.split(), right.split()
            mounts.append({'path': os.path.normpath(_decode_mount(l[4])),
                           'major_minor': l[2], 'fstype': r[0],
                           'source': _decode_mount(r[1]) if len(r) > 1 else ''})
        except (ValueError, IndexError):
            continue
    return mounts


def block_device_map(run):
    rc, raw = run(['lsblk', '-J', '-b', '-o', 'NAME,TYPE,MAJ:MIN,SERIAL'], timeout=20)
    if rc:
        raise OSError('lsblk could not list block devices')
    devices = {}
    def walk(node, disk_name=None, disk_serial=None):
        if node.get('type') == 'disk':
            disk_name = str(node.get('name') or '')
            disk_serial = str(node.get('serial') or '').strip()
        mm = node.get('maj:min')
        if mm:
            devices[str(mm)] = {'device': disk_name or str(node.get('name') or ''),
                                'serial': disk_serial or ''}
        for child in node.get('children') or []:
            walk(child, disk_name, disk_serial)
    for dev in json.loads(raw).get('blockdevices', []):
        walk(dev)
    return devices


def _mount_for(path, mounts):
    # Ignore / and /mnt: those can be mounted while /mnt/diskXX is not.
    eligible = [m for m in mounts if m['path'] not in ('/', '/mnt')
                and (path == m['path'] or path.startswith(m['path'].rstrip('/') + '/'))]
    return max(eligible, key=lambda item: len(item['path'])) if eligible else None


def _load_baseline(path):
    try:
        data = json.loads(Path(path).read_text())
        if data.get('schema') != 1 or not isinstance(data.get('slots'), dict):
            raise ValueError('Unsupported baseline format')
        return data, None
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError, TypeError) as e:
        return None, f'Unable to read saved disk inventory: {e}'


def check_inventory(config_path, run, mountinfo_path='/host/proc/1/mountinfo',
                    baseline_path='/data/disk-baseline.json'):
    result = {'expected': 0, 'online': 0, 'missing': 0, 'warnings': 0,
              'entries': [], 'alerts': [], 'health': 'unknown',
              'baseline': 'Unavailable', 'checked': datetime.now(timezone.utc).isoformat()}
    try:
        expected = expected_from_config(config_path)
    except OSError as e:
        result['alerts'].append({'severity':'warning', 'label':'SnapRAID configuration unreadable',
                                  'detail':str(e)})
        return result
    result['expected'] = len(expected)
    if not expected:
        result['alerts'].append({'severity':'warning', 'label':'No expected disks found',
                                  'detail':'Check the data and parity paths in snapraid.conf.'})
        return result
    try:
        mounts = host_mounts(mountinfo_path)
    except OSError as e:
        result['alerts'].append({'severity':'warning', 'label':'Host mount table unavailable',
                                  'detail':str(e)})
        return result
    try:
        devices = block_device_map(run)
        block_error = None
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as e:
        devices, block_error = {}, str(e)

    baseline, baseline_error = _load_baseline(baseline_path)
    if baseline_error:
        result['alerts'].append({'severity':'warning', 'label':'Saved inventory unavailable',
                                  'detail':baseline_error})
    saved_slots = baseline['slots'] if baseline else {}
    current_slots = set()

    for slot in expected:
        key = slot['role'] + ':' + slot['id'] + ':' + slot['path']
        current_slots.add(key)
        record = {**slot, 'status': 'online', 'device': None, 'serial': None,
                  'expected_serial': saved_slots.get(key), 'mount': None,
                  'detail': '', 'severity': 'ok'}
        mount = _mount_for(slot['path'], mounts)
        if mount is None:
            record.update(status='missing_mount', severity='critical',
                          detail='No individual host filesystem mounted at this configured path')
        else:
            record['mount'] = mount['path']
            if block_error:
                record.update(status='unverified', severity='warning',
                              detail='Unable to verify attached disk: ' + block_error)
            elif mount['major_minor'] not in devices:
                record.update(status='device_missing', severity='critical',
                              detail='Mounted path is not backed by a block device visible to lsblk')
            else:
                device = devices[mount['major_minor']]
                record['device'], record['serial'] = device['device'], device['serial']
                if record['expected_serial'] and record['serial'] and record['serial'] != record['expected_serial']:
                    record.update(status='identity_changed', severity='warning',
                                  detail='Physical drive serial differs from saved healthy inventory')
                elif not record['serial']:
                    record.update(status='unverified', severity='warning',
                                  detail='Drive serial unavailable; cannot verify physical identity')
        if record['status'] == 'online':
            result['online'] += 1
        else:
            if record['severity'] == 'critical':
                result['missing'] += 1
            else:
                result['warnings'] += 1
            result['alerts'].append({'severity':record['severity'],
                                     'label':f"{slot['role']} {slot['id']} — {record['status'].replace('_',' ')}",
                                     'detail':f"{slot['path']} · {record['detail']}"
                                              + (f" · last serial {record['expected_serial']}" if record['expected_serial'] else '')})
        result['entries'].append(record)

    for key, serial in saved_slots.items():
        if key not in current_slots:
            result['missing'] += 1
            result['alerts'].append({'severity':'critical', 'label':'Previously monitored disk removed from configuration',
                                      'detail':f'{key} · saved serial {serial}'})
    if block_error:
        result['alerts'].append({'severity':'warning', 'label':'Physical device discovery failed',
                                  'detail':block_error})
    if not baseline and not baseline_error:
        # Only establish a serial baseline when every configured slot is online.
        # Never silently accept a degraded array as the new expected hardware.
        if (result['missing'] == 0 and result['warnings'] == 0
                and len({e['serial'] for e in result['entries']}) == len(expected)):
            snapshot = {'schema': 1, 'captured_at':result['checked'],
                        'slots': {e['role']+':'+e['id']+':'+e['path']:e['serial']
                                  for e in result['entries']}}
            try:
                dest = Path(baseline_path)
                dest.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix='.disk-baseline-')
                try:
                    with os.fdopen(fd, 'w') as stream:
                        json.dump(snapshot, stream, indent=2)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(tmp, dest)
                    baseline = snapshot
                finally:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
            except OSError as e:
                result['alerts'].append({'severity':'warning', 'label':'Cannot save healthy inventory',
                                          'detail':str(e)})
    result['baseline'] = ('Saved from a complete healthy set' if baseline and
                          baseline.get('captured_at') == result['checked'] else
                          ('Loaded (' + baseline.get('captured_at','unknown date') + ')' if baseline else
                           'Not captured — wait for all expected disks to be online'))
    result['health'] = 'critical' if result['missing'] else ('warning' if result['alerts'] else 'ok')
    return result
