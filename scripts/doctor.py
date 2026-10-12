#!/usr/bin/env python3
"""Read-only preflight; checks tool availability without dumping environment."""
import argparse
from pathlib import Path
import platform
import shutil
import subprocess
import sys

parser = argparse.ArgumentParser()
parser.add_argument('--projects', action='store_true', help='also require npm, Maven and Java')
parser.add_argument('--docker', action='store_true', help='also require running Docker and Compose')
args = parser.parse_args()
checks = [('macOS', platform.system() == 'Darwin'), ('system Python 3', Path('/usr/bin/python3').is_file())]
for name in ('swiftc', 'codesign'):
    checks.append((name, shutil.which(name) is not None))
if args.projects:
    import os
    for name, variable in (('npm', 'CSM_NPM'), ('mvn', 'CSM_MAVEN'), ('java', 'JAVA')):
        checks.append((name, shutil.which(os.environ.get(variable) or name) is not None))
if args.docker:
    for argv in (['docker', 'info'], ['docker', 'compose', 'version']):
        try:
            ok = subprocess.run(argv, capture_output=True, timeout=15).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            ok = False
        checks.append((' '.join(argv), ok))
for name, ok in checks:
    print(('PASS ' if ok else 'FAIL ') + name)
sys.exit(0 if all(ok for _, ok in checks) else 1)
