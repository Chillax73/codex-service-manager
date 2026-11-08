import concurrent.futures
import contextlib
import json
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import capture
import engine


class MigrationTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        root_patch = patch.object(engine, 'ROOT', self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.dbfile = self.root / 'manager.sqlite'

    def legacy(self):
        with contextlib.closing(sqlite3.connect(self.dbfile)) as db, db:
            db.executescript(engine.BASE_SCHEMA)
            db.execute("INSERT INTO projects VALUES ('old', '/old', '旧项目', 0, 1)")
            db.execute("INSERT INTO projects VALUES ('empty', '/empty', '空项目', 1, 0)")
            db.execute("INSERT INTO services VALUES ('live', 'old', '服务', 'process', '{}', 1, NULL)")
            db.execute("INSERT INTO services VALUES ('empty-service', 'old', '无历史', 'task', '{}', 0, NULL)")
            for run, started in [('first', 11), ('latest', 22)]:
                db.execute("INSERT INTO runs(id,service_id,started,ended,state,log) VALUES "
                           "(?, 'live', ?, 25, 'stopped', '/old/old.log')", (run, started))

    def test_upgrade_is_concurrent_and_backfills_only_once(self):
        self.legacy()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            def read(_):
                with contextlib.closing(engine.database()) as db:
                    return db.execute('PRAGMA user_version').fetchone()[0]
            self.assertEqual(list(executor.map(read, range(16))), [engine.SCHEMA_VERSION] * 16)
        project = engine.project_record('old')
        self.assertEqual((project['auto_scan'], project['pinned'], project['created_at'],
                          project['created_at_source'], project['codex_exit']), (1, 0, 11, 'estimated', 1))
        self.assertEqual(engine.service('live')['created_at'], 11)
        self.assertIsNone(engine.project_record('empty')['created_at'])
        self.assertEqual(engine.service('empty-service')['created_at_source'], 'unknown')
        self.assertEqual(engine.latest('live')['log'], '/old/old.log')
        engine.write("DELETE FROM runs WHERE id='first'")
        self.assertEqual(engine.service('live')['created_at'], 11)
        self.assertEqual(engine.project_record('old')['created_at'], 11)
        self.assertEqual(stat.S_IMODE(self.dbfile.stat().st_mode), 0o600)

    def test_incomplete_upgrade_rolls_back(self):
        self.legacy()
        with patch.object(engine, 'SCHEMA_V1', engine.SCHEMA_V1 + '\nSELECT * FROM missing_table;'):
            with self.assertRaises(sqlite3.OperationalError):
                engine.database()
        with contextlib.closing(sqlite3.connect(self.dbfile)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 0)
            self.assertNotIn('created_at', [r[1] for r in db.execute('PRAGMA table_info(services)')])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM runs').fetchone()[0], 2)
        self.assertEqual(engine.service('live')['created_at'], 11)

    def test_registration_timestamps_stay_stable(self):
        with patch.object(engine, 'timestamp', return_value=123):
            project = engine.project(self.root)
            engine.record_spec('manual', project, '服务', 'task', {})
            with patch.object(capture, 'process_cwd', return_value=str(self.root)):
                capture.record_process({'cwd': str(self.root)}, {'pid': 123, 'birth': 'test', 'name': 'test'}, [])
            with patch.object(engine, 'reconcile_docker'):
                capture.record_container({'cwd': str(self.root)},
                                         {'Id': 'abcd', 'Name': '/test', 'Config': {'Labels': {}}})
        with patch.object(engine, 'timestamp', return_value=456):
            engine.record_spec('manual', project, '更名', 'task', {})
            self.assertEqual(engine.project(self.root)['created_at'], 123)
        self.assertEqual([(r['created_at'], r['created_at_source']) for r in
                          engine.query('SELECT created_at,created_at_source FROM services')],
                         [(123, 'registered')] * 3)

    def test_restore_legacy_archive_uses_historical_evidence(self):
        self.legacy()
        with contextlib.closing(sqlite3.connect(self.dbfile)) as db:
            db.row_factory = sqlite3.Row
            archive = {'project': dict(db.execute("SELECT * FROM projects WHERE id='old'").fetchone()),
                       'services': [dict(row) for row in db.execute('SELECT * FROM services')],
                       'runs': [dict(row) for row in db.execute('SELECT * FROM runs')], 'operations': []}
        engine.write("INSERT INTO metadata VALUES ('deleted_project:old', ?)", (json.dumps(archive),))
        engine.write("DELETE FROM projects WHERE id='old'")
        engine.write("DELETE FROM services WHERE project_id='old'")
        engine.write('DELETE FROM runs')
        engine.write("UPDATE metadata SET value=? WHERE key='project_sidebar_preferences'",
                     ('{"sort_key":"created_at","direction":"desc"}',))
        project = engine.dispatch({'action': 'project_restore', 'id': 'old'})['project']
        self.assertEqual((project['created_at'], project['pinned'], project['auto_scan']), (11, 0, 1))
        self.assertEqual(engine.service('live')['created_at'], 11)
        self.assertEqual(engine.query("SELECT value FROM metadata WHERE key='project_sidebar_preferences'", one=True)['value'],
                         '{"sort_key":"created_at","direction":"desc"}')


if __name__ == '__main__':
    unittest.main()
