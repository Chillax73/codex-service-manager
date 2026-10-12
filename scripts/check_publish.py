#!/usr/bin/env python3
"""Fail closed on private artifacts and recognizable secrets in the Git index."""
import re
import subprocess
import sys

PATTERNS = {
    'home directory': re.compile(rb'/Users/[A-Za-z0-9][^\s\"\x27]*|/home/[A-Za-z0-9][^\s\"\x27]*'),
    'email': re.compile(rb'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'),
    'private key': re.compile(rb'-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----'),
    'access token': re.compile(rb'(?:github_pat_|gh[pousr]_|sk-(?:proj-)?)[A-Za-z0-9_-]{20,}'),
    'chat/share identifier': re.compile(rb'(?:chatgpt\.com/share/|codex://threads/)[0-9a-f]{8}-[0-9a-f-]{27,}'),
}


def check():
    entries = subprocess.check_output(['git', 'ls-files', '--stage', '-z']).split(b'\0')
    failures = []
    count = 0
    for entry in entries:
        if not entry:
            continue
        info, path_bytes = entry.split(b'\t', 1)
        mode, sha, stage = info.split()
        path = path_bytes.decode('utf-8')
        count += 1
        if (stage != b'0' or mode not in (b'100644', b'100755') or
            path.startswith(('docs/evidence/', '.codex/', '.test-state/', 'build/')) or
            path == 'AGENTS.md' or path == 'validation/hooks.json' or
            re.search(r'(?:\.log|\.jsonl|\.sqlite(?:-.*)?|\.db|\.pem|\.key|\.p12|\.pfx|\.png)$', path) or
            (path.startswith('validation/') and path.endswith('results.json'))):
            failures.append((path, 'private or generated artifact'))
            continue
        data = subprocess.check_output(['git', 'cat-file', 'blob', sha.decode()])
        if b'\0' in data:
            failures.append((path, 'unexpected binary'))
        for label, pattern in PATTERNS.items():
            matches = pattern.findall(data)
            if label == 'email':
                matches = [m for m in matches if m != b'git@github.com' and not m.endswith(b'@users.noreply.github.com')]
            if matches:
                failures.append((path, label))
    # Print only filenames/reasons, never the matching private values.
    for path, reason in failures:
        print('FAIL ' + path + ': ' + reason)
    if failures:
        return 1
    print('PASS: %d staged files; no detected private artifacts or recognized secrets' % count)
    return 0


if __name__ == '__main__':
    sys.exit(check())
