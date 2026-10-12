"""Only tagged services and isolated projects for actual native UI acceptance."""
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid
BASE=Path(__file__).resolve().parents[1]
from support import prepare_workspace
prepare_workspace()
TAG=uuid.uuid4().hex[:8]
ROOT=BASE/'.test-state'/('ui-'+TAG)
A=ROOT/'界面验收-A';B=ROOT/'界面验收-B'
A.mkdir(parents=True);B.mkdir()
services={}

def port():
    with socket.socket() as s:
        s.bind(('127.0.0.1',0));return s.getsockname()[1]

def launch(key,name,cwd,argv,options=()):
    sid='ui-'+TAG+'-'+key
    p=subprocess.run([str(BASE/'scripts/csm'),'launch','--id',sid,'--name',name,'--cwd',str(cwd),*options,'--',*argv],
        text=True,capture_output=True,timeout=25)
    result=json.loads(p.stdout)
    if not result['ok']:raise RuntimeError(result['error'])
    item=result['result'];services[key]={'id':sid,'project_id':item['project_id'],'name':name,'cwd':str(cwd),'run_id':item['run']['id']}
    return item

for key,name,cwd in [('web','UI 前端',A),('keep','UI 保留服务',A),('other','UI 其他项目',B)]:
    number=port();url='http://127.0.0.1:'+str(number)
    launch(key,name,cwd,[sys.executable,str(BASE/'validation/fixtures/http_service.py'),'--port',str(number)],
        ['--url',url,'--port',str(number),'--env','POC_LABEL='+key])
    services[key].update(port=number,url=url)
launch('stubborn','UI 停止超时',A,[sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print("stubborn-ready",flush=True);time.sleep(3600)'])
launch('task','UI 历史日志',A,[sys.executable,'-c','print("ui-task-output",flush=True)'],['--task'])
manifest={'generated_at':time.time(),'tag':TAG,'root':str(ROOT),'projects':{'a':str(A),'b':str(B)},'services':services}
(BASE/'docs/evidence/ui-fixtures.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(manifest,ensure_ascii=False))
