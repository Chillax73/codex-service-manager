"""Early exits and rejected starts must have durable, nonduplicated diagnostics."""
import json
from pathlib import Path
import socket
import sys
import time
import uuid
BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE/'backend'))
import engine
TAG='csm-fail-test-'+uuid.uuid4().hex[:10]
engine.ROOT=BASE/'.test-state'/TAG
engine.database().close()
owned=[];checks=[]

def check(name,passed,evidence):
    checks.append({'name':name,'passed':bool(passed),'evidence':evidence})
    print(('PASS ' if passed else 'FAIL ')+name,flush=True)

try:
    cid=engine.docker('run','-d','--pull=never','--name',TAG+'-exit','--label','csm.test='+TAG,
        'python:3.12-slim','python','-c','import sys;print("early-failure",file=sys.stderr);sys.exit(7)')
    owned.append(cid)
    engine.docker('wait',cid)
    item=engine.dispatch({'action':'register_container','container_id':cid,'cwd':str(BASE/'tests/fixtures')})
    history=engine.dispatch({'action':'history','id':item['id']})
    check('登记前已退出的容器仍保存失败运行',item['state']=='failed' and len(history)==1 and history[0]['exit_code']==7,
        {'state':item['state'],'history':history})
    native=engine.container_inspect(cid)['State']
    check('提前退出的容器保留真实起止时间',abs(history[0]['started']-engine.docker_time(native['StartedAt']))<.001 and
        abs(history[0]['ended']-engine.docker_time(native['FinishedAt']))<.001,
        {'started':history[0]['started'],'ended':history[0]['ended'],'native_started':native['StartedAt'],'native_ended':native['FinishedAt']})
    for _ in range(3):engine.observe(item['id'])
    check('重复快照不重复导入同次已退出运行',len(engine.dispatch({'action':'history','id':item['id']}))==1,{'id':item['id']})
    check('已退出容器 stderr 诊断可读取','early-failure' in engine.dispatch({'action':'logs','id':item['id']}),{'id':item['id']})
    with socket.socket() as blocker:
        blocker.bind(('127.0.0.1',0));blocker.listen(1);port=blocker.getsockname()[1]
        cid=engine.docker('create','--name',TAG+'-collision','--label','csm.test='+TAG,'-p','127.0.0.1:'+str(port)+':8000',
            'python:3.12-slim','python','-m','http.server','8000','--bind','0.0.0.0')
        owned.append(cid)
        item=engine.dispatch({'action':'register_container','container_id':cid,'cwd':str(BASE/'tests/fixtures')})
        check('仅创建未运行的容器不伪造历史',not engine.dispatch({'action':'history','id':item['id']}),{'native_state':engine.container_inspect(cid)['State']['Status']})
        failure=None
        try:engine.dispatch({'action':'start','id':item['id']})
        except RuntimeError as exc:failure=str(exc)
        state=engine.observe(item['id']);history=engine.dispatch({'action':'history','id':item['id']})
        check('Docker 端口启动失败保存运行与诊断',bool(failure) and state['state']=='failed' and len(history)==1 and bool(history[0]['error']),
            {'error':failure,'state':state['state'],'history':history})
    result=engine.dispatch({'action':'start','id':item['id']})
    check('解除端口冲突后启动产生独立运行',result['state']=='running' and len(engine.dispatch({'action':'history','id':item['id']}))==2,
        {'state':result['state'],'run_id':result['run']['id']})
    engine.dispatch({'action':'stop','id':item['id']})
except Exception as exc:
    check('执行容器失败验收',False,{'error':str(exc)})
finally:
    errors=[]
    for cid in owned:
        try:
            obj=engine.container_inspect(cid)
            if (obj['Config'].get('Labels') or {}).get('csm.test')!=TAG:raise RuntimeError('测试标签不匹配')
            engine.docker('rm','-f',cid)
        except Exception as exc:errors.append(str(exc))
    check('专属失败测试容器清理',not errors,{'errors':errors})
    (BASE/'docs/evidence/docker-failures.json').write_text(json.dumps({'generated_at':time.time(),'state_root':str(engine.ROOT),'checks':checks},ensure_ascii=False,indent=2)+'\n')
sys.exit(0 if checks and all(c['passed'] for c in checks) else 1)
