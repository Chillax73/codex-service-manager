import json
from unittest.mock import Mock
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import capture
import engine


class ServiceSettingsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        root = patch.object(engine, 'ROOT', self.root)
        root.start()
        self.addCleanup(root.stop)
        self.project = engine.project(self.root)

    def rpc(self, action, sid='s', **request):
        return engine.dispatch({'action': action, 'id': sid, **request})

    def register(self, kind='task'):
        return engine.record_spec('s', self.project, '源名称', kind,
                                  {'cwd': str(self.root), 'argv': [sys.executable, '-c', 'pass'],
                                   'env': {'CSM_TEST_KEEP': 'private-keep', 'CSM_TEST_REMOVE': 'private-remove'}})

    def test_save_validation_is_atomic_and_does_not_probe(self):
        initial = self.register()
        for change in ({'argv': []}, {'cwd': '/missing/test-path'}, {'url': 'file:///tmp/path'},
                       {'env': {'bad=key': 'value'}}, {'env': {'A': 'a'}, 'unset_env': ['A']}):
            with self.assertRaises(ValueError):
                self.rpc('service_configure', name='新名称', **change)
            self.assertEqual(engine.service('s'), initial)
        with patch.object(engine, 'probe_observation', side_effect=AssertionError('save called probe')):
            saved = self.rpc('service_configure', name='新名称', env={'CSM_TEST_NEW': 'private-new'},
                             unset_env=['CSM_TEST_REMOVE'], config_revision=0, url='')
        self.assertEqual(saved['configuration']['env_keys'], ['CSM_TEST_KEEP', 'CSM_TEST_NEW'])
        self.assertTrue(saved['has_pending_configuration'])
        self.assertEqual(engine.service('s')['spec'], initial['spec'])
        with self.assertRaisesRegex(RuntimeError, '配置已被修改'):
            self.rpc('service_configure', config_revision=0, url='http://localhost')
        self.assertNotIn('private-', json.dumps(engine.snapshot()))
        self.assertEqual(self.rpc('history'), [])

    def test_container_edit_rejects_recreation_fields(self):
        self.register('container')
        for change in ({'command': 'other'}, {'cwd': str(self.root)}, {'env': {'A': 'a'}}):
            with self.assertRaisesRegex(ValueError, '服务类型'):
                self.rpc('service_configure', **change)
        saved = self.rpc('service_configure', name='容器别名', url='https://localhost/health')
        self.assertEqual(saved['name'], '容器别名')
        self.assertEqual(saved['configuration']['url'], 'https://localhost/health')
        self.assertIsNone(saved['spec']['url'])

    def test_running_configuration_applies_only_to_next_start(self):
        original = engine.dispatch({'action': 'launch', 'id': 's', 'name': '配置测试',
                                    'cwd': str(self.root), 'kind': 'task',
                                    'argv': [sys.executable, '-c', 'import time; time.sleep(30)'],
                                    'env': {'CSM_TEST_KEEP': 'private-keep', 'CSM_TEST_REMOVE': 'private-remove'}})
        self.addCleanup(self.stop_owned)
        target = self.root / 'next'
        target.mkdir()
        result_path = target / 'result.json'
        program = ("import json,os,pathlib; pathlib.Path('result.json').write_text(json.dumps("
                   "[os.getcwd(),os.getenv('CSM_TEST_KEEP'),os.getenv('CSM_TEST_REMOVE'),os.getenv('CSM_TEST_NEW')]))")
        saved = self.rpc('service_configure', argv=[sys.executable, '-c', program], cwd=str(target),
                         env={'CSM_TEST_NEW': 'private-new'}, unset_env=['CSM_TEST_REMOVE'], url='http://localhost:9')
        self.assertEqual(saved['run']['id'], original['run']['id'])
        self.assertEqual(saved['spec']['cwd'], str(self.root))
        self.assertIsNone(saved['spec']['url'])
        self.assertFalse(result_path.exists())
        self.rpc('stop')
        started = self.rpc('start')
        self.assertNotEqual(started['run']['id'], original['run']['id'])
        self.assertEqual(started['created_at'], original['created_at'])
        self.assertFalse(started['has_pending_configuration'])
        until = time.monotonic() + 5
        while not result_path.exists() and time.monotonic() < until:
            time.sleep(.05)
        self.assertEqual(json.loads(result_path.read_text()), [str(target), 'private-keep', None, 'private-new'])

    def stop_owned(self):
        try:
            self.rpc('stop')
        except (RuntimeError, ValueError):
            pass

    def test_start_failure_preserves_pending_without_exposing_values(self):
        initial = self.register()
        self.rpc('service_configure', argv=[str(self.root / 'private-command')],
                 env={'CSM_TEST_NEW': 'private-command'})
        with self.assertRaises(RuntimeError) as error:
            self.rpc('start')
        self.assertNotIn('private-command', str(error.exception))
        self.assertEqual(engine.service('s')['spec'], initial['spec'])
        self.assertTrue(engine.cached_observation('s')['has_pending_configuration'])
        self.assertNotIn('private-command', json.dumps(self.rpc('history')))
        self.assertNotIn('private-command', json.dumps(engine.query('SELECT * FROM operations')))

    def test_pre_spawn_errors_end_run_and_allow_retry(self):
        initial = self.register()
        self.rpc('service_configure', env={'CSM_TEST_NEW': 'private-spawn'})
        failure = OSError('private-spawn')
        for target, name in ((engine, 'open'), (engine.subprocess, 'Popen')):
            with self.subTest(name=name), patch.object(target, name, create=True, side_effect=failure):
                with self.assertRaisesRegex(RuntimeError, '请查看本机日志'):
                    self.rpc('start')
                run = engine.latest('s')
                self.assertEqual(run['state'], 'failed')
                self.assertIsNotNone(run['ended'])
                self.assertTrue(engine.cached_observation('s')['has_pending_configuration'])
                self.assertEqual(engine.service('s')['spec'], initial['spec'])
                self.assertNotIn('private-spawn', json.dumps(self.rpc('history')))
        def complete(*args, **kwargs):
            engine.set_run(engine.latest('s')['id'], state='succeeded', ended=engine.timestamp())
            return Mock()
        with patch.object(engine.subprocess, 'Popen', side_effect=complete):
            started = self.rpc('start')
        self.assertNotEqual(started['run']['id'], run['id'])
        self.assertFalse(started['has_pending_configuration'])
        self.assertEqual(len(self.rpc('history')), 3)

    def test_spawned_but_unconfirmed_run_cannot_be_restarted_or_signalled(self):
        self.register()
        with patch.object(engine.subprocess, 'Popen', return_value=Mock()), \
                patch.object(engine.time, 'monotonic', side_effect=[0, 0, 9]):
            with self.assertRaisesRegex(RuntimeError, '启动确认超时'):
                self.rpc('start')
        run = engine.latest('s')
        self.assertIsNone(run['ended'])
        self.assertEqual(run['state'], 'unknown')
        with patch.object(engine.os, 'kill') as kill:
            with self.assertRaisesRegex(RuntimeError, '拒绝启动第二个副本'):
                self.rpc('start')
            with self.assertRaisesRegex(RuntimeError, '监督进程身份无法确认'):
                self.rpc('stop')
            kill.assert_not_called()
        engine.set_run(run['id'], state='starting', error=None)
        self.assertEqual(engine.observe('s')['state'], 'unknown')
        self.assertIsNone(engine.latest('s')['ended'])

    def test_supervisor_exit_does_not_overwrite_a_concurrent_completion(self):
        self.register()
        def exited():
            engine.set_run(engine.latest('s')['id'], state='succeeded', ended=engine.timestamp(), exit_code=0)
            return 0
        with patch.object(engine.subprocess, 'Popen', return_value=Mock(poll=exited)):
            self.assertEqual(self.rpc('start')['state'], 'succeeded')
        self.assertEqual(engine.latest('s')['exit_code'], 0)

    def test_delete_rechecks_stale_preview_and_never_signals(self):
        self.register()
        self.assertTrue(self.rpc('service_delete_preview')['can_delete'])
        engine.dispatch({'action': 'project_scan_policy', 'id': self.project['id'], 'enabled': False})
        run = engine.new_run('s', {'supervisor_pid': 123, 'supervisor_birth': 'test'})
        engine.set_run(run, state='running')
        with patch.object(engine, 'matches', return_value=True), patch.object(engine.os, 'kill') as kill:
            self.assertEqual(engine.snapshot()['services'][0]['state'], 'never_started')
            with self.assertRaisesRegex(RuntimeError, '不能删除服务'):
                self.rpc('service_delete')
            kill.assert_not_called()
        self.assertEqual(engine.latest('s')['id'], run)

    def test_delete_rolls_back_and_preserves_other_project_and_log(self):
        self.register()
        other = self.root / 'other'
        other.mkdir()
        engine.record_spec('other', engine.project(other), '其他', 'process', {})
        run = engine.new_run('s')
        engine.set_run(run, state='succeeded', ended=1)
        log = Path(engine.latest('s')['log'])
        log.write_text('保留')
        engine.write("CREATE TRIGGER reject_service BEFORE DELETE ON services BEGIN SELECT RAISE(ABORT,'failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.rpc('service_delete')
        self.assertEqual(engine.latest('s')['id'], run)
        self.assertFalse(engine.query("SELECT key FROM metadata WHERE key LIKE 'deleted_service:%'"))
        engine.write('DROP TRIGGER reject_service')
        self.assertEqual(self.rpc('service_delete')['removed_runs'], 1)
        self.assertIsNone(engine.latest('s'))
        self.assertEqual(log.read_text(), '保留')
        self.assertEqual(engine.service('other')['name'], '其他')
        archive = engine.query("SELECT value FROM metadata WHERE key LIKE 'deleted_service:%'", one=True)
        self.assertEqual(json.loads(archive['value'])['runs'][0]['id'], run)

    def test_deleted_observation_can_be_registered_again(self):
        item = {'pid': 123, 'ppid': 1, 'birth': 'verified', 'name': 'python'}
        event = {'id': 'event', 'cwd': str(self.root), 'shell': item}
        capture.record_process(event, item, [8011], cwd=str(self.root), table={123: item})
        old = engine.query('SELECT id,created_at FROM services', one=True)
        old_run = engine.latest(old['id'])['id']
        with patch.object(engine, 'identity', return_value=None):
            self.rpc('service_delete', old['id'])
        engine.dispatch({'action': 'project_scan_policy', 'id': self.project['id'], 'enabled': False})
        with patch.object(capture, 'load_events', return_value=[event]), \
                patch.object(capture, 'process_table', return_value={123: item}) as table, \
                patch.object(capture, 'listeners', return_value={123: [8011]}), \
                patch.object(capture, 'process_cwd', return_value=str(self.root)):
            capture.collect_once({}, {})
            table.assert_not_called()
            self.assertFalse(engine.query('SELECT id FROM services'))
            engine.dispatch({'action': 'project_scan_policy', 'id': self.project['id'], 'enabled': True})
            capture.collect_once({}, {})
        recreated = engine.service(old['id'])
        self.assertGreater(recreated['created_at'], old['created_at'])
        self.assertNotEqual(engine.latest(old['id'])['id'], old_run)
        self.assertEqual(len(self.rpc('history', old['id'])), 1)


if __name__ == '__main__':
    unittest.main()
