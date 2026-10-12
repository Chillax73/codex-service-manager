"""Native OS lifecycle proof using a disposable app; not a substitute for real Codex quit."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

BASE=Path(__file__).resolve().parents[1]
from support import prepare_workspace
prepare_workspace()
sys.path.insert(0,str(BASE/'backend'))
import engine
engine.ROOT=BASE/'.test-state/watcher'
REPORT=BASE/'docs/evidence/watcher-tests.json'
mode=sys.argv[1]


def evidence(name,passed,details):
    report=json.loads(REPORT.read_text()) if REPORT.exists() else {'source':'actual disposable macOS app; real Codex quit remains separate','checks':[]}
    report['checks'].append({'name':name,'passed':bool(passed),'evidence':details})
    report['generated_at']=time.time();report['state_root']=str(engine.ROOT)
    REPORT.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(('PASS ' if passed else 'FAIL ')+name)
    if not passed:sys.exit(1)


if mode=='prepare':
    engine.database().close()
    REPORT.unlink(missing_ok=True)
    project=engine.ROOT/'auto-project';project.mkdir(exist_ok=True)
    manual=engine.ROOT/'manual-project';manual.mkdir(exist_ok=True)
    (engine.ROOT/'watcher-config.json').write_text(json.dumps({'bundle_ids':['local.csm.exit-fixture']}))
    for sid,cwd,keep in [('auto',project,False),('keep',project,True),('manual',manual,False)]:
        item=engine.dispatch({'action':'launch','id':sid,'name':sid,'cwd':str(cwd),'argv':[sys.executable,'-c','import time;time.sleep(600)']})
        engine.dispatch({'action':'service_keep','id':sid,'keep':keep})
        engine.dispatch({'action':'project_policy','id':item['project_id'],'manual':True,'codex_exit':cwd==project})
    subprocess.Popen([str(BASE/'build/Codex Service Manager.app/Contents/MacOS/ServiceManager'),'--watch','--root',str(engine.ROOT),
        '--engine',str(BASE/'backend/engine.py')],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    evidence('退出测试准备',True,{'ids':['auto','keep','manual'],'target_bundle':'local.csm.exit-fixture'})
elif mode=='closed':
    state=engine.snapshot()
    evidence('实际测试应用关闭窗口不触发停止',all(engine.observe(s)['state']=='running' for s in ('auto','keep','manual'))
        and bool(state['watcher']['instances']),{'watcher':state['watcher'],'states':{s:engine.observe(s)['state'] for s in ('auto','keep','manual')}})
elif mode=='quit':
    until=time.time()+15
    while time.time()<until and engine.observe('auto')['state']!='stopped':time.sleep(.2)
    state=engine.snapshot();states={s:engine.observe(s)['state'] for s in ('auto','keep','manual')}
    evidence('实际测试应用终止后独立观察器执行策略',states=={'auto':'stopped','keep':'running','manual':'running'}
        and state['watcher']['alive'] and not state['watcher']['instances'],{'states':states,'watcher':state['watcher'],
            'operations':state['operations']})
elif mode=='cleanup':
    for sid in ('auto','keep','manual'):engine.dispatch({'action':'force','id':sid})
    watcher=engine.snapshot()['watcher']
    if engine.matches(watcher['pid'],watcher['birth'],'--watch'):os.kill(watcher['pid'],signal.SIGTERM)
    evidence('测试服务与测试观察器清理',all(engine.observe(s)['run']['ended'] is not None for s in ('auto','keep','manual')),{'watcher_pid':watcher['pid']})
