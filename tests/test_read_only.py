"""Monitoring-only API regression tests."""
import os
import tempfile
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
                stream.write('summary:error_io:0\nsummary:exit:sync\n')
            with patch.object(monitor, 'SNAPRAID_LOG_PATH', path), \
                 patch.object(monitor, 'snapraid_processes', return_value=[{'pid': 123, 'name': 'sync'}]):
                result = self.client.get('/api/observation')
                self.assertEqual(result.status_code, 200)
                payload = result.get_json()
                self.assertTrue(payload['running'])
                self.assertEqual(payload['name'], 'sync')
                self.assertIn('summary:exit:sync', payload['log']['text'])
            with open(path, 'r', encoding='utf-8') as stream:
                self.assertEqual(stream.read(), 'summary:error_io:0\nsummary:exit:sync\n')

    def test_dashboard_renders_with_no_maintenance_controls(self):
        with patch.dict(monitor.cache, {'updated': 'test', 'data': monitor.empty_dashboard()}):
            response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('Monitoring only', html)
        self.assertIn('id="jobOutput"', html)
        self.assertNotIn('id="syncEnabled"', html)
        self.assertNotIn('actionRun(', html)
        self.assertNotIn('saveSchedule(', html)

    def test_no_status_diff_launch_while_host_job_running(self):
        observation = {
            'running': True, 'name': 'sync', 'progress': None,
            'processes': [{'pid': 123, 'name': 'sync'}],
            'log': {'path': '/tmp/test.log', 'updated': None, 'text': '', 'error': None}
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
                    'text': 'summary:error_io:0\\nsummary:exit:ok\\n', 'error': None}
        }
        with patch.object(monitor, 'snapraid_observation', return_value=observation), \\
             patch.object(monitor, 'run', side_effect=AssertionError('no commands allowed')), \\
             patch.object(monitor, 'snapraid_last_sync', return_value=None), \\
             patch.object(monitor, 'snapraid_config_summary', return_value={'data_disks': 30, 'parity_levels': 4}), \\
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
                    'status': 'No error detected.\\n96% of the array is not scrubbed.\\n',
                    'diff': ' 300 equal\\n 0 added\\n 0 removed\\n 0 updated\\n'
                            ' 0 moved\\n 0 copied\\n 0 restored\\n',
                }, stream)
            observation = {
                'running': False, 'name': None, 'progress': None, 'processes': [],
                'log': {'path': '', 'updated': None, 'text': '', 'error': None}
            }
            with patch.object(monitor, 'SNAPRAID_SNAPSHOT_PATH', path), \\
                 patch.object(monitor, 'snapraid_observation', return_value=observation), \\
                 patch.object(monitor, 'snapraid_last_sync', return_value=None), \\
                 patch.object(monitor, 'snapraid_config_summary', return_value={'data_disks': 0, 'parity_levels': 0}), \\
                 patch.object(monitor, 'run', side_effect=AssertionError('must never run snapraid')):
                result = monitor.snapraid_summary()
            self.assertEqual(result['scrub_coverage'], 4)
            self.assertEqual(result['pending_total'], 0)
            self.assertTrue(result['status_ok'])
            self.assertEqual(result['snapshot_time'], '2026-10-11T12:00:00+00:00')
            self.assertNotEqual(result['protection'], 'Protected')


if __name__ == '__main__':
    unittest.main()
