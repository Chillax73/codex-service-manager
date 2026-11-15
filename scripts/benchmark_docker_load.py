"""Test-owned Docker fixtures for the native probe benchmark."""
from contextlib import closing
import fcntl
import json
from pathlib import Path
import subprocess
import uuid

import engine
from benchmark_capture_candidates import cli

IMAGE = 'python:3.12-slim'
HISTORY_COUNT = 53


class DockerLoad:
    def __init__(self, directory):
        self.directory = directory
        self.tag = 'csm-worker-bench-' + uuid.uuid4().hex[:12]
        self.registry = directory / 'docker-registry'
        self.image = None
        self.history = []
        self.active_specs = []
        self.active_ids = []
        self.active_service = None
        self.active_group = None

    def docker(self, *args, cwd=None):
        result = subprocess.run([engine.docker_executable() if hasattr(engine, 'docker_executable')
                                 else '/usr/local/bin/docker', *args], cwd=cwd, capture_output=True,
                                text=True, timeout=40)
        if result.returncode:
            # Only test-owned names and the exit status appear in the report.
            raise RuntimeError(f'Docker {args[0]} test fixture failed ({result.returncode})')
        return result.stdout.strip()

    def prepare(self):
        self.image = self.docker('image', 'inspect', '--format', '{{.Id}}', IMAGE)
        self.registry.mkdir()
        self.lock = (self.registry / 'watcher.lock').open('a')
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for i in range(40):
            sid, cid = self.create(f'missing-{i}')
            self.history.append((sid, 'unknown'))
            self.remove(cid)
        for i in range(8):
            code = 0 if i < 4 else 7
            sid, cid = self.create(f'stopped-{i}', 'sh', '-c', 'exit ' + str(code))
            # The stopped object is already registered in the test registry.
            self.docker('start', cid)
            if self.docker('wait', cid) != str(code):
                raise RuntimeError('Unexpected fixture exit code')
            self.history.append((sid, 'exited' if code == 0 else 'failed'))
        for i, n in enumerate((3, 2)):
            argv, members = self.compose(f'idle-{i}', n, sleep=False)
            self.docker(*argv, 'create', '--pull', 'never', cwd=self.directory)
            self.history.extend((sid, 'stopped') for sid in members)
        self.active_service, active_id = self.create('active', 'sleep', '1800')
        self.active_group, members = self.compose('active', 1, sleep=True)
        self.active_ids = [active_id]
        self.active_specs = [(sid, 'running') for sid in [self.active_service, *members]]
        return {'test_image': self.image, 'missing_ids': 40, 'exited_or_failed': 8,
                'historical_compose_members': 5, 'historical_compose_projects': 2,
                'active_standalone': 1, 'active_compose_members': 1, 'active_compose_replicas': 2}

    def compose(self, name, count, sleep):
        group = self.tag + '-' + name
        file = self.directory / (name + '.compose.json')
        data = {'services': {f'member-{i}': {'image': IMAGE, 'command': ['sleep', '1800'] if sleep else ['true'],
                                            'labels': {'csm.test': self.tag}} for i in range(count)},
                'networks': {'default': {'labels': {'csm.test': self.tag}}}}
        file.write_text(json.dumps(data))
        args = ('compose', '-p', group, '-f', str(file))
        registered = cli(self.registry, 'compose', '--file', str(file), '--cwd', str(self.directory),
                         '--project', group, '--register-only')
        return args, registered['members']

    def create(self, name, *command):
        cid = self.docker('create', '--name', self.tag + '-' + name, '--label', 'csm.test=' + self.tag,
                          IMAGE, *(command or ('true',)))
        item = cli(self.registry, 'container', cid, '--cwd', str(self.directory))
        return item['id'], cid

    def owns(self, cid):
        return self.docker('inspect', '--type=container', '--format',
                           '{{index .Config.Labels "csm.test"}}', cid) == self.tag

    def remove(self, cid):
        if not self.owns(cid):
            raise RuntimeError('Fixture ownership mismatch')
        self.docker('rm', '-f', cid)

    def activate(self):
        cli(self.registry, 'start', self.active_service)
        self.docker(*self.active_group, 'up', '-d', '--pull','never','--scale','member-0=2', cwd=self.directory)
        ids = self.docker(*self.active_group, 'ps','-q','member-0', cwd=self.directory).splitlines()
        if len(ids) != 2 or not all(self.owns(cid) for cid in ids):
            raise RuntimeError('Active Compose replica count or ownership mismatch')
        self.active_ids[1:] = ids

    def deactivate(self):
        for cid in self.active_ids:
            if not self.owns(cid):
                raise RuntimeError('Fixture ownership mismatch while stopping')
            self.docker('stop','-t','1',cid)

    def seed(self, root, started, *, active=False, stagger=False):
        selected = self.active_specs if active else self.history
        engine.ROOT = self.registry
        with engine.read_context():
            specs = [(engine.service(sid), state) for sid, state in selected]
        engine.ROOT = root
        project = engine.project(root)
        with closing(engine.database()) as db, db:
            for i, (item, state) in enumerate(specs):
                db.execute('INSERT INTO services(id,project_id,name,kind,spec,group_id,created_at) VALUES (?,?,?,?,?,?,?)',
                           (item['id'], project['id'], item['name'], item['kind'], json.dumps(item['spec']),
                            item['group_id'], 1000.0+i))
                if not active:
                    phase = (i * 11 % 17) / 2 if stagger else 0
                    db.execute('INSERT INTO service_observations VALUES (?,?,?)',
                               (item['id'], started - 20 - phase, json.dumps({'state': state, 'run': None})))

    def settle_terminal_history(self, root):
        # The preexisting worker chooses urgent rows first. Seed matching run
        # projections so cold import of old task rows cannot hide Docker work.
        engine.ROOT = root
        project_ids = [engine.project(root)['id']]
        for i in range(1, 26):
            directory = root / f'project-{i:02}'
            directory.mkdir()
            project_ids.append(engine.project(directory)['id'])
        with closing(engine.database()) as db, db:
            for i, project_id in enumerate(project_ids):
                db.execute("UPDATE services SET project_id=? WHERE id LIKE 'history-%' "
                           "AND CAST(substr(id,9) AS INTEGER) % 26=?", (project_id, i))
            # Integer timestamps round-trip through SQLite JSON without a
            # few-ULP mismatch that would mark every seeded row urgent.
            db.execute("UPDATE runs SET started=CAST(started AS INTEGER),ended=CAST(ended AS INTEGER) "
                       "WHERE service_id LIKE 'history-%'")
            db.execute("UPDATE service_observations SET checked_at=checked_at-(CAST(substr(service_id,9) AS INTEGER)%120), "
                       "projection=json_set(projection, '$.run', "
                       "(SELECT json_object('id', id, 'service_id', service_id, 'started', started, 'ended', ended, "
                       "'state',state,'exit_code',exit_code,'runtime',json(runtime),'error',error,'log',log) FROM runs "
                       "WHERE service_id=service_observations.service_id)) WHERE service_id LIKE 'history-%'")

    def historical_ready(self, root, after):
        engine.ROOT = root
        rows = engine.query("SELECT o.service_id,o.checked_at,json_extract(o.projection,'$.state') AS state "
                            "FROM service_observations o JOIN services s ON s.id=o.service_id "
                            "WHERE s.kind IN ('container','compose')")
        indexed = {row['service_id']: (row['state'], row['checked_at']) for row in rows}
        return all(sid in indexed and indexed[sid][0] == expected and indexed[sid][1] >= after
                   for sid, expected in self.history)

    def verify_active(self, root, running):
        engine.ROOT = root
        expected = 'running' if running else ('stopped', 'exited', 'failed')
        for sid, _ in self.active_specs:
            state = engine.cached_observation(sid)['state']
            if (state != expected if running else state not in expected):
                return False
        return True

    def close(self):
        errors = []
        try:
            for cid in self.docker('ps','-aq','--no-trunc','--filter','label=csm.test='+self.tag).splitlines():
                try:
                    self.remove(cid)
                except Exception as exc:
                    errors.append(type(exc).__name__)
            for net in self.docker('network','ls','-q','--filter','label=csm.test='+self.tag).splitlines():
                try:
                    owner = self.docker('network','inspect','--format',
                                        '{{index .Labels "csm.test"}} {{len .Containers}}', net)
                    if owner != self.tag + ' 0':
                        raise RuntimeError('Fixture network ownership or attachment mismatch')
                    self.docker('network','rm',net)
                except Exception as exc:
                    errors.append(type(exc).__name__)
        finally:
            if hasattr(self,'lock'):
                self.lock.close()
        if errors:
            raise RuntimeError('Docker fixture cleanup incomplete: ' + ', '.join(errors))
