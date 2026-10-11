"""Monitoring-only API regression tests."""
import os
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ['DB_PATH'] = os.path.join(tempfile.gettempdir(), 'vod-monitor-readonly-tests.sqlite3')
os.environ['ENABLE_ACTIONS'] = 'true'  # Legacy env variable MUST have no effect.
import app as monitor


class ReadOnlyTests(unittest.TestCase):
    def setUp(self):
        self.client = monitor.app.test_client()
        with self.client.session_transaction() as session:
            session['auth'] = True

    def test_no_maintenance_or_schedule_routes_exist(self):
        routes = {rule.rule for rule in monitor.app.url_map.iter_rules()}
        self.assertFalse(any(path.startswith('/api/action') for path in routes))
        self.assertNotIn('/api/schedule', routes)
        self.assertNotIn('/api/refresh', routes)
        for path in ('/api/action/sync', '/api/action/scrub10',
                     '/api/action/cancel', '/api/schedule', '/api/refresh'):
            self.assertEqual(self.client.post(path).status_code, 404)
        for rule in monitor.app.url_map.iter_rules():
            if rule.rule != '/login':
                self.assertFalse({'POST', 'PUT', 'PATCH', 'DELETE'} & rule.methods)

    def test_log_viewer_reads_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'sync.log')
            with open(path, 'w', encoding='utf-8') as stream:
                stream.write('summary:error_io:0\nsummary:exit:ok\n')
            with patch.object(monitor, 'SNAPRAID_LOG_PATH', path), \
                 patch.object(monitor, 'snapraid_processes', return_value=[{'pid': 123, 'name': 'sync'}]):
                result = self.client.get('/api/observation')
                self.assertEqual(result.status_code, 200)
                payload = result.get_json()
                self.assertTrue(payload['running'])
                self.assertEqual(payload['name'], 'sync')
                self.assertEqual(payload['log']['result'], 'OK')
                self.assertNotIn('text', payload['log'])
            with open(path, 'r', encoding='utf-8') as stream:
                self.assertEqual(stream.read(), 'summary:error_io:0\nsummary:exit:ok\n')

    def test_dashboard_renders_with_no_maintenance_controls(self):
        with patch.dict(monitor.cache, {'updated': 'test', 'data': monitor.empty_dashboard()}):
            response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('Monitoring only', html)
        self.assertIn('id="opLast"', html)
        self.assertIn('SnapRAID Run History', html)
        self.assertNotIn('id="jobOutput"', html)
        self.assertNotIn('id="syncEnabled"', html)
        self.assertNotIn('actionRun(', html)
        self.assertNotIn('saveSchedule(', html)

    def test_no_status_diff_launch_while_host_job_running(self):
        observation = {
            'running': True, 'name': 'sync', 'progress': None,
            'processes': [{'pid': 123, 'name': 'sync'}],
            'started': None, 'elapsed': None, 'last_run': {},
            'log': {'path': '/tmp/test.log', 'updated': None, 'mtime': None, 'error': None,
                    'result': None, 'detail': None}
        }
        with patch.object(monitor, 'snapraid_observation', return_value=observation), \
             patch.object(monitor, 'run', side_effect=AssertionError('must not spawn snapraid')), \
             patch.object(monitor, 'snapraid_last_sync', return_value=None), \
             patch.object(monitor, 'snapraid_config_summary', return_value={}):
            result = monitor.snapraid_summary()
        self.assertEqual(result['protection'], 'Syncing')
        self.assertTrue(result['maintenance_running'])
        self.assertIsNone(result['status_ok'])
        self.assertIsNone(result['diff_ok'])


    def test_idle_dashboard_never_executes_snapraid(self):
        observation = {
            'running': False, 'name': None, 'progress': None,
            'processes': [],
            'log': {'path': '/tmp/sync.log', 'updated': None,
                    'text': 'summary:error_io:0\nsummary:exit:ok\n', 'error': None}
        }
        with patch.object(monitor, 'snapraid_observation', return_value=observation), \
             patch.object(monitor, 'run', side_effect=AssertionError('no commands allowed')), \
             patch.object(monitor, 'snapraid_last_sync', return_value=None), \
             patch.object(monitor, 'snapraid_config_summary', return_value={'data_disks': 30, 'parity_levels': 4}), \
             patch.object(monitor, 'SNAPRAID_SNAPSHOT_PATH', '/tmp/no-such-snapraid-snapshot-98765.json'):
            result = monitor.snapraid_summary()
        self.assertEqual(result['protection'], 'Last log OK')
        self.assertIsNone(result['pending_total'])
        self.assertIsNone(result['status_ok'])
        self.assertIsNone(result['diff_ok'])
        self.assertIsNone(result['scrub_coverage'])
        self.assertTrue(any('five parity levels' in w for w in result['warnings']))

    def test_host_snapshot_is_historical_and_scrub_coverage_parses(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'snapraid-snapshot.json')
            with open(path, 'w', encoding='utf-8') as stream:
                json.dump({
                    'schema': 1, 'captured_at': '2026-10-11T12:00:00+00:00',
                    'status_rc': 0, 'diff_rc': 0,
                    'status': 'No error detected.\n96% of the array is not scrubbed.\n',
                    'diff': ' 300 equal\n 0 added\n 0 removed\n 0 updated\n'
                            ' 0 moved\n 0 copied\n 0 restored\n',
                }, stream)
            observation = {
                'running': False, 'name': None, 'progress': None, 'processes': [],
                'log': {'path': '', 'updated': None, 'text': '', 'error': None}
            }
            with patch.object(monitor, 'SNAPRAID_SNAPSHOT_PATH', path), \
                 patch.object(monitor, 'snapraid_observation', return_value=observation), \
                 patch.object(monitor, 'snapraid_last_sync', return_value=None), \
                 patch.object(monitor, 'snapraid_config_summary', return_value={'data_disks': 0, 'parity_levels': 0}), \
                 patch.object(monitor, 'run', side_effect=AssertionError('must never run snapraid')):
                result = monitor.snapraid_summary()
            self.assertEqual(result['scrub_coverage'], 4)
            self.assertEqual(result['pending_total'], 0)
            self.assertTrue(result['status_ok'])
            self.assertEqual(result['snapshot_time'], '2026-10-11T12:00:00+00:00')
            self.assertNotEqual(result['protection'], 'Protected')


class ParsingTests(unittest.TestCase):
    def test_status_health_facts(self):
        text = ("The oldest block was scrubbed 37 days ago, the median 12, the newest 2.\n"
                "The 8% of the array is not scrubbed.\n"
                "You have a sync in progress at 41%.\n"
                "DANGER! In the array there are 3 errors!\n")
        info = monitor.parse_status(text)
        self.assertEqual((info['scrub_oldest_days'], info['scrub_median_days'], info['scrub_newest_days']), (37, 12, 2))
        self.assertEqual(info['unscrubbed_pct'], 8)
        self.assertEqual(info['sync_in_progress'], 41)
        self.assertEqual(info['errors'], 3)
        clean = monitor.parse_status('No sync is in progress.\nNo error detected.\n')
        self.assertIs(clean['sync_in_progress'], False)
        self.assertEqual(clean['errors'], 0)

    def test_log_result(self):
        self.assertEqual(monitor.log_result('summary:exit:ok\n')['result'], 'OK')
        failed = monitor.log_result('summary:error_io:2\nsummary:exit:error\n')
        self.assertEqual(failed['result'], 'Error')
        self.assertEqual(failed['detail'], 'io errors: 2')
        self.assertEqual(monitor.log_result('Saving state...\nEverything OK\n')['result'], 'OK')
        self.assertIsNone(monitor.log_result('100%, 12 MB\n')['result'])

    def test_mdstat_raid0_is_healthy(self):
        mdstat = ("Personalities : [raid0] [raid1]\n"
                  "md0 : active raid0 nvme1n1[1] nvme0n1[0]\n      1000 blocks super 1.2 512k chunks\n\n"
                  "md1 : active raid1 sdb1[1] sda1[0]\n      1000 blocks [2/1] [U_]\n")
        with patch.object(monitor.Path, 'exists', return_value=True), \
             patch.object(monitor.Path, 'read_text', return_value=mdstat):
            arrays = {a['name']: a['healthy'] for a in monitor.parse_mdstat()}
        self.assertEqual(arrays, {'md0': True, 'md1': False})


class JobHistoryTests(unittest.TestCase):
    def test_finished_host_job_is_recorded_with_log_result(self):
        c = monitor.db(); c.execute('DELETE FROM events'); c.execute("DELETE FROM settings WHERE key='active_jobs'"); c.commit(); c.close()
        started = time.time() - 3600
        running = [{'pid': 42, 'name': 'sync', 'started': started}]
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'sync.log')
            with open(path, 'w', encoding='utf-8') as stream:
                stream.write('summary:exit:ok\n')  # previous run, must be ignored
            with patch.object(monitor, 'SNAPRAID_LOG_PATH', path):
                with patch.object(monitor, 'snapraid_processes', return_value=running):
                    monitor.watch_jobs_once()
                with open(path, 'a', encoding='utf-8') as stream:
                    stream.write('summary:error_data:1\nsummary:exit:error\n')
                with patch.object(monitor, 'snapraid_processes', return_value=[]):
                    monitor.watch_jobs_once()
        summary = monitor.last_run_summary()
        self.assertEqual(summary['last']['kind'], 'sync')
        self.assertEqual(summary['last']['status'], 'Error')
        self.assertEqual(summary['last_failed']['message'], 'data errors: 1')
        self.assertIsNone(summary['last_sync_ok'])
        self.assertEqual(summary['last']['duration'], '1h 0m')

    def reset_history(self):
        c = monitor.db(); c.execute('DELETE FROM events'); c.execute("DELETE FROM settings WHERE key='active_jobs'"); c.commit(); c.close()

    def test_old_summary_in_append_log_is_not_credited_to_interrupted_run(self):
        self.reset_history()
        running = [{'pid': 7, 'name': 'sync', 'started': time.time() - 60}]
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'sync.log')
            with open(path, 'w', encoding='utf-8') as stream:
                stream.write('summary:exit:ok\n')  # previous run
            with patch.object(monitor, 'SNAPRAID_LOG_PATH', path):
                with patch.object(monitor, 'snapraid_processes', return_value=running):
                    monitor.watch_jobs_once()
                with open(path, 'a', encoding='utf-8') as stream:
                    stream.write('45%, 1200 MB\n')  # killed before its summary
                with patch.object(monitor, 'snapraid_processes', return_value=[]):
                    monitor.watch_jobs_once()
        summary = monitor.last_run_summary()
        self.assertEqual(summary['last']['status'], 'Finished')
        self.assertIsNone(summary['last_sync_ok'])

    def test_recovered_fix_is_not_a_failure(self):
        result = monitor.log_result('summary:error_data:3\nsummary:exit:recovered\n')
        self.assertEqual(result['result'], 'OK')
        self.assertEqual(result['detail'], 'recovered (data errors: 3)')
        self.assertEqual(monitor.log_result('summary:exit:unrecoverable\n')['result'], 'Error')

    def test_legacy_started_markers_and_old_results_are_handled(self):
        self.reset_history()
        c = monitor.db()
        rows = [('2026-01-01T00:00:00+00:00', 'sync', 'success', 'done', 60),
                ('2026-01-01T00:00:00+00:00', 'sync', 'started', 'sync started from dashboard', 0),
                ('2026-01-02T00:00:00+00:00', 'scrub', 'failed', 'io error', 60)]
        rows += [(f'2026-02-{d:02d}T00:00:00+00:00', 'scrub', 'OK', 'fine', 60) for d in range(1, 28)] * 3
        c.executemany('INSERT INTO events(ts,kind,status,message,duration) VALUES(?,?,?,?,?)', rows)
        c.commit(); c.close()
        summary = monitor.last_run_summary()
        self.assertEqual(summary['last_failed']['message'], 'io error')
        self.assertEqual(summary['last_sync_ok']['status'], 'success')
        self.assertNotIn('started', [e['status'] for e in monitor.history(200)])

    def test_job_watcher_never_executes_snapraid(self):
        with patch.object(monitor, 'run', side_effect=AssertionError('must never run snapraid')), \
             patch.object(monitor.subprocess, 'run', side_effect=AssertionError('must never run snapraid')), \
             patch.object(monitor, 'snapraid_processes', return_value=[]):
            monitor.watch_jobs_once()

    def test_snapshot_errors_and_interrupted_sync_are_reported(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'snapshot.json')
            with open(path, 'w', encoding='utf-8') as stream:
                json.dump({'schema': 1, 'captured_at': '2026-10-11T12:00:00+00:00',
                           'status_rc': 0, 'diff_rc': 0, 'diff': '',
                           'status': 'The oldest block was scrubbed 40 days ago, the median 9, the newest 20.\n'
                                     'DANGER! In the array there are 2 errors!\n'}, stream)
            observation = {'running': False, 'name': None, 'progress': None, 'processes': [],
                           'log': {'path': '', 'updated': None, 'text': 'summary:exit:ok\n', 'error': None}}
            with patch.object(monitor, 'SNAPRAID_SNAPSHOT_PATH', path), \
                 patch.object(monitor, 'snapraid_observation', return_value=observation), \
                 patch.object(monitor, 'snapraid_last_sync', return_value=None), \
                 patch.object(monitor, 'snapraid_config_summary', return_value={}):
                result = monitor.snapraid_summary()
        self.assertEqual(result['protection'], 'Errors in snapshot')
        self.assertEqual(result['health']['errors'], 2)
        self.assertTrue(result['scrub_stale'])


if __name__ == '__main__':
    unittest.main()
