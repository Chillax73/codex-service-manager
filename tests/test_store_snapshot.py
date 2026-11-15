"""Compile the actual native Store and verify snapshot publication contracts."""
from pathlib import Path
import subprocess
import tempfile
import unittest

BASE = Path(__file__).resolve().parents[1]


class StoreSnapshotTest(unittest.TestCase):
    def test_store_contracts(self):
        with tempfile.TemporaryDirectory(prefix='csm-store-') as folder:
            root = Path(folder)
            main = root / 'main.swift'
            main.write_bytes((BASE / 'tests/store_snapshot.swift').read_bytes())
            sources = [BASE / 'app/Sources' / name for name in
                       ('Models.swift', 'Status.swift', 'Store.swift', 'Infrastructure.swift')]
            subprocess.run(['/usr/bin/swiftc', '-O', '-swift-version', '5', '-module-cache-path',
                            str(BASE / 'build/module-cache'), '-framework', 'AppKit', '-framework', 'SwiftUI',
                            *map(str, sources), str(main), '-o', str(root / 'store')], check=True)
            result = subprocess.run([str(root / 'store'), '--root', str(root), '--engine', '/dev/null'],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('PASS store snapshot', result.stdout)
            print(result.stdout.strip())


if __name__ == '__main__':
    unittest.main()
