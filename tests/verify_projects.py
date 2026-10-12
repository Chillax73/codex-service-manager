"""Real npm/Vite, Maven/Spring Boot and Python WSGI launch chains via desktop helper."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE / 'backend'))
import engine

parser = argparse.ArgumentParser()
parser.add_argument('--root',type=Path)
parser.add_argument('--keep',action='store_true')
args = parser.parse_args()
if args.root:
    engine.ROOT = args.root.resolve()
checks=[]
owned=[]


def check(name,passed,evidence):
    checks.append({'name':name,'passed':bool(passed),'evidence':evidence})
    print(('PASS ' if passed else 'FAIL ')+name,flush=True)


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));return sock.getsockname()[1]


def cli(cwd,*args):
    proc = subprocess.run([str(BASE / 'scripts/csm'),'--root',str(engine.ROOT),*args],cwd=cwd,
        capture_output=True,text=True,timeout=90)
    result=json.loads(proc.stdout)
    if not result['ok']:
        raise RuntimeError(result['error'])
    return result['result']


def wait(sid,timeout=90):
    until=time.time()+timeout
    while time.time()<until:
        item=engine.observe(sid)
        if item['health']=='available':return item
        if item['state']=='failed':raise RuntimeError(item['problem'] or str(item))
        time.sleep(.3)
    raise TimeoutError('项目未就绪：'+str(item))


try:
    for name,relative,argv,extra,path,expected in [
        ('Vite 前端','frontend',['/opt/homebrew/bin/npm','run','dev','--','--port','{port}'],{'VITE_CSM_LABEL':'persisted'},'/',b'CSM'),
        ('Spring 后端','spring',['/Applications/IntelliJ IDEA.app/Contents/plugins/maven/lib/maven3/bin/mvn','-o','spring-boot:run','-Dspring-boot.run.arguments=--server.address=127.0.0.1 --server.port={port}'],{'CSM_LABEL':'persisted'},'/health',b'spring:persisted'),
        ('Python 后端','python',['/usr/bin/python3','-m','server'],{'CSM_LABEL':'persisted'},'/',b'persisted')]:
        number=port();cwd=BASE / 'tests/fixtures' / relative
        argv=[arg.replace('{port}',str(number)) for arg in argv]
        env={**extra,'CSM_PORT':str(number)}
        url='http://127.0.0.1:'+str(number)+path
        options=[]
        for key,value in env.items():options+=['--env',key+'='+value]
        item=cli(cwd,'launch','--name',name,'--port',str(number),'--url',url,*options,'--',*argv)
        owned.append(item['id']);first=wait(item['id'])
        with urllib.request.urlopen(url) as response:body=response.read()
        check(name+'实际启动与自动项目归属',expected in body and first['spec']['cwd']==str(cwd),
            {'service_id':item['id'],'runtime':first['run']['runtime'],'url':url,'cwd':str(cwd),'argv':argv,'body':body.decode()[:300]})
        cli(cwd,'restart',item['id']);second=wait(item['id'])
        with urllib.request.urlopen(url) as response:body2=response.read()
        history=engine.dispatch({'action':'history','id':item['id']})
        check(name+'重启保留环境且生成独立记录',expected in body2 and first['run']['id'] != second['run']['id'] and len(history)>=2,
            {'first_run':first['run']['id'],'second_run':second['run']['id'],'history_count':len(history)})
        if not args.keep:
            cli(cwd,'stop',item['id'])
            check(name+'停止并释放监听端口',engine.port_free(number),{'port':number,'state':engine.observe(item['id'])['state']})
except Exception as exc:
    check('项目集成执行',False,{'error':str(exc)})
finally:
    if not args.keep or not all(c['passed'] for c in checks):
        for sid in owned:
            try:engine.dispatch({'action':'force','id':sid})
            except Exception as exc:check('清理 '+sid,False,{'error':str(exc)})
    (BASE / 'docs/evidence/project-tests.json').write_text(json.dumps({'generated_at':time.time(),'state_root':str(engine.ROOT),
        'checks':checks,'owned_service_ids':owned,'kept_for_ui':args.keep,'entry':'scripts/csm from actual desktop execution tool'},ensure_ascii=False,indent=2)+'\n')
sys.exit(0 if checks and all(c['passed'] for c in checks) else 1)
