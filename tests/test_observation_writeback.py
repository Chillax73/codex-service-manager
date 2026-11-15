"""Real SQLite checks for per-service transactions and connection ownership."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from test_docker_batch import A, B, obj, engine, observation


class ObservationWritebackTest(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root = Path(t.name).resolve()
        root = patch.object(engine, 'ROOT', self.root)
        root.start()
        self.addCleanup(root.stop)
        self.project = engine.project(self.root)

    def container(self, sid, cid):
        return engine.record_spec(sid, self.project, sid, 'container', {'container_id': cid})

    def prepare(self, sid, *objects):
        return engine.prepare_version(engine.observation_version(sid, automatic=True), background=True,
                                      docker_result=(objects, None))

    def history(self, sid):
        return engine.query('SELECT * FROM runs WHERE service_id=? ORDER BY started', [sid])

    def test_run_and_projection_rollback_together_other_service_stays_committed(self):
        self.container('one', A)
        self.container('two', B)
        initial = self.prepare('two', obj(B))
        engine.finish_observation('two', initial, automatic=True)
        before = engine.cached_observation('two')
        old_run = self.history('two')
        changed = obj(B)
        changed['State']['StartedAt'] = '2026-11-15T01:02:00Z'
        one = self.prepare('one', obj(A))
        two = self.prepare('two', changed)
        original_persist = engine.persist_observation

        def fail(db, sid, checked_at, projection):
            if sid == 'two':
                self.assertTrue(db.in_transaction)
                self.assertEqual(engine.latest(sid, db=db)['state'], 'running')
                self.assertNotEqual(engine.latest(sid, db=db)['id'], old_run[0]['id'])
                self.assertEqual(self.history(sid), old_run)
                raise sqlite3.IntegrityError('test write failure')
            return original_persist(db, sid, checked_at, projection)

        with engine.observation_write_context(automatic=True) as writer:
            with patch.object(engine, 'persist_observation', side_effect=fail):
                self.assertTrue(engine.finish_observation('one', one, True, writer=writer)['state_fresh'])
                with self.assertRaisesRegex(sqlite3.IntegrityError, 'test write failure'):
                    engine.finish_observation('two', two, True, writer=writer)
                self.assertFalse(writer.db.in_transaction)
            self.assertEqual(engine.cached_observation('two'), before)
            self.assertEqual(self.history('two'), old_run)
            self.assertEqual(len(self.history('one')), 1)
            self.assertTrue(engine.finish_observation('two', two, True, writer=writer)['state_fresh'])
            self.assertEqual(len(self.history('two')), 2)
        with self.assertRaises(sqlite3.ProgrammingError):
            writer.db.execute('SELECT 1')

    def test_unchanged_run_skips_update_but_checks_and_runtime_changes_persist(self):
        self.container('one', A)
        original = obj(A)
        first = engine.finish_observation('one', self.prepare('one', original), automatic=True)
        run = engine.latest('one')
        # Reorder keys in the stored JSON without changing its structure.
        engine.write('UPDATE runs SET runtime=? WHERE id=?',
                     [json.dumps(dict(reversed(list(run['runtime'].items())))), run['id']])
        with engine.observation_write_context(automatic=True) as writer, engine.read_context():
            statements = []
            writer.db.set_trace_callback(statements.append)
            repeat = engine.finish_observation('one', self.prepare('one', original), True, writer=writer)
            self.assertTrue(repeat['state_fresh'])
            self.assertGreater(repeat['last_checked_at'], first['last_checked_at'])
            self.assertFalse(any(sql.startswith('UPDATE runs ') for sql in statements))
            changed = obj(A)
            changed['State']['Health']['Status'] = 'unhealthy'
            result = engine.finish_observation('one', self.prepare('one', changed), True, writer=writer)
            self.assertEqual(result['health'], 'unavailable')
            self.assertEqual(result['run']['id'], run['id'])
            self.assertIn('unhealthy', engine.latest('one')['runtime']['native_health'])
            changed['State']['StartedAt'] = '2026-11-15T01:02:00Z'
            new = engine.finish_observation('one', self.prepare('one', changed), True, writer=writer)
            self.assertNotEqual(new['run']['id'], run['id'])
            self.assertEqual(len(self.history('one')), 2)

    def test_busy_writer_and_supervisor_competition_keep_fresh_history(self):
        self.container('one', A)
        engine.finish_observation('one', self.prepare('one', obj(A)), automatic=True)
        before = engine.cached_observation('one')
        prepared = self.prepare('one', obj(A))
        with closing(engine.database()) as other:
            other.execute('BEGIN IMMEDIATE')
            started = time.monotonic()
            out = engine.finish_observation('one', prepared, automatic=True)
            self.assertFalse(out['state_fresh'])
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(out['last_checked_at'], before['last_checked_at'])
            other.execute("UPDATE runs SET state='exited',ended=123 WHERE service_id='one'")
            other.commit()
        stale = engine.finish_observation('one', prepared, automatic=True)
        self.assertFalse(stale['state_fresh'])
        self.assertEqual(engine.latest('one')['ended'], 123)
        fresh = engine.finish_observation('one', self.prepare('one', obj(A)), automatic=True)
        self.assertTrue(fresh['state_fresh'])
        self.assertEqual(len(self.history('one')), 2)

    def test_same_id_restore_rejects_old_probe(self):
        engine.record_spec('task', self.project, 'task', 'task', {})
        captured = engine.prepare_observation('task', automatic=True, background=True)
        trash = engine.dispatch({'action':'service_delete', 'id':'task'})['trash_id']
        engine.dispatch({'action':'service_restore', 'id':trash})
        self.assertEqual(engine.service('task')['config_revision'], 1)
        self.assertFalse(engine.finish_observation('task', captured, automatic=True)['state_fresh'])
        self.assertIsNone(engine.cached_observation('task')['last_checked_at'])

        captured = engine.prepare_observation('task', automatic=True, background=True)
        trash = engine.dispatch({'action':'project_delete', 'id':self.project['id']})['trash_id']
        engine.dispatch({'action':'project_restore', 'id':trash})
        self.assertFalse(engine.finish_observation('task', captured, automatic=True)['state_fresh'])
        self.assertIsNone(engine.cached_observation('task')['last_checked_at'])

    def test_writer_cannot_cross_threads_roots_or_replaced_file(self):
        self.container('one', A)
        prepared = self.prepare('one', obj(A))
        replacement = self.root / 'replacement'
        with closing(engine.database()) as source, closing(sqlite3.connect(str(replacement))) as copied:
            source.backup(copied)
        with engine.observation_write_context(automatic=True) as writer:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(engine.finish_observation, 'one', prepared, True, writer=writer)
                with self.assertRaisesRegex(sqlite3.ProgrammingError, '跨线程'):
                    future.result()
            other = self.root / 'other'
            other.mkdir()
            with patch.object(engine, 'ROOT', other):
                self.container('one', A)
                self.assertFalse(engine.finish_observation('one', prepared, True, writer=writer)['state_fresh'])
                self.assertIsNone(engine.latest('one'))
            with engine.read_context():
                os.replace(replacement, self.root/'manager.sqlite')
                with self.assertRaises(engine.ObservationStoreChanged):
                    engine.query('SELECT * FROM services')
            self.assertFalse(engine.finish_observation('one', prepared, True, writer=writer)['state_fresh'])
        self.assertIsNone(engine.latest('one'))
        self.assertTrue(engine.finish_observation('one', self.prepare('one', obj(A)), True)['state_fresh'])

    def test_batch_reads_have_no_transaction_across_probes_and_close_connections(self):
        for i in range(12):
            sid = str(i)
            engine.record_spec(sid, self.project, sid, 'task', {})
        original_probe = engine.probe_observation
        opened = []
        opener = engine.database

        def probe(*args, **kwargs):
            self.assertFalse(engine._reads.current[1].in_transaction)
            return original_probe(*args, **kwargs)

        def connect():
            db = opener()
            opened.append(db)
            return db

        from observation_schedule import ensure
        ensure()
        with patch.object(engine, 'database', side_effect=connect), patch.object(engine, 'probe_observation', side_effect=probe):
            self.assertEqual(observation.observe_due(visible=True),12)
        self.assertLessEqual(len(opened),8)  # one coordinator read/write and at most six thread-owned reads
        for db in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                db.execute('SELECT 1')


if __name__ == '__main__':
    unittest.main()
