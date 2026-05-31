import sys, os, json, threading, time, queue
from datetime import datetime, timedelta
from pathlib import Path

                                                                                 
                                                                            
                                                                                
                                                                           
                                                                     
def _bootstrap_sdk_path():
    sdk_path = os.environ.get('HIKVISION_SDK_PATH')\
               or os.path.expanduser('~/Desktop/sdk/lib')
    sdk_path = os.path.abspath(sdk_path)
    if not os.path.isdir(sdk_path):
        return                                              
    com_path = os.path.join(sdk_path, 'HCNetSDKCom')
    parts = (os.environ.get('LD_LIBRARY_PATH', '') or '').split(':')
    if sdk_path in parts:
        return                             
    os.environ['LD_LIBRARY_PATH'] = ':'.join([sdk_path, com_path] + parts)
    os.execv(sys.executable, [sys.executable] + sys.argv)

_bootstrap_sdk_path()

                                                                                
                                                                        

def log(category, msg):
    print(f'[{time.strftime("%H:%M:%S")}] [{category}] {msg}', flush=True)

                                                                                
def _detect_vaapi():
    """Return the DRI render node path if VAAPI HEVC/H264 decode is usable, else None."""
    import glob, subprocess as sp
    nodes = sorted(glob.glob('/dev/dri/renderD*'))
    if not nodes:
        return None
                                                      
    try:
        out = sp.run(['ffmpeg', '-hide_banner', '-hwaccels'],
                     capture_output=True, text=True, timeout=5)
        if 'vaapi' not in out.stdout:
            return None
    except Exception:
        return None
    return nodes[0]

_VAAPI_NODE = _detect_vaapi()
if _VAAPI_NODE:
    print(f'[App] VAAPI GPU decode available at {_VAAPI_NODE} — playback will use GPU')
else:
    print('[App] VAAPI not available — playback will use PlayM4 (CPU) decode')
try:
    from hik_sdk import HCNetSDK
    try:
        _SDK = HCNetSDK()
        print('[App] Hikvision SDK loaded — playback will use SDK (port 8000)')
    except Exception as _e:
        print(f'[App] SDK init failed, falling back to RTSP playback: {_e}')
except ImportError:
    print('[App] hik_sdk.py not found — playback will use RTSP/HTTP only')

                                                                                 
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
    QTabWidget, QMessageBox, QToolBar, QAction, QSlider, QStyle
)
from PyQt5.QtCore import (
    Qt, QThread, pyqtSignal, QTimer, QDate, QSize, QMutex, QMutexLocker
)
from PyQt5.QtGui import (
    QImage, QPixmap, QFont, QColor, QPalette, QIcon
)

                                                                                 
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

                                                                                 
class NVRClient:
    def __init__(self, device_id=None, name='NVR'):
        self.device_id = device_id or str(id(self))
        self.name      = name
        self.host      = ''
        self.port      = 80
        self.username  = 'admin'
        self.password  = ''
        self.timeout   = 10
                                                                           
        self.sdk_user_id  = -1
        self.start_dchan  = 33                                                    

    @classmethod
    def load_all(cls):
        """Load all NVR configs from file. Returns list of NVRClient."""
        clients = []
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text())
                devices = data.get('devices', [])
                if not devices and data.get('host'):
                                                 
                    devices = [data]
                for d in devices:
                    c = cls(d.get('id'), d.get('name', 'NVR'))
                    c.host     = d.get('host', '')
                    c.port     = d.get('port', 80)
                    c.username = d.get('username', 'admin')
                    c.password = d.get('password', '')
                    clients.append(c)
            except: pass
        return clients

    @staticmethod
    def save_all(clients):
        CONFIG_FILE.write_text(json.dumps({'devices': [
            {'id': c.device_id, 'name': c.name, 'host': c.host,
             'port': c.port, 'username': c.username, 'password': c.password}
            for c in clients
        ]}, indent=2))

    def to_dict(self):
        return {'id': self.device_id, 'name': self.name, 'host': self.host,
                'port': self.port, 'username': self.username, 'password': self.password}

    def sdk_login(self):
        """Login via Hikvision SDK on port 8000. Used for playback to avoid
        RTSP bandwidth limits. Returns True on success."""
        if _SDK is None:
            return False
        if self.sdk_user_id >= 0:
            return True                      
        try:
            self.sdk_user_id, info = _SDK.login(self.host, 8000, self.username, self.password)
            self.start_dchan = info.byStartDChan or 33
            print(f'[SDK] {self.name} ({self.host}) → user_id={self.sdk_user_id}  '
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

        def find_text(el, tag, ns):
            """Sigurno dohvati tekst iz XML elementa — bez DeprecationWarning"""
            found = el.find(f'h:{tag}', ns)
            if found is None:
                found = el.find(tag)
            return found.text.strip() if found is not None and found.text else ''

        cameras = []
        try:
            xml = self.get('ContentMgmt/InputProxy/channels')
            root = ET.fromstring(xml)
            ns = {'h': 'http://www.hikvision.com/ver20/XMLSchema'}
            channels = root.findall('.//h:InputProxyChannel', ns)
            if not channels:
                channels = root.findall('.//InputProxyChannel')
            for ch in channels:
                cid  = find_text(ch, 'id', ns)
                name = find_text(ch, 'name', ns)
                ip   = find_text(ch, 'ipAddress', ns)
                cameras.append({
                    'id': cid,
                    'name': name if name else f'Kamera {cid}',
                    'ip': ip,
                    'status': 'online'
                })
        except Exception as e:
            print(f'InputProxy error: {e} — pokušavam Streaming/channels')
                                          
            try:
                xml = self.get('Streaming/channels')
                root = ET.fromstring(xml)
                ns = {'h': 'http://www.hikvision.com/ver20/XMLSchema'}
                chs = root.findall('.//h:StreamingChannel', ns)
                if not chs:
                    chs = root.findall('.//StreamingChannel')
                for ch in chs:
                    cid = find_text(ch, 'id', ns)
                    if cid and cid.endswith('01'):
                        base_id = cid[:-2] if len(cid) > 2 else cid
                        name = find_text(ch, 'channelName', ns)
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

        ns10 = {'h': 'http://www.hikvision.com/ver10/XMLSchema'}
        ns20 = {'h': 'http://www.hikvision.com/ver20/XMLSchema'}

        def find_text(el, path, ns):
            """Traži nested XML path s namespace prefiksom na svakom dijelu"""
                                                                      
            ns_path = '/'.join(f'h:{p}' for p in path.split('/'))
            found = el.find(ns_path, ns)
            if found is None:
                found = el.find(path)                           
            return found.text.strip() if found is not None and found.text else ''

        def detect_ns(root):
            """Otkrij koji namespace koristi ovaj XML odgovor"""
            if root.findall('.//h:matchList', ns20):
                return ns20
            if root.findall('.//h:matchList', ns10):
                return ns10
            return {}                  

        def parse_page(xml_resp):
            root = ET.fromstring(xml_resp)
            ns = detect_ns(root)
            items = (root.findall('.//h:matchList/h:searchMatchItem', ns) if ns
                     else root.findall('.//matchList/searchMatchItem'))
                                                    
            status_el = root.find('h:responseStatusStrg', ns) if ns else None
            if status_el is None:
                status_el = root.find('responseStatusStrg')
            status = status_el.text.strip() if status_el is not None and status_el.text else ''
            page_recs = []
            for item in items:
                page_recs.append({
                    'start': find_text(item, 'timeSpan/startTime', ns),
                    'end':   find_text(item, 'timeSpan/endTime', ns),
                    'uri':   find_text(item, 'mediaSegmentDescriptor/playbackURI', ns),
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

                                                                             
                                                                                   
                                                                                 
                                                                              
        recs = []
        seen_keys = set()
        WINDOW_HOURS = 2
        day_str = date.strftime('%Y-%m-%d')                                     
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
            print(f'[ISAPI] Prozor {win_start[11:16]}–{win_end[11:16]}: '
                  f'{len(win_recs)} nađeno, {added} novih, ukupno={len(recs)}')
            h += WINDOW_HOURS

        try:
            recs.sort(key=lambda r: r.get('start', ''))
        except: pass
        print(f'[ISAPI] UKUPNO za dan: {len(recs)} snimaka')
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
        self._player    = None                                             
        self._tmp_file  = None                                        
        self._dl_handle = -1                                                 
        self._dl_complete = False                                            
        self._dl_percent = 0                                         
        self._file_base_offset_s = 0.0
        self.fps        = 25.0
                                                                                
        self._frame_in_flight = False

                                                                                 
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

    def notify_displayed(self):
        """Called by the UI after it paints a frame — frees the in-flight slot."""
        self._frame_in_flight = False

    def stop(self):
        self._stop.set()
        self._pause.clear()
                                                                               
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
                                          
        if self._tmp_file:
            try: os.unlink(self._tmp_file)
            except: pass
            self._tmp_file = None

                                                                                  
    def _ffmpeg_base(self, extra_in, source):
        """Common ffmpeg command. extra_in: list of input opts. source: -i value."""
        W, H = self.decode_w, self.decode_h
        vf = f'scale={W}:{H}'
                                                                   
        if self.speed and self.speed != 1.0:
            vf += f',setpts=PTS/{self.speed}'
        return ['ffmpeg', '-loglevel', 'error', *extra_in,
                '-i', source,
                '-vf', vf, '-an',
                '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-']

    def _build_proc_rtsp(self, frame_bytes):
        import subprocess as sp
        cmd = self._ffmpeg_base(
            ['-rtsp_transport', 'tcp', '-fflags', 'nobuffer',
             '-flags', 'low_delay', '-analyzeduration', '1000000',
             '-probesize', '1000000'],
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

                                                                                 
                                                                              
                                                                                  
        wait_for = src.get('wait_for')
        if wait_for is not None:
            wait_for.wait(timeout=3.0)
        if self._stop.is_set():
            return

        tmp = tempfile.NamedTemporaryFile(prefix='hik_pb_', suffix='.mp4', delete=False)
        tmp_path = tmp.name
        tmp.close()
        self._tmp_file = tmp_path

                                                                                
                                                                                 
                                                                                  
                                                                   
        try:
            dh = _SDK.download_start(nvr.sdk_user_id, real_ch,
                                     src['start_dt'], src['end_dt'], tmp_path)
            self._dl_handle = dh
            log('DOWNLOAD', f'started (stream-as-grows) ch={real_ch} '
                            f'start={src["start_dt"]:%H:%M:%S} end={src["end_dt"]:%H:%M:%S} '
                            f'→ {tmp_path}')
        except Exception as e:
            self.error.emit(self.channel_id, f'Download failed: {e}')
            return

                                                                                  
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
            self._dl_complete = True                                               

        self._dl_complete = False
        self._dl_percent = 0
        threading.Thread(target=_dl_monitor, daemon=True).start()

                                                                               
        self.position_ms.emit(-1)                                          
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

                                                                                
        nv12_bytes = W * H * 3 // 2
        self._play_file_loop(tmp_path, W, H, nv12_bytes,
                             seek_off=src.get('seek_offset_s', 0.0))

    def _build_play_cmd(self, tmp_path, W, H, seek_off=0.0):
        """ffmpeg command for playing a local recording file with GPU decode."""
        use_gpu = bool(_VAAPI_NODE)
        if use_gpu:
            vf = f'scale_vaapi=w={W}:h={H},hwdownload,format=nv12'
        else:
            vf = f'scale={W}:{H}'
        spd = self.speed if (self.speed and self.speed > 0) else 1.0
        cmd = ['ffmpeg', '-loglevel', 'error', '-readrate', f'{spd:.3f}']
                                                                                  
        if seek_off and seek_off > 0:
            cmd += ['-ss', f'{seek_off:.3f}']
        if use_gpu:
            cmd += ['-hwaccel', 'vaapi', '-hwaccel_device', _VAAPI_NODE,
                    '-hwaccel_output_format', 'vaapi']
        cmd += ['-i', tmp_path, '-vf', vf, '-an',
                '-f', 'rawvideo', '-pix_fmt', 'nv12', '-']
        return cmd, use_gpu

    def local_seek(self, offset_s):
        """Re-position playback within the already-downloaded file WITHOUT a new
        SDK download. Kills the current ffmpeg and relaunches it with -ss."""
        if not getattr(self, '_tmp_file', None):
            return
        self._local_seek_to = offset_s
        self._local_seek_req.set()
                                                                                 
        if self._proc:
            try: self._proc.terminate()
            except: pass

    def _play_file_loop(self, tmp_path, W, H, nv12_bytes, seek_off=0.0):
        import subprocess as sp, numpy as np, cv2
        self._local_seek_req = threading.Event()
        self._local_seek_to = 0.0

        while not self._stop.is_set():
            cmd, use_gpu = self._build_play_cmd(tmp_path, W, H, seek_off)
            try:
                self._proc = sp.Popen(cmd, stdout=sp.PIPE, stderr=sp.PIPE,
                                      bufsize=nv12_bytes)
            except Exception as e:
                self.error.emit(self.channel_id, f'Playback failed: {e}')
                return
            log('PLAYFILE', f'playing {tmp_path} (GPU={"yes" if use_gpu else "no"}, '
                            f'NV12+cv2, speed={self.speed}×, -ss={seek_off:.0f}s)')

            stdout = self._proc.stdout
            base_frames = int(seek_off * self.fps)
            frame_counter = 0
            clk = os.sysconf('SC_CLK_TCK')
            def _ffmpeg_cpu():
                try:
                    with open(f'/proc/{self._proc.pid}/stat') as f:
                        p = f.read().split()
                    return (int(p[13]) + int(p[14])) / clk
                except: return 0.0
            cpu0 = _ffmpeg_cpu(); t_stat = time.time()

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
                        try: err = self._proc.stderr.read(1500)
                        except: pass
                        m = err.decode(errors='replace').strip()
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

                                                                            
                                                                      
        first = {'buf': bytearray(), 'ready': threading.Event(), 'codec': None}

        def sniff(data):
            first['buf'].extend(data)
            if len(first['buf']) >= 4096 and not first['ready'].is_set():
                first['codec'] = _sniff_codec(bytes(first['buf']))
                first['ready'].set()

                                                                                 
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

                                           
        if not first['ready'].wait(3.0):
            first['codec'] = 'hevc'                 
        codec = first['codec'] or 'hevc'
        log('VAAPI', f'{self.channel_id} codec={codec} ch={real_ch} mode={mode}')

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
            except Exception as e:
                log('DECODE', f'{self.channel_id} convert error: {e}')

        try:
            self._player = PlayM4()
            self._player.open(on_decoded, realtime=(mode == 'live'))
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
                                                                              
                self._player.input(data, is_header=(dtype == 1))
            except Exception:
                pass

                                                                                
                                                                                
                                                                           
                                                                          
                                                                               
                                                                          
                                                            
            if mode == 'playback' and not self._stop.is_set():
                while not self._stop.is_set() and not self._pause.is_set():
                    remain = self._player.source_buffer_remain()
                                                                                    
                    if remain < 1_500_000:
                        break
                    time.sleep(0.02)

        try:
            if mode == 'live':
                self._sdk_handle = _SDK.realplay(nvr.sdk_user_id, real_ch, sdk_data,
                                                 sub=src.get('sub', False))
                log('PLAYM4', f'Live {self.channel_id} ch={real_ch} '
                              f'sub={src.get("sub", False)} handle={self._sdk_handle}')
            else:
                self._sdk_handle = _SDK.playback_by_time(
                    nvr.sdk_user_id, real_ch, src['start_dt'], src['end_dt'], sdk_data)
                log('PLAYM4', f'Playback {self.channel_id} ch={real_ch} '
                              f'handle={self._sdk_handle}')
        except Exception as e:
            self.error.emit(self.channel_id, f'SDK stream failed: {e}')
            return

                                                              
        check_at = time.time() + 5.0
        warned = False
        while not self._stop.is_set():
            time.sleep(0.1)
            if not warned and time.time() > check_at:
                warned = True
                if st['fed'] == 0:
                    log('PLAYM4', f'{self.channel_id}: WARNING no stream data from SDK after 5s')
                                                                                   
                                                      
                    if src.get('sub'):
                        self.error.emit(self.channel_id, 'NO_SUBSTREAM')
                        return
                elif st['decoded'] == 0:
                    log('PLAYM4', f'{self.channel_id}: WARNING data flowing ({st["fed"]} pkts) '
                                  f'but PlayM4 decoded 0 frames — check codec/SetStreamOpenMode')
                else:
                    pass

                                                                                   
    def run(self):
        import numpy as np

                             
                                                                                 
                                                                              
                                                                         
        if self.sdk_source is not None and _SDK:
            mode = self.sdk_source.get('mode', 'playback')
            if mode == 'playback':
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

                                                                                
class FullscreenWindow(QWidget):
    def __init__(self, channel_id, name, nvr, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f'Live: {name}')
        self.setStyleSheet('background: black;')
        self.resize(1280, 720)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        self.label = QLabel(f'⏳ Connecting {name}...')
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
        pm = QPixmap.fromImage(qi).scaled(
            self.label.width(), self.label.height(),
            Qt.KeepAspectRatio, Qt.FastTransformation)
        self.label.setPixmap(pm)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Escape, Qt.Key_F, Qt.Key_Q):
            self.close()

    def closeEvent(self, e):
        self.worker.stop()
        self.worker.wait(2000)
        super().closeEvent(e)

                                                                                 
class VideoCell(QFrame):
    clicked = pyqtSignal(str)

    def __init__(self, channel_id, name, nvr: NVRClient, parent=None, real_channel=None):
        super().__init__(parent)
        self.channel_id   = channel_id
        self.real_channel = real_channel or channel_id                               
        self.name = name
        self.nvr = nvr
        self.worker = None
        self._streaming = False

        self.setFrameShape(QFrame.Box)
        self.setStyleSheet(f'QFrame {{ background: #0d1117; border: 1px solid #21262d; border-radius: 4px; }}')
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(320, 180)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

                     
        self.video_label = QLabel()
        self.video_label.setAlignment(Qt.AlignCenter)
        self.video_label.setStyleSheet('border: none; background: transparent;')
        self.video_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.video_label.setScaledContents(False)
        lay.addWidget(self.video_label)

                     
        self.placeholder = QLabel(f'📷\n{name}')
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.placeholder.setStyleSheet(f'color: {DARK["dim"]}; font-size: 13px; border: none;')
        lay.addWidget(self.placeholder)
        self.video_label.hide()

                    
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
                                                                  
        if self._streaming:
            if not self.worker or not self.worker.isRunning():
                print(f'[Cell {self.name}] Zombie streaming flag — clearing')
                self._streaming = False
            else:
                return

                                                                                
                                              
        self.video_label.setPixmap(QPixmap())
        self.video_label.hide()
        self.placeholder.show()

        self._streaming = True
        self.status_dot.setStyleSheet(f'color: {DARK["amber"]}; font-size: 10px; border: none;')
        self.placeholder.setText(f'⏳\nConnecting...')

                                                                                  
        if sub:
            dw, dh = 640, 360
        else:
            dw, dh = 1280, 720

                                                                                 
        if _PLAYM4_OK and _SDK and self.nvr.sdk_user_id >= 0:
            print(f'[SDK Live {"sub" if sub else "main"}] {self.name}  '
                  f'ch={self.real_channel}  decode={dw}x{dh}')
            self.worker = VideoWorker(
                self.channel_id, '', decode_w=dw, decode_h=dh,
                sdk_source={'nvr': self.nvr, 'channel': self.real_channel,
                            'mode': 'live', 'sub': sub})
        else:
            url = self.nvr.rtsp_live_url(self.real_channel, sub)
            print(f'[RTSP {"sub" if sub else "main"}] {self.name} → '
                  f'{url.replace(self.nvr.password, "***")}  decode={dw}x{dh}')
            self.worker = VideoWorker(self.channel_id, url, decode_w=dw, decode_h=dh)

        self.worker.frame_ready.connect(self._on_frame, Qt.QueuedConnection)
        self.worker.error.connect(self._on_error)
        self.worker.start()

    def stop_stream(self):
        if self.worker:
            print(f'[Cell {self.name}] STOP')
            old = self.worker
            self.worker = None
                                                                                 
                                                               
            try: old.frame_ready.disconnect(self._on_frame)
            except: pass
            try: old.error.disconnect(self._on_error)
            except: pass
                                                      
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
        pm = QPixmap.fromImage(qi).scaled(
            self.video_label.width(), self.video_label.height(),
            Qt.KeepAspectRatio, Qt.FastTransformation
        )
        self.video_label.setPixmap(pm)
        if not self.video_label.isVisible():
            self.placeholder.hide()
            self.video_label.show()
            self.status_dot.setStyleSheet(f'color: {DARK["green"]}; font-size: 10px; border: none;')
            self.setStyleSheet(f'QFrame {{ background: #0d1117; border: 1px solid {DARK["green"]}44; border-radius: 4px; }}')

    def _on_error(self, cid, msg):
        if msg == 'NO_SUBSTREAM':
                                                                               
            print(f'[Cell {self.name}] no sub-stream, falling back to main')
            self._streaming = False
            QTimer.singleShot(100, lambda: self.start_stream(sub=False))
            return
        self._streaming = False
        self.video_label.hide()
        self.placeholder.setText(f'⚠️\n{msg}')
        self.placeholder.show()
        self.status_dot.setStyleSheet(f'color: {DARK["red"]}; font-size: 10px; border: none;')

                                                                                 
class LiveViewTab(QWidget):
    def __init__(self, nvr: NVRClient, parent=None):
        super().__init__(parent)
        self.nvr = nvr
        self.cameras = []
        self.cells = {}

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

                 
        toolbar = QHBoxLayout()
        self.btn_all = QPushButton('▶  Start all')
        self.btn_all.setProperty('class', 'success')
        self.btn_all.clicked.connect(self.start_all)

        self.btn_stop = QPushButton('■  Stop all')
        self.btn_stop.setProperty('class', 'danger')
        self.btn_stop.clicked.connect(self.stop_all)

                                                                        
        self.lbl_quality = QLabel('Quality: auto')
        self.lbl_quality.setStyleSheet(f'color:{DARK["dim"]};font-size:11px;padding:0 8px;')

        self.grid_combo = QComboBox()
        self.grid_combo.addItems(['1×1', '2×2', '3×3', '4×4', '1+3', '1+7'])
        self.grid_combo.setFixedWidth(110)
        self.grid_combo.currentIndexChanged.connect(self._on_grid_changed)

        toolbar.addWidget(self.btn_all)
        toolbar.addWidget(self.btn_stop)
        toolbar.addWidget(self.lbl_quality)
        toolbar.addStretch()
        toolbar.addWidget(QLabel('Layout:'))
        toolbar.addWidget(self.grid_combo)
        lay.addLayout(toolbar)

                   
        self.grid_widget = QWidget()
        self.grid_widget.setAcceptDrops(True)
        self.grid_widget.dragEnterEvent = self._grid_drag_enter
        self.grid_widget.dropEvent      = self._grid_drop
        self.grid_layout = QGridLayout(self.grid_widget)
        self.grid_layout.setSpacing(6)
        lay.addWidget(self.grid_widget)

    def set_cameras(self, cameras):
        self.cameras = cameras
        self.stop_all()
        self.cells.clear()
        self._update_quality_label()
        self.selected_cam_id = cameras[0]['id'] if cameras else None
        for cam in cameras[:16]:
            nvr = cam.get('_nvr', self.nvr)
            uid = cam['id']                                                    
                                                                       
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

        if self.grid_combo.currentIndex() == 0:
            self.relayout(0)
            new_cell = self.cells.get(channel_id)
            if new_cell:
                                                     
                if new_cell._streaming and (not new_cell.worker or
                                            not new_cell.worker.isRunning()):
                    new_cell._streaming = False
                if not new_cell._streaming:
                    new_cell.start_stream(self._should_use_sub())

                                                                             
                                                                       
            if old_id and old_id != channel_id:
                old_cell = self.cells.get(old_id)
                if old_cell and old_cell._streaming:
                    QTimer.singleShot(400, lambda c=old_cell: (
                        c.stop_stream() if c is not self.cells.get(self.selected_cam_id)
                        else None))
        else:
            self.relayout(self.grid_combo.currentIndex())

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
                                                             
        sub = self._should_use_sub()
        for cell in self.cells.values():
            if cell._streaming:
                cell.stop_stream()
                                                                              
                QTimer.singleShot(300, lambda c=cell, s=sub: c.start_stream(s))

    def relayout(self, idx=None):
        while self.grid_layout.count():
            self.grid_layout.takeAt(0)

        cams = list(self.cells.values())
        if not cams:
            return

        sel = self.cells.get(getattr(self, 'selected_cam_id', None)) or cams[0]
        mode = self.grid_combo.currentIndex()

                                                                          
                                                                                 
                                                                                  
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

        if mode == 0:         
            self.grid_layout.addWidget(sel, 0, 0)
        elif mode == 1:       
            for i, c in enumerate(cams[:4]):
                self.grid_layout.addWidget(c, i//2, i%2)
        elif mode == 2:       
            for i, c in enumerate(cams[:9]):
                self.grid_layout.addWidget(c, i//3, i%3)
        elif mode == 3:       
            for i, c in enumerate(cams[:16]):
                self.grid_layout.addWidget(c, i//4, i%4)
        elif mode == 4:       
            self.grid_layout.addWidget(sel, 0, 0, 2, 2)
            others = [c for c in cams if c is not sel]
            for i, c in enumerate(others[:3]):
                self.grid_layout.addWidget(c, i, 2)
        elif mode == 5:       
            self.grid_layout.addWidget(sel, 0, 0, 2, 2)
            others = [c for c in cams if c is not sel]
            for i, c in enumerate(others[:7]):
                row, col = divmod(i, 2)
                                                    
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
                                                                
            self.select_camera(uid)
            if not cell._streaming:
                cell.start_stream(self._should_use_sub())
        else:
                                                                               
                                     
            if not cell._streaming:
                cell.start_stream(self._should_use_sub())

    def start_all(self):
        sub = self._should_use_sub()
        for cell in self.cells.values():
            cell.start_stream(sub)

    def stop_all(self):
        for cell in self.cells.values():
            cell.stop_stream()

                                                                                 
class TimelineWidget(QWidget):
    """
    iVMS-style timeline:
    - Fixed red cursor line at center
    - Timeline scrolls/zooms underneath
    - Scroll wheel: zoom in/out
    - Click: seek to time at that position
    - Playback updates cursor (timeline pans to follow)
    """
    seek_requested = pyqtSignal(int)                               
    seek_to_time   = pyqtSignal(int, float)                                                 

    RULER_H = 22
    BAR_H   = 26
    PAD     = 3

    def __init__(self, parent=None):
        super().__init__(parent)
        self.recordings  = []
        self._rec_secs   = []                                               
        self._painting   = False                                                   
        self.cursor_s    = 43200.0                                                       
        self.zoom_s      = 3600.0                                                     
        self.selected_idx = -1
        self._drag_start_x  = None
        self._drag_start_s  = None
        self._hover_s       = -1.0
        self._user_scrubbing = False                                             
        self._did_pan        = False
        self.setMinimumHeight(self.RULER_H + self.BAR_H + self.PAD * 2 + 10)
        self.setMouseTracking(True)
        self.setCursor(Qt.SizeHorCursor)

                                                        
    def set_recordings(self, recs):
        self.recordings   = recs
        self.selected_idx = -1
                                                                              
                                                                        
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

                                                         
    def paintEvent(self, event):
                                                                              
                                                                          
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

                         
            p.setPen(QPen(QColor('#21262d'), 1))
            t = (int(view_start / minor_s)) * minor_s
            while t <= view_end:
                x = self._s_to_x(t)
                if 0 <= x <= W:
                    p.drawLine(x, rh - 4, x, rh)
                t += minor_s

                                  
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

                                                           
            hover_idx = self._find_rec_idx_at(self._hover_s) if self._hover_s >= 0 else -1

                                                                           
            col_sel  = QColor('#388bfd')
            col_hov  = QColor('#58a6ff')
            col_norm = QColor('#1f6feb')
            for i, (s1, s2) in enumerate(self._rec_secs):
                x1 = self._s_to_x(s1)
                x2 = self._s_to_x(s2)
                if x2 < 0 or x1 > W:
                    continue                     
                x1c = max(0, x1); x2c = min(W, x2)
                if x2c <= x1c:
                    x2c = x1c + 1                                
                color = col_sel if i == self.selected_idx else (
                        col_hov if i == hover_idx else col_norm)
                p.fillRect(x1c, track_y + 2, x2c - x1c, bh - 4, color)

                           
            p.setPen(QPen(QColor('#e63946'), 2))
            p.drawLine(cx, 0, cx, H)
            tri = QPolygon([QPoint(cx - 5, 0), QPoint(cx + 5, 0), QPoint(cx, 8)])
            p.setBrush(QBrush(QColor('#e63946')))
            p.setPen(Qt.NoPen)
            p.drawPolygon(tri)

                                     
            hh = int(self.cursor_s // 3600) % 24
            mm = int((self.cursor_s % 3600) // 60)
            ss = int(self.cursor_s % 60)
            p.setPen(QColor('#e63946'))
            p.setFont(QFont('monospace', 9, QFont.Bold))
            p.drawText(cx + 8, 16, f'{hh:02d}:{mm:02d}:{ss:02d}')
        finally:
            p.end()
            self._painting = False

                                                         
    def wheelEvent(self, e):
        delta = e.angleDelta().y()
        factor = 0.85 if delta > 0 else 1.18                
        self.zoom_s = max(30.0, min(86400.0, self.zoom_s * factor))
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag_start_x = e.x()
            self._drag_start_s = self.cursor_s
            self._user_scrubbing = True                                             
            self._did_pan = False

    def mouseMoveEvent(self, e):
        self._hover_s = self._x_to_s(e.x())
        if self._drag_start_x is not None and (e.buttons() & Qt.LeftButton):
                                                                                
                                                                           
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

                                                                              
        target_s = self.cursor_s
        idx = self._find_rec_idx_at(target_s)
        if idx < 0:
                                                                                   
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

                                                                                 
class PlaybackTab(QWidget):
    def __init__(self, nvr: NVRClient, parent=None):
        super().__init__(parent)
        self.nvr        = nvr                                                     
        self._nvr_map   = {}                        
        self.cameras    = []
        self.recordings = []
        self.worker     = None
        self._paused    = False
        self._live_tab_ref = None                      
        self._rec_start_dt = None                                     
        self._rec_dur_s    = 0                            

        main = QHBoxLayout(self)
        main.setContentsMargins(12, 12, 12, 12)
        main.setSpacing(10)

                                                                
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

                                                               
        right = QVBoxLayout()
        right.setSpacing(8)

        self.video_label = QLabel('Select a recording to play')
        self.video_label.setAlignment(Qt.AlignCenter)
        self.video_label.setStyleSheet(
            f'background:{DARK["panel"]};border:1px solid {DARK["border"]};'
            f'border-radius:4px;color:{DARK["dim"]};font-size:14px;')
        self.video_label.setMinimumSize(640, 360)
                                                                                  
                                                                                     
        self.video_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.video_label.setScaledContents(False)
        right.addWidget(self.video_label, 1)

                        
        tl_label = QLabel('TIMELINE — click or scroll to navigate')
        tl_label.setStyleSheet(f'color:{DARK["dim"]};font-size:10px;letter-spacing:1px;')
        right.addWidget(tl_label)

        self.timeline = TimelineWidget()
        self.timeline.setFixedHeight(56)
        self.timeline.seek_requested.connect(self._on_timeline_click)
        self.timeline.seek_to_time.connect(self._on_timeline_seek)
        right.addWidget(self.timeline)

                               
        prog_row = QHBoxLayout()
        self.pos_label = QLabel('00:00')
        self.pos_label.setStyleSheet(f'color:{DARK["dim"]};font-family:monospace;font-size:11px;min-width:40px;')
        self.dur_label = QLabel('00:00')
        self.dur_label.setStyleSheet(f'color:{DARK["dim"]};font-family:monospace;font-size:11px;min-width:40px;')
        self.prog_slider = QSlider(Qt.Horizontal)
        self.prog_slider.setRange(0, 1000)
        self.prog_slider.setValue(0)
        self.prog_slider.setEnabled(False)
        prog_row.addWidget(self.pos_label)
        prog_row.addWidget(self.prog_slider)
        prog_row.addWidget(self.dur_label)
        right.addLayout(prog_row)

                                 
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
        self.speed_combo.setCurrentIndex(2)      
        self.speed_combo.setFixedWidth(80)
        self.speed_combo.currentIndexChanged.connect(self._on_speed_changed)
        self._speeds = [0.25, 0.5, 1.0, 2.0, 4.0, 8.0]

        pb_ctrl.addWidget(self.btn_play)
        pb_ctrl.addWidget(self.btn_pause)
        pb_ctrl.addWidget(self.btn_stop_pb)
        pb_ctrl.addStretch()
        pb_ctrl.addWidget(QLabel('Speed:'))
        pb_ctrl.addWidget(self.speed_combo)
        right.addLayout(pb_ctrl)

        main.addLayout(right, 3)

                                           
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
                                                                         
            real_cam_id = cam_id.split('_', 1)[-1] if '_' in cam_id else cam_id
            nvr = self._get_nvr_for_cam(cam_id)
            print(f'[Search] cam_id={cam_id!r} → real_channel={real_cam_id!r} NVR={nvr.host}')
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
            log('SEEK', f'ignored: idx={idx} out of range')
            return
        rec = self.recordings[idx]
        try:
            rec_start = datetime.strptime(rec['start'], '%Y-%m-%dT%H:%M:%SZ')
            rec_start_s = rec_start.hour*3600 + rec_start.minute*60 + rec_start.second
            offset = max(0.0, clicked_s - rec_start_s)
        except Exception as e:
            log('SEEK', f'time parse error: {e}')
            offset = 0.0

                                                                                
                                                                                   
                                                                               
                                                                            
                                           
        w = self.worker
        can_local = False
        if (idx == getattr(self, '_current_rec_idx', -1) and w
                and getattr(w, '_tmp_file', None)
                and os.path.exists(w._tmp_file)):
                                                                                
                                                                                  
                                                                             
            if getattr(w, '_dl_complete', False):
                can_local = True
            else:
                                                                                
                pct = getattr(w, '_dl_percent', 0)
                rec_dur = max(1.0, self._rec_dur_s)
                downloaded_s = rec_dur * (pct / 100.0)
                if offset < downloaded_s - 3:
                    can_local = True

        if can_local:
            log('SEEK', f'idx={idx} offset={offset:.0f}s → LOCAL seek (no re-download)')
            self._seek_offset_s = offset
            self._elapsed_s = offset
            self.rec_list.blockSignals(True)
            self.rec_list.setCurrentRow(idx)
            self.rec_list.blockSignals(False)
            file_base = getattr(w, '_file_base_offset_s', 0.0)
            local_off = max(0.0, offset - file_base)
            w.local_seek(local_off)
            return

        log('SEEK', f'idx={idx} clicked_s={clicked_s:.0f} rec_start_s={rec_start_s:.0f} '
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

                                                                               
        self.stop_playback()

        self.video_label.setText('Loading...')
        self._paused = False

        try:
            self._rec_start_dt = datetime.strptime(rec['start'], '%Y-%m-%dT%H:%M:%SZ')
            e_dt               = datetime.strptime(rec['end'],   '%Y-%m-%dT%H:%M:%SZ')
            self._rec_dur_s    = max(1, (e_dt - self._rec_start_dt).total_seconds())
        except:
            self._rec_start_dt = None
            self._rec_dur_s    = 0

                                                                  
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

                                                                            
        real_cam_id = cam_id.split('_', 1)[-1] if '_' in cam_id else cam_id
        nvr = self._get_nvr_for_cam(cam_id)
        spd = speed_override if speed_override is not None else\
              self._speeds[self.speed_combo.currentIndex()]

                                                                                     
        if _SDK and nvr.sdk_user_id >= 0:
            rec_end_dt = self._rec_start_dt + timedelta(seconds=self._rec_dur_s)
                                                                                  
                                                                                 
                                                                              
            print(f'[Playback SDK] NVR={nvr.host}  ch={real_cam_id}  '
                  f'{self._rec_start_dt:%H:%M:%S}→{rec_end_dt:%H:%M:%S}'
                  f'{"  (ffmpeg -ss +%ds)" % int(seek_offset_s) if seek_offset_s > 0 else ""}')
            self.worker = VideoWorker(
                'playback', '', speed=spd, decode_w=1280, decode_h=720,
                sdk_source={
                    'nvr': nvr, 'channel': real_cam_id, 'mode': 'playback',
                    'start_dt': self._rec_start_dt,                   
                    'end_dt':   rec_end_dt,
                    'seek_offset_s': seek_offset_s,                             
                    'wait_for': getattr(self, '_stopping', None),
                }
            )
            self.worker._file_base_offset_s = 0.0                                   
        else:
                                                                                 
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
        self.worker.frame_ready.connect(self._on_frame, Qt.QueuedConnection)
        self.worker.error.connect(self._on_pb_error)
        self.worker.position_ms.connect(self._on_position, Qt.QueuedConnection)
        self.worker.start()

                                                                         
                                                                           
                                                        
        if not (_SDK and nvr.sdk_user_id >= 0):
            self.timer.start(1000)
        self.btn_pause.setEnabled(True)
        self.btn_pause.setText('Pause')

    def _on_frame(self, _, qi):
        if self.worker:
            self.worker.notify_displayed()
                                                                           
                                                                             
                                                                                 
                                                                            
                                      
        sz = self.video_label.size()
        self._lbl_w = sz.width()
        self._lbl_h = sz.height()
        pm = QPixmap.fromImage(qi).scaled(
            self._lbl_w, self._lbl_h,
            Qt.KeepAspectRatio, Qt.FastTransformation)
        self.video_label.setPixmap(pm)

    def _on_pb_error(self, _, msg):
        self.timer.stop()
        self.btn_pause.setEnabled(False)
                                                                                 
                                                                         
        if msg == 'Playback finished':
            cur = self.rec_list.currentRow()
            nxt = cur + 1
            if 0 <= nxt < len(self.recordings):
                log('PLAYBACK', f'finished row {cur} → auto-playing next row {nxt}')
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
                                                                
            self.video_label.setText('Buffering…')
            self.timer.stop()
            return
                                                              
        if not self.timer.isActive() and not self._paused:
            self.timer.start(1000)
                                                                               
                                                                
        self._elapsed_s = ms / 1000.0
        self._update_progress_ui()

    def _tick_progress(self):
        if not self._paused:
            spd = self._speeds[self.speed_combo.currentIndex()]
            self._elapsed_s += spd
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

    def _on_speed_changed(self, idx):
                                                                                 
                                                                      
        target = self._speeds[idx]
        row = self.rec_list.currentRow()
        if row < 0 or not self.worker:
            return
                                                                             
        cur_offset = max(0.0, self._elapsed_s)
        log('SPEED', f'→ {target}× from offset {cur_offset:.0f}s')
        self._play_recording(row, seek_offset_s=cur_offset, speed_override=target)

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
        self.pos_label.setText('00:00')
        self.video_label.setText('Select a recording to play')
        self.video_label.setPixmap(QPixmap())

                                                                                
class DeviceDialog(QWidget):
    """Floating panel to add or edit an NVR device"""
    saved = pyqtSignal(object)                    

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

        self.btn_test = QPushButton('Test & Save')
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

        self.btn_test.setText('Testing...')
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
            self.btn_test.setText('Test & Save')
            self.btn_test.setEnabled(True)

        threading.Thread(target=_run, daemon=True).start()

                                                                                 
class MainWindow(QMainWindow):
    _cameras_ready = pyqtSignal(object, list)                                             

    def __init__(self):
        super().__init__()
        self._cameras_ready.connect(self._on_cameras_ready)
        self.setWindowTitle('Hikvision Monitor')
        self.setMinimumSize(1280, 720)
        self.resize(1500, 900)

                      
        self.devices = NVRClient.load_all()
        if not self.devices:
            self.devices = [NVRClient(name='NVR 1')]

        self._all_cameras = []                                         
        self._nvr_map     = {}                        

                                                                 
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0,0,0,0)
        root.setSpacing(0)

                 
        sidebar = QWidget()
        sidebar.setFixedWidth(260)
        sidebar.setStyleSheet(f'background:{DARK["panel"]};border-right:1px solid {DARK["border"]};')
        sb = QVBoxLayout(sidebar)
        sb.setContentsMargins(10,14,10,14)
        sb.setSpacing(6)

        logo = QLabel('📹  HIK MONITOR')
        logo.setStyleSheet(f'color:{DARK["accent"]};font-size:15px;font-weight:700;letter-spacing:1px;')
        sb.addWidget(logo)

                         
        dev_hdr = QHBoxLayout()
        dev_lbl = QLabel('DEVICES')
        dev_lbl.setStyleSheet(f'color:{DARK["dim"]};font-size:10px;letter-spacing:2px;')
        btn_add_dev = QPushButton('+')
        btn_add_dev.setFixedSize(22,22)
        btn_add_dev.setToolTip('Add NVR')
        btn_add_dev.clicked.connect(self._add_device)
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

                         
        cam_hdr = QLabel('CAMERAS')
        cam_hdr.setStyleSheet(f'color:{DARK["dim"]};font-size:10px;letter-spacing:2px;margin-top:8px;')
        sb.addWidget(cam_hdr)

        self.cam_list = CameraListWidget()
        self.cam_list.itemClicked.connect(self._cam_clicked)
        self.cam_list.itemDoubleClicked.connect(self._cam_dbl_clicked)
        sb.addWidget(self.cam_list)
        sb.setStretch(sb.count()-1, 1)

        root.addWidget(sidebar)

                   
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

                                                          
        ready = [d for d in self.devices if d.host]
        if ready:
            QTimer.singleShot(600, self._connect_all)

                                                                 
    def _refresh_device_list(self):
        self.dev_list.clear()
        for d in self.devices:
            status = '🟢' if any(c.get('_nvr') is d for c in self._all_cameras) else '⚪'
            item = QListWidgetItem(f'{status}  {d.name}  ({d.host or "not set"})')
            item.setData(Qt.UserRole, d.device_id)
            self.dev_list.addItem(item)

    def _add_device(self):
        import uuid
        nvr = NVRClient(str(uuid.uuid4()), f'NVR {len(self.devices)+1}')
        dlg = DeviceDialog(nvr, self)
        dlg.saved.connect(self._on_device_saved)
        dlg.show()

    def _on_device_saved(self, nvr):
        if nvr not in self.devices:
            self.devices.append(nvr)
        NVRClient.save_all(self.devices)
        self._refresh_device_list()
                                     
        threading.Thread(target=self._connect_device, args=(nvr,), daemon=True).start()

    def _device_context_menu(self, pos):
        from PyQt5.QtWidgets import QMenu
        item = self.dev_list.itemAt(pos)
        if not item:
            return
        dev_id = item.data(Qt.UserRole)
        nvr = next((d for d in self.devices if d.device_id == dev_id), None)
        if not nvr:
            return
        menu = QMenu(self)
        menu.setStyleSheet(f'background:{DARK["panel"]};color:{DARK["text"]};border:1px solid {DARK["border"]};')
        edit_action   = menu.addAction('Edit')
        remove_action = menu.addAction('Remove')
        action = menu.exec_(self.dev_list.mapToGlobal(pos))
        if action == edit_action:
            dlg = DeviceDialog(nvr, self)
            dlg.saved.connect(lambda n: (NVRClient.save_all(self.devices), self._refresh_device_list()))
            dlg.show()
        elif action == remove_action:
            self.devices.remove(nvr)
            NVRClient.save_all(self.devices)
            self._all_cameras = [c for c in self._all_cameras if c.get('_nvr') is not nvr]
            self._refresh_camera_list()
            self._refresh_device_list()

    def _connect_all(self):
        self.status.showMessage('Connecting...')
        for nvr in self.devices:
            if nvr.host:
                threading.Thread(target=self._connect_device, args=(nvr,), daemon=True).start()

    def _connect_device(self, nvr):
        ok, msg = nvr.test()
        if not ok:
                                                                      
            QTimer.singleShot(0, lambda: self.status.showMessage(f'[{nvr.name}] {msg}'))
            return
        cams = nvr.get_cameras()
                                                
        self._cameras_ready.emit(nvr, cams)

    def _on_cameras_ready(self, nvr, cams):
        """Called on main thread when a device finishes connecting."""
                                                                     
        for c in cams:
            orig_id     = c['id']
            c['uid']    = f'{nvr.device_id}_{orig_id}'
            c['nvr_name'] = nvr.name
            c['nvr_id'] = nvr.device_id
            c['_nvr']   = nvr
            self._nvr_map[c['uid']] = nvr

                                                          
        self._all_cameras = [c for c in self._all_cameras if c.get('nvr_id') != nvr.device_id]
        self._all_cameras.extend(cams)
        self._refresh_camera_list()
        self._refresh_device_list()
        total = len(self._all_cameras)
        self.status.showMessage(f'{len(self.devices)} device(s)  |  {total} camera(s)')

    def _refresh_camera_list(self):
        self.cam_list.clear()
        multi = len(self.devices) > 1
        for cam in self._all_cameras:
            uid    = cam.get('uid', cam['id'])
            prefix = f'[{cam["nvr_name"]}] ' if multi else ''
            item = QListWidgetItem(f'📷  {prefix}{cam["name"]}')
            item.setData(Qt.UserRole, uid)
            item.setForeground(QColor(DARK['green']))
            self.cam_list.addItem(item)

        if self._all_cameras:
            self.live_tab.nvr = self._all_cameras[0]['_nvr']
            self.pb_tab.nvr   = self._all_cameras[0]['_nvr']

                                                 
        live_cams = [{**c, 'id': c.get('uid', c['id'])} for c in self._all_cameras]
        self.live_tab.set_cameras(live_cams)
        self.pb_tab.set_cameras(live_cams, self._nvr_map)

    def _cam_clicked(self, item):
        uid = item.data(Qt.UserRole)
        if uid:
            nvr = self._nvr_map.get(uid)
            if nvr:
                self.live_tab.nvr = nvr
            self.live_tab.select_camera(uid)
            self.tabs.setCurrentIndex(0)

    def _cam_dbl_clicked(self, item):
        """Double-click: switch to live 1×1 and start that camera."""
        uid = item.data(Qt.UserRole)
        if not uid:
            return
        nvr = self._nvr_map.get(uid)
        if nvr:
            self.live_tab.nvr = nvr
        self.live_tab.select_camera(uid)
        self.live_tab.grid_combo.setCurrentIndex(0)        
        cell = self.live_tab.cells.get(uid)
        if cell and not cell._streaming:
            cell.start_stream(self.live_tab._should_use_sub())
        self.tabs.setCurrentIndex(0)

                                                                                 
class CameraListWidget(QListWidget):
    """Sidebar camera list that supports drag-to-cell."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setDragDropMode(QListWidget.DragOnly)
        self.setDefaultDropAction(Qt.CopyAction)

    def startDrag(self, supported_actions):
        item = self.currentItem()
        if not item:
            return
        uid = item.data(Qt.UserRole)
        if not uid:
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
