"""Unified adapter acceptance. Only uniquely labelled disposable resources are modified."""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

BASE=Path(__file__).resolve().parents[1]
from support import prepare_workspace
prepare_workspace()
sys.path.insert(0,str(BASE / 'backend'))
import engine

TAG='csm-app-test-'+uuid.uuid4().hex[:10]
engine.ROOT=BASE / '.test-state' / TAG
engine.database().close()
PROJECT=BASE / 'tests/fixtures'
VOLUME=TAG+'-volume'
checks=[];container=None;volume=False;group=None;alternate=None


def check(name,passed,evidence):
    checks.append({'name':name,'passed':bool(passed),'evidence':evidence})
    print(('PASS ' if passed else 'FAIL ')+name,flush=True)


def http_url(cid):
    obj=engine.container_inspect(cid)
    port=obj['NetworkSettings']['Ports']['8000/tcp'][0]['HostPort']
    return 'http://127.0.0.1:'+port


def snapshot_external():
    return {cid:engine.container_inspect(cid)['State'] for cid in engine.docker('ps','-aq').splitlines()}


before=snapshot_external()
try:
    engine.docker('image','inspect','python:3.12-slim')
    engine.docker('volume','create','--label','csm.test='+TAG,VOLUME);volume=True
    container=engine.docker('run','-d','--pull=never','--name',TAG,'--label','csm.test='+TAG,
        '-v',VOLUME+':/data','-p','127.0.0.1::8000','python:3.12-slim','python','-m','http.server','8000','--bind','0.0.0.0')
    engine.docker('exec',container,'sh','-c','echo single-marker > /data/single-marker')
    item=engine.dispatch({'action':'register_container','container_id':container,'cwd':str(PROJECT),'url':http_url(container)})
    sid=item['id'];first=item['run']['id']
    deadline=time.time()+10
    while engine.observe(sid)['health']!='available' and time.time()<deadline:time.sleep(.2)
    check('单容器自动登记身份和健康',engine.observe(sid)['health']=='available' and
        engine.observe(sid)['run']['runtime']['container_ids']==[container],{'service_id':sid,'run_id':first,'url':item['spec']['url']})
    log=engine.dispatch({'action':'logs','id':sid})
    check('单容器 stderr 请求日志可获取','GET / HTTP/' in log,{'log_tail':log[-500:]})
    engine.dispatch({'action':'restart','id':sid})
    second=engine.observe(sid)
    marker=engine.docker('exec',container,'cat','/data/single-marker')
    check('单容器重启产生新历史并保留数据',second['state']=='running' and second['run']['id']!=first and marker=='single-marker',
        {'run_id':second['run']['id'],'marker':marker})
    deadline=time.time()+10
    while engine.observe(sid)['health']!='available' and time.time()<deadline:time.sleep(.2)
    actual=engine.observe(sid)
    check('容器重启后的地址与健康使用当前绑定',actual['health']=='available' and actual['spec']['url']==http_url(container),
        {'configured_url':engine.service(sid)['spec']['url'],'observed_url':actual['spec']['url'],
         'current_url':http_url(container),'health':actual['health'],'binding_port':engine.service(sid)['spec'].get('url_binding_port')})
    engine.dispatch({'action':'stop','id':sid})
    check('单容器停止不删除容器或卷',engine.container_inspect(container)['State']['Status']=='exited' and
        bool(engine.docker('volume','inspect',VOLUME)),{'state':engine.observe(sid)['state']})
    registered=engine.dispatch({'action':'register_compose','file':str(PROJECT / 'compose/compose.yaml'),'cwd':str(PROJECT),
        'compose_project':TAG+'-group','env':{'CSM_TEST_VOLUME':VOLUME}})
    group=registered['group_id'];ids=registered['members']
    check('Compose 统一组与成员登记',len(ids)==2 and all(r['result']=='succeeded' for r in registered['results']),registered)
    alternate=PROJECT / 'compose' / (TAG+'-alternate.yaml')
    alternate.write_text((PROJECT / 'compose/compose.yaml').read_text())
    count_before=len(engine.query('SELECT id FROM services'))
    refused=None
    try:
        engine.dispatch({'action':'register_compose','file':str(alternate),'cwd':str(PROJECT),
            'compose_project':TAG+'-group','env':{'CSM_TEST_VOLUME':VOLUME}})
    except RuntimeError as exc:
        refused=str(exc)
    check('同名 Compose 配置冲突拒绝接管且不留下假成员',bool(refused) and '配置文件' in refused and
        len(engine.query('SELECT id FROM services'))==count_before,
        {'error':refused,'service_count_before':count_before,'service_count_after':len(engine.query('SELECT id FROM services'))})
    members=[engine.service(s) for s in ids]
    web=next(s for s in members if s['name']=='web');worker=next(s for s in members if s['name']=='worker')
    deadline=time.time()+15
    while any(engine.observe(s)['health']!='available' for s in ids) and time.time()<deadline:time.sleep(.3)
    check('Compose 原生健康状态可获取',all(engine.observe(s)['health']=='available' for s in ids),
        {s:engine.observe(s)['health'] for s in ids})
    engine.dispatch({'action':'stop','id':web['id']})
    check('停止的容器不复用过期健康结果',engine.observe(web['id'])['health']=='not_configured',
        {'state':engine.observe(web['id'])['state'],'health':engine.observe(web['id'])['health']})
    check('Compose 单成员操作隔离',engine.observe(web['id'])['state']=='stopped' and engine.observe(worker['id'])['state']=='running',
        {'web':engine.observe(web['id'])['state'],'worker':engine.observe(worker['id'])['state']})
    results=engine.dispatch({'action':'batch','group_id':group,'ids':ids,'operation':'restart'})
    worker_cid=engine.containers_for(worker)[0]['Id']
    marker=engine.docker('exec',worker_cid,'cat','/data/marker')
    url=http_url(worker_cid)
    deadline=time.time()+10
    while engine.health(url)!='available' and time.time()<deadline:time.sleep(.2)
    log=engine.dispatch({'action':'logs','id':worker['id']})
    check('Compose 成员 stderr 日志可获取','GET / HTTP/' in log,{'log_tail':log[-500:]})
    check('Compose 组/成员去重与数据保留',len(results)==2 and all(r['result']=='succeeded' for r in results) and marker=='round-one-marker',
        {'results':results,'marker':marker})
    counts={sid:len(engine.dispatch({'action':'history','id':sid})) for sid in ids}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _:engine.snapshot(),range(8)))
    check('并发快照不重复生成容器历史',counts=={sid:len(engine.dispatch({'action':'history','id':sid})) for sid in ids},counts)
    result=engine.dispatch({'action':'batch','group_id':group,'operation':'stop'})
    check('Compose 整组停止逐项反馈',len(result)==2 and all(engine.observe(s)['state']=='stopped' for s in ids),result)
except Exception as exc:
    check('Docker 集成执行',False,{'error':str(exc)})
finally:
    errors=[]
    if alternate:
        alternate.unlink(missing_ok=True)
    if group:
        try:
            member=engine.service(engine.query('SELECT id FROM services WHERE group_id=?',[group],True)['id'])
            engine.compose_command(member,'down','--timeout','3')
        except Exception as exc:errors.append(str(exc))
    if container:
        try:engine.docker('rm','-f',container)
        except Exception as exc:errors.append(str(exc))
    if volume:
        try:
            obj=json.loads(engine.docker('volume','inspect',VOLUME))[0]
            if obj['Labels'].get('csm.test')!=TAG:raise RuntimeError('测试卷标签不符')
            engine.docker('volume','rm',VOLUME)
        except Exception as exc:errors.append(str(exc))
    after=snapshot_external()
    check('测试资源清理且已有容器未受影响',not errors and before==after,{'errors':errors,'existing_count':len(before)})
    (BASE / 'docs/evidence/docker-tests.json').write_text(json.dumps({'generated_at':time.time(),'tag':TAG,'state_root':str(engine.ROOT),'checks':checks},ensure_ascii=False,indent=2)+'\n')
sys.exit(0 if checks and all(c['passed'] for c in checks) else 1)
