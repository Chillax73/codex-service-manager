import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import capture
import engine


class ObservationPolicyTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name).resolve()
        root_patch = patch.object(engine, 'ROOT', root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.projects = {}
        for name in ('a', 'b'):
            folder = root / name
            folder.mkdir()
            self.projects[name] = engine.project(folder)
            engine.record_spec(name, self.projects[name], name, 'process', {'url': 'http://localhost'})
            run = engine.new_run(name, {'supervisor_pid': 123, 'supervisor_birth': 'birth'})
            engine.set_run(run, state='running')

    def toggle(self, name, enabled):
        return engine.dispatch({'action': 'project_scan_policy', 'id': self.projects[name]['id'], 'enabled': enabled})

    def bulk(self, enabled, ids=None):
        request = {'action': 'project_scan_policy_batch', 'enabled': enabled, 'scope': 'all'}
        if ids is not None:
            request.update(scope='selected', ids=ids)
        return engine.dispatch(request)['changed']

    def test_batch_targets_selection_and_reports_actual_changes(self):
        a, b = (self.projects[name]['id'] for name in ('a', 'b'))
        self.toggle('a', False)
        self.assertEqual(self.bulk(True, [a, a, b]), 1)
        self.assertEqual(self.bulk(True, [a, b]), 0)
        self.assertEqual(engine.observation_row(a)['observation_revision'], 2)
        self.assertEqual(engine.observation_row(b)['observation_revision'], 0)
        self.assertEqual(self.bulk(False, [a]), 1)
        self.assertTrue(engine.observation_row(b)['auto_scan'])

    def test_batch_rollback_on_invalid_target_or_database_failure(self):
        a, b = (self.projects[name]['id'] for name in ('a', 'b'))
        with self.assertRaisesRegex(ValueError, '重新选择'):
            self.bulk(False, [a, 'deleted'])
        with self.assertRaises(ValueError):
            self.bulk(False, [])
        with self.assertRaises(ValueError):
            self.bulk('off')
        engine.write(f"""CREATE TRIGGER reject_policy BEFORE UPDATE OF auto_scan ON projects
            WHEN OLD.id='{b}' BEGIN SELECT RAISE(FAIL, 'fixture'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.bulk(False)
        for project_id in (a, b):
            self.assertTrue(engine.observation_row(project_id)['auto_scan'])
            self.assertEqual(engine.observation_row(project_id)['observation_revision'], 0)

    def test_batch_pause_invalidates_old_probe_and_allows_new_project(self):
        a, b = (self.projects[name]['id'] for name in ('a', 'b'))
        engine.write('UPDATE projects SET pinned=1 WHERE id=?', [b])
        with patch.object(engine, 'matches', return_value=True), patch.object(engine, 'health', return_value='available'):
            prepared = engine.prepare_observation('a', automatic=True)
        self.assertEqual(self.bulk(False), 2)
        discarded = engine.finish_observation('a', prepared, automatic=True)
        self.assertIsNone(discarded['last_checked_at'])
        self.assertTrue(discarded['observation_paused'])
        new = engine.ROOT / 'new'
        new.mkdir()
        event = {'id': 'new', 'cwd': str(new)}
        process = {'pid': 99, 'birth': 'new', 'name': 'python'}
        with patch.object(capture, 'process_cwd', return_value=str(new)):
            capture.record_process(event, process, [7001])
        self.assertTrue(engine.project(new)['auto_scan'])
        self.assertFalse(engine.observation_row(b)['auto_scan'])
        self.assertEqual(self.bulk(True), 2)
        self.assertTrue(engine.observation_row(a)['auto_scan'])

    def test_paused_snapshot_uses_cache_manual_refresh_targets_only_one(self):
        with patch.object(engine, 'matches', return_value=True) as identity, patch.object(engine, 'health', return_value='available'):
            initial = next(item for item in engine.snapshot()['services'] if item['id'] == 'a')
            self.toggle('a', False)
            identity.reset_mock()
            cache = next(item for item in engine.snapshot()['services'] if item['id'] == 'a')
            self.assertEqual(identity.call_count, 1)
            self.assertTrue(cache['observation_paused'])
            self.assertFalse(cache['state_fresh'])
            self.assertEqual(cache['last_checked_at'], initial['last_checked_at'])
            identity.reset_mock()
            result = engine.dispatch({'action': 'refresh', 'project_id': self.projects['a']['id']})
            refreshed = next(item for item in result['services'] if item['id'] == 'a')
            self.assertEqual(identity.call_count, 1)
            self.assertTrue(refreshed['state_fresh'])
            self.assertGreater(refreshed['last_checked_at'], initial['last_checked_at'])
            self.assertFalse(engine.project_record(self.projects['a']['id'])['auto_scan'])

    def test_paused_during_probe_discards_run_and_cache_updates(self):
        engine.write('UPDATE services SET spec=? WHERE id=?',
                     [json.dumps({'management': 'observed', 'ports': []}), 'a'])
        run = engine.latest('a')
        def probe(_):
            # If this cannot take the lock, the probe was incorrectly run inside it.
            self.toggle('a', False)
            self.toggle('a', True)
            return None
        with patch.object(engine, 'identity', side_effect=probe):
            discarded = engine.observe('a', automatic=True)
        self.assertIsNone(discarded['last_checked_at'])
        self.assertEqual(engine.latest('a'), run)

    def test_docker_failure_after_pause_requires_explicit_refresh(self):
        engine.record_spec('docker', self.projects['a'], 'Docker', 'container', {})
        self.toggle('a', False)
        with patch.object(engine, 'containers_for', side_effect=RuntimeError('daemon unavailable')) as inspect:
            cached = engine.observe('docker', automatic=True)
            self.assertIsNone(cached['last_checked_at'])
            inspect.assert_not_called()
            fresh = engine.observe('docker')
            self.assertIsNotNone(fresh['last_checked_at'])
            self.assertEqual(fresh['state'], 'unknown')
            self.assertTrue(fresh['observation_paused'])

    def test_docker_capture_coexists_with_manual_registration_in_same_database(self):
        def container(letter, labels=None):
            return {'Id': letter * 64, 'Name': '/' + letter,
                    'Config': {'Labels': labels or {}}, 'NetworkSettings': {'Ports': {}},
                    'State': {'Running': True, 'Status': 'running'}}

        a, b = (self.projects[name] for name in ('a', 'b'))
        managed = container('a')
        with patch.object(engine, 'container_inspect', return_value=managed):
            registered = engine.dispatch({'action': 'register_container', 'container_id': managed['Id'],
                                          'project_id': a['id']})
        event = {'cwd': b['cwd']}
        capture.record_container(event, managed)
        capture.record_container(event, container('b'))
        capture.record_container({'cwd': a['cwd']}, container('b'))
        file = str(Path(a['cwd']) / 'compose.yaml')
        engine.record_spec('compose', a, 'web', 'compose',
                           {'file': file, 'compose_project': 'shared', 'member': 'web'})
        labels = {'com.docker.compose.project': 'shared', 'com.docker.compose.service': 'web'}
        config_key = 'com.docker.compose.project.config_files'
        capture.record_container(event, container('c', {**labels, config_key: file}))
        capture.record_container(event, container('d', {**labels, config_key: b['cwd'] + '/compose.yaml'}))
        # The explicit returned ID and event establish ownership even without a Compose config label.
        capture.record_container(event, container('e', labels))
        rows = engine.query("SELECT * FROM services WHERE kind='container'")
        owners = {json.loads(row['spec'])['container_id']: row['project_id'] for row in rows}
        self.assertEqual(len(rows), 4)
        self.assertEqual(owners, {k * 64: owner for k, owner in
                                 [('a', a['id']), ('b', b['id']), ('d', b['id']), ('e', b['id'])]})
        self.assertEqual(engine.service(registered['id'])['spec']['source'], 'explicit_adoption')

    def test_collector_isolates_paused_project_and_resumes_verified_tracking(self):
        self.toggle('a', False)
        events = [{'id': name, 'cwd': self.projects[name]['cwd'], 'shell': {'pid': pid, 'birth': name}}
                  for name, pid in [('a', 101), ('b', 102)]]
        table = {pid: {'pid': pid, 'ppid': 1, 'birth': name, 'name': 'python'}
                 for name, pid in [('a', 101), ('b', 102)]}
        tracked = {101: {'birth': 'a', 'event': 'a', 'root': False}}
        with patch.object(capture, 'load_events', return_value=events), patch.object(capture, 'process_table', return_value=table), \
                patch.object(capture, 'listeners', return_value={101: [7001], 102: [7002]}), \
                patch.object(capture, 'managed_processes', return_value=set()), \
                patch.object(capture, 'process_cwd', side_effect=lambda pid, fallback: fallback) as cwd, \
                patch.object(engine, 'identity', side_effect=lambda pid: table.get(pid)):
            tracked, first_seen, error = capture.collect_once(tracked, {})
            self.assertIsNone(error)
            self.assertEqual([c.args[0] for c in cwd.call_args_list], [102])
            self.assertIn(101, tracked)
            self.assertEqual(engine.query('SELECT COUNT(*) AS n FROM services', one=True)['n'], 3)
            self.toggle('a', True)
            tracked, first_seen, error = capture.collect_once(tracked, first_seen)
            capture.collect_once(tracked, first_seen)
            self.assertIsNone(error)
            self.assertEqual(engine.query('SELECT COUNT(*) AS n FROM services', one=True)['n'], 4)

    def test_collector_discards_new_service_if_project_pauses_mid_probe(self):
        event = {'id': 'a', 'cwd': self.projects['a']['cwd'], 'shell': {'pid': 101, 'birth': 'a'}}
        table = {101: {'pid': 101, 'ppid': 1, 'birth': 'a', 'name': 'python'}}
        def listener_probe():
            self.toggle('a', False)
            return {101: [7001]}
        with patch.object(capture, 'load_events', return_value=[event]), \
                patch.object(capture, 'process_table', return_value=table), \
                patch.object(capture, 'listeners', side_effect=listener_probe), \
                patch.object(capture, 'managed_processes', return_value=set()), \
                patch.object(capture, 'process_cwd', return_value=event['cwd']):
            capture.collect_once({}, {})
        self.assertEqual(engine.query('SELECT COUNT(*) AS n FROM services', one=True)['n'], 2)

    def test_collector_only_rechecks_policy_for_tracked_events(self):
        event = {'id': 'live', 'cwd': self.projects['a']['cwd'], 'shell': {'pid': 101, 'birth': 'a'}}
        history = [{'id': str(pid), 'cwd': self.projects['a']['cwd'], 'shell': {'pid': pid, 'birth': 'old'}}
                   for pid in range(200, 220)]
        table = {101: {'pid': 101, 'ppid': 1, 'birth': 'a', 'name': 'sh'}}
        with patch.object(capture, 'process_table', return_value=table), \
                patch.object(capture, 'listeners', return_value={}), \
                patch.object(capture, 'managed_processes', return_value=set()), \
                patch.object(capture, 'event_project', wraps=capture.event_project) as project:
            tracked, _, error = capture.collect_once({}, {}, [event, *history])
        self.assertIsNone(error)
        self.assertEqual({pid: info['event'] for pid, info in tracked.items()}, {101: 'live'})
        self.assertEqual([call.args[0]['id'] for call in project.call_args_list if len(call.args) == 1], ['live'])

    def test_collector_persists_tracking_only_on_change(self):
        event = {'id': 'live', 'cwd': self.projects['a']['cwd'], 'shell': {'pid': 101, 'birth': 'a'}}
        table = {101: {'pid': 101, 'ppid': 1, 'birth': 'a', 'name': 'sh'}}
        with patch.object(capture, 'process_table', return_value=table) as processes, \
                patch.object(capture, 'listeners', return_value={}), \
                patch.object(capture, 'managed_processes', return_value=set()):
            tracked, first_seen, _ = capture.collect_once({}, {}, [event])
            engine.write("""CREATE TRIGGER reject_tracking_rewrite BEFORE INSERT ON metadata
                WHEN NEW.key='capture_tracking' BEGIN SELECT RAISE(FAIL, '重复写入'); END""")
            self.assertEqual(capture.collect_once(tracked, first_seen, [event])[0], tracked)
            engine.write('DROP TRIGGER reject_tracking_rewrite')
            processes.return_value = {}
            self.assertEqual(capture.collect_once(tracked, first_seen, [event])[0], {})
        saved = engine.query("SELECT value FROM metadata WHERE key='capture_tracking'", one=True)
        self.assertEqual(json.loads(saved['value']), {})


if __name__ == '__main__':
    unittest.main()
