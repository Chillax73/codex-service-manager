"""Snapshot cache, bounded observation, and late result contracts."""
import concurrent.futures
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import capture
import engine
import observation


class SnapshotCacheTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        root = patch.object(engine, 'ROOT', self.root)
        root.start()
        self.addCleanup(root.stop)
        for name in ('a', 'b'):
            (self.root / name).mkdir()
        self.a = engine.project(self.root / 'a')
        self.b = engine.project(self.root / 'b')

    def register(self, sid, project=None, kind='task', **spec):
        return engine.record_spec(sid, project or self.a, sid, kind, spec)

    def toggle(self, project, enabled):
        engine.dispatch({'action': 'project_scan_policy', 'id': project['id'], 'enabled': enabled})

    def test_readonly_snapshot_retains_ages_errors_and_private_values(self):
        self.register('pending', env={'TOKEN': 'fixture-secret'})
        self.register('expired')
        self.register('paused', self.b)
        self.register('error', kind='container')
        self.register('internal', source='internal')
        engine.observe('expired')
        engine.observe('paused')
        self.toggle(self.b, False)
        with patch.object(engine, 'containers_for', side_effect=RuntimeError('daemon unavailable')):
            engine.observe('error')
        age = engine.timestamp() - 300
        engine.write('UPDATE service_observations SET checked_at=? WHERE service_id=?', [age, 'expired'])
        before = engine.query('SELECT * FROM service_observations ORDER BY service_id')
        engine.write("INSERT INTO metadata VALUES ('watcher',?)", [
            json.dumps({'pid': 4, 'birth': 'fixture', 'updated': engine.timestamp()})])
        with patch.object(engine, 'database', side_effect=AssertionError('write-capable connection')), \
                patch.object(engine, 'probe_observation', side_effect=AssertionError('live probe')), \
                patch.object(engine, 'matches', side_effect=AssertionError('PID probe')):
            snapshot = engine.dispatch({'action': 'snapshot_cached'})
            self.assertEqual(snapshot['services'], engine.snapshot_cached()['services'])
            self.assertTrue(engine.snapshot_cached(component_identities={'watcher': (4, 'fixture')})['watcher']['alive'])
        self.assertEqual(engine.query('SELECT * FROM service_observations ORDER BY service_id'), before)
        self.assertNotIn('fixture-secret', json.dumps(snapshot))
        items = {row['id']: row for row in snapshot['services']}
        self.assertEqual({sid: row['cache_state'] for sid, row in items.items()},
                         {'pending': 'pending', 'expired': 'expired', 'paused': 'paused', 'error': 'recent'})
        self.assertIsNone(items['pending']['last_checked_at'])
        self.assertEqual(items['expired']['last_checked_at'], age)
        self.assertEqual(items['error']['state'], 'unknown')
        self.assertIn('daemon unavailable', items['error']['problem'])

    def test_stale_cache_does_not_change_manual_scope_or_controls(self):
        self.register('a')
        self.register('b', self.b)
        engine.observe('a')
        self.toggle(self.a, False)
        run = engine.new_run('a', {'supervisor_pid': 87, 'supervisor_birth': 'fixture'})
        engine.set_run(run, state='running')
        cached = next(row for row in engine.snapshot_cached()['services'] if row['id'] == 'a')
        self.assertEqual(cached['state'], 'never_started')
        with patch.object(engine, 'matches', return_value=False), patch.object(engine.os, 'kill') as kill:
            for action in ('start', 'stop', 'restart', 'service_delete'):
                with self.subTest(action=action), self.assertRaises(RuntimeError):
                    engine.dispatch({'action': action, 'id': 'a'})
            kill.assert_not_called()
        with patch.object(engine, 'probe_observation', wraps=engine.probe_observation) as probe, \
                patch.object(engine, 'matches', return_value=True):
            result = engine.dispatch({'action': 'refresh', 'project_id': self.a['id']})
        self.assertEqual([call.args[0]['id'] for call in probe.call_args_list], ['a'])
        self.assertEqual({row['id']: row['state'] for row in result['services']}['a'], 'running')
        self.assertFalse(engine.observation_row(self.a['id'])['auto_scan'])

    def test_scheduler_limits_work_and_rechecks_pause_and_backoff(self):
        for n in range(15):
            self.register(f'enabled-{n:02}')
        self.register('paused', self.b)
        self.toggle(self.b, False)
        with patch.object(engine, 'timestamp', return_value=1000):
            self.assertEqual(observation.observe_due(visible=True), observation.BATCH_SIZE)
            self.assertEqual(observation.observe_due(visible=True), 15 - observation.BATCH_SIZE)
            self.assertEqual(observation.observe_due(visible=True), 0)
            self.register('docker', kind='container')
            with patch.object(engine, 'containers_for', side_effect=RuntimeError('offline')) as inspect:
                self.assertEqual(observation.observe_due(visible=True), 1)
                inspect.assert_called_once()
        with patch.object(engine, 'timestamp', return_value=1008):
            self.assertEqual(observation.observe_due(visible=True), 0)
        with patch.object(engine, 'timestamp', return_value=1017):
            self.assertEqual([item[0] for item in observation.due_services(True, False, None, 1017)], ['docker'])
        self.assertIsNone(engine.cached_observation('paused')['last_checked_at'])

    def test_collector_and_desktop_share_lock_without_blocking_capture(self):
        self.register('observed', management='observed')
        self.register('managed')
        entered, release = threading.Event(), threading.Event()
        original = engine.probe_observation
        def slow(*args, **kwargs):
            entered.set()
            if not release.wait(4):
                raise RuntimeError('fixture wait expired')
            return original(*args, **kwargs)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            queue = capture.ObservationQueue(executor)
            with patch.object(engine, 'probe_observation', side_effect=slow) as probe:
                queue.tick()
                try:
                    self.assertTrue(entered.wait(3))
                    before = queue.pending
                    queue.tick()
                    self.assertIs(queue.pending, before)
                    self.assertEqual(observation.observe_due(visible=True), 0)
                    self.assertEqual(capture.collect_once({}, {}, []), ({}, {}, None))
                    self.assertEqual(len(engine.snapshot_cached()['services']), 2)
                    self.assertEqual(probe.call_count, 1)
                finally:
                    release.set()
                self.assertEqual(before.result(timeout=5), 1)
        self.assertIsNotNone(engine.cached_observation('observed')['last_checked_at'])
        self.assertIsNone(engine.cached_observation('managed')['last_checked_at'])

    def test_stale_probe_loses_to_policy_reregistration_and_manual_refresh(self):
        self.register('fixture')
        prepared = engine.prepare_observation('fixture', automatic=True, background=True)
        self.toggle(self.a, False)
        self.toggle(self.a, True)
        self.assertFalse(engine.finish_observation('fixture', prepared, automatic=True)['state_fresh'])
        self.assertIsNone(engine.cached_observation('fixture')['last_checked_at'])
        prepared = engine.prepare_observation('fixture', automatic=True, background=True)
        engine.dispatch({'action': 'service_delete', 'id': 'fixture'})
        self.register('fixture', argv=['fixture-new'])
        self.assertFalse(engine.finish_observation('fixture', prepared, automatic=True)['state_fresh'])
        self.assertIsNone(engine.cached_observation('fixture')['last_checked_at'])
        prepared = engine.prepare_observation('fixture', automatic=True, background=True)
        refreshed = engine.dispatch({'action': 'refresh', 'project_id': self.a['id']})['services'][0]
        self.assertFalse(engine.finish_observation('fixture', prepared, automatic=True)['state_fresh'])
        self.assertEqual(engine.cached_observation('fixture')['last_checked_at'], refreshed['last_checked_at'])


if __name__ == '__main__':
    unittest.main()
