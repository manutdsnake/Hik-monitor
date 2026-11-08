"""Bookmarks: moments in the recordings worth finding again.

Kept in a small JSON file on the server (next to the config file when
``$HIK_CONFIG`` points at one, as in the container, else in
``$XDG_DATA_HOME/hik_web``), so every browser sees the same list.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid

MAX_BOOKMARKS = 2000


class Bookmarks:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._items = self._load()

    @staticmethod
    def default_path(config_path):
        if os.environ.get('HIK_CONFIG'):
            return os.path.join(os.path.dirname(os.path.abspath(config_path)),
                                'hik_web_bookmarks.json')
        base = os.environ.get('XDG_DATA_HOME') or os.path.expanduser('~/.local/share')
        return os.path.join(base, 'hik_web', 'bookmarks.json')

    def _load(self):
        try:
            with open(self.path, encoding='utf-8') as f:
                items = json.load(f)
            return [b for b in items if isinstance(b, dict) and {'id', 'key', 'time'} <= b.keys()]
        except (OSError, ValueError):
            return []

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), prefix='.bookmarks-')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(self._items, f, indent=1)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def list(self, key=None):
        """Bookmarks (of one camera, or all), oldest first."""
        with self._lock:
            items = [dict(b) for b in self._items if key is None or b['key'] == key]
        return sorted(items, key=lambda b: b['time'])

    def add(self, key, when, note=''):
        b = {'id': uuid.uuid4().hex[:10], 'key': key, 'time': when, 'note': note,
             'created': int(time.time())}
        with self._lock:
            self._items.append(b)
            del self._items[:-MAX_BOOKMARKS]
            self._save()
        return dict(b)

    def delete(self, bid):
        with self._lock:
            n = len(self._items)
            self._items = [b for b in self._items if b['id'] != bid]
            if len(self._items) == n:
                return False
            self._save()
        return True
