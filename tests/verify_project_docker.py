"""Real Docker project rename/delete/restore; only uniquely owned test resources."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
from unittest.mock import patch
from support import BASE, prepare_workspace, stop_test_watcher
prepare_workspace()
sys.path.insert(0,str(BASE/'backend'))
import engine
TAG='csm-project-test-'+uuid.uuid4().hex[:10]
engine.ROOT=BASE/'.test-state'/TAG
A=engine.ROOT/'project-a';B=engine.ROOT/'project-b'
A.mkdir(parents=True);B.mkdir()
(A/'source.txt').write_text('source-retained')
checks=[];owned=[];services=[];volume=None

def check(name,passed):
    checks.append({'name':name,'passed':bool(passed)})
    print(('PASS ' if passed else 'FAIL ')+name,flush=True)

def external_states():
    fields=('Status','Running','Paused','Restarting','OOMKilled','Dead','Pid','ExitCode','StartedAt','FinishedAt')
    states={}
    for cid in engine.docker('ps','-aq').splitlines():
        state=engine.container_inspect(cid)['State']
        states[cid]={key:state.get(key) for key in fields}
    return states

before=external_states()
try:
    engine.docker('image','inspect','python:3.12-slim')
    volume=engine.docker('volume','create','--label','csm.project-test='+TAG,TAG+'-volume')
    for index,cwd in enumerate((A,B)):
        argv=['run','-d','--pull=never','--label','csm.project-test='+TAG,'--name',TAG+'-'+str(index)]
        if index==0:argv+=['-v',volume+':/data']
        cid=engine.docker(*argv,'python:3.12-slim','python','-m','http.server','8000','--bind','0.0.0.0')
        owned.append(cid)
        raw=subprocess.check_output([str(BASE/'scripts/csm'),'--root',str(engine.ROOT),'container',cid,'--cwd',str(cwd)],text=True)
        result=json.loads(raw);assert result['ok'],result
        services.append(result['result'])
    target,other=services;project_id=target['project_id']
    native_name=engine.container_inspect(owned[0])['Name']
    engine.docker('exec',owned[0],'sh','-c','printf retained > /data/marker')
    renamed=engine.dispatch({'action':'project_rename','id':project_id,'name':'Docker 项目'})
    check('重命名保留真实容器身份与名称',renamed['name']=='Docker 项目' and engine.container_inspect(owned[0])['Name']==native_name)
    try:engine.dispatch({'action':'project_delete','id':project_id});refused=False
    except RuntimeError:refused=True
    check('运行中真实容器阻止删除',refused and engine.container_inspect(owned[0])['State']['Running'])
    engine.dispatch({'action':'stop','id':target['id']})
    with patch.dict(os.environ,{'DOCKER_HOST':'unix:///nonexistent-csm-project-test.sock'}):
        saved=os.environ.pop('DOCKER_CONTEXT',None)
        try:
            preview=engine.dispatch({'action':'project_delete_preview','id':project_id})
            try:engine.dispatch({'action':'project_delete','id':project_id});refused=False
            except RuntimeError:refused=True
            check('真实 Docker 连接失败阻止删除',refused and not preview['can_delete'] and preview['blockers'][0]['state']=='unknown')
        finally:
            if saved is not None:os.environ['DOCKER_CONTEXT']=saved
    removed=engine.dispatch({'action':'project_delete','id':project_id})
    check('删除停止项目保留容器和卷',not engine.container_inspect(owned[0])['State']['Running'] and json.loads(engine.docker('volume','inspect',volume))[0]['Name']==volume)
    check('删除隔离其他项目和源码',engine.container_inspect(owned[1])['State']['Running'] and engine.service(other['id'])['project_id']==other['project_id'] and (A/'source.txt').read_text()=='source-retained')
    engine.dispatch({'action':'project_restore','id':removed['trash_id']})
    check('恢复登记不启动真实容器',engine.project(A)['name']=='Docker 项目' and not engine.container_inspect(owned[0])['State']['Running'])
    engine.dispatch({'action':'start','id':target['id']})
    check('恢复后正常启动并保留卷数据',engine.docker('exec',owned[0],'cat','/data/marker')=='retained' and engine.container_inspect(owned[0])['Name']==native_name)
except Exception as exc:
    checks.append({'name':'Docker 项目管理执行','passed':False,'error':str(exc)})
    print('FAIL Docker 项目管理执行',flush=True)
finally:
    for item in services:
        try:engine.dispatch({'action':'stop','id':item['id']})
        except Exception:pass
    for cid in owned:
        obj=engine.container_inspect(cid)
        assert obj['Config']['Labels'].get('csm.project-test')==TAG
        engine.docker('rm','-f',cid)
    if volume:
        obj=json.loads(engine.docker('volume','inspect',volume))[0]
        assert obj['Labels'].get('csm.project-test')==TAG
        engine.docker('volume','rm',volume)
    stop_test_watcher(engine.ROOT)
    check('原有容器状态未变',before==external_states())
    (BASE/'docs/evidence/project-docker-tests.json').write_text(json.dumps({'generated_at':time.time(),'tag':TAG,
        'checks':checks,'test_state':str(engine.ROOT),'resources_cleaned':True},ensure_ascii=False,indent=2)+'\n')
sys.exit(0 if checks and all(item['passed'] for item in checks) else 1)
