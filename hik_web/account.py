"""The Hik-Connect account the web viewer streams from.

Reads the same config file as the desktop app (``~/.hikvision_monitor.json``,
or ``$HIK_CONFIG``): the Hik-Connect login (an MD5 hash, never the password)
and the per-device verification codes. Nothing from it is ever sent to the
browser.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time

import hik_cloud

CAMERA_CACHE_SECONDS = 600
SNAPSHOT_MAX_AGE = 600
ASPECT_KEEP_SECONDS = 7 * 86400
EMPTY_KEEP_SECONDS = 86400
STATE_FILE = os.path.join(os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache'),
                          'hik_web', 'state.json')


def jpeg_size(data):
    """(width, height) from a JPEG's start-of-frame marker, or None."""
    i, n = 2, len(data)
    if data[:2] != b'\xff\xd8':
        return None
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if marker in (0xC0, 0xC1, 0xC2):
            h, w = int.from_bytes(data[i + 5:i + 7], 'big'), int.from_bytes(data[i + 7:i + 9], 'big')
            return (w, h) if w and h else None
        i += 2 + int.from_bytes(data[i + 2:i + 4], 'big')
    return None


class Account:
    def __init__(self, path=None):
        self.path = path or os.environ.get('HIK_CONFIG') \
            or os.path.expanduser('~/.hikvision_monitor.json')
        self._lock = threading.Lock()
        data = self._read()
        dev = next((d for d in data.get('devices', [])
                    if d.get('type') == 'hikconnect'), None)
        if dev is None:
            raise SystemExit(f'No Hik-Connect account in {self.path} — '
                             'add one in the desktop app first.')
        self.device_id = dev.get('id')
        self.verify_codes = dict(dev.get('verify_codes') or {})
        self.cloud = hik_cloud.HikConnectCloud(
            dev['account'], dev['password_md5'], api_base=dev.get('api_base'),
            client_version=dev.get('client_version'))
        self._cams = {}
        self._cams_at = 0.0
        self._aspects = {}            # camera key → (width / height, when)
        self._snaps = {}              # camera key → (when, JPEG bytes)
        self._learned, self._empty = self._load_state()
        self._snap_locks = {}

    def _read(self):
        with open(self.path, encoding='utf-8') as f:
            return json.load(f)

    # ── cameras ─────────────────────────────────────────────────────────────
    def cameras(self, refresh=False):
        """{key: CloudCamera}, cached; blocking when it has to ask the cloud."""
        with self._lock:
            fresh = self._cams and time.monotonic() - self._cams_at < CAMERA_CACHE_SECONDS
            if fresh and not refresh:
                return dict(self._cams)
        cams = {c.key: c for c in self.cloud.list_cameras()}
        with self._lock:
            self._cams, self._cams_at = cams, time.monotonic()
            return dict(cams)

    def camera(self, key):
        return self.cameras().get(key)

    def snapshot(self, cam, max_age=SNAPSHOT_MAX_AGE):
        """The camera's picture as JPEG. Taking one makes the NVR capture a
        frame, which is slow, so recent pictures are reused: the page only
        shows them as placeholders until the live video arrives."""
        with self._lock:
            lock = self._snap_locks.setdefault(cam.key, threading.Lock())
        with lock:
            hit = self._snaps.get(cam.key)
            if hit and time.monotonic() - hit[0] < max_age:
                return hit[1]
            data = self.cloud.snapshot(cam, self.verify_code(cam.device_serial))
            self._snaps[cam.key] = (time.monotonic(), data)
            return data

    @staticmethod
    def _load_state():
        try:
            with open(STATE_FILE, encoding='utf-8') as f:
                data = json.load(f)
            now = time.time()
            aspects = {k: v for k, v in (data.get('aspects') or {}).items()
                       if isinstance(v, list) and len(v) == 2 and now - v[1] < ASPECT_KEEP_SECONDS}
            empty = {k: t for k, t in (data.get('empty') or {}).items()
                     if isinstance(t, (int, float)) and now - t < EMPTY_KEEP_SECONDS}
            return aspects, empty
        except (OSError, ValueError, AttributeError):
            return {}, {}

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            tmp = STATE_FILE + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump({'aspects': self._learned, 'empty': self._empty}, f)
            os.replace(tmp, STATE_FILE)
        except OSError:
            pass

    def is_empty(self, cam):
        """True if the relay recently said this channel has no camera."""
        t = self._empty.get(cam.key)
        return bool(t) and time.time() - t < EMPTY_KEEP_SECONDS

    def mark_empty(self, cam):
        if not self.is_empty(cam):
            self._empty[cam.key] = time.time()
            self._save_state()

    def aspect(self, cam):
        """The camera's true picture shape (width / height), or None.

        Sub-streams are often 640x480 frames that hold a 16:9 picture squeezed
        sideways. The cloud snapshot is the camera's real picture, so its shape
        is the truth. Learned once per camera and remembered on disk, so a
        restart does not have to take new snapshots before the first stream."""
        known = self._learned.get(cam.key)
        if known:
            return known[0]
        hit = self._aspects.get(cam.key)
        if hit and time.monotonic() - hit[1] < 60:
            return hit[0]                  # a recent failure: do not retry at once
        ratio = None
        try:
            size = jpeg_size(self.snapshot(cam))
            if size:
                ratio = round(size[0] / size[1], 4)
        except Exception:
            pass                       # offline etc. — show the stream as sent
        self._aspects[cam.key] = (ratio, time.monotonic())
        if ratio:
            self._learned[cam.key] = [ratio, time.time()]
            self._save_state()
        return ratio

    # ── verification codes ──────────────────────────────────────────────────
    def verify_code(self, serial):
        return self.verify_codes.get(serial) or None

    def set_verify_code(self, serial, code):
        """Remember a device's code, keeping everything else in the config file
        as it is (the desktop app shares it). Written atomically, mode 0600."""
        with self._lock:
            self.verify_codes[serial] = code
            data = self._read()
            for d in data.get('devices', []):
                if d.get('id') == self.device_id:
                    d.setdefault('verify_codes', {})[serial] = code
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path) or '.',
                                       prefix='.hikcfg-')
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2)
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
