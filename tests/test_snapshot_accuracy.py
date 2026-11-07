"""Reproduce stale warnings and missed successful cron syncs from the VOD host."""
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, enumerate as threads
from unittest.mock import patch

import app as monitor
import host_sync_journal as journal
from health_status import assess_health
from maintenance_tasks import pending_maintenance

NOW = 1794024000


def stale_data():
    return {'snapraid': {
        'snapshot_time': datetime.fromtimestamp(NOW - 86400, timezone.utc).isoformat(),
        'status_ok': True, 'diff_ok': True, 'pending_total': 15,
        'health': {'errors': 0, 'sync_in_progress': False, 'unscrubbed_pct': 96},
        'last_sync': {'epoch': NOW - 3600, 'stale': False, 'age': '1h ago'},
    }}


class SnapshotAccuracyTests(unittest.TestCase):
    def test_old_pending_changes_are_unknown_not_sync_needed(self):
        data = stale_data()
        self.assertEqual(assess_health(data, now=NOW)['cards']['snapraid']['level'], 'unknown')
        tasks = {t['id']: t for t in pending_maintenance(data, now=NOW)['items']}
        self.assertEqual(tasks['sync']['status'], 'unknown')
        self.assertEqual(tasks['scrub']['status'], 'unknown')
        self.assertEqual(tasks['snapshot']['label'], 'Refresh needed')

    def test_old_interrupted_snapshot_is_not_a_current_failure(self):
        data = stale_data()
        data['snapraid']['health'].update(errors=2, sync_in_progress=True)
        self.assertEqual(assess_health(data, now=NOW)['cards']['snapraid']['level'], 'unknown')
        tasks = {t['id']: t for t in pending_maintenance(data, now=NOW)['items']}
        self.assertEqual(tasks['sync']['status'], 'unknown')

    def test_recent_snapshot_before_newer_sync_is_historical(self):
        data = stale_data()
        data['snapraid']['snapshot_time'] = datetime.fromtimestamp(NOW-600, timezone.utc).isoformat()
        data['snapraid']['last_sync']['epoch'] = NOW-300
        self.assertEqual(assess_health(data, now=NOW)['cards']['snapraid']['level'], 'unknown')

    def test_current_pending_changes_do_not_say_sync_failed(self):
        data = stale_data()
        data['snapraid']['snapshot_time'] = datetime.fromtimestamp(NOW-60, timezone.utc).isoformat()
        state = assess_health(data, now=NOW)['cards']['snapraid']
        self.assertEqual(state['level'], 'warn')
        self.assertIn('not a failed sync', state['detail'])

    def test_missing_direct_cron_does_not_claim_no_schedule(self):
        tasks = pending_maintenance(stale_data(), now=NOW, cron={'available': True})
        scrub = next(t for t in tasks['items'] if t['id'] == 'scrub')
        self.assertNotEqual(scrub['schedule']['next_display'], 'Not scheduled')

    def test_rendered_page_labels_old_snapshot_without_current_failure(self):
        data = monitor.empty_dashboard()
        data['snapraid'].update(stale_data()['snapraid'])
        data['snapraid']['health'].update(errors=2, sync_in_progress=50)
        data['snapraid']['last_sync'].update(display='2026-11-06 02:09', source='completed maintenance log')
        data['health_overview'] = assess_health(data, now=NOW)
        data['maintenance_tasks'] = pending_maintenance(data, now=NOW)
        client = monitor.app.test_client()
        with client.session_transaction() as session:
            session['auth'] = True
        with patch.dict(monitor.cache, data=data, updated='test'):
            page = client.get('/').get_data(as_text=True)
        self.assertIn('Current changes unknown', page)
        self.assertIn('Last successful sync', page)
        self.assertIn('Historical or missing snapshot', page)
        self.assertNotIn('Last sync did not finish', page)
        self.assertIn('id="opArray">Unknown — refresh snapshot', page)

    def test_unknown_history_is_neutral_not_a_failed_job(self):
        self.assertEqual(monitor.status_level('Finished'), 'unknown')


class MaintenanceLogTests(unittest.TestCase):
    def test_completed_cron_log_updates_last_success_without_rewriting_history(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory)/'sync-20261106-020001.log'
            log.write_text('=== SnapRAID sync ===\nStarting SnapRAID sync\nEverything OK\n'
                           '=== COMPLETED SUCCESSFULLY ===\nFri Nov  6 02:09:39 AM CST 2026\n')
            os.utime(log, (NOW-60, NOW-60))
            with (patch.object(monitor, 'SNAPRAID_LOG_PATH', str(log)),
                  patch.object(monitor, 'last_run_summary', return_value={'last_sync_ok': None}),
                  patch.object(monitor, 'last_successful_host_sync', return_value=None),
                  patch.object(monitor.time, 'time', return_value=NOW)):
                result = monitor.snapraid_last_sync()
            self.assertIsNotNone(result)
            self.assertEqual(result['epoch'], NOW-60)
            self.assertEqual(result['source'], 'completed maintenance log')

    def test_incomplete_or_scrub_log_is_not_a_successful_sync(self):
        for text in ('=== SnapRAID sync ===\nEverything OK\nSaving state...\n',
                     '=== SnapRAID sync ===\nStarting SnapRAID sync\n=== COMPLETED SUCCESSFULLY ===\nFri Nov  6 02:09:39 AM CST 2026\nERROR: retry failed\n',
                     '=== SnapRAID scrub ===\n=== COMPLETED SUCCESSFULLY ===\nFri Nov  6 02:09:39 AM CST 2026\n'):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as directory:
                log = Path(directory)/'sync-test.log'
                log.write_text(text)
                with (patch.object(monitor, 'SNAPRAID_LOG_PATH', str(log)),
                      patch.object(monitor, 'last_run_summary', return_value={'last_sync_ok': None}),
                      patch.object(monitor, 'last_successful_host_sync', return_value=None)):
                    self.assertIsNone(monitor.snapraid_last_sync())


class StartupJournalTests(unittest.TestCase):
    def test_dashboard_lookup_returns_while_journal_query_is_blocked(self):
        started, release = Event(), Event()
        def slow_query():
            started.set()
            release.wait(2)
            return NOW-60
        journal._sync_cache.update(expires=0, value=None, refreshing=False)
        try:
            with patch.object(journal, '_scan_successful_host_sync', side_effect=slow_query):
                result = journal.last_successful_host_sync(background=True)
                self.assertIsNone(result)
                self.assertTrue(started.wait(1))
                self.assertIsNone(journal.last_successful_host_sync(background=True))
        finally:
            release.set()
            for thread in threads():
                if thread.name == 'host-sync-journal':
                    thread.join(2)

