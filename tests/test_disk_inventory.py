"""Missing HDD regression tests, using synthetic host mounts and lsblk data."""
import json
import tempfile
import unittest
from pathlib import Path

from disk_inventory import check_inventory, expected_from_config


def block_data(data_name='sda', parity_name='sdb', data_serial='SERIAL-DATA',
               parity_serial='SERIAL-PARITY', parity_major='8:17'):
    return json.dumps({'blockdevices': [
        {'name':data_name,'type':'disk','maj:min':'8:0','serial':data_serial,
         'children':[{'name':data_name+'1','type':'part','maj:min':'8:1'}]},
        {'name':parity_name,'type':'disk','maj:min':'8:16','serial':parity_serial,
         'children':[{'name':parity_name+'1','type':'part','maj:min':parity_major}]},
    ]})


class MissingDiskTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.config = self.root / 'snapraid.conf'
        self.config.write_text('data d01 /mnt/disk01/\nparity /mnt/parity1/snapraid.parity\n')
        self.mounts = self.root / 'host-mountinfo'
        self.baseline = self.root / 'disk-baseline.json'
        self.online_mounts()

    def online_mounts(self, parity_major='8:17'):
        self.mounts.write_text(
            '31 21 0:20 / /mnt rw - tmpfs tmpfs rw\n'
            '33 31 8:1 / /mnt/disk01 rw - ext4 /dev/sda1 rw\n'
            f'34 31 {parity_major} / /mnt/parity1 rw - ext4 /dev/sdb1 rw\n'
        )

    def inspect(self, lsblk_result=None):
        return check_inventory(
            str(self.config),
            lambda cmd,timeout=20: (0, lsblk_result or block_data()),
            str(self.mounts), str(self.baseline)
        )

    def test_baseline_only_captured_after_complete_healthy_set(self):
        result = self.inspect()
        self.assertEqual(result['expected'], 2)
        self.assertEqual(result['online'], 2)
        self.assertEqual(result['missing'], 0)
        self.assertEqual(result['health'], 'ok')
        self.assertTrue(self.baseline.exists())
        self.assertEqual(len(json.loads(self.baseline.read_text())['slots']), 2)

    def test_missing_parity_mount_reports_real_alert_even_without_baseline(self):
        self.mounts.write_text('31 21 0:20 / /mnt rw - tmpfs tmpfs rw\n'
                               '33 31 8:1 / /mnt/disk01 rw - ext4 /dev/sda1 rw\n')
        result = self.inspect()
        self.assertEqual(result['missing'], 1)
        self.assertEqual(result['entries'][1]['status'], 'missing_mount')
        self.assertIn('/mnt/parity1', result['alerts'][0]['detail'])
        self.assertFalse(self.baseline.exists(), 'never baseline an already incomplete array')

    def test_missing_drive_does_not_disappear_from_expected_table(self):
        self.inspect()
        self.mounts.write_text('31 21 0:20 / /mnt rw - tmpfs tmpfs rw\n'
                               '34 31 8:17 / /mnt/parity1 rw - ext4 /dev/sdb1 rw\n')
        result = self.inspect()
        self.assertEqual(result['expected'], 2)
        self.assertEqual(result['online'], 1)
        self.assertEqual(result['missing'], 1)
        self.assertEqual(result['entries'][0]['expected_serial'], 'SERIAL-DATA')
        self.assertEqual(result['entries'][0]['status'], 'missing_mount')
        self.assertEqual(result['health'], 'critical')

    def test_mounted_path_not_in_lsblk_is_reported(self):
        self.inspect()
        self.online_mounts(parity_major='8:99')
        result = self.inspect()
        self.assertEqual(result['entries'][1]['status'], 'device_missing')
        self.assertEqual(result['missing'], 1)

    def test_device_name_changes_do_not_raise_false_alarm(self):
        self.inspect()
        self.online_mounts(parity_major='8:33')
        renamed = block_data(parity_name='sdc', parity_major='8:33')
        result = self.inspect(renamed)
        self.assertEqual(result['missing'], 0)
        self.assertEqual(result['warnings'], 0)
        self.assertEqual(result['entries'][1]['device'], 'sdc')

    def test_serial_swap_is_visible_and_never_adopted_silently(self):
        self.inspect()
        result = self.inspect(block_data(parity_serial='NEW-PARITY'))
        self.assertEqual(result['entries'][1]['status'], 'identity_changed')
        self.assertTrue(any('serial differs' in a['detail'] for a in result['alerts']))
        self.assertEqual(list(json.loads(self.baseline.read_text())['slots'].values())[-1],
                         'SERIAL-PARITY')

    def test_removed_config_entry_stays_visible_from_baseline(self):
        self.inspect()
        self.config.write_text('data d01 /mnt/disk01/\n')
        result = self.inspect()
        self.assertEqual(result['missing'], 1)
        self.assertTrue(any('removed from configuration' in a['label']
                            for a in result['alerts']))

    def test_mountinfo_unavailable_never_reports_all_disks_missing(self):
        result = check_inventory(str(self.config), lambda cmd,timeout=20: (0,block_data()),
                                 str(self.root/'no-such-mountinfo'), str(self.baseline))
        self.assertEqual(result['missing'], 0)
        self.assertEqual(result['health'], 'unknown')
        self.assertTrue(result['alerts'])
        self.assertFalse(self.baseline.exists())

    def test_snapraid_not_executed(self):
        calls = []
        def runner(cmd,timeout=20):
            calls.append(cmd)
            return 0,block_data()
        check_inventory(str(self.config), runner, str(self.mounts), str(self.baseline))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 'lsblk')

    def test_config_parses_multiple_parity_levels_and_data(self):
        self.config.write_text('# header\n'
                               'data disk30 /mnt/disk30/\n'
                               'parity /mnt/parity1/snapraid.parity\n'
                               '2-parity /mnt/parity2/snapraid.2-parity\n'
                               '3-parity /mnt/parity3/snapraid.3-parity\n'
                               '4-parity /mnt/parity4/snapraid.4-parity\n')
        configured = expected_from_config(str(self.config))
        self.assertEqual(len(configured), 5)
        self.assertEqual(configured[-1]['path'], '/mnt/parity4')
        self.assertEqual(configured[0]['id'], 'disk30')


if __name__ == '__main__':
    unittest.main()
