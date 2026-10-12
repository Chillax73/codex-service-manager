#!/usr/bin/env python3
"""Install disposable Vite/Spring fixture dependencies; never starts services."""
from pathlib import Path
import subprocess
import sys

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'tests'))
from support import prepare_workspace, tool

if __name__ == '__main__':
    prepare_workspace()
    try:
        npm = tool('npm', 'CSM_NPM')
        maven = tool('mvn', 'CSM_MAVEN')
        tool('java', 'JAVA')
        subprocess.run([npm, 'ci'], cwd=BASE / 'tests/fixtures/frontend', check=True)
        subprocess.run([maven, '-B', '-ntp', '-DskipTests', 'package'], cwd=BASE / 'tests/fixtures/spring', check=True)
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
