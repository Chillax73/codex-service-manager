"""Docker UI fixtures are registered immediately through the companion entry point."""
import json,subprocess,sys,time
from pathlib import Path
BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE/'backend'))
import engine
m=json.loads((BASE/'docs/evidence/ui-fixtures.json').read_text())
TAG='csm-ui-'+m['tag'];VOLUME=TAG+'-data';PROJECT=Path(m['projects']['a']);ROOT=Path(m['root'])

def cli(*args):
    p=subprocess.run([str(BASE/'scripts/csm'),*args],capture_output=True,text=True,timeout=90)
    data=json.loads(p.stdout)
    if not data['ok']:raise RuntimeError(data['error'])
    return data['result']

before={cid:{key:engine.container_inspect(cid)['State'][key] for key in ('Status','StartedAt')} for cid in engine.docker('ps','-aq').splitlines()}
engine.docker('volume','create','--label','csm.ui='+TAG,VOLUME)
cid=engine.docker('run','-d','--pull=never','--name',TAG+'-single','--label','csm.ui='+TAG,
    '-v',VOLUME+':/data','-p','127.0.0.1::8000','python:3.12-slim','python','-m','http.server','8000','--bind','0.0.0.0')
obj=engine.container_inspect(cid);port=obj['NetworkSettings']['Ports']['8000/tcp'][0]['HostPort']
item=cli('container',cid,'--name','UI Docker','--cwd',str(PROJECT),'--url','http://127.0.0.1:'+port)
engine.docker('exec',cid,'sh','-c','printf ui-marker > /data/ui-marker')
compose_file=ROOT/'compose.yaml';compose_file.write_text((BASE/'tests/fixtures/compose/compose.yaml').read_text())
group=cli('compose','--file',str(compose_file),'--project',TAG+'-compose','--cwd',str(PROJECT),'--env','CSM_TEST_VOLUME='+VOLUME)
data={'generated_at':time.time(),'tag':TAG,'volume':VOLUME,'container_id':cid,'service_id':item['id'],
      'group':group,'compose_file':str(compose_file),'project':str(PROJECT),'before':before}
(BASE/'docs/evidence/ui-docker-fixtures.json').write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(data,ensure_ascii=False))
