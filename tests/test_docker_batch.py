"""Docker observation contract at the subprocess and transactional boundaries."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import engine
import observation

A, B, C, D = (ch * 64 for ch in 'abcd')
ZERO = '0001-01-01T00:00:00Z'
START = '2026-11-15T01:00:00Z'


def obj(cid, *, project=None, member=None, directory=None, file=None, running=True):
    labels = {'com.docker.compose.' + k: v for k, v in
              zip(('project', 'service', 'project.working_dir', 'project.config_files'),
                  (project, member, directory, file))}
    return {'Id': cid, 'State': {'Running': running, 'Status': 'running' if running else 'created',
                                'StartedAt': START if running else ZERO, 'FinishedAt': ZERO, 'ExitCode': 0,
                                'Error': '', 'Health': {'Status': None}},
            'Config': {'Labels': labels}, 'NetworkSettings': {'Ports': None}}


class BatchTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        scoped = patch.object(engine, 'ROOT', self.root)
        scoped.start()
        self.addCleanup(scoped.stop)
        self.project = engine.project(self.root)

    def container(self, name, cid):
        return engine.record_spec(name, self.project, name, 'container', {'container_id': cid})

    def compose(self, name, *, env=None, cwd=None):
        spec = dict(compose_project='batch', file=str(self.root/'compose.yaml'), cwd=cwd or str(self.root),
                    member=name, env=env or {})
        return engine.record_spec(name, self.project, name, 'compose', spec)

    def test_partial_inspect_retains_verified_neighbor_and_confirms_missing_only_with_list(self):
        with patch.object(engine, 'docker_probe_output', side_effect=[
                (1, ['invalid json', json.dumps(obj(A))]), (0, [C])]) as invoke:
            objects, errors = engine.docker_probe_objects({A,B,C}, time.monotonic()+4)
        self.assertEqual(list(objects), [A])
        self.assertEqual(errors, {B: engine.DOCKER_PROBE_MISSING, C: engine.DOCKER_PROBE_FAILURE})
        first, second = [call.args for call in invoke.call_args_list]
        self.assertEqual((first[0][0], second[0][0]), ('inspect','ps'))
        self.assertEqual(first[1], second[1])
        with patch.object(engine, 'docker_probe_output', side_effect=[(1, []), (1, [])]):
            _, errors = engine.docker_probe_objects({A,B}, time.monotonic()+4)
        self.assertEqual(set(errors.values()), {engine.DOCKER_PROBE_FAILURE})

    def test_conflicting_duplicate_wrong_id_and_bad_fields_do_not_erase_good_object(self):
        wrong = obj(B); wrong['State']['StartedAt'] = 'not a timestamp'
        with patch.object(engine, 'docker_probe_output', side_effect=[
                (0, [json.dumps(o) for o in (obj(A), obj(A, running=False), wrong, obj(C), obj(D))]),
                (0, [])]) as command:
            objects, errors = engine.docker_probe_objects({A,B,C},time.monotonic()+4)
        self.assertEqual(list(objects), [C])
        self.assertEqual(errors, dict.fromkeys((A,B), engine.DOCKER_PROBE_FAILURE))
        command.assert_called_once()

    def test_grouping_and_failed_compose_member_are_independent_of_other_groups(self):
        for member in ('one','two','empty'):
            self.compose(member)
        self.compose('different', env={'TEST_ENV': 'distinct value'})
        self.container('direct', D)
        versions = {name: engine.observation_version(name) for name in ('one','two','empty','different','direct')}
        matched = obj(A, project='batch', member='one', directory=str(self.root), file=str(self.root/'compose.yaml'))
        mismatch = obj(C, project='batch', member='two', directory=str(self.root/'another'), file=str(self.root/'compose.yaml'))

        def compose(spec, members, deadline):
            if spec['env']:
                self.assertEqual(members, {'different'})
                raise TimeoutError()
            self.assertEqual(members, {'one','two','empty'})
            return {'one':[A,B], 'two':[C], 'empty':[]}

        with ThreadPoolExecutor(max_workers=6) as pool, \
                patch.object(engine, 'compose_probe_members', side_effect=compose) as discover, \
                patch.object(engine, 'docker_probe_objects', return_value=({A:matched,C:mismatch,D:obj(D)},
                             {B:engine.DOCKER_PROBE_MISSING})) as inspect:
            result = observation.probe_docker_batch(pool, versions)
        self.assertEqual(discover.call_count, 2)
        self.assertEqual(inspect.call_count, 1)
        self.assertEqual(inspect.call_args.args[0], {A,B,C,D})
        self.assertIsNone(result['one'][0])
        self.assertIn('身份不匹配', result['two'][1])
        self.assertEqual(result['empty'], ((), None))
        self.assertEqual(result['different'], (None, engine.DOCKER_PROBE_TIMEOUT))
        self.assertEqual(result['direct'][0][0]['Id'], D)

    def test_invalid_compose_registration_does_not_stop_other_docker_work(self):
        self.container('direct', A)
        engine.record_spec('old', self.project, 'old', 'compose', {})
        versions = {sid: engine.observation_version(sid) for sid in ('direct','old')}
        with ThreadPoolExecutor(max_workers=6) as pool, \
                patch.object(engine,'docker_probe_objects',return_value=({A:obj(A)}, {})):
            result = observation.probe_docker_batch(pool, versions)
        self.assertIsNone(result['old'][0])
        self.assertEqual(result['direct'][0][0]['Id'], A)

    def test_worker_preserves_history_and_does_not_use_batch_in_explicit_operations(self):
        self.container('live', A)
        self.container('missing', B)
        run = engine.new_run('missing', {'container_ids':[B]}, started=1)
        engine.set_run(run, state='running')
        with patch.object(engine,'docker_probe_output', side_effect=[(1,[json.dumps(obj(A))]),(0,[A])]) as calls:
            self.assertEqual(observation.observe_due(visible=True), 2)
        self.assertEqual(calls.call_count, 2)
        self.assertEqual(engine.cached_observation('live')['state'], 'running')
        self.assertEqual(engine.cached_observation('missing')['state'], 'unknown')
        self.assertIsNone(engine.latest('missing')['ended'])
        with patch.object(engine, 'docker_probe_objects', side_effect=AssertionError('batch used for refresh')), \
                patch.object(engine, 'containers_for', return_value=[obj(A)]) as current:
            self.assertEqual(engine.observe('live')['state'], 'running')
        current.assert_called_once()

    def test_version_from_before_external_probe_cannot_overwrite_changes(self):
        for change in ('config','run','policy','delete'):
            with self.subTest(change=change):
                sid = 'test-' + change
                self.container(sid, A)
                old = engine.observation_version(sid, True)
                if change=='config':
                    engine.write('UPDATE services SET config_revision=config_revision+1 WHERE id=?', [sid])
                elif change=='run':
                    engine.new_run(sid, started=1)
                elif change=='policy':
                    for enabled in (False, True):
                        engine.dispatch({'action':'project_scan_policy', 'id':self.project['id'], 'enabled':enabled})
                else:
                    engine.write('DELETE FROM services WHERE id=?',[sid])
                    self.container(sid, B)
                prepared = engine.prepare_version(old, background=True, docker_result=((obj(A),), None))
                self.assertFalse(engine.finish_observation(sid,prepared,automatic=True)['state_fresh'])
                self.assertIsNone(engine.cached_observation(sid)['last_checked_at'])

    def test_output_timeout_and_size_are_bounded_for_the_actual_child(self):
        utility = self.root / 'emit'
        utility.write_text('#!/usr/bin/env python3\nimport sys,time\n'
                           'if sys.argv[1]=="wait": time.sleep(10)\n'
                           'else: print("a"*4096)\n')
        utility.chmod(0o700)
        with patch.object(engine, 'docker_executable', return_value=str(utility)), \
                patch.object(engine, 'DOCKER_PROBE_MAX_BYTES', 1024):
            started = time.monotonic()
            with self.assertRaises((TimeoutError, subprocess.TimeoutExpired)):
                engine.docker_probe_output(('wait',),started+.15)
            self.assertLess(time.monotonic()-started,2)
            with self.assertRaisesRegex(ValueError, '超限'):
                engine.docker_probe_output(('oversize',),time.monotonic()+2)

    def test_compose_parse_rejects_duplicate_and_truncated_identity(self):
        for lines in ([A+' one',A+' one'],[A[:12]+' one'],[A+' alien']):
            with patch.object(engine,'docker_probe_output',return_value=(0,lines)):
                with self.assertRaises(ValueError):
                    engine.compose_probe_members({'cwd':str(self.root),'file':'compose.yaml',
                                                  'compose_project':'batch'},{'one'},time.monotonic()+4)


if __name__ == '__main__':
    unittest.main()
