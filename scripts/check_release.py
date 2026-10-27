#!/usr/bin/env python3
"""Keep source, bundle, changelog and security support versions in agreement."""
from pathlib import Path
import re
import sys


def check(root):
    patterns = {
        'scripts/build.sh': r'CFBundleShortVersionString</key><string>(\d+\.\d+\.\d+)</string>',
        'README.md': r'当前源码版本为 \*\*(\d+\.\d+\.\d+)\*\*',
        'CHANGELOG.md': r'^## (\d+\.\d+\.\d+) —',
        'SECURITY.md': r'当前源码 `(\d+\.\d+\.\d+)`',
    }
    versions, errors = {}, []
    for name, pattern in patterns.items():
        match = re.search(pattern, (root / name).read_text(), re.MULTILINE)
        if match:
            versions[name] = match.group(1)
        else:
            errors.append(name + ': 缺少版本声明')
    if len(set(versions.values())) != 1:
        errors.append('版本不一致：' + ', '.join(name + '=' + version for name, version in versions.items()))
    return errors


if __name__ == '__main__':
    errors = check(Path(__file__).resolve().parents[1])
    for error in errors:
        print('FAIL ' + error)
    if not errors:
        print('PASS: README / CHANGELOG / SECURITY / bundle 版本一致')
    sys.exit(1 if errors else 0)
