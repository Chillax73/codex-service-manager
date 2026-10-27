"""Acceptance-focused real-process tests in isolated state directories."""
import json
import os
from pathlib import Path
import socket
import signal
import concurrent.futures
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import engine

BASE = Path(__file__).resolve().parents[1]
from support import prepare_workspace
prepare_workspace()


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class EngineAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='csm-acceptance-', dir=str(BASE / '.test-state'))
        cls.root = Path(cls.temp.name)
        engine.ROOT = cls.root
        cls.a = cls.root / 'project-a'; cls.a.mkdir()
        cls.b = cls.root / 'project-b'; cls.b.mkdir()
        engine.database().close()

    @classmethod
    def tearDownClass(cls):
        for row in engine.query('SELECT id FROM services'):
            try:
                engine.dispatch({'action':'force', 'id':row['id']})
            except Exception:
                pass
        (BASE / 'docs/evidence/process-state-path.txt').write_text(str(cls.root) + '\n')
        # Keep SQLite/logs as authoritative diagnostics; tmp dir intentionally persists.
        cls.temp._finalizer.detach()

    def launch(self, name, argv=None, **kwargs):
        return engine.dispatch({'action':'launch', 'name':name, 'id':name,
            'cwd':str(self.a), 'argv':argv or [sys.executable,'-c','import time;time.sleep(300)'], **kwargs})

    def wait(self, sid, predicate, timeout=12):
        until = time.time() + timeout
        while time.time() < until:
            item = engine.observe(sid)
            if predicate(item):
                return item
            time.sleep(.1)
        self.fail('状态等待超时：' + sid + str(item))

    def stop(self, sid):
        return engine.dispatch({'action':'stop','id':sid})

    def test_01_lifecycle_tree_health_restart_env_and_recovery(self):
        number = free_port(); url = 'http://127.0.0.1:' + str(number)
        childfile = self.root / 'child.pid'
        item = self.launch('lifecycle', [sys.executable,str(BASE / 'validation/fixtures/http_service.py'),
            '--port',str(number),'--child-file',str(childfile)], url=url, ports=[number], env={'POC_LABEL':'preserved'})
        item = self.wait(item['id'],lambda s:s['health'] == 'available')
        self.assertEqual(item['spec']['cwd'],str(self.a))
        self.assertNotIn('env',item['spec'])
        with urllib.request.urlopen(url) as response:
            self.assertEqual(response.read(),b'poc-python:preserved')
        recovered = json.loads(subprocess.check_output([sys.executable,str(BASE / 'backend/engine.py'),'--root',str(self.root),'rpc'],
            input=json.dumps({'action':'snapshot'}).encode()))
        self.assertTrue(recovered['ok'])
        self.assertIn(item['run']['id'],[s['run']['id'] for s in recovered['result']['services']])
        child_pid = int(childfile.read_text())
        stopped = self.stop(item['id'])
        self.assertEqual(stopped['state'],'stopped')
        self.assertIsNone(engine.identity(child_pid))
        self.assertTrue(engine.port_free(number))
        second = engine.dispatch({'action':'restart','id':item['id']})
        second = self.wait(item['id'],lambda s:s['health'] == 'available')
        self.assertNotEqual(item['run']['id'],second['run']['id'])
        self.assertEqual(len(engine.dispatch({'action':'history','id':item['id']})),2)
        with urllib.request.urlopen(url) as response:
            self.assertEqual(response.read(),b'poc-python:preserved')
        self.stop(item['id'])

    def test_02_identity_mismatch_refuses_and_recovers(self):
        item = self.launch('identity')
        runtime = item['run']['runtime'].copy()
        engine.set_run(item['run']['id'],runtime={**runtime,'supervisor_birth':'mismatch'})
        try:
            self.assertEqual(engine.observe(item['id'])['state'],'unknown')
            self.assertFalse(engine.observe(item['id'])['can_stop'])
            with self.assertRaisesRegex(RuntimeError,'身份'):
                self.stop(item['id'])
            self.assertIsNotNone(engine.identity(runtime['child_pid']))
        finally:
            engine.set_run(item['run']['id'],runtime=runtime)
            self.stop(item['id'])

    def test_03_port_collision_does_not_claim_owner(self):
        number = free_port()
        owner = self.launch('port-owner',[sys.executable,'-m','http.server',str(number),'--bind','127.0.0.1'],url='http://127.0.0.1:'+str(number),ports=[number])
        self.wait('port-owner',lambda s:s['health']=='available')
        with self.assertRaisesRegex(RuntimeError,'端口'):
            self.launch('collision',ports=[number])
        collision = engine.observe('collision')
        self.assertEqual(collision['state'],'failed')
        self.assertEqual(collision['run']['runtime'],{})
        self.assertEqual(engine.observe(owner['id'])['state'],'running')
        self.stop(owner['id'])

    def test_04_missing_executable_records_failure(self):
        with self.assertRaises(RuntimeError):
            self.launch('missing',['/nonexistent-csm-command'])
        self.assertEqual(engine.observe('missing')['state'],'failed')
        self.assertIn('No such file',engine.observe('missing')['problem'])
        self.assertEqual(engine.query('SELECT result FROM operations WHERE target=?',['missing'],True)['result'],'failed')

    def test_05_process_and_health_are_separate(self):
        item = self.launch('unhealthy',url='http://127.0.0.1:'+str(free_port()))
        self.assertEqual(item['state'],'running')
        self.assertEqual(item['health'],'unavailable')
        self.stop(item['id'])

    def test_06_task_success_failure_cancel_rerun(self):
        for name,code,state in [('success',0,'succeeded'),('failure',7,'failed')]:
            item = self.launch(name,[sys.executable,'-c','import time,sys;time.sleep(.3);sys.exit('+str(code)+')'],kind='task')
            self.wait(name,lambda s:s['state']==state)
            engine.dispatch({'action':'restart','id':name})
            self.wait(name,lambda s:s['state']==state)
            self.assertEqual(len(engine.dispatch({'action':'history','id':name})),2)
            self.assertEqual(engine.latest(name)['exit_code'],code)
        self.launch('cancel',kind='task')
        self.assertEqual(self.stop('cancel')['state'],'cancelled')

    def test_07_duplicate_and_global_mutation_lock(self):
        item = self.launch('duplicate')
        with self.assertRaisesRegex(RuntimeError,'副本'):
            engine.dispatch({'action':'start','id':item['id']})
        with engine.mutation_lock():
            with self.assertRaisesRegex(RuntimeError,'另一个操作'):
                engine.dispatch({'action':'restart','id':item['id']})
        self.assertEqual(len(engine.dispatch({'action':'history','id':item['id']})),1)
        self.stop(item['id'])

    def test_08_stop_timeout_blocks_restart_then_explicit_force(self):
        self.launch('stubborn',[sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print("ready",flush=True);time.sleep(300)'])
        time.sleep(.3)
        with self.assertRaisesRegex(RuntimeError,'超时'):
            self.stop('stubborn')
        self.assertEqual(engine.observe('stubborn')['state'],'stop_failed')
        with self.assertRaisesRegex(RuntimeError,'超时'):
            engine.dispatch({'action':'restart','id':'stubborn'})
        self.assertEqual(len(engine.dispatch({'action':'history','id':'stubborn'})),1)
        self.assertEqual(engine.dispatch({'action':'force','id':'stubborn'})['state'],'stopped')

    def test_09_project_policy_keep_and_isolation(self):
        items = [self.launch('policy-main'),self.launch('policy-keep'),self.launch('policy-other',cwd=str(self.b))]
        project_id = items[0]['project_id']
        engine.dispatch({'action':'service_keep','id':'policy-keep','keep':True})
        engine.dispatch({'action':'project_policy','id':project_id,'manual':False,'codex_exit':False})
        self.assertEqual(engine.dispatch({'action':'end_project','project_id':project_id})[0]['result'],'skipped')
        engine.dispatch({'action':'codex_exit'})
        self.assertTrue(engine.observe('policy-main')['can_stop'])
        engine.dispatch({'action':'project_policy','id':project_id,'manual':True,'codex_exit':False})
        results = engine.dispatch({'action':'end_project','project_id':project_id})
        self.assertIn('skipped',[r['result'] for r in results])
        self.assertEqual(engine.observe('policy-main')['state'],'stopped')
        self.assertTrue(engine.observe('policy-keep')['can_stop'])
        self.assertTrue(engine.observe('policy-other')['can_stop'])
        engine.dispatch({'action':'start','id':'policy-main'})
        engine.dispatch({'action':'project_policy','id':project_id,'manual':False,'codex_exit':True})
        engine.dispatch({'action':'codex_exit'})
        self.assertEqual(engine.observe('policy-main')['state'],'stopped')
        self.assertTrue(engine.observe('policy-keep')['can_stop'])
        for item in ('policy-keep','policy-other'):
            self.stop(item)
        engine.dispatch({'action':'project_policy','id':project_id,'manual':True,'codex_exit':True})
        self.launch('policy-both')
        engine.dispatch({'action':'codex_exit'})
        self.assertEqual(engine.observe('policy-both')['state'],'stopped')

    def test_10_batch_deduplicates_and_partial_failure_is_recorded(self):
        item = self.launch('batch-live')
        result = engine.dispatch({'action':'batch','ids':[item['id'],item['id'],'missing-service'],'operation':'stop'})
        self.assertEqual(len(result),2)
        self.assertEqual({r['result'] for r in result},{'succeeded','failed'})
        self.assertTrue(engine.query('SELECT error FROM operations WHERE target=?',['missing-service'],True)['error'])

    def test_11_logs_rotate_and_cleanup_only_finished(self):
        self.launch('logs',[sys.executable,'-c','import sys;sys.stdout.write("x"*4000000)'],kind='task')
        item = self.wait('logs',lambda s:s['run'].get('ended') is not None)
        log = Path(item['run']['log'])
        self.assertLessEqual(sum(p.stat().st_size for p in log.parent.glob(log.name+'*')),3*engine.LOG_LIMIT)
        self.assertLessEqual(len(engine.dispatch({'action':'logs','id':'logs'})),65536)
        self.launch('cleanup-live')
        engine.dispatch({'action':'cleanup','days':1})
        self.assertTrue(engine.latest('cleanup-live'))
        self.assertTrue(engine.observe('cleanup-live')['can_stop'])
        self.stop('cleanup-live')
        self.assertEqual(self.root.stat().st_mode & 0o777,0o700)


    def test_12_actual_supervisor_loss_is_unknown_and_refuses_signals(self):
        item = self.launch('supervisor-loss')
        runtime = item['run']['runtime']
        self.assertTrue(engine.matches(runtime['supervisor_pid'],runtime['supervisor_birth'],item['run']['id']))
        os.kill(runtime['supervisor_pid'],signal.SIGKILL)
        try:
            self.wait(item['id'],lambda s:s['state']=='unknown')
            self.assertIsNotNone(engine.identity(runtime['child_pid']))
            with self.assertRaisesRegex(RuntimeError,'身份'):
                self.stop(item['id'])
        finally:
            # This test itself owns the child it launched. Production engine never bypasses its check.
            if engine.matches(runtime['child_pid'],runtime['child_birth']):
                os.killpg(runtime['pgid'],signal.SIGTERM)
            until=time.time()+5
            while engine.group_members(runtime['pgid']) and time.time()<until:time.sleep(.05)
            self.assertFalse(engine.group_members(runtime['pgid']))
            engine.set_run(item['run']['id'],ended=time.time(),state='failed',error='test-injected supervisor loss; test child cleaned')

    def test_13_two_independent_requests_create_only_one_run(self):
        request={'action':'launch','id':'race','name':'race','cwd':str(self.a),
                 'argv':[sys.executable,'-c','import time;time.sleep(300)']}
        def submit(_):
            proc=subprocess.run([sys.executable,str(BASE/'backend/engine.py'),'--root',str(self.root),'rpc'],
                input=json.dumps(request),capture_output=True,text=True,timeout=15)
            return json.loads(proc.stdout)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(submit,range(2)))
        self.assertEqual(sum(r['ok'] for r in results),1)
        self.assertEqual(len(engine.dispatch({'action':'history','id':'race'})),1)
        self.stop('race')

    def test_14_historical_logs_stay_attached_to_their_runs(self):
        first = self.launch('log-history',[sys.executable,'-c','print("first-run-output")'],kind='task')
        self.wait('log-history',lambda s:s['state']=='succeeded')
        second = self.launch('log-history',[sys.executable,'-c','print("second-run-output")'],kind='task')
        self.wait('log-history',lambda s:s['state']=='succeeded')
        self.assertNotEqual(first['run']['id'],second['run']['id'])
        self.assertEqual(engine.dispatch({'action':'logs','id':'log-history','run_id':first['run']['id']}).strip(),'first-run-output')
        self.assertEqual(engine.dispatch({'action':'logs','id':'log-history','run_id':second['run']['id']}).strip(),'second-run-output')
        self.assertEqual(engine.dispatch({'action':'logs','id':'log-history','run_id':engine.uid()}),'')


if __name__ == '__main__':
    (BASE / '.test-state').mkdir(exist_ok=True)
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(EngineAcceptance))
    evidence = {'generated_at':time.time(),'tests_run':result.testsRun,'failures':[(str(t),s) for t,s in result.failures],
                'errors':[(str(t),s) for t,s in result.errors], 'passed':result.wasSuccessful(),'state_root':str(EngineAcceptance.root)}
    (BASE / 'docs/evidence/process-tests.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n')
    sys.exit(0 if result.wasSuccessful() else 1)
