"""Read-only, confidence-aware health status UI regression tests."""
import copy
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

os.environ.setdefault('DB_PATH', os.path.join(tempfile.gettempdir(), 'vod-health-ui-tests.sqlite3'))
import app as monitor
from health_status import assess_health

NOW = datetime(2026, 10, 16, 0, 45, tzinfo=timezone.utc).timestamp()


def clean_data():
    # A recent host-captured status/diff snapshot is necessary for green
    # SnapRAID. The most recent scrub need not have covered the entire array.
    return {
        'storage': {
            'vod': {'size': 500, 'pct': 24},
            'docker': {'size': 7, 'pct': 25}
        },
        'raid': [{'name':'md3','healthy':True,'progress':None}],
        'disk_health': {'total': 37,'healthy': 37,'failed': 0,'warning': 0,
                        'other': 0,'standby': 0},
        'disk_inventory': {'expected':33,'online':33,'missing':0,'warnings':0,
                           'alerts':[],'health':'ok'},
        'snapraid': {
            'maintenance_running': False, 'status_ok': True, 'diff_ok':True,
            'snapshot_time': datetime.fromtimestamp(NOW-120,timezone.utc).isoformat(),
            'pending_total': 0, 'health': {'errors':0,'sync_in_progress':False,
                                            'unscrubbed_pct':90},
            'last_sync': {'epoch':NOW-300,'stale':False,'age':'5m ago'},
            'scrub_stale':False
        }
    }


class HealthSummaryTests(unittest.TestCase):
    def test_fully_verified_system_is_green(self):
        overview = assess_health(clean_data(), now=NOW)
        self.assertEqual(overview['level'], 'ok')
        self.assertEqual(overview['label'], 'All monitored checks healthy')
        self.assertEqual([v['level'] for v in overview['cards'].values()], ['ok']*4)

    def test_no_snapshot_does_not_claim_green_on_old_successful_log(self):
        data=clean_data()
        data['snapraid'].update(status_ok=None,diff_ok=None,snapshot_time=None,pending_total=None)
        data['snapraid']['last_log_result']='OK'
        data['snapraid']['protection']='Last log OK'
        status=assess_health(data,now=NOW)
        self.assertEqual(status['level'],'unknown')
        self.assertEqual(status['cards']['snapraid']['label'],'Verification needed')

    def test_stale_snapshot_does_not_claim_green(self):
        data=clean_data()
        data['snapraid']['snapshot_time']=datetime.fromtimestamp(NOW-7200,timezone.utc).isoformat()
        self.assertEqual(assess_health(data,now=NOW)['cards']['snapraid']['level'],'unknown')

    def test_snapshot_from_before_sync_cannot_be_green(self):
        data=clean_data()
        data['snapraid']['snapshot_time']=datetime.fromtimestamp(NOW-600,timezone.utc).isoformat()
        self.assertEqual(assess_health(data,now=NOW)['cards']['snapraid']['level'],'unknown')

    def test_sync_success_but_no_host_timestamp_is_unknown(self):
        data=clean_data()
        data['snapraid']['last_sync']=None
        self.assertEqual(assess_health(data,now=NOW)['cards']['snapraid']['level'],'unknown')

    def test_live_sync_is_busy_not_good(self):
        data=clean_data()
        data['snapraid'].update(maintenance_running=True,job={'name':'sync'})
        self.assertEqual(assess_health(data,now=NOW)['cards']['snapraid']['level'],'busy')

    def test_pending_changes_are_amber(self):
        data=clean_data()
        data['snapraid']['pending_total']=5
        self.assertEqual(assess_health(data,now=NOW)['cards']['snapraid']['level'],'warn')

    def test_critical_snapraid_interrupted_sync_is_red(self):
        data=clean_data()
        data['snapraid']['health']['sync_in_progress']=99
        self.assertEqual(assess_health(data,now=NOW)['cards']['snapraid']['level'],'bad')

    def test_unverified_disk_mounts_are_not_green(self):
        data=clean_data()
        data['disk_inventory'].update(missing=1,online=32,health='critical')
        self.assertEqual(assess_health(data,now=NOW)['level'],'bad')

    def test_smart_failure_is_red(self):
        data=clean_data()
        data['disk_health'].update(failed=1,healthy=36)
        self.assertEqual(assess_health(data,now=NOW)['cards']['disks']['level'],'bad')

    def test_disk_warning_is_amber(self):
        data=clean_data()
        data['disk_health'].update(warning=1,healthy=36)
        self.assertEqual(assess_health(data,now=NOW)['cards']['disks']['level'],'warn')

    def test_disk_unknown_is_not_reported_healthy(self):
        data=clean_data()
        data['disk_health'].update(other=1,healthy=36)
        self.assertEqual(assess_health(data,now=NOW)['cards']['disks']['level'],'unknown')

    def test_md_degraded_is_red_even_when_snapraid_is_green(self):
        data=clean_data()
        data['raid'][0]['healthy']=False
        self.assertEqual(assess_health(data,now=NOW)['cards']['raid']['level'],'bad')
        self.assertEqual(assess_health(data,now=NOW)['level'],'bad')

    def test_full_disk_is_warning_or_critical(self):
        data=clean_data()
        data['storage']['vod']['pct']=92
        self.assertEqual(assess_health(data,now=NOW)['cards']['vod']['level'],'warn')
        data['storage']['vod']['pct']=96
        self.assertEqual(assess_health(data,now=NOW)['cards']['vod']['level'],'bad')

    def test_scrub_missing_is_amber(self):
        data=clean_data()
        data['snapraid']['health']['unscrubbed_pct']=100
        self.assertEqual(assess_health(data,now=NOW)['cards']['snapraid']['level'],'warn')

    def test_stale_sync_needs_review(self):
        data=clean_data()
        data['snapraid']['last_sync']['stale']=True
        self.assertEqual(assess_health(data,now=NOW)['cards']['snapraid']['level'],'warn')

    def test_missing_initial_data_not_green(self):
        initial=monitor.empty_dashboard()
        overview=assess_health(initial,now=NOW)
        self.assertNotEqual(overview['level'],'ok')
        self.assertNotEqual(overview['cards']['snapraid']['level'],'ok')

    def test_html_server_render_contains_status_labels_and_no_actions(self):
        data=monitor.empty_dashboard()
        data['health_overview']=assess_health(clean_data(),now=NOW)
        client=monitor.app.test_client()
        with client.session_transaction() as sess:
            sess['auth']=True
        with patch.dict(monitor.cache,{'data':data,'updated':'2026-10-15 19:40'}):
            r=client.get('/')
        page=r.get_data(as_text=True)
        self.assertEqual(r.status_code,200)
        self.assertIn('id="healthSummary"',page)
        self.assertIn('state-ok',page)
        self.assertIn('All monitored checks healthy',page)
        self.assertIn('id="snapProtectionPill"',page)
        self.assertNotIn('actionRun(',page)


if __name__=='__main__':
    unittest.main()
