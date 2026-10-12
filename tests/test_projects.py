"""Real-process contracts for project display names and guarded record deletion."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from support import BASE, prepare_workspace
prepare_workspace()
sys.path.insert(0, str(BASE / 'backend'))
import engine


class ProjectManagement(unittest.TestCase):
    def setUp(self):
        self.previous = engine.ROOT
        self.temp = tempfile.TemporaryDirectory(prefix='csm-projects-', dir=BASE / '.test-state')
        engine.ROOT = self.root = Path(self.temp.name)
        self.a = self.root / 'project-a'; self.a.mkdir()
        self.b = self.root / 'project-b'; self.b.mkdir()

    def tearDown(self):
        for row in engine.query('SELECT id FROM services'):
            try:engine.dispatch({'action':'force', 'id':row['id']})
            except Exception:pass
        engine.ROOT = self.previous
        self.temp.cleanup()

    def launch(self, name, cwd=None, argv=None, kind='process'):
        return engine.dispatch({'action':'launch','name':name,'cwd':str(cwd or self.a),'kind':kind,
            'argv':argv or [sys.executable,'-c','import time; time.sleep(300)']})

    def finished(self, sid):
        until=time.time()+10
        while time.time()<until:
            item=engine.observe(sid)
            if item['run']['ended'] is not None:return item
            time.sleep(.05)
        self.fail('Owned task did not finish')

    def test_rename_running_project_preserves_identity_policy_and_reloaded_name(self):
        item=self.launch('live'); pid=item['project_id']
        engine.dispatch({'action':'project_policy','id':pid,'manual':False,'codex_exit':True})
        before=engine.observe(item['id'])
        result=engine.dispatch({'action':'project_rename','id':pid,'name':'  我的前端  '})
        self.assertEqual(result['name'],'我的前端');self.assertEqual(result['id'],pid)
        self.assertEqual((result['manual'],result['codex_exit']),(0,1))
        self.assertEqual(engine.project(self.a)['name'],'我的前端')
        after=engine.observe(item['id']);self.assertEqual(before['run']['id'],after['run']['id'])
        self.assertEqual(before['run']['runtime'],after['run']['runtime'])
        rpc=subprocess.check_output([sys.executable,str(BASE/'backend/engine.py'),'--root',str(self.root),'rpc'],
            input=json.dumps({'action':'snapshot'}).encode())
        self.assertEqual(json.loads(rpc)['result']['projects'][0]['name'],'我的前端')

    def test_name_validation_and_same_display_names_keep_directory_identity(self):
        a=engine.project(self.a);b=engine.project(self.b)
        for name in ('', '   ', 'x'*101, 'a\nb', 'a\x00b', 'a\u200bb', None, 123):
            with self.assertRaises(ValueError):engine.dispatch({'action':'project_rename','id':a['id'],'name':name})
        for proj in (a,b):engine.dispatch({'action':'project_rename','id':proj['id'],'name':'同名项目'})
        self.assertNotEqual(a['id'],b['id'])
        self.assertEqual(len(engine.snapshot()['projects']),2)
        with self.assertRaisesRegex(ValueError,'不存在'):engine.dispatch({'action':'project_rename','id':'missing','name':'valid'})

    def test_delete_empty_project_keeps_source_directory(self):
        project=engine.project(self.a); marker=self.a/'source.txt';marker.write_text('keep')
        preview=engine.dispatch({'action':'project_delete_preview','id':project['id']})
        self.assertTrue(preview['can_delete']);self.assertEqual(preview['service_count'],0)
        engine.dispatch({'action':'project_delete','id':project['id']})
        self.assertEqual(marker.read_text(),'keep')
        with self.assertRaisesRegex(ValueError,'不存在'):engine.dispatch({'action':'project_delete','id':project['id']})

    def test_delete_finished_history_isolated_and_logs_retained(self):
        task=self.launch('task',argv=[sys.executable,'-c','print("owned-log")'],kind='task')
        first=self.finished(task['id']);engine.dispatch({'action':'start','id':task['id']});second=self.finished(task['id'])
        other=self.launch('other',cwd=self.b);log=Path(first['run']['log'])
        preview=engine.dispatch({'action':'project_delete_preview','id':task['project_id']})
        self.assertEqual((preview['service_count'],preview['run_count']),(1,2));self.assertTrue(preview['can_delete'])
        result=engine.dispatch({'action':'project_delete','id':task['project_id']})
        self.assertEqual(result['removed_runs'],2)
        self.assertFalse(engine.query('SELECT * FROM runs WHERE service_id=?',[task['id']]))
        self.assertFalse(engine.query('SELECT * FROM operations WHERE target=?',[task['id']]))
        self.assertEqual(engine.observe(other['id'])['run']['id'],other['run']['id'])
        self.assertTrue(log.exists());self.assertTrue(self.a.is_dir())

    def test_running_keep_service_blocks_deletion_and_remains_live(self):
        item=self.launch('keep');engine.dispatch({'action':'service_keep','id':item['id'],'keep':True})
        self.assertFalse(engine.dispatch({'action':'project_delete_preview','id':item['project_id']})['can_delete'])
        with self.assertRaisesRegex(RuntimeError,'不能删除'):engine.dispatch({'action':'project_delete','id':item['project_id']})
        self.assertEqual(engine.observe(item['id'])['state'],'running')
        self.assertIsNotNone(engine.identity(item['run']['runtime']['child_pid']))

    def test_final_delete_rechecks_after_a_stale_preview(self):
        project=engine.project(self.a)
        self.assertTrue(engine.dispatch({'action':'project_delete_preview','id':project['id']})['can_delete'])
        item=self.launch('late')
        with self.assertRaisesRegex(RuntimeError,'不能删除'):engine.dispatch({'action':'project_delete','id':project['id']})
        self.assertEqual(engine.observe(item['id'])['state'],'running')

    def test_unknown_identity_blocks_without_signalling_child(self):
        item=self.launch('unknown');runtime=item['run']['runtime']
        engine.set_run(item['run']['id'],runtime={**runtime,'supervisor_birth':'mismatch'})
        try:
            preview=engine.dispatch({'action':'project_delete_preview','id':item['project_id']})
            self.assertEqual(preview['blockers'][0]['state'],'unknown')
            with self.assertRaisesRegex(RuntimeError,'不能删除'):engine.dispatch({'action':'project_delete','id':item['project_id']})
            self.assertIsNotNone(engine.identity(runtime['child_pid']))
        finally:engine.set_run(item['run']['id'],runtime=runtime)

    def test_actual_stop_timeout_blocks_delete_until_explicit_force(self):
        item=self.launch('stubborn',argv=[sys.executable,'-c',
            'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print("ready",flush=True);time.sleep(300)'])
        until=time.time()+5
        while 'ready' not in engine.dispatch({'action':'logs','id':item['id']}) and time.time()<until:time.sleep(.05)
        with self.assertRaisesRegex(RuntimeError,'超时'):engine.dispatch({'action':'stop','id':item['id']})
        with self.assertRaisesRegex(RuntimeError,'不能删除'):engine.dispatch({'action':'project_delete','id':item['project_id']})
        engine.dispatch({'action':'force','id':item['id']})
        engine.dispatch({'action':'project_delete','id':item['project_id']})

    def test_transaction_failure_rolls_back_all_dependent_records(self):
        item=self.launch('task',argv=[sys.executable,'-c','print("transaction")'],kind='task');self.finished(item['id'])
        before={table:engine.query('SELECT * FROM '+table) for table in ('projects','services','runs','operations')}
        engine.write("CREATE TRIGGER abort_project_delete BEFORE DELETE ON services BEGIN SELECT RAISE(ABORT,'injected database failure'); END")
        try:
            with self.assertRaises(sqlite3.IntegrityError):engine.dispatch({'action':'project_delete','id':item['project_id']})
            for table,rows in before.items():self.assertEqual(engine.query('SELECT * FROM '+table),rows)
            self.assertEqual(engine.deleted_projects(),[])
        finally:engine.write('DROP TRIGGER abort_project_delete')

    def test_restore_preserves_history_policy_and_private_configuration(self):
        item=engine.dispatch({'action':'launch','name':'restore','cwd':str(self.a),'kind':'task',
            'argv':[sys.executable,'-c','print("restore-output")'],'env':{'PROJECT_TEST_SECRET':'private-test-value'}})
        self.finished(item['id'])
        engine.dispatch({'action':'project_rename','id':item['project_id'],'name':'恢复的项目'})
        engine.dispatch({'action':'project_policy','id':item['project_id'],'manual':False,'codex_exit':True})
        engine.dispatch({'action':'service_keep','id':item['id'],'keep':True})
        result=engine.dispatch({'action':'project_delete','id':item['project_id']})
        snapshot=engine.snapshot()
        self.assertNotIn('private-test-value',json.dumps(snapshot))
        self.assertEqual(len(snapshot['deleted_projects']),1)
        restored=engine.dispatch({'action':'project_restore','id':result['trash_id']})
        self.assertEqual(restored['project']['name'],'恢复的项目')
        self.assertEqual((restored['project']['manual'],restored['project']['codex_exit']),(0,1))
        self.assertEqual(engine.latest(item['id'])['id'],item['run']['id'])
        self.assertEqual(engine.observe(item['id'])['state'],'succeeded')
        self.assertEqual(engine.service(item['id'])['spec']['env']['PROJECT_TEST_SECRET'],'private-test-value')
        self.assertTrue(engine.service(item['id'])['keep'])
        self.assertEqual(engine.snapshot()['deleted_projects'],[])
        self.assertEqual(engine.dispatch({'action':'logs','id':item['id']}).strip(),'restore-output')

    def test_restore_id_collision_rolls_back_without_touching_new_service(self):
        item=self.launch('original',argv=[sys.executable,'-c','print("original")'],kind='task');self.finished(item['id'])
        removed=engine.dispatch({'action':'project_delete','id':item['project_id']})
        replacement=engine.dispatch({'action':'launch','id':item['id'],'name':'replacement','cwd':str(self.b),
            'argv':[sys.executable,'-c','import time; time.sleep(300)']})
        with self.assertRaisesRegex(RuntimeError,'恢复已取消'):
            engine.dispatch({'action':'project_restore','id':removed['trash_id']})
        self.assertIsNone(engine.query('SELECT id FROM projects WHERE id=?',[item['project_id']],True))
        self.assertEqual(engine.observe(replacement['id'])['run']['id'],replacement['run']['id'])
        self.assertEqual(engine.observe(replacement['id'])['state'],'running')
        self.assertEqual(len(engine.deleted_projects()),1)

    def test_restore_rejects_new_project_at_same_directory(self):
        project=engine.project(self.a)
        deleted=engine.dispatch({'action':'project_delete','id':project['id']})
        replacement=engine.project(self.a,'new-name')
        with self.assertRaisesRegex(RuntimeError,'不会覆盖'):engine.dispatch({'action':'project_restore','id':deleted['trash_id']})
        self.assertEqual(engine.project(self.a)['name'],'new-name')
        self.assertEqual(len(engine.deleted_projects()),1)
        with self.assertRaisesRegex(ValueError,'不存在'):engine.dispatch({'action':'project_restore','id':'missing'})

    def test_snapshot_handles_a_service_deleted_after_id_read(self):
        item=self.launch('task',argv=[sys.executable,'-c','print("snapshot")'],kind='task');self.finished(item['id'])
        original=engine.observe;pending=[True]
        def observe_after_delete(sid):
            if sid==item['id'] and pending[0]:
                pending[0]=False;engine.dispatch({'action':'project_delete','id':item['project_id']})
            return original(sid)
        with patch.object(engine,'observe',side_effect=observe_after_delete):
            snapshot=engine.snapshot()
        self.assertEqual(snapshot['services'],[]);self.assertEqual(snapshot['projects'],[])

if __name__=='__main__':
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ProjectManagement))
    (BASE/'docs/evidence/project-management-tests.json').write_text(json.dumps({'tests_run':result.testsRun,
        'passed':result.wasSuccessful(),'failures':[(str(t),s) for t,s in result.failures],
        'errors':[(str(t),s) for t,s in result.errors]},ensure_ascii=False,indent=2)+'\n')
    sys.exit(0 if result.wasSuccessful() else 1)
