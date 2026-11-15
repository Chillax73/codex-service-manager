"""SQLite-only replay of worker batches with fixed Docker probe results."""
from collections import Counter
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(sys.argv[1]).resolve()))
import engine
import observation

START = '2026-11-15T01:00:00Z'
END = '2026-11-15T01:01:00Z'
ZERO = '0001-01-01T00:00:00Z'


def replay(shape):
    with tempfile.TemporaryDirectory() as tmp:
        engine.ROOT = Path(tmp).resolve()
        project = engine.project(engine.ROOT)
        ctx = {}
        for i in range(12):
            sid = str(i)
            if shape == 'local' or (shape == 'mixed' and i < 6):
                engine.record_spec(sid,project,sid,'task',{})
                rid=engine.new_run(sid, started=1)
                engine.set_run(rid,state='succeeded',ended=2)
            else:
                cid=f'{i+1:064x}'
                engine.record_spec(sid,project,sid,'container',{'container_id':cid})
                if i%6 == 5:
                    continue
                running=i%3 != 0
                ctx[cid]={'Id':cid,'State':{'Running':running,'Status':'running' if running else 'exited',
                          'StartedAt':START,'FinishedAt':ZERO if running else END,'ExitCode':0,'Error':'',
                          'Health':{'Status':None}},'NetworkSettings':{'Ports':None}}

        def objects(ids, deadline):
            return {x:ctx[x] for x in ids if x in ctx}, {x:'missing' for x in ids if x not in ctx}

        original=sqlite3.connect
        finish=engine.finish_observation
        lock=threading.Lock()
        counts=Counter(); services={}; finish_scope=threading.local()

        class Tracked(sqlite3.Connection):
            def execute(self,sql,/,*args):
                if sql.startswith('UPDATE runs ') or sql.startswith('INSERT INTO service_observations('):
                    with lock: counts['run_update' if sql.startswith('UPDATE') else 'cache_write']+=1
                return super().execute(sql,*args)

        def connect(*args,**kwargs):
            role=threading.current_thread().name
            role='coordinator' if role=='MainThread' else role.split('_')[-1]
            with lock:
                counts['connections']+=1; counts['open-'+role]+=1
                sid=getattr(finish_scope,'sid',None)
                if sid is not None:
                    services[sid]=services.get(sid,0)+1
            kwargs.setdefault('factory',Tracked)
            return original(*args,**kwargs)

        def complete(sid, *args, **kwargs):
            finish_scope.sid=sid
            try:
                services.setdefault(sid,0)
                return finish(sid,*args,**kwargs)
            finally:
                finish_scope.sid=None

        with patch.object(engine,'docker_probe_objects',side_effect=objects):
            assert observation.observe_due(visible=True)==12
            rounds=[]
            for _ in range(8):
                engine.write('UPDATE service_observations SET checked_at=0')
                counts.clear();services.clear()
                before=time.process_time()
                with patch.object(sqlite3,'connect',side_effect=connect), \
                        patch.object(engine,'finish_observation',side_effect=complete):
                    assert observation.observe_due(visible=True)==12
                rounds.append({'cpu_ms':(time.process_time()-before)*1000,'counts':dict(counts),
                               'finish_connections_by_service':dict(services)})
        return rounds


for tag in ('local','docker','mixed'):
    print(json.dumps({'shape':tag,'rounds':replay(tag)}))
