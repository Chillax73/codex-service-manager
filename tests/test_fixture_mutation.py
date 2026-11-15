"""Fixture lock contention is bounded and never retries an executed mutation."""
import contextlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import engine
from support import dispatch_fixture


class FixtureMutationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='csm-fixture-lock-')
        self.root = patch.object(engine, 'ROOT', Path(self.temp.name))
        self.root.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.root.stop)

    @contextlib.contextmanager
    def holder(self, seconds):
        script = ('import fcntl,sys,time\n'
                  'with open(sys.argv[1], "a") as lock:\n'
                  ' fcntl.flock(lock, fcntl.LOCK_EX)\n'
                  ' print("locked", flush=True)\n'
                  ' time.sleep(float(sys.argv[2]))\n')
        child = subprocess.Popen([sys.executable, '-c', script,
                                  str(engine.ROOT / 'mutation.lock'), str(seconds)],
                                 stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'locked')
            yield
        finally:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)
            child.stdout.close()

    def test_fixture_waits_for_real_process_lock_and_dispatches_once(self):
        original = engine.mutation_lock
        with self.holder(.15), patch.object(engine, 'launch', return_value={'id': 'fixture'}) as launch:
            with self.assertRaises(engine.MutationBusy):
                engine.dispatch({'action': 'launch'})
            launch.assert_not_called()
            self.assertEqual(dispatch_fixture({'action': 'launch'}, lock_wait=2), {'id': 'fixture'})
            launch.assert_called_once()
        self.assertIs(engine.mutation_lock, original)

    def test_persistent_contention_remains_a_failure(self):
        with self.holder(5), patch.object(engine, 'launch') as launch:
            with self.assertRaises(engine.MutationBusy):
                dispatch_fixture({'action': 'launch'}, lock_wait=.05)
            launch.assert_not_called()

    def test_mutation_busy_after_entry_is_not_retried(self):
        with patch.object(engine, 'launch', side_effect=engine.MutationBusy('after entry')) as launch:
            with self.assertRaisesRegex(engine.MutationBusy, 'after entry'):
                dispatch_fixture({'action': 'launch'})
            launch.assert_called_once()

    def test_other_failures_are_not_retried(self):
        with patch.object(engine, 'launch', side_effect=ValueError('invalid')) as launch:
            with self.assertRaisesRegex(ValueError, 'invalid'):
                dispatch_fixture({'action': 'launch'})
            launch.assert_called_once()


if __name__ == '__main__':
    unittest.main(verbosity=2)
