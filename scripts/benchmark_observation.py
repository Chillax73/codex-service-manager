#!/usr/bin/env python3
"""Finite CPU benchmarks of idle deadlines and event parsing, without collectors."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'backend'))
import engine
import capture
from event_index import EventIndex

CHILD = '''
import json,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import engine,observation
engine.ROOT=Path(sys.argv[2])
now=float(sys.argv[3])
observation.due_services(True,False,None,now)
start=time.process_time()
for _ in range(60):
    assert observation.due_services(True,False,None,now)==[]
print(json.dumps({'cpu_ms_per_selection':(time.process_time()-start)*1000/60}))
'''


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--baseline-backend',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    report={'scheduler':[],'events':[]}
    for count in (260,1000,5000):
        with tempfile.TemporaryDirectory(prefix='csm-due-') as folder:
            engine.ROOT=Path(folder)
            project=engine.project(folder)
            now=time.time()
            with engine.database() as db:
                db.executemany("INSERT INTO services(id,project_id,name,kind,spec) VALUES (?,?,?,'task',?)",
                               [(str(i),project['id'],str(i),json.dumps({'argv':['true','x'*600]})) for i in range(count)])
                db.executemany('INSERT INTO service_observations VALUES (?,?,?)',
                               [(str(i),now,json.dumps({'state':'never_started','run':None})) for i in range(count)])
            row={'services':count,'rounds':[]}
            for variant in ('before','after','after','before'):
                backend=args.baseline_backend if variant=='before' else BASE/'backend'
                result=subprocess.check_output([sys.executable,'-c',CHILD,str(backend),folder,str(now)],text=True)
                row['rounds'].append({'variant':variant,**json.loads(result)})
            report['scheduler'].append(row)
    for count in (1000,5000):
        with tempfile.TemporaryDirectory(prefix='csm-events-') as folder:
            engine.ROOT=Path(folder)
            directory=capture.event_dir()
            for i in range(count):
                capture.save_event({'id':f'{i:032x}','shell':{'pid':i+1,'birth':'expired'},'cwd':folder})
            index=EventIndex(directory); index.refresh()
            start=time.process_time()
            for _ in range(5): assert len(capture.load_events())==count
            before=(time.process_time()-start)*1000/5
            start=time.process_time()
            for _ in range(5): index.refresh(); assert len(index)==count
            after=(time.process_time()-start)*1000/5
            index.retire(index.events({}),{}, {},capture.recent_docker_event,time.time())
            start=time.process_time()
            for _ in range(1000): assert not index.events({})
            idle=(time.process_time()-start)*1000/1000
            report['events'].append({'files':count,'full_reload_cpu_ms':before,'unchanged_stat_cpu_ms':after,
                                     'retired_selection_cpu_ms':idle,'persistent_files':len(list(directory.glob('*.json')))})
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2))


if __name__=='__main__': main()
