"""Regression tests for reporting the last *verified* SnapRAID sync."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('DB_PATH', os.path.join(tempfile.gettempdir(), 'vod-monitor-sync-tests.sqlite3'))
import app as monitor
import host_sync_journal as journal


def line(message, timestamp, pid='1'):
    return json.dumps({
        'MESSAGE': message, '_PID': pid,
        '__REALTIME_TIMESTAMP': str(round(timestamp * 1_000_000)),
    })


class JournalSyncTests(unittest.TestCase):
    def test_successful_sync_service_timestamp(self):
        past = 1792110901.0
        records = '\n'.join([
            line('snapraid-sync-bg.service: Failed with result \'exit-code\'.', past - 100),
            line('snapraid-sync-bg.service: Deactivated successfully.', past),
            line('snapraid-sync-resume-20261016.service: Deactivated successfully.', past + 500),
            line('snapraid-scrub.service: Deactivated successfully.', past + 1000),
            line('snapraid-sync-resume-20261016.service: Deactivated successfully.', past + 1200, pid='2222'),
        ])
        self.assertEqual(journal.parse_sync_completions(records, now=past + 5000), past + 500)

    def test_real_world_short_unix_systemd_success(self):
        # Corresponds to the observed Oct 15 19:35:01 CDT host service.
        epoch = datetime(2026, 10, 16, 0, 35, 1, tzinfo=timezone.utc).timestamp()
        lines = '\n'.join([
            f'{epoch - 60:.6f} den-sm5049p-1 systemd[1]: snapraid-sync-bg.service: Failed with result \'exit-code\'.',
            f'{epoch:.6f} den-sm5049p-1 systemd[1]: snapraid-sync-resume-20261016.service: Deactivated successfully.',
            f'{epoch + 10:.6f} den-sm5049p-1 snapraid[2238005]: Everything OK',
            f'{epoch + 20:.6f} den-sm5049p-1 systemd[1]: snapraid-scrub.service: Deactivated successfully.',
            f'{epoch + 30:.6f} den-sm5049p-1 systemd[123]: snapraid-sync-fake.service: Deactivated successfully.',
        ])
        self.assertEqual(journal.parse_sync_completions(lines, now=epoch + 60), epoch)

    def test_short_unix_record_from_journal_command(self):
        epoch = datetime(2026, 10, 16, 0, 35, 1, tzinfo=timezone.utc).timestamp()
        output = f'{epoch:.6f} den-sm5049p-1 systemd[1]: snapraid-sync-resume-20261016.service: Deactivated successfully.\\n'
        # Simulate a host journal that returned only the persistent directory.
        def fake_run(cmd, **kwargs):
            if cmd[1].endswith('/host/var/log/journal'):
                return SimpleNamespace(returncode=0, stdout=output)
            return SimpleNamespace(returncode=0, stdout='')
        with patch.object(journal.shutil, 'which', return_value='/usr/bin/journalctl'), \
             patch.object(journal.Path, 'is_dir', return_value=True), \
             patch.object(journal.subprocess, 'run', side_effect=fake_run) as run, \
             patch.object(journal.time, 'time', return_value=epoch + 60):
            self.assertEqual(journal.last_successful_host_sync(), epoch)
        for item in run.call_args_list:
            self.assertIn('--output=short-unix', item.args[0])
            self.assertIn('--grep=snapraid-', item.args[0])

    def test_failed_or_non_sync_jobs_do_not_count(self):
        messages = '\n'.join([
            line('snapraid-sync-retry.service: Main process exited, status=1/FAILURE', 100),
            line('snapraid-sync-retry.service: Failed with result \'exit-code\'.', 200),
            line('snapraid-manual-scrub.service: Deactivated successfully.', 300),
            'not-json',
        ])
        self.assertIsNone(journal.parse_sync_completions(messages, now=1000))

    def test_journal_reader_uses_read_only_queries(self):
        stamp = 1792110901.0
        record = line('snapraid-sync-resume-20261016.service: Deactivated successfully.', stamp)
        with patch.object(journal.shutil, 'which', return_value='/usr/bin/journalctl'), \
             patch.object(journal.Path, 'is_dir', return_value=True), \
             patch.object(journal.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=record)) as run, \
             patch.object(journal.time, 'time', return_value=stamp + 20):
            self.assertEqual(journal.last_successful_host_sync(), stamp)
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            cmd = call.args[0]
            self.assertEqual(cmd[0], 'journalctl')
            self.assertTrue(any(x.startswith('--directory=') for x in cmd))
            self.assertFalse(any(x == 'snapraid' for x in cmd))

    def test_new_journal_result_replaces_stale_history(self):
        earlier = datetime(2026, 10, 11, tzinfo=timezone.utc).timestamp()
        newer = datetime(2026, 10, 16, 0, 35, tzinfo=timezone.utc).timestamp()
        with patch.object(monitor, 'last_run_summary', return_value={
                 'last_sync_ok': {'ended_epoch': earlier}}), \
             patch.object(monitor, 'last_successful_host_sync', return_value=newer), \
             patch.object(monitor.time, 'time', return_value=newer + 3600):
            value = monitor.snapraid_last_sync()
        self.assertEqual(value['epoch'], newer)
        self.assertEqual(value['source'], 'host systemd journal')
        self.assertFalse(value['stale'])

    def test_content_state_mtime_does_not_claim_success(self):
        with patch.object(monitor, 'last_run_summary', return_value={'last_sync_ok': None}), \
             patch.object(monitor, 'last_successful_host_sync', return_value=None):
            self.assertIsNone(monitor.snapraid_last_sync())


if __name__ == '__main__':
    unittest.main()
