import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import engine


class ServiceOrderingTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name).resolve()
        patch_root = patch.object(engine, 'ROOT', root)
        patch_root.start()
        self.addCleanup(patch_root.stop)
        self.project = engine.project(root)

    def register(self, sid, created, state, project=None):
        with patch.object(engine, 'timestamp', return_value=created):
            engine.record_spec(sid, project or self.project, sid, 'process', {})
        run = engine.new_run(sid, {'supervisor_pid': 1})
        engine.set_run(run, state=state, ended=None if state in engine.ACTIVE else 100)

    def test_runtime_and_timestamp_sort_independent_of_name(self):
        other = engine.ROOT / 'other'
        other.mkdir()
        self.register('run-old', 10, 'running')
        self.register('run-new', 20, 'running', engine.project(other))
        self.register('b-tie', 30, 'stopped')
        self.register('a-tie', 30, 'failed')
        self.register('starting', 35, 'starting')
        self.register('stopping', 40, 'stopping')
        self.register('unknown-date', 25, 'stopped')
        engine.write("UPDATE services SET created_at=NULL WHERE id='unknown-date'")
        expected = ['run-new', 'run-old', 'stopping', 'starting', 'a-tie', 'b-tie', 'unknown-date']
        with patch.object(engine, 'matches', return_value=True):
            self.assertEqual([item['id'] for item in engine.snapshot()['services']], expected)
        engine.dispatch({'action': 'project_scan_policy', 'id': self.project['id'], 'enabled': False})
        with patch.object(engine, 'matches', return_value=True):
            self.assertEqual([item['id'] for item in engine.snapshot()['services']], expected)

    def test_alias_survives_repeat_registration_and_keeps_identity(self):
        self.register('service', 10, 'stopped')
        before = engine.latest('service')
        engine.dispatch({'action': 'service_rename', 'id': 'service', 'name': '我常用的服务'})
        engine.record_spec('service', self.project, '更新后的来源名', 'process', {})
        result = engine.snapshot()['services'][0]
        self.assertEqual((result['name'], result['id'], result['created_at']), ('我常用的服务', 'service', 10))
        self.assertEqual(engine.latest('service'), before)


if __name__ == '__main__':
    unittest.main()
