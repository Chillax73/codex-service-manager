import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'backend'))
import engine


class ProjectOrderingTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name).resolve()
        root_patch = patch.object(engine, 'ROOT', root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.projects = {}
        for key, name, created in [('a', 'Ｚ', 10), ('b', 'a', 20), ('c', '中文', 30), ('d', 'a', 20)]:
            path = root / key
            path.mkdir()
            with patch.object(engine, 'timestamp', return_value=created):
                self.projects[key] = engine.project(path, name)
        engine.write('UPDATE projects SET created_at=NULL WHERE id=?', [self.projects['c']['id']])

    def ids(self, keys):
        return [self.projects[k]['id'] for k in keys]

    def pin(self, key, pinned=True):
        return engine.dispatch({'action': 'project_pin', 'id': self.projects[key]['id'], 'pinned': pinned})

    def ordered(self, key, direction):
        return [item['id'] for item in engine.dispatch(
            {'action': 'project_sort_preferences', 'sort_key': key, 'direction': direction})['projects']]

    def test_pin_groups_ties_and_unknown_timestamp(self):
        ties = sorted(self.ids('bd'))
        self.assertEqual(self.ordered('name', 'asc'), ties + self.ids('ac'))
        self.pin('a')
        self.pin('c')
        self.assertEqual(self.ordered('name', 'desc'), self.ids('ca') + ties)
        self.assertEqual(self.ordered('created_at', 'asc'), self.ids('ac') + ties)
        self.assertEqual(self.ordered('created_at', 'desc'), self.ids('ac') + ties)
        self.pin('a', False)
        self.assertEqual(self.ordered('created_at', 'desc'), self.ids('c') + ties + self.ids('a'))

    def test_counts_use_cache_and_survive_independent_rpc(self):
        for key, state in [('a', 'stopping'), ('b', 'running'), ('c', 'unknown')]:
            engine.record_spec(key, self.projects[key], key, 'process', {})
            engine.write('INSERT INTO service_observations VALUES (?,?,?)', [key, 50, json.dumps({'state': state})])
        engine.dispatch({'action': 'project_scan_policy', 'id': self.projects['b']['id'], 'enabled': False})
        with patch.object(engine, 'probe_observation', side_effect=AssertionError('ordering triggered a probe')):
            self.assertEqual(self.ordered('running_count', 'desc')[0], self.projects['b']['id'])
            self.assertEqual(self.ordered('running_count', 'asc')[-1], self.projects['b']['id'])
            self.assertEqual(self.pin('b')['projects'][0]['running_count'], 1)
            self.assertEqual(self.pin('b')['projects'][0]['last_checked_at'], 50)
        command = [sys.executable, str(BASE / 'backend/engine.py'), '--root', str(engine.ROOT), 'rpc']
        reply = json.loads(subprocess.check_output(command, input=b'{"action":"snapshot"}'))['result']
        self.assertEqual(reply['projects'][0]['id'], self.projects['b']['id'])
        self.assertEqual(reply['project_sort_preferences'], {'sort_key': 'running_count', 'direction': 'asc'})

    def test_invalid_preferences_do_not_replace_saved_values(self):
        with self.assertRaises(ValueError):
            self.ordered('created_at', 'reverse')
        self.assertEqual(engine.project_sort_preferences(), {'sort_key': 'name', 'direction': 'asc'})
        with patch.object(engine, 'ROOT', engine.ROOT / 'other-state'):
            self.assertEqual(engine.project_sort_preferences(), {'sort_key': 'name', 'direction': 'asc'})
            self.assertFalse(engine.project_sidebar()['projects'])


if __name__ == '__main__':
    unittest.main()
