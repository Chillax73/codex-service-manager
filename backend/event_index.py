"""Incremental event-file cache; retirement never removes persistent history."""
import json
import stat

import storage


class EventIndex:
    def __init__(self, directory):
        self.directory = directory
        self.entries = {}
        self.inactive = set()
        self.candidates = {}

    @staticmethod
    def fingerprint(path):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('event is not a regular file')
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                info.st_ctime_ns, info.st_mode, info.st_uid)

    def refresh(self):
        entries = {}
        unchanged = set()
        for path in self.directory.glob('*.json'):
            try:
                before = self.fingerprint(path)
                old = self.entries.get(path.name)
                if old and old[0] == before:
                    entries[path.name] = old
                    unchanged.add(old[1]['id'])
                    continue
                storage.private_file(path)
                before = self.fingerprint(path)
                event = json.loads(path.read_text())
                if not isinstance(event, dict) or event.get('id') != path.stem:
                    continue
                if self.fingerprint(path) != before:
                    continue  # A concurrent rewrite is retried on the next refresh.
                entries[path.name] = (before, event)
            except (OSError, ValueError, RuntimeError):
                continue
        self.entries = entries
        self.inactive.intersection_update(unchanged)
        self.candidates = {event['id']: event for _, event in entries.values()
                           if event.get('shell') and event['id'] not in self.inactive}

    def events(self, tracked):
        # A persisted/dormant lineage wins over retirement, including resume after
        # project policy changes. Newly armed events invalidate their fingerprints.
        required = {info['event'] for info in tracked.values()}
        self.inactive.difference_update(required)
        for eid in required - self.candidates.keys():
            entry = self.entries.get(eid + '.json')
            if entry and entry[1].get('shell'):
                self.candidates[eid] = entry[1]
        return list(self.candidates.values())

    def retire(self, events, table, tracked, recent_docker, now):
        retained = {info['event'] for info in tracked.values()}
        for event in events:
            shell = event['shell']
            live = table.get(shell['pid'])
            if (event['id'] not in retained and
                    (not live or live['birth'] != shell['birth']) and not recent_docker(event, now)):
                self.inactive.add(event['id'])
                self.candidates.pop(event['id'], None)

    def __len__(self):
        return len(self.entries)
