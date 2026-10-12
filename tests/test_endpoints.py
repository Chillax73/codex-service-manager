"""Endpoint changes must preserve user configuration and keep inspection JSON clean."""
import json
import datetime
from pathlib import Path
import sys
import time
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
import engine

BASE=Path(__file__).resolve().parents[1]

class EndpointContract(unittest.TestCase):
    def test_docker_nanosecond_timestamp_is_utc_and_not_observation_time(self):
        expected=datetime.datetime(2026,10,12,1,26,30,tzinfo=datetime.timezone.utc).timestamp()+.123456
        self.assertAlmostEqual(engine.docker_time('2026-10-12T01:26:30.123456789Z'),expected,places=6)
        self.assertAlmostEqual(engine.docker_time('2026-10-12T02:26:30.123456+01:00'),expected,places=6)
        self.assertIsNone(engine.docker_time('0001-01-01T00:00:00Z'))
        self.assertIsNone(engine.docker_time('invalid'))

    def test_matches_actual_published_tcp_only(self):
        ports={'8000/tcp':[{'HostIp':'127.0.0.1','HostPort':'52123'}],
               '8000/udp':[{'HostIp':'127.0.0.1','HostPort':'52123'}]}
        self.assertEqual(engine.health_binding('http://127.0.0.1:52123/health',ports),'8000/tcp')
        self.assertIsNone(engine.health_binding('http://127.0.0.1:53123',ports))
        self.assertIsNone(engine.health_binding('http://example.com:52123',ports))
        self.assertIsNone(engine.health_binding('file:///local/file',ports))

    def test_ambiguous_bindings_do_not_guess(self):
        ports={key:[{'HostIp':'127.0.0.1','HostPort':'52123'}] for key in ('8000/tcp','9000/tcp')}
        self.assertIsNone(engine.health_binding('http://localhost:52123',ports))

    def test_dynamic_host_port_preserves_path_query_and_spec(self):
        spec={'url':'http://localhost:52123/api/health?mode=ready#state','url_binding_port':'8000/tcp'}
        runtime={'ports':[{'8000/tcp':[{'HostIp':'127.0.0.1','HostPort':'53123'}]}]}
        self.assertEqual(engine.observed_url(spec,runtime),'http://localhost:53123/api/health?mode=ready#state')
        self.assertEqual(spec['url'],'http://localhost:52123/api/health?mode=ready#state')

    def test_ipv6_binding_formats_authority(self):
        spec={'url':'http://[::1]:52123/health','url_binding_port':'8000/tcp'}
        self.assertEqual(engine.observed_url(spec,{'ports':[{'8000/tcp':[{'HostIp':'::1','HostPort':'53123'}]}]}),
                         'http://[::1]:53123/health')

    def test_unchanged_and_stopped_bindings_keep_configured_url(self):
        spec={'url':'https://localhost:52123/health','url_binding_port':'8000/tcp'}
        self.assertEqual(engine.observed_url(spec,{'ports':[{'8000/tcp':None}]}),spec['url'])
        self.assertEqual(engine.observed_url({'url':'http://localhost:123/path'},{'ports':[]}),
                         'http://localhost:123/path')

    def test_url_credentials_are_not_dropped(self):
        spec={'url':'http://user:p%40ss@localhost:52123/health','url_binding_port':'8000/tcp'}
        self.assertEqual(engine.observed_url(spec,{'ports':[{'8000/tcp':[{'HostIp':'127.0.0.1','HostPort':'53123'}]}]}),
                         'http://user:p%40ss@localhost:53123/health')

    def test_json_stdout_is_not_polluted_by_stderr(self):
        value=engine.command([sys.executable,'-c','import sys;print(\'{"state":"running"}\');print("warning",file=sys.stderr)'])
        self.assertEqual(json.loads(value),{'state':'running'})

    def test_merged_logs_preserve_both_streams_and_decode_bytes(self):
        output=engine.command([sys.executable,'-c',
            'import sys;print("stdout-marker",flush=True);sys.stderr.buffer.write(b"stderr-marker\\xff\\n");sys.stderr.flush()'],merge_stderr=True)
        self.assertIn('stdout-marker',output)
        self.assertIn('stderr-marker',output)
        self.assertIn('\ufffd',output)
        with self.assertRaisesRegex(RuntimeError,'stderr-failure'):
            engine.command([sys.executable,'-c','import sys;print("stderr-failure",file=sys.stderr);sys.exit(7)'],merge_stderr=True)

if __name__=='__main__':
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(EndpointContract))
    (BASE/'docs/evidence/endpoint-tests.json').write_text(json.dumps({'generated_at':time.time(),
        'tests_run':result.testsRun,'passed':result.wasSuccessful(),'failures':[(str(t),s) for t,s in result.failures],
        'errors':[(str(t),s) for t,s in result.errors]},ensure_ascii=False,indent=2)+'\n')
    sys.exit(0 if result.wasSuccessful() else 1)
