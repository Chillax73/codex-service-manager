"""Retention never deletes business files or the last known service state."""
import json
from pathlib import Path
import os
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import capture
import engine
import storage


class MaintenanceContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'state'
        self.previous = engine.ROOT
        engine.ROOT = self.root
        engine.database().close()
        self.proj = engine.project(self.temp.name)
        engine.record_spec('fixture', self.proj, 'fixture', 'task', {'cwd': self.temp.name, 'argv': ['true']})

    def tearDown(self):
        engine.ROOT = self.previous
        self.temp.cleanup()

    def run_record(self, age=90, ended=True):
        run = engine.new_run('fixture', started=engine.timestamp() - age * 86400)
        if ended:
            engine.set_run(run, ended=engine.timestamp() - age * 86400, state='succeeded')
        path = self.root / 'logs' / (run + '.log')
        path.write_text('fixture')
        return run, path

    def clean(self, days=30):
        return engine.dispatch({'action': 'cleanup', 'days': days})

    def test_old_runs_cleaned_but_active_and_latest_state_remain(self):
        old, path = self.run_record(age=100)
        rotated = Path(str(path) + '.1'); rotated.write_text('fixture')
        active, active_path = self.run_record(age=95, ended=False)
        latest, latest_path = self.run_record(age=90)
        self.assertEqual(self.clean()['removed_runs'], 1)
        self.assertFalse(path.exists() or rotated.exists())
        self.assertTrue(active_path.exists() and latest_path.exists())
        self.assertEqual(engine.latest('fixture')['id'], latest)
        self.assertIsNotNone(engine.query('SELECT id FROM runs WHERE id=?', [active], True))
        self.assertEqual(self.clean()['removed_runs'], 0)

    def test_recorded_business_path_is_not_deleted(self):
        old, path = self.run_record(age=100)
        business = Path(self.temp.name) / 'business.log'; business.write_text('keep')
        engine.write('UPDATE runs SET log=? WHERE id=?', [str(business), old])
        self.run_record()
        result = self.clean()
        self.assertEqual(result['skipped_logs'], 1)
        self.assertEqual(business.read_text(), 'keep')
        self.assertTrue(path.exists())

    def test_log_read_refuses_external_recorded_path(self):
        old, _ = self.run_record(age=100)
        business = Path(self.temp.name) / 'business.log'; business.write_text('keep')
        business.chmod(0o644)
        engine.write('UPDATE runs SET log=? WHERE id=?', [str(business), old])
        with self.assertRaisesRegex(RuntimeError, '日志路径'):
            engine.dispatch({'action': 'logs', 'id': 'fixture'})
        self.assertEqual(business.stat().st_mode & 0o777, 0o644)

    def test_log_symlink_does_not_delete_target(self):
        old, path = self.run_record(age=100)
        business = Path(self.temp.name) / 'business'; business.write_text('keep')
        path.unlink(); path.symlink_to(business)
        self.run_record()
        self.assertEqual(self.clean()['skipped_logs'], 1)
        self.assertTrue(path.is_symlink())
        self.assertEqual(business.read_text(), 'keep')

    def test_invalid_retention_rejected_without_changing_data(self):
        self.run_record(age=100); self.run_record()
        for days in (0, -1, '30', 1.5, True, 36501):
            with self.subTest(days=days), self.assertRaises(ValueError):
                self.clean(days)
        self.assertEqual(len(engine.dispatch({'action': 'history', 'id': 'fixture'})), 2)

    def test_old_capture_events_preserve_tracked_context_and_archive_logs(self):
        old = engine.timestamp() - 90 * 86400
        ids = [engine.uid() for _ in range(3)]
        for event_id in ids:
            capture.save_event({'id': event_id, 'created': old})
        engine.write('INSERT INTO metadata VALUES (?,?)',
                     ['capture_tracking', json.dumps({'123': {'event': ids[1]}})])
        outside = Path(self.temp.name) / 'event.json'; outside.write_text('{"created":0}')
        event_link = capture.event_dir() / (ids[2] + '.json')
        event_link.unlink(); event_link.symlink_to(outside)
        archived, archived_log = self.run_record(age=100)
        engine.dispatch({'action': 'project_delete', 'id': self.proj['id']})
        self.assertEqual(self.clean()['removed_events'], 1)
        self.assertFalse((capture.event_dir() / (ids[0] + '.json')).exists())
        self.assertTrue((capture.event_dir() / (ids[1] + '.json')).exists())
        self.assertTrue(event_link.is_symlink() and outside.exists() and archived_log.exists())
        self.assertEqual(len(engine.deleted_projects()), 1)

    def test_private_permissions_and_symlink_database_rejection(self):
        self.root.chmod(0o755)
        db = self.root / 'manager.sqlite'; db.chmod(0o644)
        engine.database().close()
        capture.save_event({'id': engine.uid(), 'created': engine.timestamp()})
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(db.stat().st_mode & 0o777, 0o600)
        for path in capture.event_dir().glob('*.json'):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        external = Path(self.temp.name) / 'external-event.json'
        external.write_text('{"private":"external-fixture"}')
        (capture.event_dir() / (engine.uid() + '.json')).symlink_to(external)
        self.assertEqual(len(capture.load_events()), 1)
        outside = Path(self.temp.name) / 'outside.sqlite'
        db.rename(outside); db.symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError, '文件身份'):
            engine.database()


if __name__ == '__main__':
    unittest.main(verbosity=2)
