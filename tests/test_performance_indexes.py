"""Indexed deadlines and historical event reuse; no production collector starts."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import capture
import engine
import observation
import observation_schedule as schedule
from event_index import EventIndex


class DeadlineTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        p = patch.object(engine, 'ROOT', self.root)
        p.start(); self.addCleanup(p.stop)
        self.project = engine.project(self.root)
        engine.record_spec('one', self.project, 'One', 'task', {})
        with patch.object(engine, 'timestamp', return_value=1000):
            engine.observe('one')
        schedule.ensure()

    def due(self, now=1001, **kwargs):
        return observation.due_services(kwargs.get('visible', True), kwargs.get('observed', False),
                                        kwargs.get('priority'), now)

    def test_deadlines_match_every_interval_and_mode(self):
        for kind in ('task', 'process', 'container', 'compose'):
            for state in ('unknown', 'running', 'stopped'):
                for url in ('', 'http://fixture.invalid'):
                    engine.write('UPDATE services SET kind=?,spec=? WHERE id=?', [kind, json.dumps({'url': url}), 'one'])
                    projection = {'state': state, 'run': None}
                    engine.write('UPDATE service_observations SET projection=? WHERE service_id=?', [json.dumps(projection), 'one'])
                    for visible in (True, False):
                        delay = observation.interval(kind, {'url': url}, projection, visible)
                        self.assertFalse(self.due(1000+delay-.01, visible=visible))
                        self.assertEqual(len(self.due(1000+delay, visible=visible)), 1)

    def test_legacy_writers_invalidate_config_runs_and_pause_resume(self):
        self.assertFalse(self.due())
        with sqlite3.connect(self.root / 'manager.sqlite') as old_writer:
            self.assertEqual(old_writer.execute('PRAGMA user_version').fetchone()[0], 1)
            old_writer.execute("UPDATE services SET spec='{}',config_revision=config_revision+1 WHERE id='one'")
        self.assertEqual(self.due()[0][0], 'one')
        with patch.object(engine, 'timestamp', return_value=1000): engine.observe('one')
        self.assertFalse(self.due())
        engine.write('UPDATE projects SET auto_scan=0,observation_revision=observation_revision+1')
        self.assertFalse(self.due(10000))
        engine.write('UPDATE projects SET auto_scan=1,observation_revision=observation_revision+1')
        self.assertEqual(self.due()[0][0], 'one')
        with patch.object(engine, 'timestamp', return_value=1000): engine.observe('one')
        run = engine.new_run('one', {'supervisor_pid': 12345})
        self.assertEqual(self.due()[0][2], 12345)
        engine.set_run(run, ended=1001, state='succeeded')
        self.assertIsNone(self.due()[0][2])
        engine.write('DELETE FROM runs WHERE id=?', [run])
        self.assertTrue(self.due())
        engine.write('DELETE FROM services WHERE id=?', ['one'])
        self.assertFalse(self.due(10000))
        self.assertFalse(engine.query('SELECT * FROM observation_schedule'))

    def test_transaction_rollback_and_project_removal(self):
        before = engine.query('SELECT * FROM observation_schedule')
        db = engine.database()
        db.execute('BEGIN')
        db.execute("UPDATE services SET config_revision=config_revision+1 WHERE id='one'")
        db.rollback(); db.close()
        self.assertEqual(engine.query('SELECT * FROM observation_schedule'), before)
        engine.write('DELETE FROM projects')
        self.assertFalse(self.due(10000))

    def test_index_does_not_allow_late_probe_to_cross_policy_generation(self):
        prepared = engine.prepare_observation('one', automatic=True, background=True)
        for enabled in (False, True):
            engine.dispatch({'action': 'project_scan_policy', 'id': self.project['id'], 'enabled': enabled})
        self.assertFalse(engine.finish_observation('one', prepared, automatic=True)['state_fresh'])
        self.assertEqual(engine.cached_observation('one')['last_checked_at'], 1000)
        self.assertEqual(self.due()[0][0], 'one')

    def test_service_and_project_restore_recreate_due_entries(self):
        removed = engine.dispatch({'action': 'service_delete', 'id': 'one'})
        self.assertFalse(engine.query('SELECT * FROM observation_schedule'))
        engine.dispatch({'action': 'service_restore', 'id': removed['trash_id']})
        self.assertEqual(self.due()[0][0], 'one')
        removed = engine.dispatch({'action': 'project_delete', 'id': self.project['id']})
        self.assertFalse(engine.query('SELECT * FROM observation_schedule'))
        engine.dispatch({'action': 'project_restore', 'id': removed['trash_id']})
        self.assertEqual(self.due()[0][0], 'one')

    def test_filters_priority_and_bounded_json_decoding(self):
        p2path = self.root / 'other'; p2path.mkdir()
        p2 = engine.project(p2path)
        with engine.database() as db:
            db.executemany("INSERT INTO services(id,project_id,name,kind,spec) VALUES (?,?,?,'task',?)",
                           [(f's{i:04}', self.project['id'], f'S{i}', '{}') for i in range(1000)])
        engine.record_spec('priority', p2, 'Priority', 'task', {'management': 'observed'})
        engine.record_spec('internal', p2, 'Internal', 'task', {'source': 'internal'})
        with patch.object(observation.json, 'loads', wraps=json.loads) as decode:
            items = self.due(priority=p2['id'])
            self.assertEqual(len(items), observation.BATCH_SIZE)
            self.assertEqual(items[0][0], 'priority')
            self.assertLessEqual(decode.call_count, observation.BATCH_SIZE * 2)
        self.assertEqual([row[0] for row in self.due(observed=True)], ['priority'])
        with engine.database() as db:
            plan = db.execute('EXPLAIN QUERY PLAN SELECT service_id FROM observation_schedule WHERE eligible=1 AND next_visible<=?', [1001]).fetchall()
        self.assertTrue(any('SEARCH observation_schedule USING INDEX observation_due_visible' in row[3] for row in plan))
        self.assertEqual(engine.query('SELECT count(*) AS n FROM services', one=True)['n'], 1003)


class EventIndexTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        p = patch.object(engine, 'ROOT', self.root); p.start(); self.addCleanup(p.stop)
        engine.project(self.root)
        self.index = EventIndex(capture.event_dir())

    def event(self, eid='a', pid=123):
        event = {'id': eid * 32, 'cwd': str(self.root), 'shell': {'pid': pid, 'birth': 'original'}, 'created': 1}
        capture.save_event(event)
        return event

    def test_only_changed_files_are_parsed_and_removals_invalidate(self):
        a, b = self.event('a'), self.event('b')
        self.index.refresh()
        with patch('event_index.json.loads', wraps=json.loads) as decode:
            self.index.refresh(); self.assertEqual(decode.call_count, 0)
            a['finished'] = 2; capture.save_event(a)
            self.index.refresh(); self.assertEqual(decode.call_count, 1)
        self.assertEqual(len(self.index), 2)
        (capture.event_dir() / (b['id'] + '.json')).unlink()
        self.index.refresh(); self.assertEqual(len(self.index), 1)
        path = capture.event_dir() / (a['id'] + '.json')
        path.write_text('{')
        self.index.refresh(); self.assertEqual(len(self.index), 0)
        capture.save_event(a); self.index.refresh(); self.assertEqual(len(self.index), 1)
        path.unlink(); path.symlink_to(self.root / 'manager.sqlite')
        self.index.refresh(); self.assertEqual(len(self.index), 0)

    def test_retirement_respects_birth_tracking_recent_docker_and_file_change(self):
        a, b, c = self.event('a'), self.event('b', 124), self.event('c', 125)
        c.update(armed=1, finished=100, docker_hint={'type': 'docker'})
        capture.save_event(c); self.index.refresh()
        tracked = {42: {'event': b['id'], 'birth': 'child'}}
        self.index.retire(self.index.events({}), {123: {'birth': 'reused'}}, tracked, capture.recent_docker_event, 101)
        self.assertEqual({e['id'] for e in self.index.events(tracked)}, {b['id'], c['id']})
        self.assertEqual(len(self.index), 3)  # History is retained.
        self.index.refresh()
        self.assertNotIn(a['id'], {e['id'] for e in self.index.events({})})
        a['shell']['birth'] = 'replacement'; capture.save_event(a); self.index.refresh()
        self.assertIn(a['id'], {e['id'] for e in self.index.events({})})
        self.index.retire([a], {}, {}, capture.recent_docker_event, 1000)
        self.assertIn(a['id'], {e['id'] for e in self.index.events({42: {'event': a['id']}})})

    def test_old_dead_events_skip_repeated_process_inventory_without_deleting_history(self):
        self.event(); self.index.refresh()
        with patch.object(capture, 'process_table', return_value={}) as process_table:
            tracked, seen, error = capture.collect_once({}, {}, self.index.events({}), event_index=self.index)
            self.assertIsNone(error)
            capture.collect_once(tracked, seen, self.index.events(tracked), event_index=self.index)
            self.assertEqual(process_table.call_count, 1)
        self.assertEqual(len(list(capture.event_dir().glob('*.json'))), 1)
        self.assertEqual(len(self.index), 1)


if __name__ == '__main__':
    unittest.main()
