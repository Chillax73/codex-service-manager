"""Exit-policy result persistence and heartbeat behavior in an isolated database."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import engine


class ExitPolicyTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.enter_root = patch.object(engine, 'ROOT', Path(directory.name).resolve())
        self.enter_root.start()
        self.addCleanup(self.enter_root.stop)
        project = engine.project(engine.ROOT)
        engine.dispatch({'action': 'project_policy', 'id': project['id'], 'manual': True, 'codex_exit': True})
        for sid in ('ok', 'keep', 'failure'):
            engine.record_spec(sid, project, sid, 'process', {'env': {'TOKEN': 'private-fixture'}})
        engine.dispatch({'action': 'service_keep', 'id': 'keep', 'keep': True})
        self.event = {'event_id': 'a' * 32, 'event_at': engine.timestamp()}

    def request(self, action, **extra):
        return engine.dispatch({'action': action, **self.event, **extra})

    def test_mixed_result_and_lost_reply_do_not_replay_batch(self):
        def stop(item, force):
            if item['id'] == 'failure':
                raise RuntimeError('private-fixture')
            return {'state': 'stopped'}
        with patch.object(engine, 'stop_process', side_effect=stop) as operation:
            result = self.request('codex_exit')
            self.assertEqual({row['result'] for row in result[0]['results']}, {'succeeded', 'skipped', 'failed'})
            self.assertFalse(self.request('codex_exit_unconfirmed'))
            self.assertEqual(self.request('codex_exit'), [])
            self.assertEqual(operation.call_count, 2)
        self.request('watcher_event', watcher={'updated': engine.timestamp()})
        # Read with a separate process, independently of the observer that wrote the result.
        data = json.loads(subprocess.check_output([sys.executable, engine.__file__, '--root', str(engine.ROOT), 'rpc'],
                                                 input=b'{"action":"snapshot"}'))['result']
        self.assertNotIn('private-fixture', json.dumps(data))
        summary = data['last_codex_exit']
        self.assertEqual([summary[key] for key in ('succeeded', 'skipped', 'failed')], [1, 1, 1])
        self.assertEqual(summary['failures'][0]['name'], 'failure')
        self.assertEqual(summary['skip_reasons'], [{'count': 1, 'reason': '持续运行例外'}])

    def test_busy_before_execution_allows_heartbeats_and_reports_unconfirmed(self):
        with engine.mutation_lock():
            response = subprocess.run([sys.executable, engine.__file__, '--root', str(engine.ROOT), 'rpc'],
                                      input=json.dumps({'action': 'codex_exit', **self.event}), text=True,
                                      capture_output=True)
            self.assertEqual(json.loads(response.stdout)['error_code'], 'mutation_busy')
            self.assertTrue(self.request('watcher_event', watcher={'updated': engine.timestamp()}))
            self.assertIsNone(engine.snapshot()['last_codex_exit'])
            self.assertTrue(self.request('codex_exit_unconfirmed', busy=True))
        self.assertEqual(engine.snapshot()['last_codex_exit']['status'], 'unconfirmed')
        self.assertFalse(engine.query('SELECT id FROM operations'))
        self.assertEqual(self.request('codex_exit'), [])


if __name__ == '__main__':
    unittest.main()
