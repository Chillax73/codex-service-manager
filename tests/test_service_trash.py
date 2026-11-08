"""Single-service trash tests using real SQLite and isolated local files."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import engine


class ServiceTrashTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        root = patch.object(engine, 'ROOT', self.root)
        root.start()
        self.addCleanup(root.stop)
        self.project = engine.project(self.root)

    def rpc(self, action, **req):
        return engine.dispatch({'action': action, **req})

    def service(self, sid, kind='task', **spec):
        return engine.record_spec(sid, self.project, sid, kind, {'env': {'TOKEN': 'private-trash'}, **spec})

    def run_file(self, sid):
        start = engine.timestamp() - 120 * 86400
        rid = engine.new_run(sid, started=start)
        engine.set_run(rid, state='succeeded', ended=start + 1)
        log = self.root / 'logs' / (rid + '.log')
        log.write_text('test history')
        return rid, log

    def delete(self, sid, age=90):
        trash = self.rpc('service_delete', id=sid)['trash_id']
        if age:
            key = 'deleted_service:' + trash
            data = json.loads(engine.query('SELECT value FROM metadata WHERE key=?', [key], True)['value'])
            data['deleted_at'] -= age * 86400
            engine.write('UPDATE metadata SET value=? WHERE key=?', [json.dumps(data), key])
        return trash

    def test_restore_existing_archive_with_history_and_without_signalling(self):
        old = self.service('fixture')
        rid, log = self.run_file('fixture')
        engine.write("INSERT INTO operations VALUES ('op','fixture','start','user',1,2,'succeeded',NULL)")
        tid = self.delete('fixture')
        listed = self.rpc('service_trash_list')
        self.assertEqual(listed[0]['id'], tid)
        self.assertNotIn('private-trash', json.dumps(listed))
        with patch.object(engine, 'operate') as operate:
            restored = self.rpc('service_restore', id=tid)
            operate.assert_not_called()
        self.assertEqual(restored['restored_runs'], 1)
        self.assertEqual(engine.service('fixture'), old)
        self.assertEqual(engine.latest('fixture')['id'], rid)
        self.assertEqual(self.rpc('logs', id='fixture', run_id=rid), log.read_text())
        self.assertEqual(engine.query('SELECT id FROM operations'), [{'id': 'op'}])
        self.assertEqual(self.rpc('service_trash_list'), [])

    def test_restore_conflicts_and_transaction_rollback(self):
        self.service('fixture')
        self.run_file('fixture')
        tid = self.delete('fixture')
        self.service('fixture')
        with self.assertRaisesRegex(RuntimeError, '重新登记'):
            self.rpc('service_restore', id=tid)
        engine.write('DELETE FROM services WHERE id=?', ['fixture'])
        engine.write("CREATE TRIGGER reject_run BEFORE INSERT ON runs BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with self.assertRaisesRegex(RuntimeError, '回滚'):
            self.rpc('service_restore', id=tid)
        self.assertFalse(engine.query('SELECT id FROM services'))
        self.assertEqual(self.rpc('service_trash_list')[0]['id'], tid)
        engine.write('DELETE FROM projects WHERE id=?', [self.project['id']])
        with self.assertRaisesRegex(RuntimeError, '原项目不存在'):
            self.rpc('service_restore', id=tid)
        self.assertEqual(self.rpc('service_trash_list')[0]['id'], tid)

    def test_restore_rejects_container_and_compose_identity_conflicts(self):
        for kind, spec in [('container', {'container_id': 'a' * 64}),
                           ('compose', {'compose_project': 'demo', 'member': 'web',
                                        'file': str(self.root / 'compose.yml')})]:
            with self.subTest(kind=kind):
                self.service(kind, kind, **spec)
                with patch.object(engine, 'containers_for', return_value=[]):
                    trash = self.delete(kind)
                self.service('new-' + kind, kind, **spec)
                with self.assertRaisesRegex(RuntimeError, '已登记'):
                    self.rpc('service_restore', id=trash)
                self.assertIn(trash, [item['id'] for item in self.rpc('service_trash_list')])

    def test_purge_only_confirmed_own_logs_and_retains_live_and_archive_references(self):
        self.service('old')
        _, own = self.run_file('old')
        rotated = Path(str(own) + '.2')
        rotated.write_text('rotated')
        _, missing = self.run_file('old')
        missing.unlink()
        _, linked = self.run_file('old')
        linked.unlink()
        outside = self.root / 'outside'
        outside.write_text('protected')
        linked.symlink_to(outside)
        rid_external, _ = self.run_file('old')
        engine.write('UPDATE runs SET log=? WHERE id=?', [str(outside), rid_external])
        _, referenced = self.run_file('old')
        self.service('live')
        live, live_log = self.run_file('live')
        engine.write('UPDATE runs SET log=? WHERE id=?', [str(referenced), live])
        _, project_ref = self.run_file('old')
        engine.write('INSERT INTO metadata VALUES (?,?)',
                     ['deleted_project:fixture', json.dumps({'runs': [{'log': str(project_ref)}]})])
        tid = self.delete('old')
        self.service('new')
        _, new_log = self.run_file('new')
        newer = self.delete('new', 0)
        self.rpc('cleanup', days=30)
        self.assertTrue(own.exists())
        self.assertEqual(self.rpc('service_trash_purge_preview', days=30)['ids'], [tid])
        with self.assertRaisesRegex(ValueError, '重新预览'):
            self.rpc('service_trash_purge', days=30, ids=[tid, newer])
        result = self.rpc('service_trash_purge', days=30, ids=[tid])
        self.assertEqual(result, dict(removed_archives=1, removed_logs=2, skipped_logs=4, failed_archives=0))
        self.assertFalse(own.exists() or rotated.exists())
        self.assertTrue(all(path.exists() for path in (outside, linked, referenced, live_log, project_ref, new_log)))
        self.assertIsNotNone(engine.latest('live'))
        self.assertEqual([item['id'] for item in self.rpc('service_trash_list')], [newer])
        self.assertIsNotNone(engine.query("SELECT key FROM metadata WHERE key='deleted_project:fixture'", one=True))

    def test_interrupted_purge_remains_retryable_and_cannot_restore(self):
        self.service('old')
        _, log = self.run_file('old')
        rotated = Path(str(log) + '.1')
        rotated.write_text('rotated')
        tid = self.delete('old')
        unlink = os.unlink
        def fail_on_rotation(name, **kwargs):
            if name.endswith('.1'):
                raise PermissionError('fixture')
            return unlink(name, **kwargs)
        with patch('service_trash.os.unlink', side_effect=fail_on_rotation):
            failed = self.rpc('service_trash_purge', days=30, ids=[tid])
        self.assertEqual(failed['failed_archives'], 1)
        self.assertFalse(log.exists())
        self.assertTrue(rotated.exists())
        self.assertEqual(self.rpc('service_trash_list')[0]['status'], 'purging')
        self.assertEqual(self.rpc('service_trash_purge_preview', days=365)['retry_count'], 1)
        with self.assertRaisesRegex(RuntimeError, '已开始清理'):
            self.rpc('service_restore', id=tid)
        result = self.rpc('service_trash_purge', days=365, ids=[tid])
        self.assertEqual(result['removed_archives'], 1)
        self.assertFalse(rotated.exists())


if __name__ == '__main__':
    unittest.main()
