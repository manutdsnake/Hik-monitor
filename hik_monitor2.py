#!/usr/bin/env python3
"""
Hikvision Monitor — Python Desktop App
DS-7604NI-K1/4P kompatibilan
ISAPI za upravljanje + OpenCV za RTSP video
"""

import sys, os, json, threading, time, queue
from datetime import datetime, timedelta
from pathlib import Path

# ── Bootstrap: set LD_LIBRARY_PATH and re-exec ONCE before loading any SDK .so ─
# The Hikvision libs (libPlayCtrl.so deps, HCNetSDKCom) must be found by the
# dynamic loader, which only reads LD_LIBRARY_PATH at process startup. We set it
# and re-exec here — before importing hik_sdk/hik_play — so the SDK is only
# initialized once (after the re-exec), not wastefully before it too.
def _bootstrap_sdk_path():
    sdk_path = os.environ.get('HIKVISION_SDK_PATH') \
               or os.path.expanduser('~/Desktop/sdk/lib')
    sdk_path = os.path.abspath(sdk_path)
    if not os.path.isdir(sdk_path):
        # Fall back to the auto-installed location (see sdk_installer.py), where
        # the on-demand SDK download puts the lib tree.
        xdg = os.environ.get('XDG_DATA_HOME') or os.path.expanduser('~/.local/share')
        alt = os.path.join(xdg, 'hikvision-monitor', 'sdk', 'lib')
        if os.path.isdir(alt):
            sdk_path = alt
        else:
            return   # no SDK installed — app will run ONVIF/RTSP-only
    com_path = os.path.join(sdk_path, 'HCNetSDKCom')
    parts = (os.environ.get('LD_LIBRARY_PATH', '') or '').split(':')
    if sdk_path in parts:
        return   # already set — don't loop
    os.environ['LD_LIBRARY_PATH'] = ':'.join([sdk_path, com_path] + parts)
    os.execv(sys.executable, [sys.executable] + sys.argv)

_bootstrap_sdk_path()

# ── Debug logging ─────────────────────────────────────────────────────────────
# Set HIK_DEBUG=1 in the environment for verbose per-frame/stat logging.
DEBUG = os.environ.get('HIK_DEBUG', '0') == '1'

def dlog(category, msg):
    if DEBUG:
        print(f'[{time.strftime("%H:%M:%S")}] [{category}] {msg}', flush=True)

def log(category, msg):
    print(f'[{time.strftime("%H:%M:%S")}] [{category}] {msg}', flush=True)


class StatsMonitor:
    """Periodically logs CPU/RAM (and GPU if available). Runs only when HIK_DEBUG=1."""
    def __init__(self, interval=3.0):
        self.interval = interval
        self._stop = threading.Event()
        self._have_psutil = False
        try:
            import psutil  # noqa
            self._have_psutil = True
        except ImportError:
            pass

    def start(self):
        if DEBUG:
            threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stop.set()

    def _gpu(self):
        try:
            import subprocess as sp
            out = sp.run(['nvidia-smi', '--query-gpu=utilization.gpu,memory.used',
                          '--format=csv,noheader,nounits'],
                         capture_output=True, text=True, timeout=2)
            if out.returncode == 0:
                return f'GPU {out.stdout.strip()}'
        except Exception: pass
        try:
            import glob
            for p in glob.glob('/sys/class/drm/card*/device/gpu_busy_percent'):
                with open(p) as f:
                    return f'GPU {f.read().strip()}%'
        except Exception: pass
        return ''

    def _run(self):
        proc = None
        if self._have_psutil:
            import psutil
            proc = psutil.Process(); proc.cpu_percent(None)
        else:
            log('STATS', 'psutil not installed — CPU/RAM stats unavailable (pip install psutil)')
        while not self._stop.wait(self.interval):
            parts = []
            if proc is not None:
                parts += [f'CPU {proc.cpu_percent(None):.0f}%',
                          f'RAM {proc.memory_info().rss/1024/1024:.0f}MB']
            g = self._gpu()
            if g: parts.append(g)
            if parts: log('STATS', '  '.join(parts))

_STATS = StatsMonitor()

# ── GPU (VAAPI) hardware-decode detection ─────────────────────────────────────
def _detect_vaapi():
    """Return a DRI render node that can ACTUALLY decode HEVC via VAAPI, else None.

    Many machines expose a render node whose VAAPI driver can't decode HEVC (e.g.
    Intel iGPUs without HEVC profiles, or NVIDIA which doesn't use VAAPI). We probe
    each node with a tiny real decode so we don't pick a node that fails at runtime.
    """
    import glob, subprocess as sp
    nodes = sorted(glob.glob('/dev/dri/renderD*'))
    if not nodes:
        return None
    # ffmpeg must have vaapi support compiled in
    try:
        out = sp.run(['ffmpeg', '-hide_banner', '-hwaccels'],
                     capture_output=True, text=True, timeout=5)
        if 'vaapi' not in out.stdout:
            return None
    except Exception:
        return None

    # Generate a 1-frame HEVC test clip, then try to HW-decode+scale it on each
    # node exactly the way playback does (scale_vaapi). Pick the first that works.
    import tempfile, os as _os
    test_clip = _os.path.join(tempfile.gettempdir(), 'hik_vaapi_probe.hevc')
    try:
        sp.run(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'lavfi',
                '-i', 'testsrc=size=320x240:rate=1:duration=1',
                '-c:v', 'libx265', '-frames:v', '1', test_clip],
               capture_output=True, timeout=15)
    except Exception:
        test_clip = None

    for node in nodes:
        if not test_clip or not _os.path.exists(test_clip):
            break
        try:
            r = sp.run(['ffmpeg', '-hide_banner', '-loglevel', 'error',
                        '-hwaccel', 'vaapi', '-hwaccel_device', node,
                        '-hwaccel_output_format', 'vaapi',
                        '-i', test_clip,
                        '-vf', 'scale_vaapi=w=160:h=120,hwdownload,format=nv12',
                        '-f', 'null', '-'],
                       capture_output=True, text=True, timeout=15)
            if r.returncode == 0 and 'not supported' not in (r.stderr or ''):
                return node
        except Exception:
            continue
    return None

_VAAPI_NODE = _detect_vaapi()
if _VAAPI_NODE:
    print(f'[App] VAAPI GPU decode available at {_VAAPI_NODE} — playback will use GPU')
else:
    print('[App] VAAPI HEVC decode not available — playback will use CPU decode')


def _detect_audio_out():
    """Pick an ffmpeg audio OUTPUT device for playback sound. Prefer PulseAudio
    (works with PipeWire too), fall back to ALSA. Returns (fmt, target) or
    (None, None) if ffmpeg has no usable audio output — in which case playback
    stays video-only. `target` for pulse is a stream name shown in the mixer."""
    import subprocess as sp
    try:
        out = sp.run(['ffmpeg', '-hide_banner', '-devices'],
                     capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return (None, None)
    if 'pulse' in out:
        return ('pulse', 'Hikvision Monitor')
    if 'alsa' in out:
        return ('alsa', 'default')
    return (None, None)

_AUDIO_FMT, _AUDIO_TARGET = _detect_audio_out()
if _AUDIO_FMT:
    print(f'[App] Playback audio via ffmpeg -f {_AUDIO_FMT}')
else:
    print('[App] No ffmpeg audio output device — playback will be silent')
_SDK = None   # safe default if the SDK fails to load (ONVIF/RTSP still work)
try:
    from hik_sdk import HCNetSDK
    try:
        _SDK = HCNetSDK()
        print('[App] Hikvision SDK loaded — playback will use SDK (port 8000)')
    except Exception as _e:
        print(f'[App] SDK init failed, falling back to RTSP playback: {_e}')
except ImportError:
    print('[App] hik_sdk.py not found — playback will use RTSP/HTTP only')

# PlayM4 native decoder (libPlayCtrl.so) — decodes without ffmpeg, like iVMS-4200
_PLAYM4_OK = False
try:
    from hik_play import PlayM4, yv12_to_rgb
    _PLAYM4_OK = True
    print('[App] PlayM4 decoder loaded — SDK streams decode natively (no ffmpeg)')
except Exception as _e:
    print(f'[App] PlayM4 unavailable, SDK streams will use ffmpeg: {_e}')

import cv2
import requests
from requests.auth import HTTPDigestAuth

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLabel, QPushButton, QLineEdit, QComboBox, QSpinBox,
    QDateEdit, QListWidget, QListWidgetItem, QSplitter, QFrame,
    QGroupBox, QScrollArea, QSizePolicy, QProgressBar, QStatusBar,
    QTabWidget, QMessageBox, QToolBar, QAction, QSlider, QStyle,
    QTreeWidget, QTreeWidgetItem, QDialog
)
from PyQt5.QtCore import (
    Qt, QThread, pyqtSignal, QTimer, QDate, QSize, QMutex, QMutexLocker,
    QPoint, QPointF, QRectF
)
from PyQt5.QtGui import (
    QImage, QPixmap, QFont, QColor, QPalette, QIcon, QPainter
)

# ── Konfiguracija ──────────────────────────────────────────────────────────────
CONFIG_FILE = Path.home() / '.hikvision_monitor.json'

DARK = {
    'bg':       '#0d1117',
    'panel':    '#161b22',
    'border':   '#21262d',
    'accent':   '#1f6feb',
    'text':     '#c9d1d9',
    'dim':      '#6e7681',
    'green':    '#2ea043',
    'red':      '#da3633',
    'amber':    '#d29922',
}

STYLE = f"""
QMainWindow, QWidget {{ background: {DARK['bg']}; color: {DARK['text']}; font-family: 'Segoe UI', sans-serif; }}
QGroupBox {{
    border: 1px solid {DARK['border']};
    border-radius: 4px;
    margin-top: 8px;
    padding-top: 8px;
    font-size: 11px;
    color: {DARK['dim']};
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 8px; padding: 0 4px; }}
QLineEdit, QSpinBox, QComboBox, QDateEdit {{
    background: {DARK['panel']};
    border: 1px solid {DARK['border']};
    border-radius: 3px;
    color: {DARK['text']};
    padding: 5px 8px;
    font-size: 13px;
}}
QLineEdit:focus, QComboBox:focus, QDateEdit:focus {{ border-color: {DARK['accent']}; }}
QComboBox::drop-down {{ border: none; }}
QPushButton {{
    background: {DARK['panel']};
    border: 1px solid {DARK['border']};
    border-radius: 3px;
    color: {DARK['text']};
    padding: 6px 14px;
    font-size: 13px;
    font-weight: 500;
}}
QPushButton:hover {{ background: #21262d; border-color: {DARK['accent']}; color: white; }}
QPushButton:pressed {{ background: #1f6feb33; }}
QPushButton.primary {{
    background: {DARK['accent']};
    border-color: {DARK['accent']};
    color: white;
    font-weight: 600;
}}
QPushButton.primary:hover {{ background: #388bfd; }}
QPushButton.danger {{ border-color: {DARK['red']}; color: {DARK['red']}; }}
QPushButton.success {{ border-color: {DARK['green']}; color: {DARK['green']}; }}
QListWidget {{
    background: {DARK['panel']};
    border: 1px solid {DARK['border']};
    border-radius: 3px;
    outline: none;
}}
QListWidget::item {{ padding: 8px; border-bottom: 1px solid {DARK['border']}; }}
QListWidget::item:selected {{ background: {DARK['accent']}33; color: white; border-left: 2px solid {DARK['accent']}; }}
QListWidget::item:hover {{ background: #21262d; }}
QTreeWidget {{
    background: {DARK['panel']};
    border: 1px solid {DARK['border']};
    border-radius: 3px;
    outline: none;
}}
QTreeWidget::item {{ padding: 5px; }}
QTreeWidget::item:selected {{ background: {DARK['accent']}33; color: white; }}
QTreeWidget::item:hover {{ background: #21262d; }}
QTabWidget::pane {{ border: 1px solid {DARK['border']}; border-top: none; }}
QTabBar::tab {{
    background: {DARK['bg']};
    border: 1px solid {DARK['border']};
    border-bottom: none;
    padding: 8px 20px;
    margin-right: 2px;
    color: {DARK['dim']};
    font-size: 13px;
}}
QTabBar::tab:selected {{ background: {DARK['panel']}; color: {DARK['text']}; border-bottom: none; }}
QTabBar::tab:hover {{ color: {DARK['text']}; }}
QStatusBar {{ background: {DARK['panel']}; border-top: 1px solid {DARK['border']}; font-size: 12px; color: {DARK['dim']}; }}
QLabel {{ color: {DARK['text']}; }}
QScrollBar:vertical {{ background: {DARK['bg']}; width: 8px; }}
QScrollBar::handle:vertical {{ background: {DARK['border']}; border-radius: 4px; min-height: 20px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QSlider::groove:horizontal {{ height: 4px; background: {DARK['border']}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {DARK['accent']}; border-radius: 7px; width: 14px; height: 14px; margin: -5px 0; }}
QSlider::sub-page:horizontal {{ background: {DARK['accent']}; border-radius: 2px; }}
"""

# ── NVR API ────────────────────────────────────────────────────────────────────
class NVRClient:
    def __init__(self, device_id=None, name='NVR'):
        self.device_id = device_id or str(id(self))
        self.name      = name
        self.host      = ''
        self.port      = 80
        self.username  = 'admin'
        self.password  = ''
        self.timeout   = 10
        # SDK state — populated by sdk_login() after ISAPI connect succeeds
        self.sdk_user_id  = -1
        self.start_dchan  = 33   # IP channel base, populated from SDK device info
        # True for genuine Hikvision (V30 login, SDK file-download works → playback
        # via download path with seeking). False for OEM rebrands (V40/ISAPI login)
        # whose SDK rejects the download API → playback must stream via PlayM4.
        self.sdk_supports_download = True

    @classmethod
    def load_all(cls):
        """Load all NVR configs from file. Returns list of NVRClient."""
        clients = []
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text())
                devices = data.get('devices', [])
                if not devices and data.get('host'):
                    # Legacy single-device format
                    devices = [data]
                for d in devices:
                    dtype = d.get('type')
                    if dtype == 'onvif':
                        c = ONVIFClient(d.get('id'), d.get('name', 'ONVIF Camera'))
                        c.xaddr = d.get('xaddr')
                        c.username = d.get('username', '')
                        c.password = d.get('password', '')
                        c.use_sub = d.get('use_sub', False)
                        c.extra_channels = d.get('extra_channels', []) or []
                    elif dtype == 'rtsp':
                        c = ManualRTSPClient(d.get('id'), d.get('name', 'RTSP Camera'))
                        c.url = d.get('url', '')
                    else:
                        c = cls(d.get('id'), d.get('name', 'NVR'))
                        c.username = d.get('username', 'admin')
                        c.password = d.get('password', '')
                    c.host = d.get('host', '')
                    c.port = d.get('port', 80)
                    clients.append(c)
            except: pass
        return clients

    @staticmethod
    def save_all(clients):
        CONFIG_FILE.write_text(json.dumps(
            {'devices': [c.to_dict() for c in clients]}, indent=2))

    def to_dict(self):
        return {'id': self.device_id, 'name': self.name, 'host': self.host,
                'port': self.port, 'username': self.username, 'password': self.password}

    def sdk_login(self):
        """Login via Hikvision SDK. Enables native PlayM4 decode (handles the
        non-standard H.264/H.265 some OEM cameras emit, which ffmpeg renders
        green/garbled). Returns True on success.

        Two login paths are tried, in order:
          1. V40 ISAPI-mode on the HTTP port — required by OEM rebrands
             (Safire/Sapphire by Hik) that reject the legacy login on 8000.
          2. V30 private-protocol on port 8000 — the classic Hikvision path."""
        if _SDK is None:
            return False
        if self.sdk_user_id >= 0:
            return True   # already logged in

        # Path 1: legacy V30 private-protocol login on the SDK port (8000).
        # This is the path genuine Hikvision NVRs use — try it FIRST so their
        # behaviour (incl. byStartDChan channel base) is unchanged. It fails
        # fast on OEM rebrands, which then fall through to Path 2.
        try:
            self.sdk_user_id, info = _SDK.login(self.host, 8000, self.username, self.password)
            self.start_dchan = info.byStartDChan or 33
            self.sdk_supports_download = True
            print(f'[SDK] {self.name} ({self.host}) V30/8000 → user_id={self.sdk_user_id}  '
                  f'IP chan base={self.start_dchan}')
            return True
        except Exception as e_v30:
            print(f'[SDK] V30/8000 login to {self.host} failed: {e_v30} — trying V40/ISAPI')

        # Path 2: V40 ISAPI-mode login over the HTTP port — required by OEM
        # rebrands (Safire/Sapphire by Hik) that reject the V30 login on 8000.
        try:
            self.sdk_user_id, info = _SDK.login_v40(
                self.host, self.port, self.username, self.password, login_mode=1)
            self.start_dchan = info.byStartDChan or 1
            self.sdk_supports_download = False   # OEM: stream via PlayM4, no download
            print(f'[SDK] {self.name} ({self.host}) V40/ISAPI → user_id={self.sdk_user_id}  '
                  f'IP chan base={self.start_dchan}')
            return True
        except Exception as e:
            print(f'[SDK] Login to {self.host} failed: {e}')
            self.sdk_user_id = -1
            return False

    def sdk_logout(self):
        if _SDK and self.sdk_user_id >= 0:
            try: _SDK.logout(self.sdk_user_id)
            except: pass
            self.sdk_user_id = -1

    def sdk_channel(self, channel_num):
        """Convert logical channel (1, 2, 3...) to real SDK channel (33, 34, ...)"""
        return self.start_dchan + (int(channel_num) - 1)

    def _auth(self):
        return HTTPDigestAuth(self.username, self.password)

    def _url(self, path):
        return f'http://{self.host}:{self.port}/ISAPI/{path}'

    def get(self, path):
        r = requests.get(self._url(path), auth=self._auth(), timeout=self.timeout)
        r.raise_for_status()
        return r.text

    def post(self, path, body):
        r = requests.post(self._url(path), auth=self._auth(), data=body,
                          headers={'Content-Type': 'application/xml'}, timeout=self.timeout)
        r.raise_for_status()
        return r.text

    def test(self):
        try:
            self.get('System/deviceInfo')
            # Best-effort SDK login (port 8000) — non-fatal if it fails
            self.sdk_login()
            return True, 'OK'
        except requests.exceptions.ConnectionError:
            return False, f'Cannot connect to {self.host}:{self.port}'
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401:
                return False, 'Wrong username or password'
            return False, f'HTTP error: {e.response.status_code}'
        except Exception as e:
            return False, str(e)

    def get_cameras(self):
        import xml.etree.ElementTree as ET

        def find_text(el, tag, deep=False):
            """Dohvati tekst djeteta po LOKALNOM imenu, neovisno o XML namespace-u.
            Hikvision koristi namespace hikvision.com, a OEM rebrandovi (npr.
            Safire/Sapphire 'std-cgi.com') koriste drugi — zato matchamo
            wildcardom {*} umjesto fiksnog namespacea. deep=True traži i ugniježđene."""
            prefix = './/' if deep else ''
            found = el.find(f'{prefix}{{*}}{tag}')
            if found is None:
                found = el.find(f'{prefix}{tag}')
            return found.text.strip() if found is not None and found.text else ''

        cameras = []
        try:
            xml = self.get('ContentMgmt/InputProxy/channels')
            root = ET.fromstring(xml)
            channels = root.findall('.//{*}InputProxyChannel')
            for ch in channels:
                cid  = find_text(ch, 'id')
                name = find_text(ch, 'name')
                ip   = find_text(ch, 'ipAddress', deep=True)  # ugniježđen u sourceInputPortDescriptor
                cameras.append({
                    'id': cid,
                    'name': name if name else f'Kamera {cid}',
                    'ip': ip,
                    'status': 'online'
                })
        except Exception as e:
            print(f'InputProxy error: {e} — pokušavam Streaming/channels')
            # Fallback: streaming channels
            try:
                xml = self.get('Streaming/channels')
                root = ET.fromstring(xml)
                chs = root.findall('.//{*}StreamingChannel')
                for ch in chs:
                    cid = find_text(ch, 'id')
                    if cid and cid.endswith('01'):
                        base_id = cid[:-2] if len(cid) > 2 else cid
                        name = find_text(ch, 'channelName')
                        cameras.append({'id': base_id, 'name': name if name else f'Kamera {base_id}', 'ip': '', 'status': 'online'})
            except: pass
        return cameras

    def _track_id(self, channel_id):
        """Konvertira ID kamere (1,2..) u trackID format (101,201..) za ISAPI search"""
        try:
            n = int(str(channel_id).rstrip('0'))
            return str(n * 100 + 1)
        except:
            return str(channel_id)

    def get_recordings(self, channel, date):
        import xml.etree.ElementTree as ET
        import uuid
        from datetime import timedelta
        track_id = self._track_id(channel)

        def find_text(el, path):
            """Nested XML put po LOKALNIM imenima, neovisno o namespace-u.
            'timeSpan/startTime' → '{*}timeSpan/{*}startTime' (matcha i hikvision.com
            i OEM std-cgi.com namespace). Fallback na put bez namespacea."""
            ns_path = '/'.join(f'{{*}}{p}' for p in path.split('/'))
            found = el.find(ns_path)
            if found is None:
                found = el.find(path)  # fallback bez namespacea
            return found.text.strip() if found is not None and found.text else ''

        def parse_page(xml_resp):
            root = ET.fromstring(xml_resp)
            items = root.findall('.//{*}matchList/{*}searchMatchItem')
            status_el = root.find('.//{*}responseStatusStrg')
            status = status_el.text.strip() if status_el is not None and status_el.text else ''
            page_recs = []
            for item in items:
                page_recs.append({
                    'start': find_text(item, 'timeSpan/startTime'),
                    'end':   find_text(item, 'timeSpan/endTime'),
                    'uri':   find_text(item, 'mediaSegmentDescriptor/playbackURI'),
                })
            return page_recs, status

        def fetch_window(win_start, win_end):
            """Fetch all recordings in one time window, paginating within it."""
            out = []
            position = 0
            search_id = str(uuid.uuid4())
            local_seen = set()
            for _page in range(20):
                body = f'''<?xml version="1.0" encoding="utf-8"?>
<CMSearchDescription xmlns="http://www.hikvision.com/ver20/XMLSchema">
  <searchID>{search_id}</searchID>
  <trackList>
    <trackID>{track_id}</trackID>
  </trackList>
  <timeSpanList>
    <timeSpan>
      <startTime>{win_start}</startTime>
      <endTime>{win_end}</endTime>
    </timeSpan>
  </timeSpanList>
  <contentTypeList>
    <contentType>video</contentType>
  </contentTypeList>
  <maxResults>100</maxResults>
  <searchResultPosition>{position}</searchResultPosition>
</CMSearchDescription>'''
                try:
                    xml_resp = self.post('ContentMgmt/search', body)
                except Exception as http_e:
                    print(f'[ISAPI] HTTP error: {http_e}')
                    body_no_ns = body.replace(
                        '<CMSearchDescription xmlns="http://www.hikvision.com/ver20/XMLSchema">',
                        '<CMSearchDescription>')
                    xml_resp = self.post('ContentMgmt/search', body_no_ns)
                page_recs, status = parse_page(xml_resp)
                new = []
                for r in page_recs:
                    k = (r.get('start'), r.get('end'))
                    if k in local_seen:
                        continue
                    local_seen.add(k)
                    new.append(r)
                out.extend(new)
                if not new or status not in ('MORE',) or not page_recs:
                    break
                position += len(page_recs)
            return out

        # Hikvision pagination via searchResultPosition is unreliable on many
        # firmwares (returns the same first page → recordings only up to ~mid-day).
        # Robust fix: split the 24h day into smaller time windows and search each
        # separately, then merge + dedup. Each window easily fits in one page.
        recs = []
        seen_keys = set()
        WINDOW_HOURS = 2
        day_str = date.strftime('%Y-%m-%d')   # works for both date and datetime
        h = 0
        while h < 24:
            we_h = min(h + WINDOW_HOURS, 24)
            win_start = f'{day_str}T{h:02d}:00:00Z'
            if we_h >= 24:
                win_end = f'{day_str}T23:59:59Z'
            else:
                win_end = f'{day_str}T{we_h:02d}:00:00Z'
            try:
                win_recs = fetch_window(win_start, win_end)
            except Exception as e:
                print(f'[ISAPI] window {win_start}..{win_end} error: {e}')
                win_recs = []
            added = 0
            for r in win_recs:
                k = (r.get('start'), r.get('end'))
                if k in seen_keys:
                    continue
                seen_keys.add(k)
                recs.append(r)
                added += 1
            dlog('ISAPI', f'Prozor {win_start[11:16]}–{win_end[11:16]}: '
                  f'{len(win_recs)} nađeno, {added} novih, ukupno={len(recs)}')
            h += WINDOW_HOURS

        try:
            recs.sort(key=lambda r: r.get('start', ''))
        except: pass
        dlog('ISAPI', f'UKUPNO za dan: {len(recs)} snimaka')
        return recs

    def rtsp_live_url(self, channel_id, sub=False):
        subtype = 1 if sub else 0
        return f'rtsp://{self.username}:{self.password}@{self.host}:554/Streaming/Channels/{channel_id}0{subtype+1}'

    def probe_rtsp_urls(self, channel_id):
        """
        Testira različite RTSP URL formate za dani channel_id.
        Ispisuje koje rade — kopiraj konzolu i pošalji za dijagnozu.
        """
        import subprocess as sp
        ch = str(channel_id)
        # Tipični Hikvision URL formati
        candidates = [
            f'rtsp://{self.host}:554/Streaming/Channels/{ch}01',
            f'rtsp://{self.host}:554/Streaming/Channels/{ch}02',
            f'rtsp://{self.host}:554/Streaming/Channels/{int(ch)*100+1 if ch.isdigit() else ch}',
            f'rtsp://{self.host}:554/h264/ch{ch}/main/av_stream',
            f'rtsp://{self.host}:554/h264/ch{ch}/sub/av_stream',
            f'rtsp://{self.host}:554/Streaming/tracks/{ch}01',
            f'rtsp://{self.host}:554/ISAPI/Streaming/channels/{ch}01',
        ]
        print(f'\n=== RTSP PROBE: NVR {self.host}, channel={ch} ===')
        for url in candidates:
            auth_url = url.replace(self.host, f'{self.username}:{self.password}@{self.host}')
            cmd = [
                'ffprobe', '-v', 'quiet', '-print_format', 'json',
                '-show_streams', '-rtsp_transport', 'tcp',
                '-timeout', '5000000',
                auth_url
            ]
            try:
                result = sp.run(cmd, capture_output=True, text=True, timeout=8)
                if result.returncode == 0 and '"codec_type"' in result.stdout:
                    print(f'  ✓  RADI   → {url.replace(self.host, "NVR")}')
                else:
                    stderr_short = (result.stderr or '').strip().split('\n')[-1][:80]
                    print(f'  ✗  GREŠKA → {url.replace(self.host, "NVR")}  [{stderr_short}]')
            except sp.TimeoutExpired:
                print(f'  ✗  TIMEOUT → {url.replace(self.host, "NVR")}')
            except FileNotFoundError:
                print('  [!] ffprobe nije pronađen — instaliraj ffmpeg')
                break
        print('=== KRAJ PROBE ===\n')

    def http_playback_url(self, playback_uri):
        """
        Build HTTP download URL for playback.
        Uses ISAPI/ContentMgmt/download — avoids RTSP 453 bandwidth limits.
        ffmpeg receives the stream over HTTP (no RTSP connection needed).
        """
        import urllib.parse
        encoded = urllib.parse.quote(playback_uri, safe='')
        return (f'http://{self.username}:{self.password}@'
                f'{self.host}:{self.port}'
                f'/ISAPI/ContentMgmt/download?playbackURI={encoded}')


# ── ONVIF camera (any manufacturer) ─────────────────────────────────────────────
class ONVIFClient:
    """An ONVIF camera exposed through the same interface the UI and live-stream
    path expect from NVRClient. ONVIF cameras deliver standard H.264/H.265 over
    RTSP, which ffmpeg decodes cleanly — so live works via the RTSP path. No
    Hikvision SDK, no NVR recordings (playback not applicable)."""
    device_type = 'onvif'

    def __init__(self, device_id=None, name='ONVIF Camera'):
        self.device_id = device_id or str(id(self))
        self.name      = name
        self.host      = ''
        self.port      = 80
        self.username  = ''
        self.password  = ''
        self.xaddr     = None
        self.timeout   = 8
        # Compatibility with the NVRClient-based streaming path:
        self.sdk_user_id = -1            # never uses the Hikvision SDK → RTSP path
        self.start_dchan = 1
        self.sdk_supports_download = True  # not an OEM-NVR → don't block live
        self._lenses   = []              # [{'label','main','sub'}, …]
        self.has_ptz   = False
        self._ptz_token = ''
        self.has_imaging = False
        self._img_src  = 'V_SRC_000'     # video source token for imaging
        self._cam      = None            # cached ONVIFCamera for PTZ/imaging
        # user preferences (set via Manage dialog, persisted in config)
        self.use_sub   = False           # stream quality: main (False) / sub (True)
        self.audio_on  = False           # listen to microphone
        self.extra_channels = []         # [{'name','url'}] user-added RTSP channels

    def _client(self):
        import onvif_client
        return onvif_client.ONVIFCamera(self.host, self.port, self.username,
                                        self.password, xaddr=self.xaddr)

    def _fetch_streams(self):
        """Resolve the camera's RTSP streams (main + sub) from ONVIF.

        NOTE: some cameras (e.g. O-KAM) report 2 video sources but only expose
        ONE on RTSP — the other lens is reachable only via their proprietary app.
        Sibling RTSP channels (…/avN_M) on those turn out to be delayed aliases
        of the same lens, so we DON'T auto-create extra cameras; the Manage dialog
        offers an explicit channel scan instead."""
        try:
            streams = self._client().stream_urls()   # [(label,url) main, sub…]
        except Exception as e:
            print(f'[ONVIF] {self.host} stream fetch failed: {e}')
            self._lenses = []
            return
        if not streams:
            self._lenses = []
            return
        main0 = streams[0][1]
        sub0  = streams[1][1] if len(streams) > 1 else main0
        self._lenses = [{'label': '', 'main': main0, 'sub': sub0}]
        # Append any extra RTSP channels the user picked via the channel scan.
        for ec in self.extra_channels:
            u = ec.get('url')
            if u:
                self._lenses.append({'label': ec.get('name', 'ch'),
                                     'main': u, 'sub': u})

    def test(self):
        try:
            cl = self._client()
            info = cl.get_device_information()
            self._fetch_streams()
            if not self._lenses:
                return False, 'No RTSP stream from ONVIF'
            self._cam = cl
            # Detect PTZ + imaging (day/night) support.
            try:
                pd = cl.get_profiles_detail()
                if pd:
                    self._ptz_token = pd[0]['token']
                    self._img_src = pd[0]['source'] or 'V_SRC_000'
                self.has_ptz = bool(cl.get_ptz_url())
                self.has_imaging = bool(cl.get_imaging_url())
            except Exception:
                pass
            extra = (' +PTZ' if self.has_ptz else '') + \
                    (' +IR' if self.has_imaging else '')
            return True, (info.get('model') or info.get('manufacturer') or 'OK') + extra
        except Exception as e:
            return False, str(e)

    def _cam_inst(self):
        if self._cam is None:
            self._cam = self._client()
        return self._cam

    def ptz_move(self, pan=0.0, tilt=0.0, zoom=0.0):
        """Pan/tilt/zoom the PTZ lens (velocities -1..1). Call ptz_stop() to halt."""
        if not self.has_ptz:
            return
        try:
            self._cam_inst().ptz(self._ptz_token, pan, tilt, zoom)
        except Exception as e:
            print(f'[PTZ] {self.host} move failed: {e}')

    def ptz_stop(self):
        self.ptz_move(0.0, 0.0, 0.0)

    def set_day_night(self, mode):
        """mode: 'AUTO' / 'ON' (day/colour) / 'OFF' (night/IR)."""
        if not self.has_imaging:
            return False
        try:
            return self._cam_inst().set_ir_cut_filter(self._img_src, mode)
        except Exception as e:
            print(f'[IMG] {self.host} day/night failed: {e}')
            return False

    def get_day_night(self):
        if not self.has_imaging:
            return None
        try:
            return self._cam_inst().get_ir_cut_filter(self._img_src)
        except Exception:
            return None

    def get_cameras(self):
        if not self._lenses:
            self._fetch_streams()
        if len(self._lenses) <= 1:
            return [{'id': '1', 'name': self.name, 'ip': self.host, 'status': 'online'}]
        cams = []
        for i, L in enumerate(self._lenses):
            nm = self.name if i == 0 else f'{self.name} [{L["label"] or "ch" + str(i)}]'
            cams.append({'id': str(i + 1), 'name': nm,
                         'ip': self.host, 'status': 'online'})
        return cams

    def rtsp_live_url(self, channel_id, sub=False):
        # Each channel_id maps to one lens; sub selects its sub-stream.
        sub = sub or self.use_sub          # per-camera quality preference
        if not self._lenses:
            self._fetch_streams()
        if not self._lenses:
            return ''
        idx = max(0, min(int(channel_id) - 1, len(self._lenses) - 1))
        L = self._lenses[idx]
        return L['sub'] if sub else L['main']

    # No-op SDK shims so the shared streaming code can call them uniformly.
    def sdk_login(self):  return False
    def sdk_logout(self): pass
    def sdk_channel(self, channel_num): return int(channel_num)

    # ONVIF cameras have no NVR recordings (Replay service not implemented).
    def get_recordings(self, channel, date): return []

    def to_dict(self):
        return {'id': self.device_id, 'name': self.name, 'host': self.host,
                'port': self.port, 'username': self.username,
                'password': self.password, 'type': 'onvif', 'xaddr': self.xaddr,
                'use_sub': self.use_sub, 'extra_channels': self.extra_channels}


# ── Manual RTSP camera ───────────────────────────────────────────────────────────
class ManualRTSPClient:
    """A camera defined by a raw RTSP URL the user types in. Streams via the same
    RTSP path as everything else. For any camera/stream the scanner can't find."""
    device_type = 'rtsp'

    def __init__(self, device_id=None, name='RTSP Camera'):
        self.device_id = device_id or str(id(self))
        self.name      = name
        self.url       = ''
        self.host      = ''
        self.port      = 554
        self.username  = ''
        self.password  = ''
        self.sdk_user_id = -1
        self.start_dchan = 1
        self.sdk_supports_download = True

    def _host_from_url(self):
        from urllib.parse import urlparse
        try:
            return urlparse(self.url).hostname or ''
        except Exception:
            return ''

    def test(self):
        if not self.url.strip():
            return False, 'No RTSP URL'
        self.host = self._host_from_url() or self.host
        return True, 'OK'

    def get_cameras(self):
        return [{'id': '1', 'name': self.name,
                 'ip': self._host_from_url(), 'status': 'online'}]

    def rtsp_live_url(self, channel_id, sub=False):
        return self.url

    def sdk_login(self):  return False
    def sdk_logout(self): pass
    def sdk_channel(self, channel_num): return int(channel_num)
    def get_recordings(self, channel, date): return []

    def to_dict(self):
        return {'id': self.device_id, 'name': self.name, 'host': self.host,
                'port': self.port, 'type': 'rtsp', 'url': self.url}


# ── Video Worker Thread ────────────────────────────────────────────────────────
def _sniff_codec(data: bytes):
    """
    Detect H.264 vs H.265 from a raw elementary stream by inspecting NAL headers.
    H.265 NAL type (bits 1-6 of byte after start code): VPS=32/SPS=33/PPS=34.
    H.264 NAL type (low 5 bits): SPS=7/PPS=8. Returns 'hevc', 'h264', or None.
    """
    i, n = 0, len(data)
    while i < n - 5:
        if data[i] == 0 and data[i+1] == 0 and (
                data[i+2] == 1 or (data[i+2] == 0 and i+3 < n and data[i+3] == 1)):
            off = 3 if data[i+2] == 1 else 4
            if i + off < n:
                nal = data[i + off]
                if ((nal >> 1) & 0x3F) in (32, 33, 34):
                    return 'hevc'
                if (nal & 0x1F) in (7, 8):
                    return 'h264'
            i += off
        else:
            i += 1
    return None


class VideoWorker(QThread):
    """
    Decodes a video stream (RTSP live, SDK live, or SDK playback) via ffmpeg
    into raw RGB frames and emits them as QImages.

    Performance design:
      - Backpressure: at most ONE frame "in flight" to the UI at a time. If the
        UI hasn't displayed the previous frame, new frames are dropped at the
        decode stage. This decouples decode rate from paint rate → no lag pileup.
      - Adaptive decode resolution: grid cells decode small (640×360), single/
        fullscreen/playback decode 1280×720.
      - SDK playback uses ffmpeg '-re' so the recording plays at real-time speed
        instead of being dumped as fast as the SDK can deliver it (which floods
        the UI). The fast SDK delivery is naturally throttled by pipe backpressure.
    """
    frame_ready = pyqtSignal(str, QImage)
    error       = pyqtSignal(str, str)
    position_ms = pyqtSignal(int)

    def __init__(self, channel_id, url, speed=1.0, http_auth=None, sdk_source=None,
                 decode_w=1280, decode_h=720):
        super().__init__()
        self.channel_id = channel_id
        self.rtsp_url   = url
        self.http_auth  = http_auth
        self.sdk_source = sdk_source
        self.speed      = speed
        self.decode_w   = decode_w
        self.decode_h   = decode_h
        self._stop      = threading.Event()
        self._pause     = threading.Event()
        self._proc      = None
        self._sdk_handle = -1
        self._sdk_mode   = 'playback'
        self._feed_thread = None
        self._player    = None       # PlayM4 instance (native decode path)
        self._tmp_file  = None       # temp download file for playback
        self._dl_handle = -1         # SDK download handle (must be stopped!)
        self._dl_complete = False    # True once full recording is downloaded
        self._dl_percent = 0         # live download progress (0-100)
        self._file_base_offset_s = 0.0
        self.fps        = 25.0
        # Backpressure flag — True while a frame is queued but not yet displayed
        self._frame_in_flight = False
        # Audio (playback): the same ffmpeg that decodes video also routes the
        # recording's audio track to PulseAudio, so A/V share one clock.
        self.audio_on   = True       # user mute toggle
        self.volume     = 1.0        # 0.0–1.0, applied via ffmpeg 'volume' filter
        self._has_audio = None       # None=unprobed, then True/False for the file

    # ── Public control ─────────────────────────────────────────────────────────
    def pause(self):
        self._pause.set()
        if self._player:
            try: self._player.pause(True)
            except: pass
    def resume(self):
        self._pause.clear()
        if self._player:
            try: self._player.pause(False)
            except: pass
    def is_paused(self): return self._pause.is_set()
    def set_speed(self, s): self.speed = s
    def set_audio(self, on):
        self.audio_on = bool(on)
        # PlayM4's own audio render (libAudioRender) stays silent under PipeWire,
        # so LIVE audio is handled separately via RTSP→ffmpeg→pulse. Only drive
        # PlayM4 sound for the OEM PLAYBACK-via-PlayM4 path (no ffmpeg file there).
        p = self._player
        if p is not None and self._sdk_mode != 'live':
            try: p.set_audio(self.audio_on)
            except Exception: pass

    def set_volume(self, v):
        self.volume = max(0.0, min(1.0, float(v)))
        p = self._player
        if p is not None and self._sdk_mode != 'live':
            try: p.set_volume(self.volume)
            except Exception: pass

    def _probe_has_audio(self, path):
        """Detect an audio stream in the recording file. A positive result is
        cached; a negative one is only cached once the download is complete, so
        we re-probe a still-growing file rather than latching 'no audio'."""
        if self._has_audio:
            return True
        import subprocess as sp
        found = False
        try:
            r = sp.run(['ffprobe', '-v', 'error', '-select_streams', 'a',
                        '-show_entries', 'stream=index', '-of', 'csv=p=0', path],
                       capture_output=True, text=True, timeout=5)
            found = bool(r.stdout.strip())
        except Exception:
            found = False
        if found or self._dl_complete:
            self._has_audio = found
        return found

    def _audio_active(self, path):
        """True when we should route sound out: enabled, a device exists, the
        file has audio, and volume is audible."""
        return (self.audio_on and self.volume > 0.001
                and _AUDIO_FMT is not None and self._probe_has_audio(path))

    def _audio_filter(self, spd):
        """Build the ffmpeg -af chain: tempo-match to playback speed (atempo is
        limited to 0.5–2.0, so chain it) then apply the volume gain."""
        chain = []
        s = spd if (spd and spd > 0) else 1.0
        while s > 2.0 + 1e-6:
            chain.append('atempo=2.0'); s /= 2.0
        while s < 0.5 - 1e-6:
            chain.append('atempo=0.5'); s *= 2.0
        if abs(s - 1.0) > 1e-6:
            chain.append(f'atempo={s:.4f}')
        if abs(self.volume - 1.0) > 1e-6:
            chain.append(f'volume={self.volume:.3f}')
        return ','.join(chain)

    def notify_displayed(self):
        """Called by the UI after it paints a frame — frees the in-flight slot."""
        self._frame_in_flight = False

    def stop(self):
        self._stop.set()
        self._pause.clear()
        # Stop SDK download if one is in progress (its threads spin otherwise!)
        if self._dl_handle >= 0 and _SDK:
            try: _SDK.download_stop(self._dl_handle)
            except: pass
            self._dl_handle = -1
        if self._sdk_handle >= 0 and _SDK:
            try:
                if self._sdk_mode == 'live':
                    _SDK.stop_realplay(self._sdk_handle)
                else:
                    _SDK.stop_playback(self._sdk_handle)
            except: pass
            self._sdk_handle = -1
        if self._player:
            try: self._player.close()
            except: pass
            self._player = None
        if self._proc:
            try:
                if self._proc.stdin: self._proc.stdin.close()
            except: pass
            try: self._proc.terminate()
            except: pass
        # Delete temp download file if any
        if self._tmp_file:
            try: os.unlink(self._tmp_file)
            except: pass
            self._tmp_file = None

    # ── ffmpeg builders ─────────────────────────────────────────────────────────
    def _ffmpeg_base(self, extra_in, source):
        """Common ffmpeg command. extra_in: list of input opts. source: -i value."""
        W, H = self.decode_w, self.decode_h
        vf = f'scale={W}:{H}'
        # Speed control for playback via setpts (only when not 1.0)
        if self.speed and self.speed != 1.0:
            vf += f',setpts=PTS/{self.speed}'
        return ['ffmpeg', '-loglevel', 'error', *extra_in,
                '-i', source,
                '-vf', vf, '-an',
                '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-']

    def _build_proc_rtsp(self, frame_bytes):
        import subprocess as sp
        # NOTE: '-fflags nobuffer -flags low_delay' + small probe (1M) starve the
        # demuxer on some OEM/Safire H.264/H.265 streams — ffmpeg gives up before
        # the first keyframe + SPS/PPS arrive and never emits a frame ("connecting"
        # forever). Larger probe/analyze (5M) and dropping the low-delay flags let
        # it sync reliably. Costs a little startup latency, gains decodability.
        cmd = self._ffmpeg_base(
            ['-rtsp_transport', 'tcp',
             '-analyzeduration', '5000000', '-probesize', '5000000'],
            self.rtsp_url)
        return sp.Popen(cmd, stdout=sp.PIPE, stderr=sp.PIPE, bufsize=frame_bytes)

    def _build_proc_http(self, frame_bytes):
        import subprocess as sp, requests
        from requests.auth import HTTPDigestAuth
        user, passwd = self.http_auth
        resp = requests.get(self.rtsp_url, auth=HTTPDigestAuth(user, passwd),
                            stream=True, timeout=30)
        resp.raise_for_status()
        cmd = self._ffmpeg_base(['-analyzeduration', '3000000', '-probesize', '3000000'],
                                'pipe:0')
        proc = sp.Popen(cmd, stdin=sp.PIPE, stdout=sp.PIPE, stderr=sp.PIPE,
                        bufsize=frame_bytes)

        def _feed():
            try:
                for chunk in resp.iter_content(65536):
                    if self._stop.is_set():
                        break
                    proc.stdin.write(chunk)
            except Exception:
                pass
            finally:
                try: proc.stdin.close()
                except: pass

        self._feed_thread = threading.Thread(target=_feed, daemon=True)
        self._feed_thread.start()
        return proc

    def _build_proc_sdk(self, frame_bytes):
        import subprocess as sp
        src     = self.sdk_source
        nvr     = src['nvr']
        real_ch = nvr.sdk_channel(src['channel'])
        mode    = src.get('mode', 'playback')
        self._sdk_mode = mode

        # For playback, '-re' paces ffmpeg to real-time → SDK feed naturally
        # throttled by pipe backpressure (prevents flooding the UI).
        extra_in = ['-analyzeduration', '2000000', '-probesize', '2000000']
        if mode == 'playback':
            extra_in = ['-re', *extra_in]

        cmd = self._ffmpeg_base(extra_in, 'pipe:0')
        proc = sp.Popen(cmd, stdin=sp.PIPE, stdout=sp.PIPE, stderr=sp.PIPE,
                        bufsize=frame_bytes)

        def data_cb(dtype, data):
            if self._stop.is_set():
                return
            try:
                proc.stdin.write(data)
            except (BrokenPipeError, OSError, ValueError):
                pass

        try:
            if mode == 'live':
                self._sdk_handle = _SDK.realplay(nvr.sdk_user_id, real_ch, data_cb,
                                                 sub=src.get('sub', False))
            else:
                self._sdk_handle = _SDK.playback_by_time(
                    nvr.sdk_user_id, real_ch, src['start_dt'], src['end_dt'], data_cb)
        except Exception as e:
            proc.terminate()
            raise RuntimeError(f'SDK start failed: {e}')
        return proc

    # ── PlayM4 native decode path (no ffmpeg) ────────────────────────────────────
    def _run_download_play(self):
        """
        Download the recording segment to a temp file via the SDK, then play the
        COMPLETE file with ffmpeg. A complete file has proper timestamps, so '-re'
        paces it to real-time correctly (impossible with a headerless live pipe),
        and the GPU handles scaling. This is the low-CPU playback path.
        """
        import subprocess as sp, numpy as np, tempfile
        src     = self.sdk_source
        nvr     = src['nvr']
        real_ch = nvr.sdk_channel(src['channel'])
        W, H = self.decode_w, self.decode_h
        frame_bytes = W * H * 3

        # Wait for the previous playback worker to fully release its SDK download
        # session before we start ours. Rapid switching otherwise overlaps two
        # downloads on the same channel and the NVR returns an empty/invalid file.
        wait_for = src.get('wait_for')
        if wait_for is not None:
            wait_for.wait(timeout=3.0)
        if self._stop.is_set():
            return

        tmp = tempfile.NamedTemporaryFile(prefix='hik_pb_', suffix='.mp4', delete=False)
        tmp_path = tmp.name
        tmp.close()
        self._tmp_file = tmp_path

        # ── 1. Start download in BACKGROUND; play the file as it grows ────────
        # We don't wait for 100%. The SDK writes to tmp_path continuously; ffmpeg
        # reads the growing file. We only wait for a small prebuffer so ffmpeg has
        # a valid header + a few seconds of data to start decoding.
        try:
            dh = _SDK.download_start(nvr.sdk_user_id, real_ch,
                                     src['start_dt'], src['end_dt'], tmp_path)
            self._dl_handle = dh
            dlog('DOWNLOAD', f'started (stream-as-grows) ch={real_ch} '
                            f'start={src["start_dt"]:%H:%M:%S} end={src["end_dt"]:%H:%M:%S} '
                            f'→ {tmp_path}')
        except Exception as e:
            self.error.emit(self.channel_id, f'Download failed: {e}')
            return

        # Background thread keeps the download alive + reports progress + stops it
        dl_done = {'flag': False}
        self._dl_percent = 0
        def _dl_monitor():
            last_pct = -1
            while not self._stop.is_set():
                pct = _SDK.download_progress(dh)
                if 0 <= pct <= 100:
                    self._dl_percent = pct
                if pct != last_pct and 0 <= pct <= 100:
                    last_pct = pct
                if pct >= 100 or pct < 0:
                    break
                time.sleep(0.3)
            dl_done['flag'] = True
            self._dl_percent = 100
            try: _SDK.download_stop(dh)
            except: pass
            self._dl_handle = -1
            self._dl_complete = True   # full recording now on disk → local seek OK
            try:
                if os.path.exists(tmp_path):
                    dlog('DOWNLOAD', f'finished ({os.path.getsize(tmp_path)/1024:.0f} KB)')
            except Exception:
                pass
        self._dl_complete = False
        self._dl_percent = 0
        threading.Thread(target=_dl_monitor, daemon=True).start()

        # Wait for a small prebuffer (≈2 MB) so ffmpeg can read a valid header.
        self.position_ms.emit(-1)   # show "Buffering…" via negative signal
        prebuf_t0 = time.time()
        while not self._stop.is_set():
            sz = os.path.getsize(tmp_path) if os.path.exists(tmp_path) else 0
            if sz >= 2_000_000 or dl_done['flag']:
                break
            if time.time() - prebuf_t0 > 30:
                self.error.emit(self.channel_id, 'Buffering timed out')
                return
            time.sleep(0.1)
        if self._stop.is_set():
            return
        dlog('PLAYFILE', f'prebuffer ready ({os.path.getsize(tmp_path)/1024:.0f} KB), starting playback')

        # ── 2. Play the (growing) file with GPU decode + real-time pacing ─────
        nv12_bytes = W * H * 3 // 2
        self._play_file_loop(tmp_path, W, H, nv12_bytes,
                             seek_off=src.get('seek_offset_s', 0.0))

    def _build_play_cmd(self, tmp_path, W, H, seek_off=0.0, force_sw=False):
        """ffmpeg command for playing a local recording file. Uses GPU (VAAPI)
        decode when available; force_sw=True forces pure software decode (used as
        a fallback when GPU decode produces no frames on this machine)."""
        use_gpu = bool(_VAAPI_NODE) and not force_sw
        if use_gpu:
            vf = f'scale_vaapi=w={W}:h={H},hwdownload,format=nv12'
        else:
            vf = f'scale={W}:{H}'
        spd = self.speed if (self.speed and self.speed > 0) else 1.0
        cmd = ['ffmpeg', '-loglevel', 'error', '-readrate', f'{spd:.3f}']
        # -ss BEFORE -i = fast input seek (keyframe-accurate, very fast on a file)
        if seek_off and seek_off > 0:
            cmd += ['-ss', f'{seek_off:.3f}']
        if use_gpu:
            cmd += ['-hwaccel', 'vaapi', '-hwaccel_device', _VAAPI_NODE,
                    '-hwaccel_output_format', 'vaapi']
        cmd += ['-i', tmp_path]
        # Video → stdout (raw NV12), consumed by the frame reader.
        cmd += ['-map', '0:v:0', '-vf', vf,
                '-f', 'rawvideo', '-pix_fmt', 'nv12', 'pipe:1']
        # Audio → PulseAudio/ALSA, tempo-matched to the playback speed so it
        # stays in sync with the -readrate-paced video. One ffmpeg = one clock,
        # and stalling the video pipe (pause) naturally stalls audio too.
        if self._audio_active(tmp_path):
            af = self._audio_filter(spd)
            cmd += ['-map', '0:a:0']
            if af:
                cmd += ['-af', af]
            cmd += ['-ac', '2', '-f', _AUDIO_FMT, _AUDIO_TARGET]
        return cmd, use_gpu

    def local_seek(self, offset_s):
        """Re-position playback within the already-downloaded file WITHOUT a new
        SDK download. Kills the current ffmpeg and relaunches it with -ss."""
        if not getattr(self, '_tmp_file', None):
            return
        self._local_seek_to = offset_s
        self._local_seek_req.set()
        # Stop the current ffmpeg; the play loop will relaunch at the new offset.
        if self._proc:
            try: self._proc.terminate()
            except: pass

    def _play_file_loop(self, tmp_path, W, H, nv12_bytes, seek_off=0.0):
        import subprocess as sp, numpy as np, cv2
        self._local_seek_req = threading.Event()
        self._local_seek_to = 0.0
        force_sw = False   # set True if GPU decode produced no frames → retry on CPU

        while not self._stop.is_set():
            cmd, use_gpu = self._build_play_cmd(tmp_path, W, H, seek_off,
                                                force_sw=force_sw)
            try:
                self._proc = sp.Popen(cmd, stdout=sp.PIPE, stderr=sp.PIPE,
                                      bufsize=nv12_bytes)
            except Exception as e:
                self.error.emit(self.channel_id, f'Playback failed: {e}')
                return
            dlog('PLAYFILE', f'playing {tmp_path} (GPU={"yes" if use_gpu else "no"}, '
                            f'NV12+cv2, speed={self.speed}×, -ss={seek_off:.0f}s)')

            stdout = self._proc.stdout
            # Absolute position = file's start-within-recording (_file_base_offset_s)
            # + in-file seek + frames played. Emitted position must be absolute so
            # the progress bar is right even when the file starts mid-recording.
            base_frames = int((seek_off + self._file_base_offset_s) * self.fps)
            frame_counter = 0

            relaunch = False
            while not self._stop.is_set():
                if self._local_seek_req.is_set():
                    self._local_seek_req.clear()
                    seek_off = self._local_seek_to
                    relaunch = True
                    break
                if self._pause.is_set():
                    time.sleep(0.05); continue
                if self._frame_in_flight:
                    time.sleep(0.005); continue
                raw = stdout.read(nv12_bytes)
                if len(raw) != nv12_bytes:
                    if self._proc.poll() is not None:
                        if self._local_seek_req.is_set():
                            self._local_seek_req.clear()
                            seek_off = self._local_seek_to
                            relaunch = True
                            break
                        err = b''
                        try: err = self._proc.stderr.read(2500)
                        except: pass
                        m = err.decode(errors='replace').strip()

                        # ffmpeg exited having produced NO frames → decode failed
                        # (commonly GPU/VAAPI not usable on this machine). Retry
                        # once with pure software decode before giving up.
                        if frame_counter == 0 and use_gpu and not force_sw:
                            if m:
                                log('PLAYFILE', f'GPU decode failed, retrying on CPU. ffmpeg: {m[:300]}')
                            else:
                                log('PLAYFILE', 'GPU decode produced no frames, retrying on CPU')
                            force_sw = True
                            relaunch = True
                            break

                        if frame_counter == 0:
                            # Even software decode produced nothing → real error,
                            # report it (don't silently auto-advance forever).
                            log('PLAYFILE', f'playback produced no frames. ffmpeg: {m[:300]}')
                            self.error.emit(self.channel_id, 'Playback error')
                            return

                        if m: dlog('PLAYFILE', f'ffmpeg: {m}')
                        self.error.emit(self.channel_id, 'Playback finished')
                        return
                    time.sleep(0.02); continue
                yuv = np.frombuffer(raw, np.uint8).reshape((H * 3 // 2, W))
                rgb = cv2.cvtColor(yuv, cv2.COLOR_YUV2RGB_NV12)
                rgb = np.ascontiguousarray(rgb)
                qi = QImage(rgb.data, W, H, W * 3, QImage.Format_RGB888).copy()
                self._frame_in_flight = True
                self.frame_ready.emit(self.channel_id, qi)
                frame_counter += 1
                if frame_counter % 15 == 0:
                    abs_frame = base_frames + frame_counter
                    self.position_ms.emit(int(abs_frame / self.fps * 1000))

            # tear down this ffmpeg before relaunch / exit
            if self._proc:
                try: self._proc.terminate(); self._proc.wait(timeout=2)
                except: pass
            if not relaunch:
                break

    def _run_sdk_vaapi(self):
        """
        GPU decode path: HCNetSDK delivers the encoded stream into ffmpeg, which
        decodes on the GPU via VAAPI, then outputs RGB frames we read.

        Key detail: we must tell ffmpeg the input is a raw H.264/H.265 elementary
        stream (-f hevc / -f h264) so it sets up the hardware decoder immediately
        instead of probing in software. We detect the codec from the first bytes.
        """
        import subprocess as sp, numpy as np
        src     = self.sdk_source
        nvr     = src['nvr']
        real_ch = nvr.sdk_channel(src['channel'])
        mode    = src.get('mode', 'playback')
        self._sdk_mode = mode
        W, H = self.decode_w, self.decode_h
        frame_bytes = W * H * 3

        # Buffer the first stream bytes to sniff the codec (H.264 vs H.265),
        # then launch ffmpeg with the correct -f and VAAPI HW decoder.
        first = {'buf': bytearray(), 'ready': threading.Event(), 'codec': None}

        def sniff(data):
            first['buf'].extend(data)
            if len(first['buf']) >= 4096 and not first['ready'].is_set():
                first['codec'] = _sniff_codec(bytes(first['buf']))
                first['ready'].set()

        # Start SDK stream into a queue first; we hold data until ffmpeg launches
        import queue as _q
        dq = _q.Queue(maxsize=2000)

        def sdk_data(dtype, data):
            if self._stop.is_set():
                return
            sniff(data)
            try:
                dq.put_nowait(data)
            except _q.Full:
                pass

        try:
            if mode == 'live':
                self._sdk_handle = _SDK.realplay(nvr.sdk_user_id, real_ch, sdk_data,
                                                 sub=src.get('sub', False))
            else:
                self._sdk_handle = _SDK.playback_by_time(
                    nvr.sdk_user_id, real_ch, src['start_dt'], src['end_dt'], sdk_data)
        except Exception as e:
            self.error.emit(self.channel_id, f'SDK stream failed: {e}')
            return

        # Wait for codec detection (max 3s)
        if not first['ready'].wait(3.0):
            first['codec'] = 'hevc'  # default guess
        codec = first['codec'] or 'hevc'
        dlog('VAAPI', f'{self.channel_id} codec={codec} ch={real_ch} mode={mode}')

        vf = f'scale_vaapi=w={W}:h={H},hwdownload,format=nv12'
        if mode == 'playback' and self.speed and self.speed != 1.0:
            vf = f'setpts=PTS/{self.speed},' + vf

        cmd = ['ffmpeg', '-loglevel', 'error',
               '-hwaccel', 'vaapi', '-hwaccel_device', _VAAPI_NODE,
               '-hwaccel_output_format', 'vaapi',
               '-f', codec, '-i', 'pipe:0',
               '-vf', vf, '-an',
               '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-']
        if mode == 'playback':
            cmd = cmd[:1] + ['-re'] + cmd[1:]

        try:
            self._proc = sp.Popen(cmd, stdin=sp.PIPE, stdout=sp.PIPE,
                                  stderr=sp.PIPE, bufsize=frame_bytes)
        except Exception as e:
            self.error.emit(self.channel_id, f'GPU decoder failed: {e}')
            return

        # Feeder thread: drains the queue (incl. buffered first bytes) into ffmpeg
        def feeder():
            try:
                self._proc.stdin.write(bytes(first['buf']))
            except Exception:
                pass
            while not self._stop.is_set():
                try:
                    data = dq.get(timeout=0.5)
                except _q.Empty:
                    if self._proc.poll() is not None:
                        break
                    continue
                try:
                    self._proc.stdin.write(data)
                except (BrokenPipeError, OSError, ValueError):
                    break
            try: self._proc.stdin.close()
            except: pass
        self._feed_thread = threading.Thread(target=feeder, daemon=True)
        self._feed_thread.start()

        stdout = self._proc.stdout
        frame_counter = 0
        last = time.time()
        while not self._stop.is_set():
            if self._pause.is_set():
                time.sleep(0.05); continue
            raw = stdout.read(frame_bytes)
            if len(raw) != frame_bytes:
                if self._proc.poll() is not None:
                    err = b''
                    try: err = self._proc.stderr.read(1500)
                    except: pass
                    m = err.decode(errors='replace').strip()
                    if m: dlog('VAAPI', f'{self.channel_id} ffmpeg: {m}')
                    self.error.emit(self.channel_id,
                        'Playback finished' if mode == 'playback' else 'Stream ended')
                    break
                time.sleep(0.02); continue
            if self._frame_in_flight:
                continue
            frame = np.frombuffer(raw, dtype=np.uint8).reshape((H, W, 3))
            qi = QImage(frame.data, W, H, W * 3, QImage.Format_RGB888).copy()
            self._frame_in_flight = True
            self.frame_ready.emit(self.channel_id, qi)
            frame_counter += 1
            if mode == 'playback' and frame_counter % 15 == 0:
                self.position_ms.emit(int(frame_counter / self.fps * 1000))
            if DEBUG and time.time() - last >= 2.0:
                dlog('VAAPI', f'{self.channel_id}: {frame_counter} frames')
                last = time.time()

        if self._proc:
            try: self._proc.terminate(); self._proc.wait(timeout=2)
            except: pass

    def _run_playm4(self):
        """
        Native SDK decode: HCNetSDK delivers the encoded stream straight into
        PlayM4, which decodes to YV12 and calls back. We convert YV12→RGB and
        emit. This is the iVMS-4200 path — lowest latency, lowest CPU.
        """
        src     = self.sdk_source
        nvr     = src['nvr']
        real_ch = nvr.sdk_channel(src['channel'])
        mode    = src.get('mode', 'playback')
        self._sdk_mode = mode

        # Stats
        st = {'decoded': 0, 'emitted': 0, 'dropped': 0, 'fed': 0,
              'bytes': 0, 'last': time.time(), 'conv_ms': 0.0}
        target_w, target_h = self.decode_w, self.decode_h

        def on_decoded(w, h, yv12):
            st['decoded'] += 1
            if self._stop.is_set() or self._pause.is_set():
                return
            if self._frame_in_flight:
                st['dropped'] += 1
                return
            try:
                import numpy as np
                t0 = time.time()
                rgb = yv12_to_rgb(w, h, yv12)
                if w > target_w or h > target_h:
                    ys = max(1, h // target_h)
                    xs = max(1, w // target_w)
                    rgb = np.ascontiguousarray(rgb[::ys, ::xs])
                st['conv_ms'] += (time.time() - t0) * 1000
                rh, rw = rgb.shape[0], rgb.shape[1]
                qi = QImage(rgb.data, rw, rh, rw * 3, QImage.Format_RGB888).copy()
                self._frame_in_flight = True
                st['emitted'] += 1
                self.frame_ready.emit(self.channel_id, qi)
                if st['emitted'] == 1:
                    dlog('DECODE', f'{self.channel_id}: first frame {w}x{h} → {rw}x{rh}')
            except Exception as e:
                log('DECODE', f'{self.channel_id} convert error: {e}')

            # FPS report every ~2s in debug
            now = time.time()
            if DEBUG and now - st['last'] >= 2.0:
                dt = now - st['last']
                avg_conv = st['conv_ms'] / max(1, st['emitted'])
                dlog('FPS', f'{self.channel_id}: dec={st["decoded"]/dt:.0f}/s '
                            f'emit={st["emitted"]/dt:.0f}/s drop={st["dropped"]/dt:.0f}/s '
                            f'conv={avg_conv:.1f}ms feed={st["bytes"]/1024/dt:.0f}KB/s')
                st.update(decoded=0, emitted=0, dropped=0, bytes=0, conv_ms=0.0, last=now)

        try:
            self._player = PlayM4()
            self._player.open(on_decoded, realtime=(mode == 'live'))
            dlog('PLAYM4', f'{self.channel_id}: decoder port opened (mode={mode})')
        except Exception as e:
            log('PLAYM4', f'{self.channel_id}: decoder init FAILED: {e}')
            self.error.emit(self.channel_id, f'Decoder init failed: {e}')
            return

        def sdk_data(dtype, data):
            if self._stop.is_set():
                return
            st['fed'] += 1
            st['bytes'] += len(data)
            try:
                # dtype: 1 = NET_DVR_SYSHEAD (stream header), 2 = stream data.
                self._player.input(data, is_header=(dtype == 1))
            except Exception as e:
                dlog('FEED', f'{self.channel_id} input error: {e}')

            # ── Flow control (playback only) ──────────────────────────────────
            # The NVR dumps recordings far faster than real-time (~6MB/s). If we
            # let PlayM4 decode all of it, CPU spikes and 90% of frames are
            # dropped. Throttle the SDK feed: if PlayM4 still has a lot of
            # undecoded data buffered, sleep here (this runs on the SDK thread,
            # so sleeping naturally slows the NVR's delivery). PlayM4 then
            # decodes at ~real-time and we stop wasting CPU.
            if mode == 'playback' and not self._stop.is_set():
                while not self._stop.is_set() and not self._pause.is_set():
                    remain = self._player.source_buffer_remain()
                    # Keep ~1.5 MB buffered (≈0.5-1s of HD video). Above that, wait.
                    if remain < 1_500_000:
                        break
                    time.sleep(0.02)

        try:
            if mode == 'live':
                self._sdk_handle = _SDK.realplay(nvr.sdk_user_id, real_ch, sdk_data,
                                                 sub=src.get('sub', False))
                dlog('PLAYM4', f'Live {self.channel_id} ch={real_ch} '
                              f'sub={src.get("sub", False)} handle={self._sdk_handle}')
            else:
                self._sdk_handle = _SDK.playback_by_time(
                    nvr.sdk_user_id, real_ch, src['start_dt'], src['end_dt'], sdk_data)
                dlog('PLAYM4', f'Playback {self.channel_id} ch={real_ch} '
                              f'handle={self._sdk_handle}')
        except Exception as e:
            self.error.emit(self.channel_id, f'SDK stream failed: {e}')
            return

        # Watchdog: warn if no data/frames after a few seconds
        check_at = time.time() + 5.0
        warned = False
        while not self._stop.is_set():
            time.sleep(0.1)
            if not warned and time.time() > check_at:
                warned = True
                if st['fed'] == 0:
                    log('PLAYM4', f'{self.channel_id}: WARNING no stream data from SDK after 5s')
                    # Sub-stream may not be configured on this camera. Ask the cell
                    # to fall back to the main stream.
                    if src.get('sub'):
                        self.error.emit(self.channel_id, 'NO_SUBSTREAM')
                        return
                elif st['decoded'] == 0:
                    log('PLAYM4', f'{self.channel_id}: WARNING data flowing ({st["fed"]} pkts) '
                                  f'but PlayM4 decoded 0 frames — check codec/SetStreamOpenMode')
                else:
                    dlog('PLAYM4', f'{self.channel_id}: healthy — '
                                   f'fed={st["fed"]} decoded={st["decoded"]} emitted={st["emitted"]}')

    # ── Main loop ────────────────────────────────────────────────────────────────
    def run(self):
        import numpy as np

        # SDK source routing:
        #   playback → download to file then play (GPU, low CPU, real-time paced)
        #   live     → PlayM4 native decode (proven good; VAAPI streaming gave
        #              a green/garbled image due to NV12 layout mismatch)
        if self.sdk_source is not None and _SDK:
            mode = self.sdk_source.get('mode', 'playback')
            nvr  = self.sdk_source.get('nvr')
            if mode == 'playback':
                # Genuine Hikvision: download-to-file path (supports in-file seek).
                # OEM rebrands (Safire) reject the SDK download API → their file
                # stays empty ("Buffering timed out"); stream via PlayM4 instead.
                if _PLAYM4_OK and nvr is not None and not getattr(nvr, 'sdk_supports_download', True):
                    self._run_playm4()
                else:
                    self._run_download_play()
                return
            if _PLAYM4_OK:
                self._run_playm4()
                return
            if _VAAPI_NODE:
                self._run_sdk_vaapi()
                return

        W, H = self.decode_w, self.decode_h
        frame_bytes = W * H * 3

        try:
            if self.sdk_source is not None:
                self._proc = self._build_proc_sdk(frame_bytes)
            elif self.http_auth and self.rtsp_url.startswith('http'):
                self._proc = self._build_proc_http(frame_bytes)
            else:
                self._proc = self._build_proc_rtsp(frame_bytes)
        except FileNotFoundError:
            self.error.emit(self.channel_id, 'ffmpeg not found — please install ffmpeg')
            return
        except Exception as e:
            self.error.emit(self.channel_id, f'Cannot open stream: {e}')
            return

        stdout = self._proc.stdout
        empty_count   = 0
        frame_counter = 0

        while not self._stop.is_set():
            if self._pause.is_set():
                time.sleep(0.05)
                continue

            # Read exactly one full frame (read() loops internally until n or EOF)
            raw = stdout.read(frame_bytes)

            if len(raw) != frame_bytes:
                ret = self._proc.poll()
                if ret is not None or not raw:
                    err = b''
                    try: err = self._proc.stderr.read(2000)
                    except: pass
                    msg = err.decode(errors='replace').strip()
                    if msg:
                        print(f'[ffmpeg {self.channel_id}] {msg}')
                    self.error.emit(self.channel_id,
                        'Playback finished' if self.channel_id == 'playback'
                        else 'Stream ended')
                    break
                empty_count += 1
                if empty_count > 20:
                    self.error.emit(self.channel_id, 'No video data received')
                    break
                time.sleep(0.03)
                continue

            empty_count    = 0
            frame_counter += 1

            # ── Backpressure: drop frame if UI is still busy with the last one ──
            if self._frame_in_flight:
                continue

            frame = np.frombuffer(raw, dtype=np.uint8).reshape((H, W, 3))
            qi = QImage(frame.data, W, H, W * 3, QImage.Format_RGB888).copy()
            self._frame_in_flight = True
            self.frame_ready.emit(self.channel_id, qi)

            if self.channel_id == 'playback' and frame_counter % 15 == 0:
                self.position_ms.emit(int(frame_counter / self.fps * 1000))

        if self._proc:
            try: self._proc.terminate()
            except: pass
            try: self._proc.wait(timeout=2)
            except: pass

# ── Fullscreen prozor ─────────────────────────────────────────────────────────
class ZoomableVideoLabel(QLabel):
    """A QLabel replacement for video display that supports mouse-wheel zoom
    (centred on the cursor) and click-drag panning. Call set_frame(QImage)
    each frame instead of setPixmap(). When zoom == 1.0 it behaves like the
    old label (full frame, KeepAspectRatio, centred)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._img = None          # current QImage (full-res decoded frame)
        self._zoom = 1.0          # 1.0 = fit, >1 = zoomed in
        self._min_zoom = 1.0
        self._max_zoom = 8.0
        # Pan offset in *image* pixels: the image-space point shown at widget
        # centre. Starts at image centre.
        self._cx = 0.5            # normalized 0..1 focus point (x)
        self._cy = 0.5            # normalized 0..1 focus point (y)
        self._panning = False
        self._pan_last = QPoint()
        # When True (grid cells), clicks/double-clicks are forwarded to the
        # parent so single-click-select and double-click-fullscreen still work;
        # panning only engages once zoomed in.
        self._forward_clicks = False
        self.setMouseTracking(True)

    def set_forward_clicks(self, on):
        self._forward_clicks = on

    # -- public API ---------------------------------------------------------
    def set_frame(self, qimg: QImage):
        """Supply a new video frame (full resolution)."""
        self._img = qimg
        self.update()

    def clear_frame(self):
        self._img = None
        self.reset_zoom()
        self.update()

    def reset_zoom(self):
        self._zoom = 1.0
        self._cx = 0.5
        self._cy = 0.5
        self.update()

    def has_zoom(self):
        return self._zoom > 1.001

    # -- zoom / pan input ---------------------------------------------------
    def wheelEvent(self, e):
        if self._img is None:
            return
        delta = e.angleDelta().y()
        if delta == 0:
            return
        factor = 1.25 if delta > 0 else 1 / 1.25
        old_zoom = self._zoom
        new_zoom = max(self._min_zoom, min(self._max_zoom, old_zoom * factor))
        if abs(new_zoom - old_zoom) < 1e-6:
            return

        # Zoom centred on cursor: keep the image point under the cursor fixed.
        # Compute image-space point under cursor at old zoom, then set focus so
        # it stays under the cursor at the new zoom.
        ip = self._widget_to_img(e.pos(), old_zoom)
        if ip is not None:
            iw = self._img.width()
            ih = self._img.height()
            # Where is the cursor within the widget, normalized -0.5..0.5
            disp = self._display_rect(old_zoom)
            if disp.width() > 0 and disp.height() > 0:
                self._zoom = new_zoom
                # New focus so that image point ip maps back under the cursor
                cur = e.pos()
                disp2 = self._display_rect(new_zoom)
                # fraction of cursor across the *visible* viewport
                fx = (cur.x() - self.width() / 2) / self.width()
                fy = (cur.y() - self.height() / 2) / self.height()
                # visible span in normalized image coords at new zoom
                vis_w = self._visible_frac_w(new_zoom)
                vis_h = self._visible_frac_h(new_zoom)
                self._cx = ip[0] / iw - fx * vis_w
                self._cy = ip[1] / ih - fy * vis_h
                self._clamp_focus(new_zoom)
        else:
            self._zoom = new_zoom
            self._clamp_focus(new_zoom)

        if self._zoom <= 1.001:
            self.reset_zoom()
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.has_zoom():
            self._panning = True
            self._pan_last = e.pos()
            self.setCursor(Qt.ClosedHandCursor)
        elif self._forward_clicks:
            e.ignore()            # let the parent VideoCell handle selection
        else:
            super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._panning and self._img is not None:
            dx = e.pos().x() - self._pan_last.x()
            dy = e.pos().y() - self._pan_last.y()
            self._pan_last = e.pos()
            # Convert pixel drag to normalized focus shift (inverted: drag right
            # moves image right => focus moves left).
            self._cx -= dx / self.width() * self._visible_frac_w(self._zoom)
            self._cy -= dy / self.height() * self._visible_frac_h(self._zoom)
            self._clamp_focus(self._zoom)
            self.update()
        else:
            super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._panning:
            self._panning = False
            self.setCursor(Qt.OpenHandCursor if self.has_zoom() else Qt.ArrowCursor)
        elif self._forward_clicks:
            e.ignore()
        else:
            super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e):
        # In grid mode, double-click belongs to the cell (fullscreen), not zoom.
        if self._forward_clicks:
            e.ignore()
            return
        # Standalone (fullscreen/playback): double-click resets zoom.
        if self.has_zoom():
            self.reset_zoom()
            self.setCursor(Qt.ArrowCursor)
        else:
            super().mouseDoubleClickEvent(e)

    # -- geometry helpers ---------------------------------------------------
    def _visible_frac_w(self, zoom):
        return 1.0 / zoom

    def _visible_frac_h(self, zoom):
        return 1.0 / zoom

    def _clamp_focus(self, zoom):
        half_w = self._visible_frac_w(zoom) / 2
        half_h = self._visible_frac_h(zoom) / 2
        self._cx = max(half_w, min(1 - half_w, self._cx))
        self._cy = max(half_h, min(1 - half_h, self._cy))

    def _display_rect(self, zoom):
        """The rect (in widget coords) where the full frame would be drawn at
        zoom=1 with KeepAspectRatio (letterboxed)."""
        if self._img is None:
            return QRectF()
        iw, ih = self._img.width(), self._img.height()
        ww, wh = self.width(), self.height()
        if iw == 0 or ih == 0:
            return QRectF()
        scale = min(ww / iw, wh / ih)
        dw, dh = iw * scale, ih * scale
        x = (ww - dw) / 2
        y = (wh - dh) / 2
        return QRectF(x, y, dw, dh)

    def _widget_to_img(self, pos, zoom):
        """Map a widget-space point to image-space pixels at the given zoom."""
        if self._img is None:
            return None
        iw, ih = self._img.width(), self._img.height()
        vis_w = self._visible_frac_w(zoom)
        vis_h = self._visible_frac_h(zoom)
        # Source rect (in normalized image coords) currently shown:
        sx0 = self._cx - vis_w / 2
        sy0 = self._cy - vis_h / 2
        fx = pos.x() / self.width()
        fy = pos.y() / self.height()
        nx = sx0 + fx * vis_w
        ny = sy0 + fy * vis_h
        return (nx * iw, ny * ih)

    # -- rendering ----------------------------------------------------------
    def paintEvent(self, e):
        if self._img is None:
            super().paintEvent(e)   # show text placeholder etc.
            return
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0))
        iw, ih = self._img.width(), self._img.height()
        if iw == 0 or ih == 0:
            return

        if self._zoom <= 1.001:
            # Fit whole frame, KeepAspectRatio, centred (old behaviour).
            target = self._display_rect(1.0)
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            p.drawImage(target, self._img, QRectF(0, 0, iw, ih))
        else:
            # Show a sub-region of the image scaled to fill the widget.
            vis_w = self._visible_frac_w(self._zoom)
            vis_h = self._visible_frac_h(self._zoom)
            sx = (self._cx - vis_w / 2) * iw
            sy = (self._cy - vis_h / 2) * ih
            sw = vis_w * iw
            sh = vis_h * ih
            src = QRectF(sx, sy, sw, sh)
            # Keep aspect ratio inside the widget (letterbox if needed).
            target = self._display_rect(1.0)
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            p.drawImage(target, self._img, src)

            # Small zoom indicator in the corner.
            p.setPen(QColor(255, 255, 255, 200))
            f = p.font(); f.setPixelSize(11); p.setFont(f)
            p.fillRect(6, 6, 54, 18, QColor(0, 0, 0, 140))
            p.drawText(10, 19, f'{self._zoom:.1f}×')


class FullscreenWindow(QWidget):
    def __init__(self, channel_id, name, nvr, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f'Live: {name}')
        self.setStyleSheet('background: black;')
        self.resize(1280, 720)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        self.label = ZoomableVideoLabel(f'⏳ Connecting {name}...')
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setStyleSheet('color: #555; font-size: 14px;')
        lay.addWidget(self.label)

        real_ch = channel_id.split('_', 1)[-1] if '_' in channel_id else channel_id
        self.worker = VideoWorker(f'fs_{channel_id}', nvr.rtsp_live_url(real_ch),
                                  decode_w=1280, decode_h=720)
        self.worker.frame_ready.connect(self._on_frame, Qt.QueuedConnection)
        self.worker.error.connect(lambda _, m: self.label.setText(f'⚠ {m}'))
        self.worker.start()

    def _on_frame(self, _, qi):
        if self.worker:
            self.worker.notify_displayed()
        self.label.set_frame(qi)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Escape, Qt.Key_F, Qt.Key_Q):
            self.close()

    def closeEvent(self, e):
        self.worker.stop()
        self.worker.wait(2000)
        super().closeEvent(e)


# ── Video Cell Widget ──────────────────────────────────────────────────────────
class VideoCell(QFrame):
    clicked = pyqtSignal(str)

    def __init__(self, channel_id, name, nvr: NVRClient, parent=None, real_channel=None):
        super().__init__(parent)
        self.channel_id   = channel_id
        self.real_channel = real_channel or channel_id  # actual NVR channel for RTSP
        self.name = name
        self.nvr = nvr
        self.worker = None
        self._streaming = False
        self._audio_on = False   # live audio for this cell (managed by LiveViewTab)
        self._volume   = 1.0

        self.setFrameShape(QFrame.Box)
        self.setStyleSheet(f'QFrame {{ background: #0d1117; border: 1px solid #21262d; border-radius: 4px; }}')
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(320, 180)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # Video label (zoomable)
        self.video_label = ZoomableVideoLabel()
        self.video_label.setAlignment(Qt.AlignCenter)
        self.video_label.setStyleSheet('border: none; background: transparent;')
        self.video_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.video_label.setScaledContents(False)
        self.video_label.set_forward_clicks(True)  # grid: click selects, dbl=fullscreen
        lay.addWidget(self.video_label)

        # Placeholder
        self.placeholder = QLabel(f'📷\n{name}')
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.placeholder.setStyleSheet(f'color: {DARK["dim"]}; font-size: 13px; border: none;')
        lay.addWidget(self.placeholder)
        self.video_label.hide()

        # Bottom bar
        bar = QWidget()
        bar.setFixedHeight(30)
        bar.setStyleSheet(f'background: rgba(0,0,0,0.7); border: none;')
        bar_lay = QHBoxLayout(bar)
        bar_lay.setContentsMargins(8, 2, 8, 2)

        self.name_label = QLabel(name)
        self.name_label.setStyleSheet(f'color: white; font-size: 11px; border: none;')
        bar_lay.addWidget(self.name_label)
        bar_lay.addStretch()

        self.status_dot = QLabel('●')
        self.status_dot.setStyleSheet(f'color: {DARK["dim"]}; font-size: 10px; border: none;')
        bar_lay.addWidget(self.status_dot)

        lay.addWidget(bar)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit(self.channel_id)

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            if not self._streaming:
                self.start_stream()
            else:
                self._open_fullscreen()

    def _open_fullscreen(self):
        if not self._streaming:
            return
        self._fs_win = FullscreenWindow(self.channel_id, self.name, self.nvr)
        self._fs_win.show()

    def start_stream(self, sub=False):
        # Zombie detection: flag says streaming but worker is gone
        if self._streaming:
            if not self.worker or not self.worker.isRunning():
                dlog('Cell', f'{self.name} zombie streaming flag — clearing')
                self._streaming = False
            else:
                dlog('CELL', f'{self.name}: already streaming, skip start')
                return

        # OEM rebrands (Safire/Sapphire by Hik) emit a non-standard LIVE stream
        # (H.264 profile 53 / H.265+) that no available decoder — ffmpeg, VLC,
        # GStreamer, even Hikvision's own PlayM4 — can render. Rather than a green/
        # garbled image or an endless "Connecting…", say so plainly. Their
        # RECORDINGS are standard, so Playback works fine.
        if getattr(self.nvr, 'sdk_supports_download', True) is False:
            self._streaming = False
            self.video_label.hide()
            self.placeholder.setText('ℹ️\nLive nije podržan za ovaj uređaj\n(koristi Playback)')
            self.placeholder.show()
            self.status_dot.setStyleSheet(f'color: {DARK["dim"]}; font-size: 10px; border: none;')
            return

        # Clear any stale frame from a previous camera so we don't show its last
        # image while the new stream spins up.
        self.video_label.clear_frame()
        self.video_label.hide()
        self.placeholder.show()

        self._streaming = True
        self.status_dot.setStyleSheet(f'color: {DARK["amber"]}; font-size: 10px; border: none;')
        self.placeholder.setText(f'⏳\nConnecting...')

        # Decode resolution: sub-stream (multi-view) small, main-stream (1×1) full
        if sub:
            dw, dh = 640, 360
        else:
            dw, dh = 1280, 720

        # Prefer SDK + PlayM4 native decode (no ffmpeg, no RTSP bandwidth limit).
        # ONLY for genuine Hikvision (sdk_supports_download). OEM rebrands (Safire)
        # emit a non-standard live stream (H.264 profile 53) that PlayM4 cannot
        # decode → it would feed data but yield 0 frames ("connecting" forever).
        # Those fall through to the RTSP path instead.
        if (_PLAYM4_OK and _SDK and self.nvr.sdk_user_id >= 0
                and getattr(self.nvr, 'sdk_supports_download', True)):
            dlog('Live', f'SDK {"sub" if sub else "main"} {self.name}  '
                  f'ch={self.real_channel}  decode={dw}x{dh}')
            self.worker = VideoWorker(
                self.channel_id, '', decode_w=dw, decode_h=dh,
                sdk_source={'nvr': self.nvr, 'channel': self.real_channel,
                            'mode': 'live', 'sub': sub})
        else:
            url = self.nvr.rtsp_live_url(self.real_channel, sub)
            dlog('Live', f'RTSP {"sub" if sub else "main"} {self.name} → '
                  f'{url.replace(self.nvr.password, "***")}  decode={dw}x{dh}')
            self.worker = VideoWorker(self.channel_id, url, decode_w=dw, decode_h=dh)

        # Apply this cell's live-audio state before the stream starts so sound
        # comes on with the first frame (only the selected cell is ever audio-on).
        self.worker.set_audio(self._audio_on)
        self.worker.set_volume(self._volume)
        self.worker.frame_ready.connect(self._on_frame, Qt.QueuedConnection)
        self.worker.error.connect(self._on_error)
        self.worker.start()

    def apply_audio(self, on, volume):
        """Set live audio for this cell (LiveViewTab enables only the selected
        camera). Applies immediately to a running worker; PlayM4 toggles sound
        without restarting the stream."""
        self._audio_on = bool(on)
        self._volume   = max(0.0, min(1.0, float(volume)))
        if self.worker is not None:
            self.worker.set_audio(self._audio_on)
            self.worker.set_volume(self._volume)

    def stop_stream(self):
        if self.worker:
            dlog('Cell', f'{self.name} STOP')
            old = self.worker
            self.worker = None
            # Disconnect signals FIRST so late frames from the dying worker don't
            # hit _on_frame and interfere with the next stream.
            try: old.frame_ready.disconnect(self._on_frame)
            except: pass
            try: old.error.disconnect(self._on_error)
            except: pass
            # Background cleanup — UI stays responsive
            threading.Thread(
                target=lambda w=old: (w.stop(), w.wait(3000)),
                daemon=True
            ).start()
        self._streaming = False
        self.video_label.hide()
        self.placeholder.setText(f'📷\n{self.name}')
        self.placeholder.show()
        self.status_dot.setStyleSheet(f'color: {DARK["dim"]}; font-size: 10px; border: none;')
        self.setStyleSheet(f'QFrame {{ background: #0d1117; border: 1px solid #21262d; border-radius: 4px; }}')

    def _on_frame(self, cid, qi):
        if self.worker:
            self.worker.notify_displayed()
        if not self._streaming:
            return
        self.video_label.set_frame(qi)
        if not self.video_label.isVisible():
            self.placeholder.hide()
            self.video_label.show()
            self.status_dot.setStyleSheet(f'color: {DARK["green"]}; font-size: 10px; border: none;')
            self.setStyleSheet(f'QFrame {{ background: #0d1117; border: 1px solid {DARK["green"]}44; border-radius: 4px; }}')

    def _on_error(self, cid, msg):
        if msg == 'NO_SUBSTREAM':
            # Sub-stream not available on this camera → retry with main stream.
            dlog('Cell', f'{self.name} no sub-stream, falling back to main')
            self._streaming = False
            QTimer.singleShot(100, lambda: self.start_stream(sub=False))
            return
        self._streaming = False
        self.video_label.hide()
        self.placeholder.setText(f'⚠️\n{msg}')
        self.placeholder.show()
        self.status_dot.setStyleSheet(f'color: {DARK["red"]}; font-size: 10px; border: none;')

class LiveAudioPlayer:
    """Plays ONE live camera's audio via a dedicated audio-only ffmpeg → Pulse.
    Kept fully separate from the video pipeline (SDK/PlayM4 video is untouched):
    Hikvision's own PlayM4 audio render stays silent under PipeWire, but the
    camera's RTSP main stream carries the same audio track as the recordings, so
    we pull just that. Only one camera plays at a time."""
    def __init__(self):
        self._proc   = None
        self._lock   = threading.Lock()
        self._url    = None
        self._volume = 1.0

    def _build_cmd(self, url):
        cmd = ['ffmpeg', '-loglevel', 'error', '-nostdin',
               '-rtsp_transport', 'tcp', '-fflags', 'nobuffer',
               '-i', url, '-vn', '-map', '0:a:0?', '-ac', '2']
        if abs(self._volume - 1.0) > 1e-6:
            cmd += ['-af', f'volume={self._volume:.3f}']
        cmd += ['-f', _AUDIO_FMT, _AUDIO_TARGET]
        return cmd

    def _stop_proc_locked(self):
        if self._proc:
            try: self._proc.terminate()
            except Exception: pass
            try: self._proc.wait(timeout=1)
            except Exception:
                try: self._proc.kill()
                except Exception: pass
            self._proc = None

    def _relaunch_locked(self):
        import subprocess as sp
        self._stop_proc_locked()
        if not self._url or _AUDIO_FMT is None:
            return
        try:
            self._proc = sp.Popen(self._build_cmd(self._url),
                                   stdin=sp.DEVNULL, stdout=sp.DEVNULL, stderr=sp.DEVNULL)
            dlog('LIVE-AUDIO', f'ffmpeg started ({_AUDIO_FMT})')
        except Exception as e:
            print(f'[LiveAudio] start failed: {e}')
            self._proc = None

    def play(self, url):
        with self._lock:
            self._url = url
            self._relaunch_locked()

    def set_volume(self, v):
        with self._lock:
            self._volume = max(0.0, min(1.0, float(v)))
            if self._proc and self._proc.poll() is None:
                self._relaunch_locked()   # volume is baked into the filter

    def stop(self):
        with self._lock:
            self._url = None
            self._stop_proc_locked()


# ── Live View Tab ──────────────────────────────────────────────────────────────
class LiveViewTab(QWidget):
    # Emitted from a background ffprobe thread → UI: (camera uid, has_audio)
    _audio_probed = pyqtSignal(str, bool)

    def __init__(self, nvr: NVRClient, parent=None):
        super().__init__(parent)
        self.nvr = nvr
        self.cameras = []
        self.cells = {}
        self._cam_audio = {}      # uid -> True/False (has audio); missing = unprobed
        self._audio_probing = set()   # uids with an in-flight probe
        self._audio_probed.connect(self._on_audio_probed)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        # Toolbar
        toolbar = QHBoxLayout()
        self.btn_all = QPushButton('▶  Start all')
        self.btn_all.setProperty('class', 'success')
        self.btn_all.clicked.connect(self.start_all)

        self.btn_stop = QPushButton('■  Stop all')
        self.btn_stop.setProperty('class', 'danger')
        self.btn_stop.clicked.connect(self.stop_all)

        # Quality auto-select label (replaces the old sub-stream toggle)
        self.lbl_quality = QLabel('Quality: auto')
        self.lbl_quality.setStyleSheet(f'color:{DARK["dim"]};font-size:11px;padding:0 8px;')

        self.grid_combo = QComboBox()
        self.grid_combo.addItems(['1×1', '2×2', '3×3', '4×4', '1+3', '1+7'])
        self.grid_combo.setFixedWidth(110)
        self.grid_combo.currentIndexChanged.connect(self._on_grid_changed)

        self.ptz_box = self._build_ptz_controls()
        self.ptz_box.setVisible(False)

        # ── Live audio (follows the selected camera; only one plays at a time) ──
        # Audio comes from the camera's RTSP main stream via a separate ffmpeg,
        # independent of the SDK/PlayM4 video pipeline.
        self._live_audio_on = False
        self._live_volume   = 1.0
        self._audio_player   = LiveAudioPlayer()
        self.btn_live_mute = QPushButton('🔇')
        self.btn_live_mute.setFixedWidth(46)
        self.btn_live_mute.setStyleSheet('font-size:20px;')
        self.btn_live_mute.setToolTip('Listen to the selected camera')
        self.btn_live_mute.clicked.connect(self._on_live_mute)
        self.vol_live = QSlider(Qt.Horizontal)
        self.vol_live.setRange(0, 100)
        self.vol_live.setValue(100)
        self.vol_live.setFixedWidth(90)
        self.vol_live.setToolTip('Volume')
        # Update the number live; relaunch the audio ffmpeg only on release/click.
        self.vol_live.valueChanged.connect(self._on_live_volume_preview)
        self.vol_live.sliderReleased.connect(self._on_live_volume_committed)
        if _AUDIO_FMT is None:
            self.btn_live_mute.setEnabled(False)
            self.vol_live.setEnabled(False)
            self.btn_live_mute.setToolTip('No audio output device available')

        toolbar.addWidget(self.btn_all)
        toolbar.addWidget(self.btn_stop)
        toolbar.addWidget(self.lbl_quality)
        toolbar.addStretch()
        toolbar.addWidget(self.btn_live_mute)
        toolbar.addWidget(self.vol_live)
        toolbar.addSpacing(12)
        toolbar.addWidget(self.ptz_box)
        toolbar.addWidget(QLabel('Layout:'))
        toolbar.addWidget(self.grid_combo)
        lay.addLayout(toolbar)

        # Grid area
        self.grid_widget = QWidget()
        self.grid_widget.setAcceptDrops(True)
        self.grid_widget.dragEnterEvent = self._grid_drag_enter
        self.grid_widget.dropEvent      = self._grid_drop
        self.grid_layout = QGridLayout(self.grid_widget)
        self.grid_layout.setSpacing(6)
        lay.addWidget(self.grid_widget)

    def _build_ptz_controls(self):
        """Compact PTZ d-pad — shown only when the selected camera supports PTZ.
        Hold a button to move, release to stop (ONVIF ContinuousMove/Stop)."""
        from PyQt5.QtWidgets import QGridLayout
        w = QWidget()
        g = QGridLayout(w); g.setContentsMargins(0, 0, 0, 0); g.setSpacing(1)
        lbl = QLabel('PTZ '); lbl.setStyleSheet(f'color:{DARK["dim"]};font-size:10px;')
        g.addWidget(lbl, 1, 0)

        def mk(text, pan, tilt, zoom, tip):
            b = QPushButton(text); b.setFixedSize(26, 20); b.setToolTip(tip)
            b.pressed.connect(lambda: self._ptz(pan, tilt, zoom))
            b.released.connect(self._ptz_stop)
            return b
        # Cross d-pad: up / left·right / down, then zoom out/in.
        g.addWidget(mk('↑', 0,  0.6, 0, 'Tilt up'),    0, 2)
        g.addWidget(mk('←', -0.6, 0, 0, 'Pan left'),   1, 1)
        g.addWidget(mk('→', 0.6,  0, 0, 'Pan right'),  1, 3)
        g.addWidget(mk('↓', 0, -0.6, 0, 'Tilt down'),  2, 2)
        zl = QLabel(' Zoom'); zl.setStyleSheet(f'color:{DARK["dim"]};font-size:10px;')
        g.addWidget(zl, 1, 4)
        g.addWidget(mk('–', 0, 0, -0.6, 'Zoom out'),   1, 5)
        g.addWidget(mk('+', 0, 0,  0.6, 'Zoom in'),    1, 6)
        return w

    def _ptz(self, pan, tilt, zoom):
        nvr = self.nvr
        if nvr is not None and getattr(nvr, 'has_ptz', False):
            nvr.ptz_move(pan, tilt, zoom)

    def _ptz_stop(self):
        nvr = self.nvr
        if nvr is not None and getattr(nvr, 'has_ptz', False):
            nvr.ptz_stop()

    def set_cameras(self, cameras):
        self.cameras = cameras
        self.stop_all()
        self.cells.clear()
        self._update_quality_label()
        self.selected_cam_id = cameras[0]['id'] if cameras else None
        for cam in cameras[:16]:
            nvr = cam.get('_nvr', self.nvr)
            uid = cam['id']  # already uid after _refresh_camera_list transform
            # Extract real channel id for RTSP (strip deviceid_ prefix)
            real_ch = uid.split('_', 1)[-1] if '_' in uid else uid
            cell = VideoCell(uid, cam['name'], nvr, real_channel=real_ch)
            cell.clicked.connect(self.select_camera)
            self.cells[uid] = cell
        self.relayout(0)

    def select_camera(self, channel_id):
        """Select camera for 1×1 view. To avoid a slow gap while the old worker
        dies, we START the new camera first, then stop the old one a moment later
        (overlap). This makes switching feel instant."""
        old_id = getattr(self, 'selected_cam_id', None)
        self.selected_cam_id = channel_id
        self.ptz_box.setVisible(bool(getattr(self.nvr, 'has_ptz', False)))

        # Audio is OFF by default on EVERY camera — never carry it across a switch.
        if channel_id != old_id and self._live_audio_on:
            self._live_audio_on = False
            self.btn_live_mute.setText('🔇')

        if self.grid_combo.currentIndex() == 0:
            self.relayout(0)
            new_cell = self.cells.get(channel_id)
            if new_cell:
                # Clear stale flag if the worker died
                if new_cell._streaming and (not new_cell.worker or
                                            not new_cell.worker.isRunning()):
                    new_cell._streaming = False
                if not new_cell._streaming:
                    new_cell.start_stream(self._should_use_sub())

            # Stop the previously shown camera shortly after, so its teardown
            # overlaps the new stream's startup instead of blocking it.
            if old_id and old_id != channel_id:
                old_cell = self.cells.get(old_id)
                if old_cell and old_cell._streaming:
                    QTimer.singleShot(400, lambda c=old_cell: (
                        c.stop_stream() if c is not self.cells.get(self.selected_cam_id)
                        else None))
        else:
            self.relayout(self.grid_combo.currentIndex())
        # Move live audio to the newly selected camera (silence the others).
        self._probe_selected_audio()   # detect audio capability (async, cached)
        self._apply_live_audio()

    def _selected_rtsp_url(self):
        sel = getattr(self, 'selected_cam_id', None)
        cell = self.cells.get(sel) if sel else None
        if cell is None:
            return sel, None
        try:
            return sel, cell.nvr.rtsp_live_url(cell.real_channel, False)  # main stream
        except Exception as e:
            dlog('LIVE-AUDIO', f'no RTSP url: {e}')
            return sel, None

    def _probe_selected_audio(self):
        """Detect whether the selected camera's stream carries audio (background
        ffprobe, cached per camera) so we can disable the controls if it doesn't."""
        if _AUDIO_FMT is None:
            return
        sel, url = self._selected_rtsp_url()
        if not sel or not url or sel in self._cam_audio or sel in self._audio_probing:
            self._update_audio_controls()
            return

        self._audio_probing.add(sel)

        def _probe(uid=sel, u=url):
            import subprocess as sp
            has = True   # optimistic default if the probe itself fails
            try:
                r = sp.run(['ffprobe', '-v', 'error', '-rtsp_transport', 'tcp',
                            '-select_streams', 'a', '-show_entries', 'stream=index',
                            '-of', 'csv=p=0', u],
                           capture_output=True, text=True, timeout=10)
                if r.returncode == 0:
                    has = bool(r.stdout.strip())
            except Exception as e:
                dlog('LIVE-AUDIO', f'probe failed for {uid}: {e}')
            self._audio_probed.emit(uid, has)
        threading.Thread(target=_probe, daemon=True).start()

    def _on_audio_probed(self, uid, has):
        self._cam_audio[uid] = has
        self._audio_probing.discard(uid)
        dlog('LIVE-AUDIO', f'{uid}: {"has audio" if has else "NO audio"}')
        if uid == getattr(self, 'selected_cam_id', None):
            self._update_audio_controls()
            self._apply_live_audio()   # re-evaluate playback now capability is known

    def _update_audio_controls(self):
        """Enable/disable the live-audio controls for the selected camera based on
        the probed capability. A camera known to have no audio gets them greyed."""
        if _AUDIO_FMT is None:
            return
        sel = getattr(self, 'selected_cam_id', None)
        has = self._cam_audio.get(sel, None)   # None = still probing → stay enabled
        no_audio = (has is False)
        self.btn_live_mute.setEnabled(not no_audio)
        self.vol_live.setEnabled(not no_audio)
        if no_audio:
            self.btn_live_mute.setText('🔇')
            self.btn_live_mute.setToolTip('This camera has no audio')
            self._audio_player.stop()   # nothing to play
        else:
            self.btn_live_mute.setText('🔊' if self._live_audio_on else '🔇')
            self.btn_live_mute.setToolTip('Listen to the selected camera')

    def _apply_live_audio(self):
        """Play audio for the selected camera only (one stream at a time), or stop
        it when muted / nothing selected. Uses the camera's RTSP main stream."""
        self._update_audio_controls()
        sel = getattr(self, 'selected_cam_id', None)
        # Skip cameras known to have no audio.
        if self._cam_audio.get(sel, None) is False:
            self._audio_player.stop()
            return
        _, url = self._selected_rtsp_url()
        if self._live_audio_on and url:
            self._audio_player.set_volume(self._live_volume)
            self._audio_player.play(url)
            return
        self._audio_player.stop()

    def _on_live_mute(self):
        self._live_audio_on = not self._live_audio_on
        self.btn_live_mute.setText('🔊' if self._live_audio_on else '🔇')
        name = getattr(self, 'selected_cam_id', None) or '?'
        dlog('LIVE-AUDIO', f'{"on" if self._live_audio_on else "off"} (cam={name})')
        self._probe_selected_audio()
        self._apply_live_audio()

    def _on_live_volume_preview(self, val):
        self._live_volume = val / 100.0
        if self._live_audio_on:
            self.btn_live_mute.setText('🔊' if self._live_volume > 0 else '🔇')
        # Commit immediately for click/keyboard (not held); drag commits on release.
        if not self.vol_live.isSliderDown():
            self._audio_player.set_volume(self._live_volume)

    def _on_live_volume_committed(self):
        self._audio_player.set_volume(self._live_volume)

    def _should_use_sub(self):
        """Sub-stream for multi-view (saves NVR bandwidth), main for 1×1."""
        return self.grid_combo.currentIndex() != 0

    def _update_quality_label(self):
        sub = self._should_use_sub()
        self.lbl_quality.setText(f'Quality: {"sub (low)" if sub else "main (HD)"}')

    def _on_grid_changed(self, idx):
        """Restart active streams when grid changes to apply new quality."""
        self._update_quality_label()
        self.relayout(idx)
        # Restart any active streams with appropriate quality
        sub = self._should_use_sub()
        for cell in self.cells.values():
            if cell._streaming:
                cell.stop_stream()
                # Tiny delay between stop/start to let NVR release the session
                QTimer.singleShot(300, lambda c=cell, s=sub: c.start_stream(s))

    def relayout(self, idx=None):
        while self.grid_layout.count():
            self.grid_layout.takeAt(0)

        cams = list(self.cells.values())
        if not cams:
            return

        sel = self.cells.get(getattr(self, 'selected_cam_id', None)) or cams[0]
        mode = self.grid_combo.currentIndex()

        # Determine which cells are visible in this layout, hide the rest.
        # takeAt() only removes the layout item — the widget stays visible at its
        # old position unless we explicitly hide it (the "ghost grid in 1×1" bug).
        if mode == 0:
            visible = [sel]
        elif mode == 1:
            visible = cams[:4]
        elif mode == 2:
            visible = cams[:9]
        elif mode == 3:
            visible = cams[:16]
        elif mode in (4, 5):
            visible = cams[:(4 if mode == 4 else 8)]
        else:
            visible = cams
        for c in self.cells.values():
            if c in visible:
                c.show()
            else:
                c.hide()

        if mode == 0:    # 1×1
            self.grid_layout.addWidget(sel, 0, 0)
        elif mode == 1:  # 2×2
            for i, c in enumerate(cams[:4]):
                self.grid_layout.addWidget(c, i//2, i%2)
        elif mode == 2:  # 3×3
            for i, c in enumerate(cams[:9]):
                self.grid_layout.addWidget(c, i//3, i%3)
        elif mode == 3:  # 4×4
            for i, c in enumerate(cams[:16]):
                self.grid_layout.addWidget(c, i//4, i%4)
        elif mode == 4:  # 1+3
            self.grid_layout.addWidget(sel, 0, 0, 2, 2)
            others = [c for c in cams if c is not sel]
            for i, c in enumerate(others[:3]):
                self.grid_layout.addWidget(c, i, 2)
        elif mode == 5:  # 1+7
            self.grid_layout.addWidget(sel, 0, 0, 2, 2)
            others = [c for c in cams if c is not sel]
            for i, c in enumerate(others[:7]):
                row, col = divmod(i, 2)
                # Right column (2 rows) + bottom row
                if i < 2:
                    self.grid_layout.addWidget(c, i, 2)
                else:
                    self.grid_layout.addWidget(c, 2, i-2)

    def _grid_drag_enter(self, e):
        if e.mimeData().hasText():
            e.acceptProposedAction()

    def _grid_drop(self, e):
        uid = e.mimeData().text()
        if uid not in self.cells:
            return
        cell = self.cells[uid]
        if self.grid_combo.currentIndex() == 0:
            # Already in 1×1 → make this the single shown camera
            self.select_camera(uid)
            if not cell._streaming:
                cell.start_stream(self._should_use_sub())
        else:
            # Multi-view (2×2, 3×3, …): just start this camera in its own cell;
            # do NOT collapse to 1×1.
            if not cell._streaming:
                cell.start_stream(self._should_use_sub())

    def start_all(self):
        sub = self._should_use_sub()
        for cell in self.cells.values():
            cell.start_stream(sub)
        self._apply_live_audio()

    def stop_all(self):
        self._audio_player.stop()
        for cell in self.cells.values():
            cell.stop_stream()


# ── Timeline Widget ────────────────────────────────────────────────────────────
class BufferedSlider(QSlider):
    """Progress slider that also paints a YouTube-style 'buffered' region:
    the played part shows in the accent colour (styled sub-page), the part that
    is downloaded-but-not-yet-played shows as a lighter track ahead of the
    playhead, and the rest stays dark. Call set_buffered(0.0–1.0) to update."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._buffered = 0.0

    def set_buffered(self, frac):
        frac = max(0.0, min(1.0, float(frac)))
        if abs(frac - self._buffered) > 0.001:
            self._buffered = frac
            self.update()

    def paintEvent(self, e):
        super().paintEvent(e)   # groove + played sub-page + handle
        if self._buffered <= 0:
            return
        rng = self.maximum() - self.minimum()
        played = (self.value() - self.minimum()) / rng if rng else 0.0
        if self._buffered <= played:
            return
        w = self.width()
        # 4px groove, vertically centred (matches the QSS groove style)
        gy = self.height() // 2 - 2
        x_from = int(played * w) + 8   # start just past the handle
        x_to   = int(self._buffered * w)
        if x_to <= x_from:
            return
        p = QPainter(self)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 60))   # subtle light track
        p.drawRoundedRect(QRectF(x_from, gy, x_to - x_from, 4), 2, 2)
        p.end()


class TimelineWidget(QWidget):
    """
    iVMS-style timeline:
    - Fixed red cursor line at center
    - Timeline scrolls/zooms underneath
    - Scroll wheel: zoom in/out
    - Click: seek to time at that position
    - Playback updates cursor (timeline pans to follow)
    """
    seek_requested = pyqtSignal(int)        # emits recording index
    seek_to_time   = pyqtSignal(int, float) # emits (recording index, seconds-from-midnight)

    RULER_H = 22
    BAR_H   = 26
    PAD     = 3

    def __init__(self, parent=None):
        super().__init__(parent)
        self.recordings  = []
        self._rec_secs   = []        # cached (start_s, end_s) per recording
        self._painting   = False     # re-entrancy guard (prevents painter overlap)
        self.cursor_s    = 43200.0   # current time (seconds from midnight), default noon
        self.zoom_s      = 3600.0   # visible window width in seconds (1 hour default)
        self.selected_idx = -1
        self._drag_start_x  = None
        self._drag_start_s  = None
        self._hover_s       = -1.0
        self._user_scrubbing = False   # True while user holds/drags the timeline
        self._did_pan        = False
        self.setMinimumHeight(self.RULER_H + self.BAR_H + self.PAD * 2 + 10)
        self.setMouseTracking(True)
        self.setCursor(Qt.SizeHorCursor)

    # ── Data ──────────────────────────────────────────
    def set_recordings(self, recs):
        self.recordings   = recs
        self.selected_idx = -1
        # Pre-compute start/end seconds ONCE. Doing strptime in paintEvent for
        # every recording on every repaint was O(n²) and pegged the CPU.
        self._rec_secs = []
        for r in recs:
            self._rec_secs.append((self._iso_to_s(r['start']),
                                   self._iso_to_s(r['end'])))
        if recs:
            try:
                dt = datetime.strptime(recs[0]['start'], '%Y-%m-%dT%H:%M:%SZ')
                self.cursor_s = dt.hour * 3600 + dt.minute * 60 + dt.second
            except: pass
        self.update()

    def _find_rec_idx_at(self, s):
        """Find recording index covering second s, using the cached seconds."""
        for i, (s1, s2) in enumerate(getattr(self, '_rec_secs', [])):
            if s1 <= s <= s2:
                return i
        return -1

    def set_cursor_time(self, abs_s):
        """Update playhead from playback progress — but NOT while the user is
        scrubbing the timeline with the mouse (otherwise it fights their drag)."""
        if self._user_scrubbing:
            return
        self.cursor_s = max(0.0, min(86400.0, abs_s))
        self.update()

    # ── Coordinate helpers ─────────────────────────────
    def _s_to_x(self, s):
        """Seconds-from-midnight -> pixel x"""
        W = self.width()
        half = self.zoom_s / 2.0
        return int((s - self.cursor_s + half) / self.zoom_s * W)

    def _x_to_s(self, x):
        """Pixel x -> seconds-from-midnight"""
        W = self.width()
        half = self.zoom_s / 2.0
        return self.cursor_s - half + (x / W) * self.zoom_s

    def _iso_to_s(self, iso):
        try:
            dt = datetime.strptime(iso, '%Y-%m-%dT%H:%M:%SZ')
            return dt.hour * 3600 + dt.minute * 60 + dt.second
        except:
            return 0.0

    # ── Paint ──────────────────────────────────────────
    def paintEvent(self, event):
        # Re-entrancy guard: if a previous paint is still running (or update()
        # was triggered mid-paint), skip — overlapping QPainters segfault.
        if self._painting:
            return
        self._painting = True
        from PyQt5.QtGui import QPainter, QPen, QBrush, QFont, QPolygon
        from PyQt5.QtCore import QPoint
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing)

            W, H   = self.width(), self.height()
            rh     = self.RULER_H
            bh     = self.BAR_H
            track_y = rh + self.PAD
            cx     = W // 2

            p.fillRect(0, 0, W, H, QColor('#0d1117'))
            p.fillRect(0, track_y, W, bh, QColor('#0d1117'))

            font = QFont('monospace', 8)
            p.setFont(font)

            if self.zoom_s <= 300:
                major_s, minor_s = 60, 10
            elif self.zoom_s <= 1800:
                major_s, minor_s = 300, 60
            elif self.zoom_s <= 7200:
                major_s, minor_s = 1800, 300
            elif self.zoom_s <= 21600:
                major_s, minor_s = 3600, 900
            else:
                major_s, minor_s = 7200, 3600

            view_start = self.cursor_s - self.zoom_s / 2
            view_end   = self.cursor_s + self.zoom_s / 2

            # Minor ticks
            p.setPen(QPen(QColor('#21262d'), 1))
            t = (int(view_start / minor_s)) * minor_s
            while t <= view_end:
                x = self._s_to_x(t)
                if 0 <= x <= W:
                    p.drawLine(x, rh - 4, x, rh)
                t += minor_s

            # Major ticks + labels
            t = (int(view_start / major_s)) * major_s
            while t <= view_end:
                x = self._s_to_x(t)
                if 0 <= x <= W:
                    p.setPen(QPen(QColor('#30363d'), 1))
                    p.drawLine(x, 0, x, rh)
                    hh = int(t // 3600) % 24
                    mm = int((t % 3600) // 60)
                    p.setPen(QColor('#6e7681'))
                    p.drawText(x + 3, rh - 5, f'{hh:02d}:{mm:02d}')
                t += major_s

            p.setPen(QPen(QColor('#21262d'), 1))
            p.drawLine(0, rh, W, rh)

            # Hover index computed ONCE (not per-recording)
            hover_idx = self._find_rec_idx_at(self._hover_s) if self._hover_s >= 0 else -1

            # Recording blocks — use cached seconds, only draw visible ones
            col_sel  = QColor('#388bfd')
            col_hov  = QColor('#58a6ff')
            col_norm = QColor('#1f6feb')
            for i, (s1, s2) in enumerate(self._rec_secs):
                x1 = self._s_to_x(s1)
                x2 = self._s_to_x(s2)
                if x2 < 0 or x1 > W:
                    continue   # off-screen, skip
                x1c = max(0, x1); x2c = min(W, x2)
                if x2c <= x1c:
                    x2c = x1c + 1   # ensure at least 1px visible
                color = col_sel if i == self.selected_idx else (
                        col_hov if i == hover_idx else col_norm)
                p.fillRect(x1c, track_y + 2, x2c - x1c, bh - 4, color)

            # Center cursor
            p.setPen(QPen(QColor('#e63946'), 2))
            p.drawLine(cx, 0, cx, H)
            tri = QPolygon([QPoint(cx - 5, 0), QPoint(cx + 5, 0), QPoint(cx, 8)])
            p.setBrush(QBrush(QColor('#e63946')))
            p.setPen(Qt.NoPen)
            p.drawPolygon(tri)

            # Time label above cursor
            hh = int(self.cursor_s // 3600) % 24
            mm = int((self.cursor_s % 3600) // 60)
            ss = int(self.cursor_s % 60)
            p.setPen(QColor('#e63946'))
            p.setFont(QFont('monospace', 9, QFont.Bold))
            p.drawText(cx + 8, 16, f'{hh:02d}:{mm:02d}:{ss:02d}')
        finally:
            p.end()
            self._painting = False

    # ── Mouse ──────────────────────────────────────────
    def wheelEvent(self, e):
        delta = e.angleDelta().y()
        factor = 0.85 if delta > 0 else 1.18   # zoom in/out
        self.zoom_s = max(30.0, min(86400.0, self.zoom_s * factor))
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag_start_x = e.x()
            self._drag_start_s = self.cursor_s
            self._user_scrubbing = True   # freeze auto cursor updates from playback
            self._did_pan = False

    def mouseMoveEvent(self, e):
        self._hover_s = self._x_to_s(e.x())
        if self._drag_start_x is not None and (e.buttons() & Qt.LeftButton):
            # PAN style (like the original): dragging moves the timeline under a
            # fixed center cursor. Drag right → timeline goes back in time.
            dx = e.x() - self._drag_start_x
            ds = -(dx / self.width()) * self.zoom_s
            self.cursor_s = max(0, min(86400, self._drag_start_s + ds))
            if abs(dx) > 2:
                self._did_pan = True
            self.update()
        else:
            self.update()

    def mouseReleaseEvent(self, e):
        was_dragging = self._drag_start_x is not None
        self._drag_start_x = None
        self._drag_start_s = None
        if not was_dragging:
            self._user_scrubbing = False
            return

        # Seek to whatever time is now under the CENTER cursor (the playhead).
        target_s = self.cursor_s
        idx = self._find_rec_idx_at(target_s)
        if idx < 0:
            # Released on empty space → jump to the NEXT recording after this time.
            idx, start_s = self._next_rec_after(target_s)
            if idx >= 0:
                target_s = start_s
                self.cursor_s = start_s
                self.update()
        if idx >= 0:
            self.selected_idx = idx
            self.seek_to_time.emit(idx, target_s)
        self._user_scrubbing = False

    def _next_rec_after(self, s):
        """Return (index, start_s) of the first recording starting at/after s."""
        best = (-1, 0.0)
        best_start = 1e12
        for i, (s1, s2) in enumerate(getattr(self, '_rec_secs', [])):
            if s1 >= s and s1 < best_start:
                best_start = s1
                best = (i, s1)
        return best

    def leaveEvent(self, e):
        self._hover_s = -1.0
        self.update()


# ── Playback Tab ───────────────────────────────────────────────────────────────
class PlaybackTab(QWidget):
    def __init__(self, nvr: NVRClient, parent=None):
        super().__init__(parent)
        self.nvr        = nvr   # kept for compat; use self._nvr_map for multi-NVR
        self._nvr_map   = {}   # cam_id -> NVRClient
        self.cameras    = []
        self.recordings = []
        self.worker     = None
        self._paused    = False
        self._live_tab_ref = None   # set by MainWindow
        self._rec_start_dt = None   # datetime pocetka trenutne snimke
        self._rec_dur_s    = 0      # trajanje u sekundama

        main = QHBoxLayout(self)
        main.setContentsMargins(12, 12, 12, 12)
        main.setSpacing(10)

        # ── Lijevo: pretraga + lista ──────────────────────────
        left = QVBoxLayout()

        ctrl = QGroupBox('Search recordings')
        ctrl_lay = QVBoxLayout(ctrl)

        self.cam_combo = QComboBox()
        ctrl_lay.addWidget(QLabel('Camera:'))
        ctrl_lay.addWidget(self.cam_combo)

        self.date_edit = QDateEdit(QDate.currentDate())
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat('dd.MM.yyyy')
        ctrl_lay.addWidget(QLabel('Date:'))
        ctrl_lay.addWidget(self.date_edit)

        self.btn_search = QPushButton('Search')
        self.btn_search.setStyleSheet(f'background:{DARK["accent"]};color:white;font-weight:600;padding:8px;')
        self.btn_search.clicked.connect(self.search_recordings)
        ctrl_lay.addWidget(self.btn_search)
        left.addWidget(ctrl)

        self.rec_list = QListWidget()
        self.rec_list.itemClicked.connect(self.on_rec_selected)
        left.addWidget(QLabel('Recordings (click to play):'))
        left.addWidget(self.rec_list)
        left.setStretch(2, 1)

        main.addLayout(left, 1)

        # ── Desno: video + timeline + controls ───────────────
        right = QVBoxLayout()
        right.setSpacing(8)

        self.video_label = ZoomableVideoLabel('Select a recording to play')
        self.video_label.setAlignment(Qt.AlignCenter)
        self.video_label.setStyleSheet(
            f'background:{DARK["panel"]};border:1px solid {DARK["border"]};'
            f'border-radius:4px;color:{DARK["dim"]};font-size:14px;')
        self.video_label.setMinimumSize(640, 360)
        # Ignored size policy: the pixmap content must NEVER influence the label's
        # size hint, otherwise setPixmap → relayout → resize → rescale feedback loop.
        self.video_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.video_label.setScaledContents(False)
        right.addWidget(self.video_label, 1)

        # ── Timeline ──
        tl_label = QLabel('TIMELINE — click or scroll to navigate')
        tl_label.setStyleSheet(f'color:{DARK["dim"]};font-size:10px;letter-spacing:1px;')
        right.addWidget(tl_label)

        self.timeline = TimelineWidget()
        self.timeline.setFixedHeight(56)
        self.timeline.seek_requested.connect(self._on_timeline_click)
        self.timeline.seek_to_time.connect(self._on_timeline_seek)
        right.addWidget(self.timeline)

        # ── Progress slider ──
        prog_row = QHBoxLayout()
        self.pos_label = QLabel('00:00')
        self.pos_label.setStyleSheet(f'color:{DARK["dim"]};font-family:monospace;font-size:11px;min-width:40px;')
        self.dur_label = QLabel('00:00')
        self.dur_label.setStyleSheet(f'color:{DARK["dim"]};font-family:monospace;font-size:11px;min-width:40px;')
        self.prog_slider = BufferedSlider(Qt.Horizontal)
        self.prog_slider.setRange(0, 1000)
        self.prog_slider.setValue(0)
        self.prog_slider.setEnabled(False)
        prog_row.addWidget(self.pos_label)
        prog_row.addWidget(self.prog_slider)
        prog_row.addWidget(self.dur_label)
        right.addLayout(prog_row)

        # ── Playback controls ──
        pb_ctrl = QHBoxLayout()

        self.btn_play = QPushButton('▶ Play')
        self.btn_play.setFixedWidth(80)
        self.btn_play.clicked.connect(self._on_play_clicked)

        self.btn_pause = QPushButton('⏸ Pause')
        self.btn_pause.setFixedWidth(90)
        self.btn_pause.clicked.connect(self._on_pause_clicked)
        self.btn_pause.setEnabled(False)

        self.btn_stop_pb = QPushButton('■ Stop')
        self.btn_stop_pb.setFixedWidth(70)
        self.btn_stop_pb.setStyleSheet(f'border-color:{DARK["red"]};color:{DARK["red"]};')
        self.btn_stop_pb.clicked.connect(self.stop_playback)

        self.speed_combo = QComboBox()
        self.speed_combo.addItems(['0.25×', '0.5×', '1×', '2×', '4×', '8×'])
        self.speed_combo.setCurrentIndex(2)  # 1×
        self.speed_combo.setFixedWidth(80)
        self.speed_combo.currentIndexChanged.connect(self._on_speed_changed)
        self._speeds = [0.25, 0.5, 1.0, 2.0, 4.0, 8.0]

        # ── Audio: mute toggle + volume slider (persisted across recordings) ──
        # Audio is OFF by default; the user turns it on per playback.
        self._audio_on = False
        self._volume   = 1.0
        self.btn_mute = QPushButton('🔇')
        self.btn_mute.setFixedWidth(46)
        self.btn_mute.setStyleSheet('font-size:20px;')
        self.btn_mute.setToolTip('Mute / unmute')
        self.btn_mute.clicked.connect(self._on_mute_toggled)
        self.vol_slider = QSlider(Qt.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(100)
        self.vol_slider.setFixedWidth(90)
        self.vol_slider.setToolTip('Volume')
        # Live-preview the gain while dragging; relaunch ffmpeg on release.
        self.vol_slider.valueChanged.connect(self._on_volume_preview)
        self.vol_slider.sliderReleased.connect(self._on_volume_committed)
        if _AUDIO_FMT is None:
            self.btn_mute.setEnabled(False)
            self.vol_slider.setEnabled(False)
            self.btn_mute.setToolTip('No audio output device available')

        pb_ctrl.addWidget(self.btn_play)
        pb_ctrl.addWidget(self.btn_pause)
        pb_ctrl.addWidget(self.btn_stop_pb)
        pb_ctrl.addStretch()
        pb_ctrl.addWidget(self.btn_mute)
        pb_ctrl.addWidget(self.vol_slider)
        pb_ctrl.addSpacing(12)
        pb_ctrl.addWidget(QLabel('Speed:'))
        pb_ctrl.addWidget(self.speed_combo)
        right.addLayout(pb_ctrl)

        main.addLayout(right, 3)

        # Timer za progress (svaka sekunda)
        self.timer = QTimer()
        self.timer.timeout.connect(self._tick_progress)
        self._elapsed_s = 0.0
        self.fps = 25.0

    def set_cameras(self, cameras, nvr_map=None):
        self.cameras  = cameras
        self._nvr_map = nvr_map or {}
        self.cam_combo.clear()
        for cam in cameras:
            label = cam['name']
            if cam.get('nvr_name'):
                label = f"[{cam['nvr_name']}] {cam['name']}"
            self.cam_combo.addItem(label, cam['id'])

    def _get_nvr_for_cam(self, cam_id):
        """cam_id here is the uid (deviceid_channel)."""
        return self._nvr_map.get(cam_id, self.nvr)

    def search_recordings(self):
        self.rec_list.clear()
        cam_id = self.cam_combo.currentData()
        date   = self.date_edit.date().toPyDate()
        if not cam_id:
            return
        self.btn_search.setText('Searching...')
        self.btn_search.setEnabled(False)

        def _search():
            # Izvuci pravi channel broj iz UID-a (uuid_channel → channel)
            real_cam_id = cam_id.split('_', 1)[-1] if '_' in cam_id else cam_id
            nvr = self._get_nvr_for_cam(cam_id)
            dlog('Search', f'cam_id={cam_id!r} → real_channel={real_cam_id!r} NVR={nvr.host}')
            recs = nvr.get_recordings(real_cam_id, date)
            self.recordings = recs
            self._populate_list()
            self.timeline.set_recordings(recs)
            self.btn_search.setText('Search')
            self.btn_search.setEnabled(True)

        threading.Thread(target=_search, daemon=True).start()

    def _populate_list(self):
        self.rec_list.clear()
        if not self.recordings:
            self.rec_list.addItem('No recordings for selected date')
            return
        for i, rec in enumerate(self.recordings):
            try:
                s   = datetime.strptime(rec['start'], '%Y-%m-%dT%H:%M:%SZ')
                e   = datetime.strptime(rec['end'],   '%Y-%m-%dT%H:%M:%SZ')
                dur = int((e - s).total_seconds())
                hh, mm, ss = dur//3600, (dur%3600)//60, dur%60
                dur_str = (f'{hh}h {mm:02d}m' if hh else f'{mm:02d}:{ss:02d}')
                txt = f'{s.strftime("%H:%M:%S")}  ->  {e.strftime("%H:%M:%S")}  [{dur_str}]'
            except:
                txt = f'Snimka {i+1}: {rec["start"]}'
            item = QListWidgetItem(txt)
            item.setData(Qt.UserRole, i)
            self.rec_list.addItem(item)

    def on_rec_selected(self, item):
        idx = item.data(Qt.UserRole)
        if idx is None:
            return
        self._play_recording(idx)

    def _on_timeline_click(self, idx):
        self._play_recording(idx)
        self.rec_list.setCurrentRow(idx)

    def _on_timeline_seek(self, idx, clicked_s):
        """Timeline clicked at a specific time → play that recording starting
        from the clicked offset (not the recording's beginning)."""
        if idx < 0 or idx >= len(self.recordings):
            dlog('SEEK', f'ignored: idx={idx} out of range')
            return
        rec = self.recordings[idx]
        try:
            rec_start = datetime.strptime(rec['start'], '%Y-%m-%dT%H:%M:%SZ')
            rec_start_s = rec_start.hour*3600 + rec_start.minute*60 + rec_start.second
            offset = max(0.0, clicked_s - rec_start_s)
        except Exception as e:
            log('SEEK', f'time parse error: {e}')
            offset = 0.0

        # OPTIMIZATION: if we're seeking within the SAME recording whose file is
        # already (partly) downloaded locally, just re-position ffmpeg in that file
        # via -ss instead of starting a brand-new SDK download. This works even
        # while the download is still growing, as long as the seek target is
        # within the bytes we already have.
        w = self.worker
        can_local = False
        if (idx == getattr(self, '_current_rec_idx', -1) and w
                and getattr(w, '_tmp_file', None)
                and os.path.exists(w._tmp_file)):
            # The file covers [file_base, file_base + downloaded]. A seek before
            # file_base (the file's start within the recording) needs a fresh
            # download from the new point; otherwise reuse the local file.
            file_base = getattr(w, '_file_base_offset_s', 0.0)
            if offset < file_base:
                can_local = False
            elif getattr(w, '_dl_complete', False):
                can_local = True
            else:
                # SDK download % is of the remaining span (file_base → end).
                pct = getattr(w, '_dl_percent', 0)
                remaining = max(1.0, self._rec_dur_s - file_base)
                downloaded_s = file_base + remaining * (pct / 100.0)
                if offset < downloaded_s - 3:
                    can_local = True

        if can_local:
            dlog('SEEK', f'idx={idx} offset={offset:.0f}s → LOCAL seek (no re-download)')
            self._seek_offset_s = offset
            self._elapsed_s = offset
            self.rec_list.blockSignals(True)
            self.rec_list.setCurrentRow(idx)
            self.rec_list.blockSignals(False)
            file_base = getattr(w, '_file_base_offset_s', 0.0)
            local_off = max(0.0, offset - file_base)
            w.local_seek(local_off)
            return

        dlog('SEEK', f'idx={idx} clicked_s={clicked_s:.0f} rec_start_s={rec_start_s:.0f} '
                    f'→ offset={offset:.0f}s  ({rec["start"]})')
        self.rec_list.blockSignals(True)
        self.rec_list.setCurrentRow(idx)
        self.rec_list.blockSignals(False)
        self._play_recording(idx, seek_offset_s=offset)

    def _play_recording(self, idx, seek_offset_s=0.0, speed_override=None):
        if idx < 0 or idx >= len(self.recordings):
            return
        rec    = self.recordings[idx]
        cam_id = self.cam_combo.currentData()
        self._current_rec_idx = idx

        # Stop any existing playback (non-blocking — runs in background thread)
        self.stop_playback()

        self.video_label.clear_frame()
        self.video_label.setText('Loading...')
        self._paused = False

        try:
            self._rec_start_dt = datetime.strptime(rec['start'], '%Y-%m-%dT%H:%M:%SZ')
            e_dt               = datetime.strptime(rec['end'],   '%Y-%m-%dT%H:%M:%SZ')
            self._rec_dur_s    = max(1, (e_dt - self._rec_start_dt).total_seconds())
        except:
            self._rec_start_dt = None
            self._rec_dur_s    = 0

        # Apply seek offset: shift the playback start time forward
        play_start_dt = self._rec_start_dt
        if seek_offset_s > 0 and self._rec_start_dt:
            play_start_dt = self._rec_start_dt + timedelta(seconds=seek_offset_s)
            self._elapsed_s = seek_offset_s
            self._seek_offset_s = seek_offset_s
        else:
            self._elapsed_s = 0.0
            self._seek_offset_s = 0.0

        self._fmt_duration(self._rec_dur_s)
        self.prog_slider.setValue(0)
        self.prog_slider.set_buffered(0.0)

        # uid is 'deviceid_origchannel'; extract real channel for NVR lookup
        real_cam_id = cam_id.split('_', 1)[-1] if '_' in cam_id else cam_id
        nvr = self._get_nvr_for_cam(cam_id)
        spd = speed_override if speed_override is not None else \
              self._speeds[self.speed_combo.currentIndex()]

        # SDK is preferred for playback — avoids RTSP bandwidth (453) errors entirely
        if _SDK and nvr.sdk_user_id >= 0:
            rec_end_dt = self._rec_start_dt + timedelta(seconds=self._rec_dur_s)
            # iVMS-style seek: when seeking, START the SDK download AT the seek
            # point (not the recording's beginning). The file then begins at the
            # seek offset, so playback starts almost immediately (only a small
            # prebuffer to download) instead of waiting for the download to reach
            # the middle. _file_base_offset_s records where the file starts within
            # the recording, so position + later local seeks stay correct.
            dl_start_dt = self._rec_start_dt
            file_base   = 0.0
            if seek_offset_s > 0:
                dl_start_dt = self._rec_start_dt + timedelta(seconds=seek_offset_s)
                file_base   = seek_offset_s
            dlog('Playback', f'SDK NVR={nvr.host}  ch={real_cam_id}  '
                  f'{dl_start_dt:%H:%M:%S}→{rec_end_dt:%H:%M:%S}'
                  f'{"  (from seek +%ds)" % int(seek_offset_s) if seek_offset_s > 0 else ""}')
            self.worker = VideoWorker(
                'playback', '', speed=spd, decode_w=1280, decode_h=720,
                sdk_source={
                    'nvr': nvr, 'channel': real_cam_id, 'mode': 'playback',
                    'start_dt': dl_start_dt,          # download begins at seek point
                    'end_dt':   rec_end_dt,
                    'seek_offset_s': 0.0,             # file already starts here → no -ss
                    'wait_for': getattr(self, '_stopping', None),
                }
            )
            self.worker._file_base_offset_s = file_base   # file starts at this offset
        else:
            # RTSP fallback (uses URI from ISAPI if present, else compact format)
            uri = rec.get('uri', '')
            if uri:
                from urllib.parse import urlparse, urlunparse
                parsed = urlparse(uri)
                port = parsed.port or 554
                auth_netloc = f'{nvr.username}:{nvr.password}@{parsed.hostname}:{port}'
                rtsp_url = urlunparse(parsed._replace(netloc=auth_netloc))
            else:
                track = nvr._track_id(real_cam_id)
                def _compact(iso):
                    return iso.replace('-', '').replace(':', '')
                rtsp_url = (f'rtsp://{nvr.username}:{nvr.password}@{nvr.host}:554'
                            f'/Streaming/tracks/{track}'
                            f'?starttime={_compact(rec["start"])}&endtime={_compact(rec["end"])}')
            print(f'[Playback RTSP fallback] {rtsp_url.replace(nvr.password, "***")}')
            self.worker = VideoWorker('playback', rtsp_url, speed=spd)
        self._apply_audio_to_worker(self.worker)
        self.worker.frame_ready.connect(self._on_frame, Qt.QueuedConnection)
        self.worker.error.connect(self._on_pb_error)
        self.worker.position_ms.connect(self._on_position, Qt.QueuedConnection)
        self.worker.start()

        # NOTE: _elapsed_s was already set above (0, or the seek offset).
        # Timer starts when the first real frame arrives (after prebuffer).
        # For RTSP fallback (no download), start it now.
        if not (_SDK and nvr.sdk_user_id >= 0):
            self.timer.start(1000)
        self.btn_pause.setEnabled(True)
        self.btn_pause.setText('Pause')

    def _on_frame(self, _, qi):
        if self.worker:
            self.worker.notify_displayed()
        self.video_label.set_frame(qi)

    def _on_pb_error(self, _, msg):
        self.timer.stop()
        self.btn_pause.setEnabled(False)
        # When a recording finishes normally, auto-advance to the next one in the
        # list (continuous playback). Other errors just show the message.
        if msg == 'Playback finished':
            # Only auto-advance if this recording actually played for a bit. A
            # near-instant "finished" means a decode problem, not a real end —
            # chaining to the next file would loop forever.
            played_s = self._elapsed_s - getattr(self, '_seek_offset_s', 0.0)
            if played_s < 2.0:
                log('PLAYBACK', 'recording ended almost immediately — stopping '
                                'auto-advance (likely a decode issue)')
                self.video_label.setText('Playback problem — see console')
                return
            cur = self.rec_list.currentRow()
            nxt = cur + 1
            if 0 <= nxt < len(self.recordings):
                dlog('PLAYBACK', f'finished row {cur} → auto-playing next row {nxt}')
                self.rec_list.blockSignals(True)
                self.rec_list.setCurrentRow(nxt)
                self.rec_list.blockSignals(False)
                self._play_recording(nxt)
                return
            self.video_label.setText('End of recordings')
        else:
            self.video_label.setText(f'{msg}')

    def _on_position(self, ms):
        if ms < 0:
            # Negative = buffering status before playback starts
            self.video_label.setText('Buffering…')
            self.timer.stop()
            return
        # First real frame position → start the progress timer
        if not self.timer.isActive() and not self._paused:
            self.timer.start(1000)
        # Worker now reports ABSOLUTE position within the recording (it already
        # accounts for the -ss seek offset), so use it directly.
        self._elapsed_s = ms / 1000.0
        self._update_progress_ui()

    def _tick_progress(self):
        if not self._paused:
            spd = self._speeds[self.speed_combo.currentIndex()]
            self._elapsed_s += spd
        # YouTube-style buffered track: how much of the recording is downloaded.
        w = self.worker
        if w is not None:
            if getattr(w, '_dl_complete', False):
                self.prog_slider.set_buffered(1.0)
            elif getattr(w, '_tmp_file', None):
                self.prog_slider.set_buffered(getattr(w, '_dl_percent', 0) / 100.0)
        self._update_progress_ui()

    def _update_progress_ui(self):
        s = self._elapsed_s
        self.pos_label.setText(f'{int(s)//60:02d}:{int(s)%60:02d}')
        if self._rec_dur_s > 0:
            pct = min(1.0, s / self._rec_dur_s)
            self.prog_slider.setValue(int(pct * 1000))
            if self._rec_start_dt:
                abs_s = (self._rec_start_dt.hour * 3600 +
                         self._rec_start_dt.minute * 60 +
                         self._rec_start_dt.second + s)
                self.timeline.set_cursor_time(abs_s)

    def _fmt_duration(self, secs):
        mm, ss = int(secs)//60, int(secs)%60
        self.dur_label.setText(f'{mm:02d}:{ss:02d}')

    def _on_play_clicked(self):
        if self._paused and self.worker:
            self._paused = False
            self.worker.resume()
            self.btn_pause.setText('Pause')
            self.timer.start(1000)
        else:
            idx = self.rec_list.currentRow()
            if idx < 0 and self.recordings:
                idx = 0
            if idx >= 0:
                self._play_recording(idx)

    def _on_pause_clicked(self):
        if not self.worker:
            return
        if self._paused:
            self._paused = False
            self.worker.resume()
            self.btn_pause.setText('Pause')
            self.timer.start(1000)
        else:
            self._paused = True
            self.worker.pause()
            self.btn_pause.setText('Resume')
            self.timer.stop()

    def _can_local_relaunch(self, cur_offset):
        """True when the current worker can relaunch ffmpeg on its already-
        downloaded file at cur_offset without a fresh SDK download. A brand-new
        download only has a ~2 MB prebuffer, so ffmpeg -ss to a mid-recording
        offset would land past EOF and freeze — reuse the local file instead."""
        w = self.worker
        if not (w and getattr(w, '_tmp_file', None) and os.path.exists(w._tmp_file)):
            return False
        file_base = getattr(w, '_file_base_offset_s', 0.0)
        if cur_offset < file_base:
            return False
        if getattr(w, '_dl_complete', False):
            return True
        pct = getattr(w, '_dl_percent', 0)
        remaining = max(1.0, self._rec_dur_s - file_base)
        downloaded_s = file_base + remaining * (pct / 100.0)
        return cur_offset < downloaded_s - 3

    def _local_seek_current(self, cur_offset):
        """Relaunch ffmpeg on the local file at cur_offset (no re-download)."""
        w = self.worker
        file_base = getattr(w, '_file_base_offset_s', 0.0)
        w.local_seek(max(0.0, cur_offset - file_base))

    def _on_speed_changed(self, idx):
        # Playback uses ffmpeg -readrate for speed, set at launch. Changing speed
        # relaunches ffmpeg at the CURRENT position with the new rate.
        target = self._speeds[idx]
        row = self.rec_list.currentRow()
        if row < 0 or not self.worker:
            return
        cur_offset = max(0.0, self._elapsed_s)   # resume from here
        if self._can_local_relaunch(cur_offset):
            dlog('SPEED', f'→ {target}× from offset {cur_offset:.0f}s (local, no re-download)')
            self.worker.set_speed(target)
            self._local_seek_current(cur_offset)
            return
        dlog('SPEED', f'→ {target}× from offset {cur_offset:.0f}s (re-download)')
        self._play_recording(row, seek_offset_s=cur_offset, speed_override=target)

    def _apply_audio_to_worker(self, w):
        if w is not None:
            w.set_audio(self._audio_on)
            w.set_volume(self._volume)

    def _relaunch_audio(self):
        """Apply the current mute/volume to live playback. Audio routing is baked
        into the ffmpeg command, so relaunch it at the current position — locally
        (no re-download) when the file is available, else restart the recording."""
        w = self.worker
        if w is None:
            return
        self._apply_audio_to_worker(w)
        cur_offset = max(0.0, self._elapsed_s)
        if self._can_local_relaunch(cur_offset):
            self._local_seek_current(cur_offset)
        else:
            row = self.rec_list.currentRow()
            if row >= 0:
                self._play_recording(row, seek_offset_s=cur_offset)

    def _on_mute_toggled(self):
        self._audio_on = not self._audio_on
        on = self._audio_on and self._volume > 0
        self.btn_mute.setText('🔊' if on else '🔇')
        dlog('AUDIO', f'{"unmuted" if self._audio_on else "muted"}')
        self._relaunch_audio()

    def _on_volume_preview(self, val):
        # Fires continuously while dragging: update gain + icon, but don't
        # relaunch mid-drag (only on release / click, see _on_volume_committed).
        self._volume = val / 100.0
        if self._audio_on:
            self.btn_mute.setText('🔊' if self._volume > 0 else '🔇')
        if not self.vol_slider.isSliderDown():
            self._relaunch_audio()

    def _on_volume_committed(self):
        dlog('AUDIO', f'volume → {int(self._volume * 100)}%')
        self._relaunch_audio()

    def stop_playback(self):
        """Stop current playback. Worker shutdown runs on a background thread so
        the UI never blocks, but we track it so the next playback can wait for the
        SDK download session to be released (avoids overlapping downloads → 0KB files)."""
        if self.worker:
            old_worker = self.worker
            self.worker = None
            done = threading.Event()
            self._stopping = done
            def _shutdown(w=old_worker, ev=done):
                try: w.stop(); w.wait(3000)
                finally: ev.set()
            threading.Thread(target=_shutdown, daemon=True).start()
        self.timer.stop()
        self._paused = False
        self.btn_pause.setEnabled(False)
        self.btn_pause.setText('Pause')
        self.prog_slider.setValue(0)
        self.prog_slider.set_buffered(0.0)
        self.pos_label.setText('00:00')
        self.video_label.setText('Select a recording to play')
        self.video_label.clear_frame()

# ── Device Add/Edit Dialog ────────────────────────────────────────────────────
class AudioPlayer:
    """Plays one camera's microphone audio via ffplay (handles its own RTSP
    connection + audio sink). Only one camera at a time."""
    def __init__(self):
        self._proc = None
        self.current = None     # the nvr currently playing

    def play(self, url, nvr=None):
        self.stop()
        if not url:
            return
        import subprocess as sp
        try:
            self._proc = sp.Popen(
                ['ffplay', '-loglevel', 'quiet', '-nodisp', '-vn', '-autoexit',
                 '-rtsp_transport', 'tcp', '-fflags', 'nobuffer', '-i', url],
                stdout=sp.DEVNULL, stderr=sp.DEVNULL, stdin=sp.DEVNULL)
            self.current = nvr
        except FileNotFoundError:
            print('[Audio] ffplay not found')
            self._proc = None

    def stop(self):
        if self._proc:
            try: self._proc.terminate()
            except Exception: pass
            self._proc = None
        self.current = None

    def is_playing(self, nvr):
        return self._proc is not None and self._proc.poll() is None and self.current is nvr


class ChannelScanDialog(QWidget):
    """Scans the camera's RTSP channels, shows a live thumbnail of each, and lets
    the user pick which to add as separate cameras. The user decides what to keep —
    we don't hide anything (works for any camera, not just this one)."""
    _found = pyqtSignal(str, str, str)   # label, url, thumb_path
    _done  = pyqtSignal(int)

    def __init__(self, nvr, main, parent=None):
        super().__init__(parent, Qt.Dialog | Qt.WindowCloseButtonHint)
        self.nvr = nvr; self.main = main
        self.setWindowTitle(f'Channels — {nvr.name}')
        self.setMinimumSize(440, 460)
        lay = QVBoxLayout(self)
        self.info = QLabel('Scanning channels — preview each and tick the ones to add…')
        self.info.setWordWrap(True); lay.addWidget(self.info)
        self.scroll = QScrollArea(); self.scroll.setWidgetResizable(True)
        inner = QWidget(); self.vbox = QVBoxLayout(inner); self.vbox.addStretch()
        self.scroll.setWidget(inner); lay.addWidget(self.scroll)
        self._rows = []
        row = QHBoxLayout()
        self.b_add = QPushButton('Add selected')
        self.b_add.setStyleSheet(f'background:{DARK["accent"]};color:white;font-weight:600;padding:6px;')
        self.b_add.clicked.connect(self._add)
        close = QPushButton('Close'); close.clicked.connect(self.close)
        row.addWidget(self.b_add); row.addWidget(close); lay.addLayout(row)
        self._found.connect(self._on_found)
        self._done.connect(self._on_done)
        threading.Thread(target=self._scan, daemon=True).start()

    def _candidates(self):
        import re
        base = self.nvr.rtsp_live_url('1', sub=False)
        if re.search(r'av(\d+)_(\d+)', base):
            return [(f'av{i}', re.sub(r'av\d+_(\d+)', rf'av{i}_\1', base)) for i in range(8)]
        if re.search(r'channel=(\d+)', base):
            return [(f'ch{i}', re.sub(r'channel=\d+', f'channel={i}', base)) for i in range(8)]
        return [('main', base)]

    def _scan(self):
        import subprocess as sp, tempfile, os
        n = 0
        for label, url in self._candidates():
            thumb = os.path.join(tempfile.gettempdir(), f'hikscan_{label}.png')
            try:
                sp.run(['ffmpeg', '-loglevel', 'quiet', '-rtsp_transport', 'tcp',
                        '-analyzeduration', '3000000', '-probesize', '3000000',
                        '-i', url, '-frames:v', '1', '-vf', 'scale=240:135',
                        '-y', thumb], timeout=12, stdin=sp.DEVNULL,
                       stdout=sp.DEVNULL, stderr=sp.DEVNULL)
                if os.path.exists(thumb) and os.path.getsize(thumb) > 0:
                    self._found.emit(label, url, thumb); n += 1
            except Exception:
                pass
        self._done.emit(n)

    def _on_found(self, label, url, thumb):
        from PyQt5.QtWidgets import QCheckBox
        from PyQt5.QtGui import QPixmap
        w = QWidget(); r = QHBoxLayout(w); r.setContentsMargins(2, 2, 2, 2)
        cb = QCheckBox()
        pic = QLabel(); pm = QPixmap(thumb)
        if not pm.isNull():
            pic.setPixmap(pm)
        txt = QLabel(label)
        r.addWidget(cb); r.addWidget(pic); r.addWidget(txt); r.addStretch()
        self.vbox.insertWidget(self.vbox.count() - 1, w)
        self._rows.append((cb, label, url))

    def _on_done(self, n):
        self.info.setText(f'Found {n} channel(s). Tick the ones to add, then "Add selected".'
                          if n else 'No channels responded.')

    def _add(self):
        sel = [{'name': label, 'url': url} for cb, label, url in self._rows if cb.isChecked()]
        if sel:
            self.main._add_onvif_channels(self.nvr, sel)
        self.close()


class ManageCameraDialog(QWidget):
    """Per-camera options for ONVIF/RTSP cameras: stream quality, audio (mic),
    day/night (IR), PTZ note, and an explicit extra-channel scan."""
    _scan_done = pyqtSignal(str)

    def __init__(self, nvr, main, parent=None):
        super().__init__(parent, Qt.Dialog | Qt.WindowCloseButtonHint)
        self.nvr = nvr; self.main = main
        self.setWindowTitle(f'Manage — {nvr.name}')
        self.setFixedWidth(380)
        lay = QVBoxLayout(self)

        lay.addWidget(QLabel('Stream quality:'))
        row = QHBoxLayout()
        self.q_main = QPushButton('Main (HD)'); self.q_sub = QPushButton('Sub (low)')
        for b in (self.q_main, self.q_sub):
            b.setCheckable(True)
        sub = getattr(nvr, 'use_sub', False)
        self.q_main.setChecked(not sub); self.q_sub.setChecked(sub)
        self.q_main.clicked.connect(lambda: self._quality(False))
        self.q_sub.clicked.connect(lambda: self._quality(True))
        row.addWidget(self.q_main); row.addWidget(self.q_sub)
        lay.addLayout(row)

        self.b_audio = QPushButton('🔊  Listen to microphone')
        self.b_audio.setCheckable(True)
        self.b_audio.setChecked(main.audio.is_playing(nvr))
        self.b_audio.clicked.connect(self._audio)
        lay.addWidget(self.b_audio)

        if getattr(nvr, 'has_imaging', False):
            lay.addWidget(QLabel('Day / Night (IR):'))
            r2 = QHBoxLayout()
            for label, mode in [('Auto', 'AUTO'), ('Day', 'ON'), ('Night', 'OFF')]:
                bb = QPushButton(label)
                bb.clicked.connect(lambda _, m=mode: self._daynight(m))
                r2.addWidget(bb)
            lay.addLayout(r2)

        if getattr(nvr, 'has_ptz', False):
            n = QLabel('PTZ: arrow pad appears in the live toolbar when selected.')
            n.setStyleSheet(f'color:{DARK["dim"]};font-size:11px;')
            n.setWordWrap(True); lay.addWidget(n)

        self.b_scan = QPushButton('Scan extra channels…')
        self.b_scan.clicked.connect(self._scan)
        lay.addWidget(self.b_scan)
        self.lbl_msg = QLabel(''); self.lbl_msg.setWordWrap(True)
        self.lbl_msg.setStyleSheet(f'color:{DARK["dim"]};font-size:11px;')
        lay.addWidget(self.lbl_msg)
        self._scan_done.connect(self.lbl_msg.setText)

        close = QPushButton('Close'); close.clicked.connect(self.close)
        lay.addWidget(close)

    def _quality(self, sub):
        self.nvr.use_sub = sub
        self.q_main.setChecked(not sub); self.q_sub.setChecked(sub)
        NVRClient.save_all(self.main.devices)
        self.main._restart_nvr_streams(self.nvr)
        self.lbl_msg.setText('Stream quality: ' + ('Sub' if sub else 'Main'))

    def _audio(self):
        self.main._set_audio(self.nvr, self.b_audio.isChecked())

    def _daynight(self, mode):
        self.lbl_msg.setText(f'Setting day/night → {mode}…')
        def run():
            ok = self.nvr.set_day_night(mode)
            self._scan_done.emit('Day/Night → ' + (mode if ok else 'failed'))
        threading.Thread(target=run, daemon=True).start()

    def _scan(self):
        dlg = ChannelScanDialog(self.nvr, self.main, self.main)
        dlg.show()


class ONVIFScanDialog(QWidget):
    """Lists ONVIF cameras found on the LAN and adds the selected ones."""
    add_cameras = pyqtSignal(list)   # [{host, port, xaddr, name, username, password}]

    def __init__(self, devs, parent=None):
        super().__init__(parent, Qt.Dialog | Qt.WindowCloseButtonHint)
        self.setWindowTitle('ONVIF cameras on network')
        self.setMinimumWidth(420)
        self._devs = devs
        lay = QVBoxLayout(self)
        if not devs:
            lay.addWidget(QLabel('No new ONVIF cameras found.\n'
                                 'Make sure you are on the same network as the camera.'))
            close = QPushButton('Close'); close.clicked.connect(self.close)
            lay.addWidget(close)
            return
        lay.addWidget(QLabel(f'Found {len(devs)} camera(s). Select to add:'))
        self._checks = []
        for d in devs:
            from PyQt5.QtWidgets import QCheckBox
            cb = QCheckBox(f"{d['host']}  (port {d['port']})")
            cb.setChecked(True)
            lay.addWidget(cb)
            self._checks.append((cb, d))
        lay.addWidget(QLabel('Credentials (leave blank if camera needs none):'))
        cred = QHBoxLayout()
        self.f_user = QLineEdit('admin'); self.f_user.setPlaceholderText('username')
        self.f_pass = QLineEdit(); self.f_pass.setEchoMode(QLineEdit.Password)
        self.f_pass.setPlaceholderText('password')
        cred.addWidget(self.f_user); cred.addWidget(self.f_pass)
        lay.addLayout(cred)
        btns = QHBoxLayout()
        add = QPushButton('Add selected'); add.setStyleSheet(
            f'background:{DARK["accent"]};color:white;font-weight:600;padding:6px;')
        add.clicked.connect(self._add)
        cancel = QPushButton('Cancel'); cancel.clicked.connect(self.close)
        btns.addWidget(add); btns.addWidget(cancel)
        lay.addLayout(btns)

    def _add(self):
        u = self.f_user.text().strip(); p = self.f_pass.text()
        out = []
        for cb, d in self._checks:
            if cb.isChecked():
                out.append({'host': d['host'], 'port': d['port'],
                            'xaddr': d.get('xaddr'),
                            'name': f"ONVIF {d['host'].split('.')[-1]}",
                            'username': u, 'password': p})
        if out:
            self.add_cameras.emit(out)
        self.close()


class ManualRTSPDialog(QWidget):
    """Add a camera by typing its RTSP URL directly."""
    saved = pyqtSignal(object)   # emits ManualRTSPClient

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Dialog | Qt.WindowCloseButtonHint)
        self.setWindowTitle('Add RTSP camera')
        self.setFixedWidth(440)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel('Name:'))
        self.f_name = QLineEdit('RTSP Camera')
        lay.addWidget(self.f_name)
        lay.addWidget(QLabel('RTSP URL:'))
        self.f_url = QLineEdit()
        self.f_url.setPlaceholderText('rtsp://user:pass@192.168.1.50:554/stream')
        lay.addWidget(self.f_url)
        hint = QLabel('Tip: include credentials in the URL if the camera needs them.')
        hint.setStyleSheet(f'color:{DARK["dim"]};font-size:11px;')
        lay.addWidget(hint)
        row = QHBoxLayout()
        save = QPushButton('Save')
        save.setStyleSheet(f'background:{DARK["accent"]};color:white;font-weight:600;padding:6px;')
        save.clicked.connect(self._save)
        cancel = QPushButton('Cancel'); cancel.clicked.connect(self.close)
        row.addWidget(save); row.addWidget(cancel)
        lay.addLayout(row)

    def _save(self):
        import uuid
        from urllib.parse import urlparse
        url = self.f_url.text().strip()
        if not url:
            return
        c = ManualRTSPClient(str(uuid.uuid4()),
                             self.f_name.text().strip() or 'RTSP Camera')
        c.url = url
        try:
            p = urlparse(url)
            c.host = p.hostname or ''
            c.port = p.port or 554
        except Exception:
            pass
        self.saved.emit(c)
        self.close()


class DeviceDialog(QWidget):
    """Floating panel to add or edit an NVR device"""
    saved = pyqtSignal(object)   # emits NVRClient

    def __init__(self, nvr=None, parent=None):
        super().__init__(parent, Qt.Dialog | Qt.WindowCloseButtonHint)
        self.setWindowTitle('Add Device' if nvr is None else 'Edit Device')
        self.setFixedWidth(340)
        self._nvr = nvr or NVRClient()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(8)

        lay.addWidget(QLabel('Device name:'))
        self.f_name = QLineEdit(self._nvr.name)
        lay.addWidget(self.f_name)

        lay.addWidget(QLabel('IP address:'))
        self.f_host = QLineEdit(self._nvr.host)
        lay.addWidget(self.f_host)

        row = QHBoxLayout()
        row.addWidget(QLabel('Port:'))
        self.f_port = QSpinBox(); self.f_port.setRange(1,65535); self.f_port.setValue(self._nvr.port)
        row.addWidget(self.f_port)
        lay.addLayout(row)

        lay.addWidget(QLabel('Username:'))
        self.f_user = QLineEdit(self._nvr.username)
        lay.addWidget(self.f_user)

        lay.addWidget(QLabel('Password:'))
        self.f_pass = QLineEdit(self._nvr.password)
        self.f_pass.setEchoMode(QLineEdit.Password)
        lay.addWidget(self.f_pass)

        self.btn_test = QPushButton('Save')
        self.btn_test.setStyleSheet(f'background:{DARK["accent"]};color:white;font-weight:600;padding:8px;')
        self.btn_test.clicked.connect(self._test_and_save)
        lay.addWidget(self.btn_test)

        self.lbl_status = QLabel('')
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setStyleSheet('font-size:12px;')
        lay.addWidget(self.lbl_status)

    def _test_and_save(self):
        self._nvr.name     = self.f_name.text().strip() or 'NVR'
        self._nvr.host     = self.f_host.text().strip()
        self._nvr.port     = self.f_port.value()
        self._nvr.username = self.f_user.text().strip()
        self._nvr.password = self.f_pass.text()

        self.btn_test.setText('Saving...')
        self.btn_test.setEnabled(False)

        def _run():
            ok, msg = self._nvr.test()
            if ok:
                self.lbl_status.setText(f'✓ Connected')
                self.lbl_status.setStyleSheet(f'font-size:12px;color:{DARK["green"]};')
                self.saved.emit(self._nvr)
                self.close()
            else:
                self.lbl_status.setText(f'✗ {msg}')
                self.lbl_status.setStyleSheet(f'font-size:12px;color:{DARK["red"]};')
            self.btn_test.setText('Save')
            self.btn_test.setEnabled(True)

        threading.Thread(target=_run, daemon=True).start()


# ── Hikvision SDK on-demand installer dialog ────────────────────────────────────
class SDKInstallDialog(QDialog):
    """Downloads and installs the proprietary Hikvision HCNetSDK on user
    confirmation. The SDK is not bundled with the app; this fetches it on demand
    into the per-user data dir. On success the caller restarts the app so the
    SDK is loaded at startup. See sdk_installer.py for the actual work."""
    _progress = pyqtSignal(str, object)   # stage, frac (0..1 or None)
    _done     = pyqtSignal(bool, str)     # ok, error message

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Install Hikvision SDK')
        self.setModal(True)
        self.setMinimumWidth(440)
        self.installed = False

        v = QVBoxLayout(self)
        self.lbl = QLabel(
            'Connecting to Hikvision / Safire NVRs needs the Hikvision HCNetSDK, '
            'which is proprietary and is not bundled with this app.\n\n'
            'Click Install to download (~70 MB) and set it up automatically. '
            'ONVIF and RTSP cameras work without it.')
        self.lbl.setWordWrap(True)
        v.addWidget(self.lbl)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.hide()
        v.addWidget(self.bar)

        row = QHBoxLayout()
        row.addStretch()
        self.btn_cancel = QPushButton('Not now')
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_install = QPushButton('Install')
        self.btn_install.setStyleSheet(
            f'background:{DARK["accent"]};color:white;font-weight:600;padding:6px 14px;')
        self.btn_install.clicked.connect(self._start)
        row.addWidget(self.btn_cancel)
        row.addWidget(self.btn_install)
        v.addLayout(row)

        self._progress.connect(self._on_progress)
        self._done.connect(self._on_done)

    def _start(self):
        self.btn_install.setEnabled(False)
        self.btn_cancel.setEnabled(False)
        self.bar.show()
        self.lbl.setText('Downloading Hikvision SDK…')
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        try:
            import sdk_installer
            sdk_installer.download_and_install(
                progress_cb=lambda stage, frac: self._progress.emit(stage, frac))
            self._done.emit(True, '')
        except Exception as e:
            self._done.emit(False, str(e))

    def _on_progress(self, stage, frac):
        if stage == 'download':
            self.lbl.setText('Downloading Hikvision SDK…')
            if frac is None:
                self.bar.setRange(0, 0)            # indeterminate (busy)
            else:
                self.bar.setRange(0, 100)
                self.bar.setValue(int(frac * 100))
        elif stage == 'extract':
            self.lbl.setText('Extracting…')
            self.bar.setRange(0, 0)
        elif stage == 'install':
            self.lbl.setText('Installing…')
            self.bar.setRange(0, 0)

    def _on_done(self, ok, err):
        if ok:
            self.installed = True
            QMessageBox.information(
                self, 'SDK installed',
                'Hikvision SDK installed successfully.\n'
                'The app will now restart to load it.')
            self.accept()
        else:
            self.bar.hide()
            self.lbl.setText(
                f'Install failed:\n{err}\n\n'
                'You can still use ONVIF and RTSP cameras.')
            self.btn_cancel.setEnabled(True)
            self.btn_cancel.setText('Close')
            self.btn_install.setEnabled(True)
            self.btn_install.setText('Retry')


# ── Main Window ────────────────────────────────────────────────────────────────
class MainWindow(QMainWindow):
    _cameras_ready = pyqtSignal(object, list)   # nvr, cameras — for thread-safe UI update
    _onvif_found   = pyqtSignal(list)           # discovered ONVIF devices

    def __init__(self):
        super().__init__()
        self._cameras_ready.connect(self._on_cameras_ready)
        self._onvif_found.connect(self._on_onvif_found)
        self.audio = AudioPlayer()
        self.setWindowTitle('Hikvision Monitor')
        self.setMinimumSize(1280, 720)
        self.resize(1500, 900)

        # Load devices
        self.devices = NVRClient.load_all()
        if not self.devices:
            self.devices = [NVRClient(name='NVR 1')]

        self._all_cameras = []   # [{id, name, nvr_name, nvr_id, _nvr}]
        self._nvr_map     = {}   # cam_id -> NVRClient

        # ── UI ─────────────────────────────────────────────────
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0,0,0,0)
        root.setSpacing(0)

        # Sidebar
        sidebar = QWidget()
        sidebar.setFixedWidth(260)
        sidebar.setStyleSheet(f'background:{DARK["panel"]};border-right:1px solid {DARK["border"]};')
        sb = QVBoxLayout(sidebar)
        sb.setContentsMargins(10,14,10,14)
        sb.setSpacing(6)

        logo = QLabel('📹  HIK MONITOR')
        logo.setStyleSheet(f'color:{DARK["accent"]};font-size:15px;font-weight:700;letter-spacing:1px;')
        sb.addWidget(logo)

        # Devices section
        dev_hdr = QHBoxLayout()
        dev_lbl = QLabel('DEVICES')
        dev_lbl.setStyleSheet(f'color:{DARK["dim"]};font-size:10px;letter-spacing:2px;')
        btn_add_dev = QPushButton('+ Add')
        btn_add_dev.setToolTip('Add a camera or NVR')
        btn_add_dev.clicked.connect(self._show_add_menu)
        dev_hdr.addWidget(dev_lbl)
        dev_hdr.addStretch()
        dev_hdr.addWidget(btn_add_dev)
        sb.addLayout(dev_hdr)

        self.dev_list = QListWidget()
        self.dev_list.setMaximumHeight(130)
        self.dev_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.dev_list.customContextMenuRequested.connect(self._device_context_menu)
        sb.addWidget(self.dev_list)

        btn_connect_all = QPushButton('Connect All')
        btn_connect_all.setStyleSheet(f'background:{DARK["accent"]};color:white;font-weight:600;padding:6px;')
        btn_connect_all.clicked.connect(self._connect_all)
        sb.addWidget(btn_connect_all)

        # Cameras section
        cam_hdr = QLabel('CAMERAS')
        cam_hdr.setStyleSheet(f'color:{DARK["dim"]};font-size:10px;letter-spacing:2px;margin-top:8px;')
        sb.addWidget(cam_hdr)

        self.cam_list = CameraListWidget()
        self.cam_list.itemClicked.connect(self._cam_clicked)
        self.cam_list.itemDoubleClicked.connect(self._cam_dbl_clicked)
        self.cam_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.cam_list.customContextMenuRequested.connect(self._camera_context_menu)
        sb.addWidget(self.cam_list)
        sb.setStretch(sb.count()-1, 1)

        # Support link — pinned at the bottom of the sidebar, always visible
        coffee = QLabel(
            '<a href="https://buymeacoffee.com/manutdsnake" '
            'style="text-decoration:none;color:#0d1117;">☕  Buy me a coffee</a>'
        )
        coffee.setOpenExternalLinks(True)
        coffee.setAlignment(Qt.AlignCenter)
        coffee.setToolTip('Support development — buymeacoffee.com/manutdsnake')
        coffee.setStyleSheet(
            'background:#FFDD00;border-radius:6px;padding:7px;'
            'font-weight:700;font-size:12px;margin-top:8px;'
        )
        sb.addWidget(coffee)

        kofi = QLabel(
            '<a href="https://ko-fi.com/manutdsnake" '
            'style="text-decoration:none;color:#ffffff;">❤  Support on Ko-fi</a>'
        )
        kofi.setOpenExternalLinks(True)
        kofi.setAlignment(Qt.AlignCenter)
        kofi.setToolTip('Support development — ko-fi.com/manutdsnake')
        kofi.setStyleSheet(
            'background:#FF5E5B;border-radius:6px;padding:7px;'
            'font-weight:700;font-size:12px;margin-top:6px;'
        )
        sb.addWidget(kofi)

        root.addWidget(sidebar)

        # Main tabs
        self.tabs = QTabWidget()
        self.live_tab = LiveViewTab(self.devices[0])
        self.pb_tab   = PlaybackTab(self.devices[0])
        self.pb_tab._live_tab_ref = self.live_tab
        self.tabs.addTab(self.live_tab, '  Live View  ')
        self.tabs.addTab(self.pb_tab,   '  Playback  ')
        root.addWidget(self.tabs)

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage('Add an NVR device and click Connect All')

        self._refresh_device_list()

        # Auto-connect if we have saved devices with hosts
        ready = [d for d in self.devices if d.host]
        if ready:
            QTimer.singleShot(600, self._connect_all)

    # ── Devices ────────────────────────────────────────────────
    def _refresh_device_list(self):
        self.dev_list.clear()
        for d in self.devices:
            status = '🟢' if any(c.get('_nvr') is d for c in self._all_cameras) else '⚪'
            item = QListWidgetItem(f'{status}  {d.name}  ({d.host or "not set"})')
            item.setData(Qt.UserRole, d.device_id)
            self.dev_list.addItem(item)

    def _show_add_menu(self):
        from PyQt5.QtWidgets import QMenu
        m = QMenu(self)
        m.setStyleSheet(f'background:{DARK["panel"]};color:{DARK["text"]};'
                        f'border:1px solid {DARK["border"]};')
        a_nvr   = m.addAction('NVR  (Hikvision / Safire)…')
        a_onvif = m.addAction('Scan ONVIF network…')
        a_rtsp  = m.addAction('Manual RTSP URL…')
        btn = self.sender()
        pos = btn.mapToGlobal(btn.rect().bottomLeft()) if btn else self.cursor().pos()
        act = m.exec_(pos)
        if act == a_nvr:
            # Hikvision/Safire NVRs need the native SDK. Offer to install it
            # on demand if it's missing (the app restarts on success).
            if self._maybe_offer_sdk():
                return
            self._add_device()
        elif act == a_onvif:
            self._scan_onvif()
        elif act == a_rtsp:
            self._add_manual_rtsp()

    def _maybe_offer_sdk(self):
        """If the native Hikvision SDK isn't loaded, offer to download+install
        it. Returns True if a restart was triggered (caller should stop)."""
        if _SDK is not None:
            return False
        try:
            import sdk_installer
        except ImportError:
            return False
        if sdk_installer.is_installed():
            return False   # present but not loaded — nothing to download
        dlg = SDKInstallDialog(self)
        dlg.exec_()
        if dlg.installed:
            os.environ['HIKVISION_SDK_PATH'] = str(sdk_installer.install_lib_dir())
            os.execv(sys.executable, [sys.executable] + sys.argv)
            return True   # not reached (process replaced)
        return False

    def _add_device(self):
        import uuid
        nvr = NVRClient(str(uuid.uuid4()), f'NVR {len(self.devices)+1}')
        dlg = DeviceDialog(nvr, self)
        dlg.saved.connect(self._on_device_saved)
        dlg.show()

    def _add_manual_rtsp(self):
        dlg = ManualRTSPDialog(self)
        dlg.saved.connect(self._on_device_saved)
        dlg.show()

    def _on_device_saved(self, nvr):
        if nvr not in self.devices:
            self.devices.append(nvr)
        NVRClient.save_all(self.devices)
        self._refresh_device_list()
        # Auto-connect the new device
        threading.Thread(target=self._connect_device, args=(nvr,), daemon=True).start()

    # ── ONVIF discovery ─────────────────────────────────────────────────────
    def _scan_onvif(self):
        self.status.showMessage('Scanning network for ONVIF cameras…')
        def _run():
            try:
                import onvif_client
                devs = onvif_client.discover(timeout=5)
            except Exception as e:
                devs = []
                print(f'[ONVIF] scan error: {e}')
            self._onvif_found.emit(devs)
        threading.Thread(target=_run, daemon=True).start()

    def _on_onvif_found(self, devs):
        # Drop devices we already have configured (match by host).
        known = {d.host for d in self.devices}
        fresh = [d for d in devs if d['host'] not in known]
        self.status.showMessage(
            f'ONVIF scan: {len(devs)} found, {len(fresh)} new')
        dlg = ONVIFScanDialog(fresh, self)
        dlg.add_cameras.connect(self._add_onvif_cameras)
        dlg.show()

    def _add_onvif_cameras(self, cams):
        import uuid
        for c in cams:   # c = {host, port, xaddr, name, username, password}
            cam = ONVIFClient(str(uuid.uuid4()), c['name'])
            cam.host = c['host']; cam.port = c['port']; cam.xaddr = c.get('xaddr')
            cam.username = c.get('username', ''); cam.password = c.get('password', '')
            self.devices.append(cam)
            threading.Thread(target=self._connect_device, args=(cam,), daemon=True).start()
        NVRClient.save_all(self.devices)
        self._refresh_device_list()

    def _device_context_menu(self, pos):
        from PyQt5.QtWidgets import QMenu
        item = self.dev_list.itemAt(pos)
        if not item:
            return
        dev_id = item.data(Qt.UserRole)
        nvr = next((d for d in self.devices if d.device_id == dev_id), None)
        if not nvr:
            return
        connected = any(c.get('_nvr') is nvr for c in self._all_cameras)
        menu = QMenu(self)
        menu.setStyleSheet(f'background:{DARK["panel"]};color:{DARK["text"]};border:1px solid {DARK["border"]};')
        if connected:
            conn_action = menu.addAction('Disconnect')
        else:
            conn_action = menu.addAction('Connect')
        menu.addSeparator()
        edit_action   = menu.addAction('Edit')
        remove_action = menu.addAction('Remove')
        action = menu.exec_(self.dev_list.mapToGlobal(pos))
        if action == conn_action:
            if connected:
                self._disconnect_device(nvr)
            elif nvr.host:
                self.status.showMessage(f'[{nvr.name}] connecting...')
                threading.Thread(target=self._connect_device, args=(nvr,), daemon=True).start()
        elif action == edit_action:
            dlg = DeviceDialog(nvr, self)
            dlg.saved.connect(lambda n: (NVRClient.save_all(self.devices), self._refresh_device_list()))
            dlg.show()
        elif action == remove_action:
            self._disconnect_device(nvr)
            self.devices.remove(nvr)
            NVRClient.save_all(self.devices)
            self._refresh_device_list()

    def _disconnect_device(self, nvr):
        """Drop a device's live connection: stop its streams, log out of the SDK,
        and remove its cameras from the list. The device stays configured."""
        if self.audio.is_playing(nvr):
            self.audio.stop()
        # Stop any live cells streaming this device's cameras.
        try:
            for uid, cell in list(getattr(self.live_tab, 'cells', {}).items()):
                if self._nvr_map.get(uid) is nvr and getattr(cell, '_streaming', False):
                    cell.stop_stream()
        except Exception as e:
            print(f'[Disconnect] stop streams: {e}')
        try:
            nvr.sdk_logout()
        except Exception as e:
            print(f'[Disconnect] sdk_logout: {e}')
        self._all_cameras = [c for c in self._all_cameras if c.get('_nvr') is not nvr]
        self._nvr_map = {u: n for u, n in self._nvr_map.items() if n is not nvr}
        self._refresh_camera_list()
        self._refresh_device_list()
        self.status.showMessage(f'[{nvr.name}] disconnected')

    def _connect_all(self):
        self.status.showMessage('Connecting...')
        for nvr in self.devices:
            if nvr.host:
                threading.Thread(target=self._connect_device, args=(nvr,), daemon=True).start()

    def _connect_device(self, nvr):
        ok, msg = nvr.test()
        if not ok:
            # Safe: status.showMessage is generally thread-safe in Qt5
            QTimer.singleShot(0, lambda: self.status.showMessage(f'[{nvr.name}] {msg}'))
            return
        cams = nvr.get_cameras()
        # Emit signal → UI update on main thread
        self._cameras_ready.emit(nvr, cams)

    def _on_cameras_ready(self, nvr, cams):
        """Called on main thread when a device finishes connecting."""
        # Assign unique IDs: deviceid_origchannel to avoid collisions
        for c in cams:
            orig_id     = c['id']
            c['uid']    = f'{nvr.device_id}_{orig_id}'
            c['nvr_name'] = nvr.name
            c['nvr_id'] = nvr.device_id
            c['_nvr']   = nvr
            self._nvr_map[c['uid']] = nvr

        # Remove old cameras from this NVR, add fresh ones
        self._all_cameras = [c for c in self._all_cameras if c.get('nvr_id') != nvr.device_id]
        self._all_cameras.extend(cams)
        self._refresh_camera_list()
        self._refresh_device_list()
        total = len(self._all_cameras)
        self.status.showMessage(f'{len(self.devices)} device(s)  |  {total} camera(s)')

    def _refresh_camera_list(self):
        self.cam_list.clear()
        # Group cameras under an expandable header per device (preserves order).
        for nvr in self.devices:
            cams = [c for c in self._all_cameras if c.get('nvr_id') == nvr.device_id]
            if not cams:
                continue
            grp = QTreeWidgetItem(self.cam_list, [f'{nvr.name}  ({len(cams)})'])
            grp.setData(0, Qt.UserRole, None)          # header → no uid, not draggable
            grp.setForeground(0, QColor(DARK['dim']))
            gf = grp.font(0); gf.setBold(True); grp.setFont(0, gf)
            grp.setFlags(grp.flags() & ~Qt.ItemIsDragEnabled)
            for cam in cams:
                uid   = cam.get('uid', cam['id'])
                child = QTreeWidgetItem(grp, [f'📷  {cam["name"]}'])
                child.setData(0, Qt.UserRole, uid)
                child.setForeground(0, QColor(DARK['green']))
            grp.setExpanded(True)

        if self._all_cameras:
            self.live_tab.nvr = self._all_cameras[0]['_nvr']
            self.pb_tab.nvr   = self._all_cameras[0]['_nvr']

        # Pass cameras with uid as the working id
        live_cams = [{**c, 'id': c.get('uid', c['id'])} for c in self._all_cameras]
        self.live_tab.set_cameras(live_cams)
        self.pb_tab.set_cameras(live_cams, self._nvr_map)

    def _cam_clicked(self, item, column=0):
        uid = item.data(0, Qt.UserRole)
        if not uid:                       # group header → toggle expand/collapse
            item.setExpanded(not item.isExpanded())
            return
        nvr = self._nvr_map.get(uid)
        if nvr:
            self.live_tab.nvr = nvr
        self.live_tab.select_camera(uid)
        self.tabs.setCurrentIndex(0)

    def _cam_dbl_clicked(self, item, column=0):
        """Double-click: switch to live 1×1 and start that camera."""
        uid = item.data(0, Qt.UserRole)
        if not uid:
            return
        nvr = self._nvr_map.get(uid)
        if nvr:
            self.live_tab.nvr = nvr
        self.live_tab.select_camera(uid)
        self.live_tab.grid_combo.setCurrentIndex(0)   # 1×1
        cell = self.live_tab.cells.get(uid)
        if cell and not cell._streaming:
            cell.start_stream(self.live_tab._should_use_sub())
        self.tabs.setCurrentIndex(0)

    # ── Camera management (ONVIF / RTSP options) ─────────────────────────────
    def _camera_context_menu(self, pos):
        from PyQt5.QtWidgets import QMenu
        item = self.cam_list.itemAt(pos)
        if not item:
            return
        uid = item.data(0, Qt.UserRole)
        if not uid:
            return
        nvr = self._nvr_map.get(uid)
        if nvr is None:
            return
        menu = QMenu(self)
        menu.setStyleSheet(f'background:{DARK["panel"]};color:{DARK["text"]};'
                           f'border:1px solid {DARK["border"]};')
        manage = None
        if getattr(nvr, 'device_type', '') in ('onvif', 'rtsp'):
            manage = menu.addAction('Manage camera…')
        else:
            menu.addAction('(no options for NVR cameras)').setEnabled(False)
        act = menu.exec_(self.cam_list.viewport().mapToGlobal(pos))
        if manage is not None and act == manage:
            self._manage_camera(nvr)

    def _manage_camera(self, nvr):
        dlg = ManageCameraDialog(nvr, self, self)
        dlg.show()

    def _add_onvif_channels(self, nvr, channels):
        """Add user-picked RTSP channels as extra cameras under this device."""
        existing = {c.get('url') for c in getattr(nvr, 'extra_channels', [])}
        for ch in channels:
            if ch['url'] not in existing:
                nvr.extra_channels.append(ch)
                existing.add(ch['url'])
        nvr._lenses = []                       # force re-fetch with new channels
        NVRClient.save_all(self.devices)
        self.status.showMessage(f'[{nvr.name}] added {len(channels)} channel(s)')
        threading.Thread(target=self._connect_device, args=(nvr,), daemon=True).start()

    def _set_audio(self, nvr, on):
        if on:
            url = nvr.rtsp_live_url('1', sub=False)
            self.audio.play(url, nvr)
            self.status.showMessage(f'[{nvr.name}] listening to microphone')
        else:
            self.audio.stop()
            self.status.showMessage('Audio stopped')

    def _restart_nvr_streams(self, nvr):
        """Restart any live cells belonging to this device (e.g. after a quality
        change) so the new stream URL takes effect."""
        for uid, cell in list(self.live_tab.cells.items()):
            if self._nvr_map.get(uid) is nvr and getattr(cell, '_streaming', False):
                cell.stop_stream()
                QTimer.singleShot(300, lambda c=cell: c.start_stream(
                    self.live_tab._should_use_sub()))

    def closeEvent(self, e):
        # Stop the live-audio ffmpeg so it doesn't linger after the app quits.
        try: self.live_tab._audio_player.stop()
        except Exception: pass
        super().closeEvent(e)


# ── Drag-enabled camera list ───────────────────────────────────────────────────
class CameraListWidget(QTreeWidget):
    """Sidebar camera list grouped per device (expandable). Camera leaf items
    support drag-to-cell; group (device) headers do not."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setIndentation(12)
        self.setRootIsDecorated(True)
        self.setDragEnabled(True)
        self.setDragDropMode(QTreeWidget.DragOnly)
        self.setDefaultDropAction(Qt.CopyAction)

    def startDrag(self, supported_actions):
        item = self.currentItem()
        if not item:
            return
        uid = item.data(0, Qt.UserRole)
        if not uid:   # group header — nothing to drag
            return
        from PyQt5.QtCore import QMimeData
        from PyQt5.QtGui import QDrag
        mime = QMimeData()
        mime.setText(uid)
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec_(Qt.CopyAction)


def _cleanup_sdk():
    """Logout from all NVRs and clean up SDK."""
    if _SDK is None:
        return
    try:
        _SDK.cleanup()
    except: pass

import atexit
atexit.register(_cleanup_sdk)


if __name__ == '__main__':
    os.environ['QT_LOGGING_RULES'] = 'qt.qpa.wayland=false'
    if DEBUG:
        log('APP', 'Debug logging ON (HIK_DEBUG=1). Stats every 3s.')
    _STATS.start()
    app = QApplication(sys.argv)
    app.setStyleSheet(STYLE)
    app.setStyle('Fusion')

    pal = QPalette()
    pal.setColor(QPalette.Window,     QColor(DARK['bg']))
    pal.setColor(QPalette.WindowText, QColor(DARK['text']))
    pal.setColor(QPalette.Base,       QColor(DARK['panel']))
    pal.setColor(QPalette.Text,       QColor(DARK['text']))
    pal.setColor(QPalette.Button,     QColor(DARK['panel']))
    pal.setColor(QPalette.ButtonText, QColor(DARK['text']))
    pal.setColor(QPalette.Highlight,  QColor(DARK['accent']))
    app.setPalette(pal)

    win = MainWindow()
    win.show()
    sys.exit(app.exec_())
