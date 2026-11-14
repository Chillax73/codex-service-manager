"""List/menu summaries and on-demand details against the real SQLite schema."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import engine


class SnapshotSummaryTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        patched = patch.object(engine, 'ROOT', self.root)
        patched.start(); self.addCleanup(patched.stop)
        self.project = engine.project(self.root)
        engine.record_spec('one', self.project, 'One', 'task',
                           {'argv': ['echo', 'x' * 3000], 'cwd': str(self.root), 'env': {'TOKEN': 'fixture-private'}})
        engine.observe('one')
        engine.dispatch({'action': 'project_scan_policy', 'id': self.project['id'], 'enabled': False})

    def list(self):
        return engine.dispatch({'action': 'snapshot_cached', 'snapshot_format': 'list-v1'})

    def test_summary_preserves_display_and_controls_without_heavy_details(self):
        full = engine.snapshot_cached()['services'][0]
        result = self.list()
        summary = result['services'][0]
        for key in engine.SUMMARY_FIELDS:
            self.assertEqual(summary[key], full.get(key), key)
        self.assertTrue(summary['summary'])
        self.assertNotIn('argv', summary['spec'])
        self.assertNotIn('runtime', summary['run'])
        self.assertNotIn('configuration', summary)
        self.assertNotIn('operations', result)
        details = engine.dispatch({'action': 'service_details', 'id': 'one'})['service']
        self.assertEqual(details['spec']['argv'], ['echo', 'x' * 3000])
        self.assertEqual(summary['detail_revision'], details['detail_revision'])
        self.assertNotIn('fixture-private', json.dumps([result, details]))
        self.assertLess(len(json.dumps(result)), len(json.dumps(engine.snapshot_cached())) * .5)

    def test_details_token_ignores_check_time_but_tracks_runtime_configuration_and_policy(self):
        revision = self.list()['services'][0]['detail_revision']
        engine.write('UPDATE service_observations SET checked_at=checked_at+1 WHERE service_id=?', ['one'])
        self.assertEqual(self.list()['services'][0]['detail_revision'], revision)
        for sql, values in [
            ('UPDATE services SET pending_spec=?,config_revision=config_revision+1 WHERE id=?', [json.dumps({'argv': ['true']}), 'one']),
            ('UPDATE services SET display_name=? WHERE id=?', ['Renamed', 'one']),
            ('UPDATE services SET created_at=?,created_at_source=? WHERE id=?', [123.0, 'estimated', 'one']),
            ('UPDATE projects SET auto_scan=1 WHERE id=?', [self.project['id']]),
        ]:
            engine.write(sql, values)
            new = self.list()['services'][0]['detail_revision']
            self.assertNotEqual(new, revision); revision = new
            self.assertEqual(new, engine.service_details('one')['service']['detail_revision'])
        projection = json.loads(engine.query('SELECT projection FROM service_observations', one=True)['projection'])
        projection['run'] = {'id': 'changed-run', 'state': 'exited', 'runtime': {'child_pid': 17}}
        engine.write('UPDATE service_observations SET projection=?', [json.dumps(projection)])
        self.assertNotEqual(self.list()['services'][0]['detail_revision'], revision)

    def test_menu_keeps_global_counts_but_bounds_rows(self):
        for n in range(35):
            sid = f'service-{n}'
            engine.record_spec(sid, self.project, sid, 'task', {})
            projection = {'state': 'running', 'health': 'available', 'run': None,
                          'can_stop': True, 'can_start': False, 'can_restart': True, 'can_force': False}
            engine.write('INSERT INTO service_observations VALUES (?,?,?)', [sid, 10, json.dumps(projection)])
        full = self.list()
        menu = engine.snapshot_cached(snapshot_format='menu-v1', visible=False)
        self.assertEqual(menu['summary']['total'], 36)
        self.assertEqual(menu['summary']['running'], 35)
        self.assertEqual(menu['summary']['attention'], 0)
        self.assertEqual(menu['services'], full['services'][:16])
        self.assertNotIn('operations', menu)
        self.assertNotIn('deleted_projects', menu)
        self.assertEqual(len(menu['services']), 16)

    def test_summary_and_details_only_read_cache_and_preserve_history(self):
        before = engine.query('SELECT * FROM service_observations')
        with patch.object(engine, 'database', side_effect=AssertionError('write connection')), \
                patch.object(engine, 'probe_observation', side_effect=AssertionError('live probe')):
            self.list()
            engine.snapshot_cached(snapshot_format='menu-v1')
            engine.service_details('one')
        self.assertEqual(engine.query('SELECT * FROM service_observations'), before)
        self.assertEqual(engine.service_details('one')['service']['state'], 'never_started')

    def test_selected_operations_and_invalid_or_removed_identity(self):
        for sid in ('one', 'other'):
            engine.write('INSERT INTO operations(id,target,action,trigger,started,result) VALUES (?,?,?,?,?,?)',
                         [sid, sid, 'rename', 'test', 1, 'succeeded'])
        self.assertEqual([r['target'] for r in engine.service_details('one')['operations']], ['one'])
        for sid in (None, 5, '', 'removed'):
            with self.subTest(id=sid), self.assertRaises(ValueError):
                engine.service_details(sid)
        for mode in (False, 'unknown', None):
            with self.assertRaises(ValueError):
                engine.snapshot_cached(snapshot_format=mode)

    def test_persistent_worker_supports_legacy_and_both_new_formats(self):
        requests = [{'action': 'snapshot_cached', 'visible': True},
                    {'action': 'snapshot_cached', 'visible': True, 'snapshot_format': 'list-v1'},
                    {'action': 'snapshot_cached', 'visible': False, 'snapshot_format': 'menu-v1'},
                    {'action': 'snapshot_cached', 'visible': True, 'snapshot_format': 'bad'},
                    {'action': 'snapshot_cached', 'visible': True, 'snapshot_format': 'list-v1'}]
        result = subprocess.run([sys.executable, engine.__file__, '--root', str(self.root), 'snapshot-worker'],
                                input=''.join(json.dumps(r) + '\n' for r in requests), text=True,
                                capture_output=True, timeout=15, check=True)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([row['ok'] for row in rows], [True, True, True, False, True])
        self.assertNotIn('snapshot_format', rows[0]['result'])
        self.assertEqual([rows[i]['result']['snapshot_format'] for i in (1, 2, 4)], ['list-v1', 'menu-v1', 'list-v1'])
        self.assertEqual(rows[1]['result']['services'][0]['id'], 'one')


if __name__ == '__main__':
    unittest.main()
