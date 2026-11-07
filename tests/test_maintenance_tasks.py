"""Maintenance checklist and host crontab next-run regression tests."""
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo
import os
import tempfile
import unittest

from cron_schedule import host_cron_schedules, infer_wrapper_scrub, _operation, _parse_line
from maintenance_tasks import pending_maintenance

TZ = ZoneInfo('America/Chicago')
NOW = datetime(2026, 10, 15, 21, 30, tzinfo=TZ).timestamp()


def sample_data(pending=629, unscrubbed=100):
    return {
        'snapraid': {
            'maintenance_running': False, 'status_ok': True, 'diff_ok': True,
            'snapshot_time': datetime.fromtimestamp(NOW - 60, TZ).isoformat(),
            'pending_total': pending,
            'health': {'errors': 0, 'sync_in_progress': False,
                       'unscrubbed_pct': unscrubbed, 'scrub_oldest_days': 4},
            'last_sync': {'epoch': NOW - 7200, 'stale': False, 'age': '2h ago'},
            'scrub_stale': False,
            'job': {'name': None}
        },
        'disk_inventory': {'missing': 0, 'warnings': 0},
        'disk_health': {'failed': 0, 'warning': 0}
    }


def cron():
    next_sync = datetime(2026, 10, 16, 2, 10, tzinfo=TZ)
    next_scrub = datetime(2026, 10, 18, 3, 30, tzinfo=TZ)
    return {
        'available': True,
        'next_by_operation': {
            'sync': {
                'source': 'root crontab', 'expression': '10 2 * * *',
                'timezone': 'America/Chicago', 'next_epoch': next_sync.timestamp(),
                'next_iso': next_sync.isoformat(), 'next_display': 'Fri 16 Oct 2026, 02:10 AM CDT'
            },
            'scrub': {
                'source': '/etc/cron.d/snapraid', 'expression': '30 3 * * 0',
                'timezone': 'America/Chicago', 'next_epoch': next_scrub.timestamp(),
                'next_iso': next_scrub.isoformat(), 'next_display': 'Sun 18 Oct 2026, 03:30 AM CDT'
            }
        }
    }


class CronReadTests(unittest.TestCase):
    def test_root_crontab_and_system_cron_d_schedule(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            spool = home / 'spool'
            spool.mkdir()
            root = spool / 'root'
            root.write_text(
                'CRON_TZ=America/Chicago\n'
                '# 0 0 * * * /usr/bin/snapraid sync\n'
                '10 2 * * * /usr/local/sbin/snapraid-maintenance.sh sync\n'
                '45 4 * * * find /var/log/snapraid-maintenance -name *.log -delete\n'
                'MAILTO=admin@example.com\n'
            )
            system = home / 'crontab'
            system.write_text('SHELL=/bin/sh\n')
            cron_d = home / 'cron.d'
            cron_d.mkdir()
            (cron_d / 'snapraid').write_text(
                'CRON_TZ=America/Chicago\n'
                '30 3 * * 0 root /usr/bin/snapraid -c /etc/snapraid.conf scrub\n'
            )
            (cron_d / 'ignored.disabled').write_text('10 5 * * * root /usr/bin/snapraid sync\n')
            with (patch('cron_schedule.ROOT_CRONTAB', root),
                  patch('cron_schedule.SYSTEM_CRONTAB', system),
                  patch('cron_schedule.SYSTEM_CRON_D', cron_d),
                  patch.dict(os.environ, {'TZ': 'America/Chicago'})):
                status = host_cron_schedules(now=NOW)
        self.assertTrue(status['available'])
        self.assertEqual(len(status['jobs']), 2)
        self.assertEqual(status['next_by_operation']['sync']['next_display'],
                         'Fri 16 Oct 2026, 02:10 AM CDT')
        self.assertEqual(status['next_by_operation']['scrub']['next_display'],
                         'Sun 18 Oct 2026, 03:30 AM CDT')
        self.assertFalse(any('MAILTO=' in str(v) for v in status['jobs']))

    def test_no_host_crontab_is_unknown_not_unconfigured(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            with (patch('cron_schedule.ROOT_CRONTAB', base/'missing-root'),
                  patch('cron_schedule.SYSTEM_CRONTAB', base/'missing-etc'),
                  patch('cron_schedule.SYSTEM_CRON_D', base/'missing-dir')):
                status = host_cron_schedules(now=NOW)
        self.assertFalse(status['available'])
        self.assertFalse(status['jobs'])
        self.assertTrue(status['warnings'])

    def test_friendly_macros_and_disabled_jobs(self):
        self.assertEqual(_parse_line('@weekly root /usr/bin/snapraid scrub', True),
                         ('@weekly', 'scrub'))
        self.assertEqual(_parse_line('@daily /usr/local/sbin/snapraid-maintenance.sh scrub10', False),
                         ('@daily', 'scrub'))
        self.assertIsNone(_parse_line('@reboot /usr/bin/snapraid sync', False))
        self.assertIsNone(_parse_line('0 2 * * * echo snapraid sync', False))
        self.assertIsNone(_parse_line('not-a-cron-entry', False))

    def test_no_shell_or_snapraid_is_invoked_by_schedule_reader(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'root'
            root.write_text('0 1 * * * /usr/bin/snapraid sync\n')
            with (patch('cron_schedule.ROOT_CRONTAB', root),
                  patch('cron_schedule.SYSTEM_CRONTAB', Path(directory)/'missing-etc'),
                  patch('cron_schedule.SYSTEM_CRON_D', Path(directory)/'missing-dir'),
                  patch('os.system', side_effect=AssertionError('No host shell allowed'))):
                self.assertEqual(len(host_cron_schedules(now=NOW)['jobs']), 1)


class MaintenanceTasksTests(unittest.TestCase):
    def test_pending_sync_scrub_and_snapshot_with_cron_dates(self):
        # Changes since a recent sync with the next cron sync due before the
        # last sync goes stale are normal drift, not an action item.
        status = pending_maintenance(sample_data(), now=NOW, cron=cron())
        jobs = {t['id']: t for t in status['items']}
        self.assertEqual(jobs['sync']['status'], 'waiting')
        self.assertEqual(jobs['sync']['label'], 'Scheduled sync')
        self.assertIn('629', jobs['sync']['detail'])
        self.assertIn('Fri 16 Oct', jobs['sync']['schedule']['next_display'])
        self.assertEqual(jobs['scrub']['status'], 'waiting')
        self.assertIn('100%', jobs['scrub']['detail'])
        self.assertIn('Sun 18 Oct', jobs['scrub']['schedule']['next_display'])
        self.assertEqual(jobs['snapshot']['status'], 'waiting')
        self.assertEqual(status['attention'], 0)

    def test_pending_changes_without_schedule_still_need_sync(self):
        result = pending_maintenance(sample_data(), now=NOW, cron={'available': True, 'jobs': []})
        jobs = {t['id']: t for t in result['items']}
        self.assertEqual(jobs['sync']['status'], 'due')
        self.assertEqual(result['attention'], 1)

    def test_pending_changes_when_cron_sync_is_too_late_still_need_sync(self):
        d = sample_data()
        d['snapraid']['last_sync']['epoch'] = NOW - 23 * 3600  # 23h + 4.7h to next cron > 26h
        jobs = {t['id']: t for t in pending_maintenance(d, now=NOW, cron=cron())['items']}
        self.assertEqual(jobs['sync']['status'], 'due')

    def test_stale_last_sync_is_not_excused_by_schedule(self):
        d = sample_data()
        d['snapraid']['last_sync'].update(stale=True, epoch=NOW - 30 * 3600)
        jobs = {t['id']: t for t in pending_maintenance(d, now=NOW, cron=cron())['items']}
        self.assertEqual(jobs['sync']['status'], 'due')

    def test_zero_pending_and_full_scrub_is_done(self):
        d = sample_data(pending=0, unscrubbed=0)
        result = pending_maintenance(d,now=NOW,cron=cron())
        jobs={t['id']:t for t in result['items']}
        self.assertEqual(jobs['sync']['status'], 'done')
        self.assertEqual(jobs['scrub']['status'], 'done')
        self.assertEqual(jobs['snapshot']['status'], 'done')

    def test_missing_cron_is_unavailable_not_fake_next_run(self):
        result=pending_maintenance(sample_data(),now=NOW,cron={'available':False})
        jobs={t['id']:t for t in result['items']}
        self.assertEqual(jobs['sync']['schedule']['status'],'unknown')
        self.assertNotIn('next_iso',jobs['sync']['schedule'])

    def test_cron_configured_without_matching_sync_job(self):
        result=pending_maintenance(sample_data(),now=NOW,cron={'available':True,'jobs':[]})
        jobs={t['id']:t for t in result['items']}
        self.assertEqual(jobs['sync']['schedule']['status'],'none')
        self.assertEqual(jobs['scrub']['schedule']['next_display'],'Script/timer schedule not verified')

    def test_disk_fault_blocks_sync_and_scrub(self):
        d=sample_data()
        d['disk_inventory']['missing']=1
        result=pending_maintenance(d,now=NOW,cron=cron())
        jobs={t['id']:t for t in result['items']}
        self.assertEqual(jobs['disks']['status'],'blocked')
        self.assertEqual(jobs['sync']['status'],'waiting')
        self.assertEqual(jobs['scrub']['status'],'waiting')

    def test_monitoring_only_no_maintenance_controls(self):
        from app import app, empty_dashboard
        from unittest.mock import patch as p
        data = empty_dashboard()
        data['maintenance_tasks']=pending_maintenance(sample_data(),now=NOW,cron=cron())
        client = app.test_client()
        with client.session_transaction() as session:
            session['auth']=True
        with p('app.cache',{'data':data,'updated':'test'}):
            result=client.get('/')
        text=result.get_data(as_text=True)
        self.assertIn('Pending Maintenance Tasks',text)
        self.assertIn('Next cron run',text)
        self.assertIn('2026',text)
        self.assertNotIn('id="actionRun"',text)
        self.assertNotIn('onclick="runMaintenance(',text)


if __name__ == '__main__':
    unittest.main()



class WrapperScrubInferenceTests(unittest.TestCase):
    def schedule(self):
        with tempfile.TemporaryDirectory() as directory:
            cron_d = Path(directory) / 'cron.d'
            cron_d.mkdir()
            (cron_d / 'snapraid-maintenance').write_text(
                'CRON_TZ=America/Chicago\n'
                '0 2 * * * root flock -n /run/snapraid.lock /usr/local/sbin/snapraid-maintenance.sh sync\n')
            with (patch('cron_schedule.ROOT_CRONTAB', Path(directory) / 'none'),
                  patch('cron_schedule.SYSTEM_CRONTAB', Path(directory) / 'none2'),
                  patch('cron_schedule.SYSTEM_CRON_D', cron_d)):
                return host_cron_schedules(now=NOW)

    def test_sunday_scrub_inside_wrapper_is_inferred(self):
        sched = self.schedule()
        self.assertTrue(sched['jobs'][0]['wrapper'])
        self.assertNotIn('scrub', sched['next_by_operation'])
        scrubs = [datetime(2026, 10, 4, 2, 9, tzinfo=TZ).timestamp(),
                  datetime(2026, 10, 11, 2, 13, tzinfo=TZ).timestamp()]
        result = infer_wrapper_scrub(sched, scrubs, now=NOW)
        scrub = result['next_by_operation']['scrub']
        self.assertTrue(scrub['inferred'])
        self.assertEqual(scrub['next_display'], 'Sun 18 Oct 2026, 02:00 AM CDT')
        self.assertIn('inside sync wrapper', scrub['source'])
        tasks = pending_maintenance(sample_data(), now=NOW, cron=result)
        job = {t['id']: t for t in tasks['items']}['scrub']
        self.assertEqual(job['schedule']['status'], 'configured')
        self.assertEqual(job['schedule']['label'], 'Next run (from run history)')

    def test_single_or_mixed_weekday_scrubs_are_not_inferred(self):
        sched = self.schedule()
        one = [datetime(2026, 10, 11, 2, 13, tzinfo=TZ).timestamp()]
        self.assertNotIn('scrub', infer_wrapper_scrub(sched, one, now=NOW)['next_by_operation'])
        mixed = one + [datetime(2026, 10, 7, 2, 10, tzinfo=TZ).timestamp()]
        self.assertNotIn('scrub', infer_wrapper_scrub(sched, mixed, now=NOW)['next_by_operation'])

    def test_plain_snapraid_cron_is_not_a_wrapper(self):
        sched = {'jobs': [{'operation': 'sync', 'wrapper': False, 'expression': '0 2 * * *',
                           'timezone': 'America/Chicago', 'source': 'root crontab'}],
                 'next_by_operation': {}}
        scrubs = [datetime(2026, 10, 4, 2, 9, tzinfo=TZ).timestamp(),
                  datetime(2026, 10, 11, 2, 13, tzinfo=TZ).timestamp()]
        self.assertNotIn('scrub', infer_wrapper_scrub(sched, scrubs, now=NOW)['next_by_operation'])
