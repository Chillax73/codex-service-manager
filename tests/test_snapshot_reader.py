"""Compile and exercise the production Swift connection with real pipes/processes."""
from pathlib import Path
import subprocess
import tempfile
import unittest

BASE = Path(__file__).resolve().parents[1]


def connection_source(path=None):
    source = (path or BASE / 'app/Sources/Infrastructure.swift').read_text()
    body = source.split('final class CachedSnapshotConnection {', 1)[1].split('\nenum ExitRequestOutcome', 1)[0]
    return 'final class CachedSnapshotConnection {' + body


class SnapshotReaderTest(unittest.TestCase):
    def test_production_connection(self):
        with tempfile.TemporaryDirectory(prefix='csm-reader-') as folder:
            root = Path(folder)
            worker = root / 'worker.py'
            worker.write_text('''import json, pathlib, sys
root = pathlib.Path(sys.argv[2])
marker = root / 'attempt'
attempt = int(marker.read_text()) if marker.exists() else 0
marker.write_text(str(attempt + 1))
for line in sys.stdin:
    if attempt == 0:
        sys.stdout.write('{"ok":')
        sys.stdout.flush()
        break
    if attempt == 1:
        print('invalid json', flush=True)
        break
    print(json.dumps({'ok': True, 'result': {'value': '恢复成功'}}), flush=True)
''')
            source = 'import Foundation\nimport Darwin\n'
            source += 'let statePath = CommandLine.arguments[1]\nlet enginePath = CommandLine.arguments[2]\n'
            source += connection_source()
            source += (BASE / 'tests/snapshot_reader.swift').read_text()
            (root / 'main.swift').write_text(source)
            subprocess.run(['/usr/bin/swiftc', '-O', '-module-cache-path', str(BASE / 'build/module-cache'),
                            str(root / 'main.swift'), '-o', str(root / 'reader')], check=True)
            result = subprocess.run([str(root / 'reader'), str(root), str(worker)],
                                    text=True, capture_output=True, timeout=35)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('PASS 12', result.stdout)
            print(result.stdout.strip())


if __name__ == '__main__':
    unittest.main()
