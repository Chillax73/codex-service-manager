"""Candidate selection and stale-write guards in isolated SQLite state."""
from contextlib import ExitStack, closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import adoption
import capture
import engine
import observation_schedule


class CaptureCandidatesTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        replacement = patch.object(engine, 'ROOT', self.root)
        replacement.start(); self.addCleanup(replacement.stop)
        self.project = engine.project(self.root)
        self.event = {'id': 'a' * 32, 'cwd': str(self.root),
                      'shell': {'pid': 1234, 'birth': 'original'}}
        self.tracked = {1234: {'birth': 'original', 'event': self.event['id'], 'root': True}}
        self.events = {self.event['id']: self.event}
        observation_schedule.ensure()

    def seed(self, count, *, state='exited', ended=2, kind='task', project=None, prefix='s'):
        project = project or self.project
        with closing(engine.database()) as db, db:
            db.executemany('INSERT INTO services(id,project_id,name,kind,spec) VALUES (?,?,?,?,?)',
                           [(f'{prefix}{i}', project['id'], str(i), kind,
                             json.dumps({'management': 'observed'})) for i in range(count)])
            db.executemany('INSERT INTO runs(id,service_id,started,ended,state,runtime) VALUES (?,?,?,?,?,?)',
                           [(f'r{prefix}{i}', f'{prefix}{i}', 1, ended, state,
                             json.dumps({'pid': 1234, 'birth': 'original'})) for i in range(count)])

    def candidates(self):
        return list(capture.scope_candidates(self.tracked, self.events))

    def legacy(self):
        result = []
        for row in engine.query('SELECT s.id FROM services s JOIN projects p ON p.id=s.project_id '
                                'WHERE p.auto_scan=1 AND s.spec LIKE ?', ['%"management": "observed"%']):
            item, run = engine.service(row['id']), engine.latest(row['id'])
            if (item['kind'] in ('process', 'task') and run and run['ended'] is None
                    and run['state'] == 'running'
                    and self.tracked.get(run['runtime'].get('pid'), {}).get('event') in self.events):
                result.append((item, run))
        return sorted(result, key=lambda pair: pair[0]['id'])

    def test_no_lineage_skips_history_but_still_checks_containers(self):
        self.seed(1000)
        with patch.object(engine, 'database', wraps=engine.database) as connections, \
                patch.object(capture, 'collect_containers') as containers:
            result = capture.collect_once({}, {}, [])
        self.assertEqual(result, ({}, {}, None))
        containers.assert_called_once()
        self.assertLessEqual(connections.call_count, 2)
        self.assertEqual(engine.query('SELECT count(*) AS n FROM services', one=True)['n'], 1000)

    def test_history_query_is_constant_connections_and_decodes_only_candidates(self):
        for count in (260, 1000, 5000):
            with self.subTest(count=count):
                with closing(engine.database()) as db, db:
                    db.execute('DELETE FROM runs'); db.execute('DELETE FROM services')
                self.seed(count)
                with patch.object(engine, 'database', wraps=engine.database) as connections, \
                        patch.object(capture.json, 'loads', wraps=json.loads) as decode:
                    self.assertEqual(self.candidates(), [])
                self.assertEqual(connections.call_count, 1)
                self.assertEqual(decode.call_count, 0)

    def test_mixed_candidates_match_legacy_full_values_and_latest_ties(self):
        self.seed(3, state='running', ended=None)
        self.seed(1, state='running', ended=None, kind='container', prefix='container')
        (self.root / 'paused').mkdir()
        paused = engine.project(self.root / 'paused')
        engine.write('UPDATE projects SET auto_scan=0 WHERE id=?', [paused['id']])
        self.seed(1, state='running', ended=None, project=paused, prefix='paused')
        self.seed(1, state='stop_failed', ended=None, prefix='failed')
        self.seed(1, state='running', ended=2, prefix='ended')
        engine.record_spec('never', self.project, 'Never', 'task', {'management': 'observed'})
        # Older running rows must not resurrect a service with a newer terminal run.
        rid = engine.new_run('s1', {'pid': 1234}, started=3)
        engine.set_run(rid, state='exited', ended=4)
        # Equal timestamps retain the exact engine.latest ordering contract.
        rid = engine.new_run('s2', {'pid': 9999}, started=1)
        engine.set_run(rid, state='running')
        self.assertEqual(sorted(self.candidates(), key=lambda pair: pair[0]['id']), self.legacy())
        self.assertIn('s0', [item['id'] for item, _ in self.candidates()])

    def test_unrelated_pid_skips_spec_decode_and_dormant_lineage_skips_query(self):
        self.seed(1, state='running', ended=None)
        engine.set_run('rs0', runtime={'pid': 5678, 'birth': 'other'})
        with patch.object(capture.json, 'loads', wraps=json.loads) as decode:
            self.assertEqual(self.candidates(), [])
        self.assertEqual(decode.call_count, 1)  # runtime only
        with patch.object(engine, 'database', side_effect=AssertionError('unnecessary database read')):
            self.assertEqual(list(capture.scope_candidates(self.tracked, {})), [])

    def run_scope_pass(self, mutate=None, bad_birth=False):
        table = {1234: {'pid': 1234, 'ppid': 1, 'birth': 'reused' if bad_birth else 'original',
                        'name': 'python3'}}
        def plan(*args, **kwargs):
            if mutate:
                mutate()
            return {'root': 1234, 'members': [table[1234]]}
        with ExitStack() as stack:
            stack.enter_context(patch.object(capture, 'process_table', return_value=table))
            stack.enter_context(patch.object(capture, 'listeners', return_value={}))
            stack.enter_context(patch.object(capture, 'managed_processes', return_value=set()))
            stack.enter_context(patch.object(capture, 'collect_containers'))
            stack.enter_context(patch.object(adoption, 'stop_plan', side_effect=plan))
            scope = stack.enter_context(patch.object(adoption, 'remember_scope'))
            recipe = stack.enter_context(patch.object(adoption, 'remember_recipe'))
            capture.collect_once(self.tracked.copy(), {}, [self.event])
            return scope.call_count, recipe.call_count

    def test_valid_candidate_keeps_scope_and_recipe_path(self):
        self.seed(1, state='running', ended=None)
        self.assertEqual(self.run_scope_pass(), (1, 1))

    def test_candidate_connection_is_closed_before_external_probe(self):
        self.seed(1, state='running', ended=None)
        db = engine.database()
        self.addCleanup(db.close)
        with patch.object(engine, 'database', return_value=db):
            iterator = capture.scope_candidates(self.tracked, self.events)
            self.assertEqual(next(iterator)[0]['id'], 's0')
            with self.assertRaises(sqlite3.ProgrammingError):
                db.execute('SELECT 1')

    def test_reused_pid_does_not_retain_old_lineage(self):
        self.seed(1, state='running', ended=None)
        self.assertEqual(self.run_scope_pass(bad_birth=True), (0, 0))

    def test_new_run_cannot_receive_scope_from_previous_candidate(self):
        self.seed(1, state='running', ended=None)
        def replace_run():
            rid = engine.new_run('s0', {'pid': 5678, 'birth': 'new'}, started=3)
            engine.set_run(rid, state='running')
        self.assertEqual(self.run_scope_pass(replace_run), (0, 0))

    def test_changed_run_config_policy_or_deleted_service_cannot_write_stale_scope(self):
        self.seed(1, state='running', ended=None)
        mutations = [
            lambda: engine.set_run('rs0', state='exited', ended=3),
            lambda: engine.write('UPDATE services SET config_revision=config_revision+1'),
            lambda: engine.write('UPDATE projects SET observation_revision=observation_revision+2'),
            lambda: engine.write('UPDATE projects SET auto_scan=0'),
            lambda: engine.write('DELETE FROM services'),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                engine.write('UPDATE projects SET auto_scan=1')
                engine.set_run('rs0', state='running', ended=None)
                self.assertEqual(self.run_scope_pass(mutate), (0, 0))


if __name__ == '__main__':
    unittest.main()
