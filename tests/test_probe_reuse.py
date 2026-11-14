"""P4 read/probe reuse must not weaken control or stale-result protection."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import adoption
import capture
import engine
import observation


class ProbeReuseTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        p = patch.object(engine, 'ROOT', self.root); p.start(); self.addCleanup(p.stop)
        self.project = engine.project(self.root)
        self.table = {101: {'pid': 101, 'ppid': 1, 'pgid': 101, 'name': 'python3', 'birth': 'original'}}
        self.tracked = {'101': {'birth': 'original', 'event': 'a', 'root': True}}
        engine.write('INSERT INTO metadata VALUES (?,?)', ['capture_tracking', json.dumps(self.tracked)])

    def service(self, sid='local', pid=101, management='observed'):
        item = engine.record_spec(sid, self.project, sid, 'task', {'management': management})
        rid = engine.new_run(sid, {'pid': pid, 'birth': 'original'}, started=1)
        engine.set_run(rid, state='running')
        return item, engine.latest(sid)

    def context(self, ids):
        with patch.object(capture, 'process_table', return_value=self.table), \
                patch.object(capture, 'listeners', return_value={}):
            return observation.local_probe_context(ids)

    def test_read_context_reuses_connection_without_stale_transaction(self):
        with patch.object(engine, 'database', wraps=engine.database) as opened:
            with engine.read_context():
                engine.query('SELECT * FROM projects')
                conn = engine._reads.current[1]
                self.assertFalse(conn.in_transaction)
                with self.assertRaises(sqlite3.OperationalError):
                    engine.query("UPDATE projects SET name='not allowed'")
                with engine.read_context():
                    engine.query('SELECT * FROM projects')
                self.assertEqual(opened.call_count, 1)
                with closing(sqlite3.connect(self.root / 'manager.sqlite')) as writer, writer:
                    writer.execute("UPDATE projects SET name='changed'")
                self.assertEqual(engine.query('SELECT name FROM projects', one=True)['name'], 'changed')
                self.assertFalse(conn.in_transaction)
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute('SELECT 1')
        self.assertIsNone(engine._reads.current)

    def test_connections_are_thread_owned_and_restored_after_failure(self):
        barrier = threading.Barrier(2)
        def read():
            with engine.read_context():
                conn = engine._reads.current[1]
                barrier.wait(timeout=3)
                return id(conn), engine.query('SELECT count(*) AS n FROM projects', one=True)['n']
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = [f.result() for f in [pool.submit(read), pool.submit(read)]]
        self.assertNotEqual(a[0], b[0]); self.assertEqual(a[1:], b[1:])
        with self.assertRaisesRegex(RuntimeError, 'fixture'):
            with engine.read_context():
                raise RuntimeError('fixture')
        self.assertIsNone(getattr(engine._reads, 'current', None))

    def test_nested_different_state_root_cannot_reuse_outer_database(self):
        other = self.root / 'other'; other.mkdir()
        with engine.read_context():
            outer = engine._reads.current[1]
            with patch.object(engine, 'ROOT', other):
                engine.project(other)
                with engine.read_context():
                    self.assertIsNot(engine._reads.current[1], outer)
                    self.assertEqual(engine.query('SELECT cwd FROM projects', one=True)['cwd'], str(other))
            self.assertIs(engine._reads.current[1], outer)
            self.assertEqual(engine.query('SELECT cwd FROM projects', one=True)['cwd'], str(self.root))

    def test_scope_ownership_query_includes_paused_runs_and_ignores_old_runs(self):
        item, _ = self.service()
        other, rid = self.service('other')
        engine.write('UPDATE projects SET auto_scan=0')
        with patch.object(engine, 'database', wraps=engine.database) as opened:
            with self.assertRaisesRegex(RuntimeError, '共用'):
                adoption.exclusive_scope(item, {101}, self.table)
        self.assertEqual(opened.call_count, 1)
        new = engine.new_run('other', {}, started=2)
        engine.set_run(new, state='exited', ended=3)
        adoption.exclusive_scope(item, {101}, self.table)

    def test_managed_inventory_uses_same_birth_and_groups_without_subprocesses(self):
        item, run = self.service(management='managed')
        engine.set_run(run['id'], runtime={'supervisor_pid': 101, 'supervisor_birth': 'original', 'pgid': 202})
        table = {**self.table, 202: {'pid': 202, 'ppid': 101, 'pgid': 202, 'birth': 'child', 'name': 'python3'}}
        with patch.object(engine, 'identity', side_effect=AssertionError('extra ps')), \
                patch.object(engine, 'group_members', side_effect=AssertionError('extra groups')):
            self.assertEqual(capture.managed_processes(table), {202})
            table[101] = {**table[101], 'birth': 'reused'}
            self.assertEqual(capture.managed_processes(table), set())

    def test_scoped_listeners_intersect_pid_and_tcp_filters(self):
        class Result:
            returncode = 0
            stdout = 'p101\nn127.0.0.1:8080\n'
        with patch.object(capture.subprocess, 'run', return_value=Result()) as command:
            self.assertEqual(capture.listeners([]), {})
            command.assert_not_called()
            self.assertEqual(capture.listeners([101, 101]), {101: [8080]})
        argv = command.call_args[0][0]
        self.assertIn('-a', argv); self.assertEqual(argv[argv.index('-p') + 1], '101')

    def test_background_batch_shares_inventory_and_never_calls_individual_identity(self):
        item, run = self.service()
        ctx = self.context(['local'])
        with patch.object(engine, 'identity', side_effect=AssertionError('extra identity')), \
                patch.object(capture, 'process_table', side_effect=AssertionError('extra table')), \
                patch.object(capture, 'listeners', side_effect=AssertionError('extra listeners')):
            result, _, _ = engine.probe_observation(item, run, background=True, process_context=ctx)
        self.assertEqual(result['state'], 'running'); self.assertTrue(result['can_stop'])

    def test_new_run_or_config_uses_fresh_identity_instead_of_old_table(self):
        item, run = self.service()
        ctx = self.context(['local'])
        rid = engine.new_run('local', {'pid': 202, 'birth': 'new'}, started=3)
        engine.set_run(rid, state='running')
        with patch.object(engine, 'identity', return_value={'birth': 'new'}) as identity, \
                patch.object(adoption, 'stop_plan', side_effect=RuntimeError('no scope')):
            result, _, changes = engine.probe_observation(item, engine.latest('local'), background=True, process_context=ctx)
        self.assertEqual(result['state'], 'running'); self.assertIsNone(changes)
        identity.assert_called_once_with(202)
        engine.write('UPDATE services SET config_revision=config_revision+1')
        with patch.object(engine, 'identity', return_value=None) as identity:
            engine.probe_observation(engine.service('local'), run, background=True, process_context=ctx)
        identity.assert_called_once_with(101)

    def test_explicit_inspection_and_stop_plan_ignore_batch_cache(self):
        item, run = self.service()
        ctx = self.context(['local'])
        with patch.object(engine, 'identity', return_value=None) as identity:
            result, _, _ = engine.probe_observation(item, run, process_context=ctx)
        identity.assert_called_once_with(101); self.assertEqual(result['state'], 'exited')
        with patch.object(capture, 'process_table', return_value={}) as table:
            with self.assertRaisesRegex(RuntimeError, '身份'):
                adoption.stop_plan(item)
        table.assert_called_once()

    def test_failed_batch_inventory_falls_back_and_pid_reuse_is_not_live(self):
        item, run = self.service()
        with patch.object(capture, 'process_table', side_effect=RuntimeError('fixture failure')):
            self.assertIsNone(observation.local_probe_context(['local']))
        self.table[101]['birth'] = 'replacement'
        ctx = self.context(['local'])
        result, _, changes = engine.probe_observation(item, run, background=True, process_context=ctx)
        self.assertEqual(result['state'], 'exited'); self.assertIsNotNone(changes['ended'])

    def test_reused_batch_cannot_cross_pause_resume_generation(self):
        item, run = self.service()
        ctx = self.context(['local'])
        prepared = engine.prepare_observation('local', automatic=True, background=True, process_context=ctx)
        for enabled in (False, True):
            engine.dispatch({'action': 'project_scan_policy', 'id': self.project['id'], 'enabled': enabled})
        self.assertFalse(engine.finish_observation('local', prepared, automatic=True)['state_fresh'])
        self.assertEqual(engine.latest('local'), run)

    def test_checkpoint_table_is_reused_by_collect_pass(self):
        event = {'id': 'a', 'cwd': str(self.root), 'shell': self.table[101]}
        with patch.object(capture, 'process_table', side_effect=AssertionError('second scan')), \
                patch.object(capture, 'listeners', return_value={}), \
                patch.object(capture, 'managed_processes', return_value=set()), \
                patch.object(capture, 'collect_containers'):
            result = capture.collect_once({}, {}, [event], processes=self.table)
        self.assertEqual(result[0][101]['birth'], 'original')

    def test_existing_process_update_does_not_query_unused_cwd(self):
        item, _ = self.service()
        event = {'id': 'a', 'cwd': str(self.root)}
        with patch.object(capture, 'process_cwd', side_effect=AssertionError('unused cwd')):
            capture.update_observed_process(event, self.project, self.table[101], [8080], self.table)
        self.assertEqual(engine.service(item['id'])['spec']['ports'], [8080])

    def test_new_process_cwd_probe_remains_outside_mutation_lock(self):
        def probe(*args):
            with engine.mutation_lock():
                return str(self.root)
        with patch.object(capture, 'process_cwd', side_effect=probe) as cwd:
            capture.update_observed_process({'id': 'a', 'cwd': str(self.root)}, self.project,
                                             self.table[101], [8080], self.table)
        cwd.assert_called_once()
        self.assertEqual(len(engine.live_runtimes()), 1)

    def test_deleted_registration_is_deferred_until_new_cwd_can_be_read(self):
        self.service()
        original = engine.mutation_lock
        from contextlib import contextmanager
        @contextmanager
        def remove_before_lock():
            engine.write("DELETE FROM services WHERE id='local'")
            with original():
                yield
        with patch.object(engine, 'mutation_lock', remove_before_lock), \
                patch.object(capture, 'process_cwd', side_effect=AssertionError('probe under lock')):
            capture.update_observed_process({'id': 'a', 'cwd': str(self.root)}, self.project,
                                             self.table[101], [8080], self.table)
        self.assertEqual(engine.query('SELECT * FROM services'), [])


if __name__ == '__main__':
    unittest.main()
