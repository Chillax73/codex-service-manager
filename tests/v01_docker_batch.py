"""V-01: disposable-resource contract for the Docker batch adapter."""
import argparse
import fcntl
from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

BASE = Path(__file__).resolve().parents[1]
IMAGE = 'python:3.12-slim'
COMPOSE_FIELDS = '{{.ID}} {{.Service}}'
INSPECT_FIELDS = (
    '{"id":{{json .Id}},"status":{{json .State.Status}},'
    '"health":{{with (index .State "Health")}}{{json .Status}}{{else}}null{{end}},'
    '"ports":{{json .NetworkSettings.Ports}},'
    '"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
    '"member":{{json (index .Config.Labels "com.docker.compose.service")}},'
    '"cwd":{{json (index .Config.Labels "com.docker.compose.project.working_dir")}},'
    '"file":{{json (index .Config.Labels "com.docker.compose.project.config_files")}}}'
)
FIXTURE = '''services:
  alpha:
    image: python:3.12-slim
    command: ["true"]
    labels: {"csm.test": "%s"}
  beta:
    image: python:3.12-slim
    command: ["true"]
    labels: {"csm.test": "%s"}
  empty:
    image: python:3.12-slim
    command: ["true"]
    labels: {"csm.test": "%s"}
'''


def run(argv, *, cwd=BASE, timeout=15, check=True):
    return subprocess.run(argv, cwd=cwd, timeout=timeout, text=True, capture_output=True, check=check)


def docker(*args, **kwargs):
    return run(['docker', *args], **kwargs)


def inspect(ids, **kwargs):
    return docker('inspect', '--type=container', '--format', INSPECT_FIELDS, *ids, **kwargs)


def rows(result):
    return [json.loads(line) for line in result.stdout.splitlines()]


def verify(output, worker=False):
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tag = 'csm-v01-' + uuid.uuid4().hex[:12]
    results = {}

    def expect(name, condition, **evidence):
        results[name] = dict(passed=bool(condition), **evidence)
        print(('PASS ' if condition else 'FAIL ') + name, flush=True)

    docker('image', 'inspect', '--format', '{{.Id}}', IMAGE)
    results['versions'] = {
        'docker': docker('version', '--format', '{{.Client.Version}} / {{.Server.Version}}').stdout.strip(),
        'compose': docker('compose', 'version', '--short').stdout.strip(),
    }
    with tempfile.TemporaryDirectory(prefix=tag, dir=output.parent) as scratch:
        project = Path(scratch).resolve()
        root = project / 'registry'
        root.mkdir(mode=0o700)
        watcher_lock = (root / 'watcher.lock').open('a')
        fcntl.flock(watcher_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        file = project / 'compose.yaml'
        file.write_text(FIXTURE % (tag, tag, tag))
        compose = ['compose', '-p', tag, '-f', str(file)]
        owner_filter = ('--filter', 'label=csm.test=' + tag)
        old_ids = []
        services = []

        def csm(*args):
            return json.loads(run([str(BASE / 'scripts/csm'), '--root', str(root), *args]).stdout)['result']

        def owned_ids():
            return docker('ps', '-aq', '--no-trunc', *owner_filter).stdout.splitlines()

        def owned(cid):
            value = docker('inspect', '--type=container', '--format',
                           '{{index .Config.Labels "csm.test"}}', cid)
            return value.stdout.strip() == tag

        def remove(cid):
            if not owned(cid):
                raise RuntimeError('容器所有权不匹配，拒绝删除')
            docker('rm', '-f', cid)

        try:
            for name in ('keep', 'missing'):
                cid = docker('create', '--name', tag+'-'+name, '--label', 'csm.test='+tag,
                             IMAGE, *(('sleep', '300') if name == 'missing' and worker else ('true',))).stdout.strip()
                old_ids.append(cid)
                services.append(csm('container', cid, '--cwd', str(project))['id'])
            live, missing = old_ids
            if worker:
                csm('start', services[1])
            remove(missing)
            begun = time.monotonic()
            mixed = inspect([missing, live], check=False, timeout=4)
            reused = rows(mixed)
            remain = 4 - (time.monotonic() - begun)
            listing = docker('ps', '-aq', '--no-trunc', timeout=remain)
            expect('部分失败保留完整有效对象', mixed.returncode != 0
                   and [row['id'] for row in reused] == [live] and missing not in listing.stdout.splitlines(),
                   exit=mixed.returncode, stdout_rows=len(reused), supplement_count=1,
                   elapsed_ms=round((time.monotonic()-begun)*1000))
            expect('独立容器字段缺省可表示', len(reused)==1 and reused[0]['status']=='created'
                   and all(reused[0][k] is None for k in ('health','project','member','cwd','file')))
            nothing = inspect([missing, uuid.uuid4().hex + uuid.uuid4().hex], timeout=4, check=False)
            expect('全部缺失返回非零且无行', nothing.returncode != 0 and not nothing.stdout.strip(),
                   exit=nothing.returncode, stdout_rows=len(nothing.stdout.strip().splitlines()))

            registration = csm('compose','--file',str(file),'--cwd',str(project),'--project',tag,'--register-only')
            docker(*compose, 'create', '--pull','never','--scale','alpha=2','alpha','beta', cwd=project)
            found = docker(*compose,'ps','-a','--no-trunc','--format',COMPOSE_FIELDS,
                           'alpha','beta','empty',cwd=project,timeout=4)
            mapping = dict(line.split() for line in found.stdout.splitlines())
            placed = {row['id']: row for row in rows(inspect(list(mapping), timeout=4))}
            expect('多成员及多副本取得完整最小映射', len(mapping)==3 and len(placed)==3
                   and list(mapping.values()).count('alpha')==2 and list(mapping.values()).count('beta')==1
                   and all(len(cid)==64 and placed[cid]['project']==tag and placed[cid]['member']==member
                           and placed[cid]['cwd']==str(project) and placed[cid]['file']==str(file)
                           for cid,member in mapping.items()), exit=found.returncode, members=len(set(mapping.values())),
                   ids=len(mapping))
            empty = docker(*compose,'ps','-a','--no-trunc','--format',COMPOSE_FIELDS,'empty',cwd=project,
                           timeout=4,check=False)
            absent_file = docker('compose','-p',tag,'-f',str(project/'does-not-exist.yaml'),
                                 'ps','-a','--format',COMPOSE_FIELDS,'empty',cwd=project,timeout=4,check=False)
            expect('正常空成员与 Compose 失败可区分', empty.returncode==0 and not empty.stdout.strip()
                   and absent_file.returncode!=0, empty_exit=empty.returncode, failure_exit=absent_file.returncode)
            daemon = docker('--host','unix://'+str(project/'connection.sock'),'inspect','--type=container',
                            '--format',INSPECT_FIELDS,live,check=False,timeout=4)
            daemon_list = docker('--host','unix://'+str(project/'connection.sock'),'ps','-aq','--no-trunc',
                                 check=False,timeout=4)
            expect('隔离故障端点不能确认缺失', daemon.returncode!=0 and not daemon.stdout.strip()
                   and daemon_list.returncode!=0, inspect_exit=daemon.returncode, list_exit=daemon_list.returncode)
            if worker:
                sys.path.insert(0, str(BASE / 'backend'))
                import engine
                import observation
                from unittest.mock import patch
                engine.ROOT = root
                members = {engine.service(sid)['name']: sid for sid in registration['members']}
                conflict_file = project / 'conflict.yaml'
                conflict_file.write_bytes(file.read_bytes())
                alpha = engine.service(members['alpha'])
                parent = engine.project(project)
                engine.record_spec('conflict', parent, 'conflict', 'compose',
                                   {**alpha['spec'], 'file': str(conflict_file)})
                engine.write('UPDATE service_observations SET checked_at=0')
                original = engine.latest(services[1])
                with patch.object(engine,'docker_probe_output',wraps=engine.docker_probe_output) as probe:
                    checked = observation.observe_due(visible=True)
                names = [call.args[0][0] for call in probe.call_args_list]
                expect('真实 worker 混合部分失败保留运行和健康邻项',
                       checked==6 and engine.cached_observation(services[0])['state']=='stopped'
                       and engine.cached_observation(services[1])['state']=='unknown'
                       and engine.latest(services[1])==original and original['ended'] is None
                       and names.count('inspect')==1 and names.count('ps')==1 and names.count('compose')==2,
                       checked=checked, commands=names)
                expect('真实 worker Compose 配置冲突不影响同批正常成员',
                       engine.cached_observation('conflict')['state']=='unknown'
                       and all(engine.cached_observation(sid)['state']=='stopped' for sid in members.values()))
                for force in (False, True):
                    if force:
                        docker(*compose, 'up','-d','--no-deps','--force-recreate','--pull','never',
                               '--scale','alpha=2','alpha',cwd=project)
                    else:
                        docker(*compose, 'start','alpha',cwd=project)
                    ids = docker(*compose,'ps','-aq','alpha',cwd=project).stdout.split()
                    docker('wait',*ids)
                    engine.write('UPDATE service_observations SET checked_at=0 WHERE service_id=?',[alpha['id']])
                    with patch.object(engine,'docker_probe_output',wraps=engine.docker_probe_output) as probe:
                        checked = observation.observe_due(visible=True)
                    current = engine.latest(alpha['id'])
                    expect('真实 worker 外部重建' if force else '真实 worker 错峰只查询单个到期成员',
                           checked==1 and probe.call_count==2 and
                           set(current['runtime']['container_ids'])==set(ids) and len(ids)==2
                           and (not force or previous['id']!=current['id']),
                           checked=checked, commands=probe.call_count, replicas=len(ids))
                    previous = current
        finally:
            try:
                for cid in owned_ids():
                    remove(cid)
                networks = docker('network','ls','-q','--filter','label=com.docker.compose.project='+tag).stdout.splitlines()
                for network in networks:
                    label, count = docker('network','inspect','--format',
                                          '{{index .Labels "com.docker.compose.project"}} {{len .Containers}}',network
                                          ).stdout.strip().split()
                    if label != tag or count != '0':
                        raise RuntimeError('网络所有权或连接状态不符，拒绝删除')
                    docker('network','rm',network)
                expect('测试资源已清理', not owned_ids(), container_count=0, removed_networks=len(networks))
            finally:
                watcher_lock.close()
                output.write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
    return all(info['passed'] for key, info in results.items() if key != 'versions')


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path, default=BASE/'.test-state/p4-04/V01.json')
    parser.add_argument('--worker', action='store_true', help='also verify the production worker against the fixtures')
    args = parser.parse_args()
    raise SystemExit(0 if verify(args.output.resolve(), args.worker) else 1)
