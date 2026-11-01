"""Hik-Connect cloud client: login, camera list, cloud snapshot and live view.

Lets Hik Monitor show cameras through a Hik-Connect account (the same cloud the
official mobile app uses), without a local IP, port forwarding or the native SDK.

  * REST part (login, device/channel list, snapshot, VTDU tokens) is our own,
    built on ``requests``.
  * The VTM relay transport (TCP framing, stream-info handshake, keepalive) is
    imported from ``pyezvizapi.stream`` — we do not reimplement it.

Portions of the REST flow, RTP depacketizer and SPS/PPS injector are adapted
from hikcloudstream 0.1.4 (https://pypi.org/project/hikcloudstream/),
Copyright its authors, licensed under the Apache License 2.0.

This is an unofficial API. Use it only with your own account and cameras.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Iterator, Optional
from urllib.parse import urlparse

import requests

DEFAULT_API_BASE = 'https://api.hik-connect.com'
DEFAULT_CLIENT_VERSION = '6.0.0.20250101'
CLIENT_TYPE = '55'

_DEVICE_FILTER = ('TIME_PLAN,CONNECTION,SWITCH,STATUS,STATUS_EXT,WIFI,'
                  'NODISTURB,P2P,KMS,HIDDNS')
_JPEG_MAGIC = b'\xff\xd8'
_HIK_ENCODED_PREFIX = b'hikencodepicture'
_START_CODE = b'\x00\x00\x00\x01'

# meta.code values that mean "session no longer valid → log in again"
_AUTH_EXPIRED_CODES = {401, 1003, 1004, 1005, 1006}

STREAM_MAIN = 1
STREAM_SUB = 2


# ── Errors ──────────────────────────────────────────────────────────────────
class HikCloudError(Exception):
    """Base error for the cloud client."""


class AuthError(HikCloudError):
    """Login failed or the session is not valid."""


class CaptchaRequired(AuthError):
    """Account needs CAPTCHA — log in once in the official app, then retry."""


class ApiError(HikCloudError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f'Hik-Connect API error {code}: {message}')


class StreamError(HikCloudError):
    """Cloud live stream could not be opened or delivered no usable video."""


# ── Models ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class CloudCamera:
    device_serial: str
    channel_no: int
    name: str
    device_name: str

    @property
    def key(self):
        return f'{self.device_serial}_{self.channel_no}'


def md5_password(password: str) -> str:
    """Hik-Connect never receives the plain password, only its MD5 — so this
    is all we ever need to store."""
    return hashlib.md5(password.encode('utf-8')).hexdigest()


def _check_meta(payload):
    meta = payload.get('meta') or {}
    code = meta.get('code', 0)
    if code == 200:
        return
    msg = str(meta.get('message') or meta.get('langMsg') or 'unknown error')
    raise ApiError(int(code or 0), msg)


# ── REST client ─────────────────────────────────────────────────────────────
class HikConnectCloud:
    """Thread-safe Hik-Connect REST client (one per account)."""

    def __init__(self, account, password_md5, *, api_base=None,
                 client_version=DEFAULT_CLIENT_VERSION, timeout=20.0):
        self.account = account
        self.password_md5 = password_md5
        self.base_url = (api_base or DEFAULT_API_BASE).rstrip('/')
        self.client_version = client_version or DEFAULT_CLIENT_VERSION
        self.timeout = timeout
        self.session_id = None
        self.refresh_session_id = None
        self._feature_code = secrets.token_hex(16)
        self._service_urls = None
        self._http = requests.Session()
        self._lock = threading.RLock()

    # ── plumbing ────────────────────────────────────────────────────────────
    @property
    def api_host(self):
        return urlparse(self.base_url).netloc

    def _headers(self, with_session=True):
        h = {
            'clientType': CLIENT_TYPE,
            'clientVersion': self.client_version,
            'featureCode': self._feature_code,
            'lang': 'en-US',
            'clientNo': 'hikmonitor',
            'clientName': 'hikmonitor',
            'osVersion': 'Linux',
            'netType': 'WIFI',
        }
        if with_session and self.session_id:
            h['sessionId'] = self.session_id
        return h

    def close(self):
        try:
            self._http.close()
        except Exception:
            pass

    # ── auth ────────────────────────────────────────────────────────────────
    def login(self, _depth=0):
        with self._lock:
            r = self._http.post(
                f'{self.base_url}/v3/users/login/v2',
                data={'account': self.account, 'password': self.password_md5,
                      'featureCode': self._feature_code},
                headers=self._headers(with_session=False), timeout=self.timeout)
            r.raise_for_status()
            payload = r.json()
            code = (payload.get('meta') or {}).get('code')
            if code == 1100:   # account lives on a regional API host
                domain = (payload.get('loginArea') or {}).get('apiDomain')
                if not domain or _depth > 2:
                    raise ApiError(1100, 'login redirect without apiDomain')
                self.base_url = f'https://{domain}'
                return self.login(_depth + 1)
            if code in (1013, 1014):
                raise AuthError('Wrong Hik-Connect account or password')
            if code == 1015:
                raise CaptchaRequired(
                    'Hik-Connect wants a CAPTCHA — log in once in the official '
                    'app, then try again')
            _check_meta(payload)
            sess = payload.get('loginSession') or {}
            self.session_id = sess.get('sessionId')
            self.refresh_session_id = sess.get('rfSessionId')
            if not self.session_id:
                raise AuthError('Login succeeded but no sessionId was returned')
            self._service_urls = None

    def ensure_login(self):
        if not self.session_id:
            self.login()

    def _request(self, method, path, *, params=None, data=None, retry=True):
        self.ensure_login()
        url = path if path.startswith('http') else f'{self.base_url}{path}'
        r = self._http.request(method, url, params=params, data=data,
                               headers=self._headers(), timeout=self.timeout)
        if r.status_code == 401 and retry:
            self.login()
            return self._request(method, path, params=params, data=data, retry=False)
        r.raise_for_status()
        payload = r.json()
        code = (payload.get('meta') or {}).get('code')
        if retry and code in _AUTH_EXPIRED_CODES:
            self.login()
            return self._request(method, path, params=params, data=data, retry=False)
        return payload

    def request_json(self, method, path, **kw):
        payload = self._request(method, path, **kw)
        _check_meta(payload)
        return payload

    # ── devices ─────────────────────────────────────────────────────────────
    def list_cameras(self):
        cams = []
        offset, limit = 0, 50
        while True:
            payload = self.request_json(
                'GET', '/v3/userdevices/v1/devices/pagelist',
                params={'groupId': -1, 'limit': limit, 'offset': offset,
                        'filter': _DEVICE_FILTER})
            for dev in payload.get('deviceInfos') or []:
                serial = dev.get('deviceSerial')
                if not serial:
                    continue
                cams.extend(self._device_cameras(str(serial),
                                                 str(dev.get('name') or serial)))
            if not (payload.get('page') or {}).get('hasNext'):
                break
            offset += limit
        return cams

    def _device_cameras(self, serial, device_name):
        payload = self.request_json('GET', '/v3/userdevices/v1/cameras/info',
                                    params={'deviceSerial': serial})
        out = []
        for item in payload.get('cameraInfos') or []:
            ch = item.get('channelNo')
            if ch is None:
                continue
            out.append(CloudCamera(serial, int(ch),
                                   item.get('cameraName') or f'Channel {ch}',
                                   device_name))
        out.sort(key=lambda c: c.channel_no)
        return out

    # ── snapshot (cloud thumbnail, max ~352×288) ────────────────────────────
    def snapshot(self, cam: CloudCamera, validate_code: Optional[str] = None) -> bytes:
        """Return JPEG bytes. ``validate_code`` (sticker verify code) is only
        needed for devices with image encryption turned on."""
        encrypted = None
        last_err = None
        for is_enc in (0, 1):
            try:
                payload = self.request_json(
                    'PUT', '/v3/devices/security/capture',
                    params={'deviceSerial': cam.device_serial,
                            'channelNo': cam.channel_no, 'isEncrypted': is_enc})
                pic = (payload.get('captureInfo') or {}).get('picUrl')
                if not pic:
                    raise HikCloudError('capture response has no picUrl')
                img = self._http.get(pic, timeout=self.timeout)
                img.raise_for_status()
                raw = img.content
            except (HikCloudError, requests.RequestException) as e:
                last_err = e
                continue
            if raw.startswith(_JPEG_MAGIC):
                return raw
            if raw.startswith(_HIK_ENCODED_PREFIX):
                encrypted = raw
        if encrypted is not None:
            if not validate_code:
                raise HikCloudError('Snapshot is encrypted — device verify code needed')
            return _decrypt_capture(encrypted, validate_code)
        raise HikCloudError(f'Snapshot failed: {last_err or "no image"}')

    # ── live stream bootstrap ───────────────────────────────────────────────
    def _service_url_map(self):
        if self._service_urls is None:
            payload = self.request_json('GET', '/v3/configurations/system/info')
            self._service_urls = dict(payload.get('systemConfigInfo') or {})
        return self._service_urls

    def _session_sign(self):
        parts = (self.session_id or '').split('.')
        if len(parts) < 2:
            raise AuthError('sessionId is not a JWT')
        body = parts[1] + '=' * (-len(parts[1]) % 4)
        sign = json.loads(base64.urlsafe_b64decode(body.encode())).get('s')
        if not isinstance(sign, str) or not sign:
            raise AuthError('sessionId has no VTDU sign claim')
        return sign

    def _vtdu_tokens(self, count=5):
        self.ensure_login()
        last = None
        try:
            r = self._http.post(f'{self.base_url}/api/user/token/get',
                                data={'sessionId': self.session_id, 'count': count},
                                headers=self._headers(), timeout=self.timeout)
            r.raise_for_status()
            p = r.json()
            toks = p.get('tokenArray') or p.get('tokens')
            if isinstance(toks, list) and toks:
                return [str(t) for t in toks]
            last = HikCloudError(f'token API returned no tokens: {p}')
        except Exception as e:
            last = e
        sign = self._session_sign()
        for base in self._auth_bases():
            try:
                r = self._http.get(f'{base}/vtdutoken2',
                                   params={'ssid': self.session_id, 'sign': sign},
                                   headers=self._headers(), timeout=self.timeout)
                r.raise_for_status()
                p = r.json()
                toks = p.get('tokens')
                if p.get('retcode') in (0, '0') and isinstance(toks, list) and toks:
                    return [str(t) for t in toks]
                last = HikCloudError(f'VTDU token error: {p}')
            except Exception as e:
                last = e
        raise StreamError(f'Could not obtain a VTDU stream token ({last})')

    def _auth_bases(self):
        out = []
        auth = str(self._service_url_map().get('authAddr') or '').strip()
        if auth and auth.lower() not in ('https://null', 'null', 'none'):
            if not auth.startswith(('http://', 'https://')):
                auth = f'https://{auth}'
            out.append(auth.rstrip('/'))
        host = self.api_host
        if host.startswith('api') and host.endswith('.hik-connect.com'):
            region = host[3:-len('.hik-connect.com')]
            out += [f'https://{region}auth.hik-connect.com',
                    f'https://auth{region}.hik-connect.com']
        seen, uniq = set(), []
        for u in out:
            if u not in seen:
                seen.add(u); uniq.append(u)
        return uniq

    def live_url(self, cam: CloudCamera, stream_type=STREAM_SUB):
        """Build the ysproto:// VTM URL for one camera channel."""
        from pyezvizapi.stream import build_vtm_url

        pl = self.request_json('GET', '/v3/userdevices/v1/resources/pagelist',
                               params={'groupId': -1, 'limit': 50, 'offset': 0,
                                       'filter': 'VTM'})
        resources = pl.get('resourceInfos') or []
        vtms = pl.get('VTM') or {}
        res = next((r for r in resources if isinstance(r, dict)
                    and r.get('deviceSerial') == cam.device_serial
                    and str(r.get('localIndex')) == str(cam.channel_no)), None)
        if res is None:
            raise StreamError(f'No cloud stream resource for {cam.device_serial} '
                              f'ch{cam.channel_no} (offline or not shared?)')
        vtm = dict(vtms.get(res.get('resourceId')) or {})
        try:
            fresh = self.request_json(
                'GET', f'/v3/streaming/vtm/{cam.device_serial}/{max(1, cam.channel_no)}')
            vtm.update(fresh.get('streamServerConfig') or {})
        except HikCloudError:
            pass   # fall back to the pagelist VTM entry
        host = vtm.get('externalIp') or vtm.get('domain') or vtm.get('internalIp')
        port = vtm.get('port')
        if not host or not str(port or '').isdigit():
            raise StreamError('No VTM relay endpoint for this camera')
        token = self._vtdu_tokens()[0]
        url = build_vtm_url(str(host).strip(), int(port), cam.device_serial,
                            str(res.get('streamBizUrl') or ''), token,
                            channel=cam.channel_no, client_type=int(CLIENT_TYPE))
        return url.replace('stream=1', f'stream={int(stream_type)}', 1)

    # ── recordings on the device (NVR disk / SD card), via the cloud ────────
    def search_records(self, cam: CloudCamera, start, end):
        """Recordings between two datetimes (device local time). Returns a list
        of dicts {'begin': 'YYYY-MM-DDTHH:MM:SS', 'end': ..., 'type': 'ALARM'|...}."""
        out, seen = [], set()
        cursor = start.strftime('%Y-%m-%dT%H:%M:%S')
        stop = end.strftime('%Y-%m-%dT%H:%M:%S')
        for _ in range(50):                     # hard cap on pages
            payload = self.request_json(
                'GET', '/v3/streaming/records',
                params={'deviceSerial': cam.device_serial,
                        'channelNo': cam.channel_no,
                        'startTime': cursor, 'stopTime': stop, 'size': 100})
            recs = payload.get('records') or []
            for r in recs:
                k = (r.get('begin'), r.get('end'))
                if r.get('begin') and r.get('end') and k not in seen:
                    seen.add(k)
                    out.append(r)
            if str(payload.get('isFinished', 'TRUE')).upper() != 'FALSE' or not recs:
                break
            nxt = recs[-1].get('end')
            if not nxt or nxt <= cursor:
                break
            cursor = nxt
        out.sort(key=lambda r: r['begin'])
        return out

    def playback_url(self, cam: CloudCamera, start, end, stream_type=STREAM_MAIN):
        """ysproto:// URL that replays the device's own recording. Found in
        libezstreamclient.so: same VTM handshake as live, path /playback and
        begin/end/serial params — `serial` is mandatory (else VTM code 6103)."""
        url = self.live_url(cam, stream_type).replace('/live?', '/playback?', 1)
        return (f'{url}&begin={_cloud_time(start)}&end={_cloud_time(end)}'
                f'&serial={cam.device_serial}')


def _cloud_time(t):
    """datetime → VTM playback time. Like ISAPI, the 'Z' is a lie: it is the
    device's LOCAL wall-clock time (verified: real UTC returns 5402/no data)."""
    return t.strftime('%Y%m%dT%H%M%SZ')


def _decrypt_capture(raw, validate_code):
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad

    def key16(v):
        b = (v or '').encode()
        return b[:16] if len(b) >= 16 else b + b'\0' * (16 - len(b))

    if len(raw) <= 48:
        raise HikCloudError('Encrypted snapshot too small')
    dec = AES.new(key16(validate_code), AES.MODE_CBC, key16('01234567')).decrypt(raw[48:])
    try:
        dec = unpad(dec, AES.block_size)
    except ValueError:
        pass
    if not dec.startswith(_JPEG_MAGIC):
        raise HikCloudError('Snapshot decrypt failed — wrong verify code?')
    return dec


# ── RTP video depacketizer (H.264 + H.265) ─────────────────────────────────
# Idea and parameter-set handling adapted from hikcloudstream (Apache-2.0).
# The VTM stream mixes several RTP payload types (video, G.711 audio, a Hik
# private PT 112). We lock onto the PT that carries video parameter sets and
# ignore the rest. Sub streams are usually H.264, main streams H.265.
class _RtpVideoDepacketizer:
    _PS = {'h264': (7, 8), 'hevc': (32, 33, 34)}

    def __init__(self):
        self.codec = None        # 'h264' | 'hevc' once detected
        self.pt = None
        self.saw_slice = False
        self._frag = None
        self._seq = None
        self._ps = {}
        self._just_sent_ps = False

    # NAL type of an Annex-B unit (start code included)
    def _type(self, nal):
        h = nal[4]
        return (h >> 1) & 0x3F if self.codec == 'hevc' else h & 0x1F

    def _detect(self, p):
        if len(p) > 3 and p[1] == 0x01 and ((p[0] >> 1) & 0x3F) in (32, 33, 34):
            return 'hevc'
        if len(p) > 3 and not (p[0] & 0x80) and (p[0] & 0x1F) in (7, 8):
            return 'h264'
        return None

    def feed(self, pkt):
        from pyezvizapi.stream import rtp_payload
        if len(pkt) < 12:
            return []
        pt = pkt[1] & 0x7F
        seq = int.from_bytes(pkt[2:4], 'big')
        try:
            p = rtp_payload(pkt)
        except Exception:
            return []
        if not p:
            return []
        if self.pt is None:
            codec = self._detect(p)
            if codec is None:
                return []
            self.codec, self.pt = codec, pt
        elif pt != self.pt:
            return []
        if self._seq is not None and seq != (self._seq + 1) & 0xFFFF:
            self._frag = None            # lost a packet → drop partial NAL
        self._seq = seq
        nals = self._depay_hevc(p) if self.codec == 'hevc' else self._depay_h264(p)
        return [o for o in map(self._inject, nals) if o]

    def _depay_h264(self, p):
        t = p[0] & 0x1F
        if 1 <= t <= 23:
            self._frag = None
            return [_START_CODE + p]
        if t == 24:                                   # STAP-A
            return self._split_aggregate(p, 1)
        if t != 28 or len(p) < 2:
            return []
        fu = p[1]
        return self._fu(fu & 0x80, fu & 0x40,
                        bytes([(p[0] & 0xE0) | (fu & 0x1F)]), p[2:])

    def _depay_hevc(self, p):
        if len(p) < 3:
            return []
        t = (p[0] >> 1) & 0x3F
        if t < 48:
            self._frag = None
            return [_START_CODE + p]
        if t == 48:                                   # AP (no DONL)
            return self._split_aggregate(p, 2)
        if t != 49:
            return []
        fu = p[2]
        hdr = bytes([(p[0] & 0x81) | ((fu & 0x3F) << 1), p[1]])
        return self._fu(fu & 0x80, fu & 0x40, hdr, p[3:])

    def _fu(self, start, end, hdr, data):
        if start:
            self._frag = bytearray(_START_CODE + hdr)
            self._frag += data
        elif self._frag is not None:
            self._frag += data
        else:
            return []
        if end:
            done, self._frag = bytes(self._frag), None
            return [done]
        return []

    def _split_aggregate(self, p, off):
        self._frag = None
        out = []
        while off + 2 <= len(p):
            n = int.from_bytes(p[off:off + 2], 'big'); off += 2
            if n == 0 or off + n > len(p):
                break
            out.append(_START_CODE + p[off:off + n]); off += n
        return out

    def _inject(self, nal):
        """Hold back slices until all parameter sets are known and re-send
        them before every keyframe (Hik main streams sometimes start with an
        IDR before PPS, which ffmpeg cannot begin decoding from)."""
        if len(nal) <= 5:
            return b''
        t = self._type(nal)
        ps_types = self._PS[self.codec]
        if t in ps_types:
            self._ps[t] = nal
            self._just_sent_ps = True
            return nal
        if self.codec == 'hevc':
            is_slice, is_key = t <= 31, 16 <= t <= 23
        else:
            is_slice, is_key = 1 <= t <= 5, t == 5
        if not is_slice:
            return nal
        if len(self._ps) < len(ps_types):
            return b''
        if not self.saw_slice and not is_key:
            return b''       # joined mid-GOP: wait for the first keyframe
        self.saw_slice = True
        prefix = b''
        if is_key and not self._just_sent_ps:
            prefix = b''.join(self._ps[k] for k in ps_types)
        self._just_sent_ps = False
        return prefix + nal


# ── Live session ────────────────────────────────────────────────────────────
class CloudLiveStream:
    """One VTM live session. Usage::

        s = CloudLiveStream(cloud, cam, STREAM_SUB)
        s.start()                 # blocks until handshake + first video
        fmt = s.input_format      # 'h264' | 'hevc' | 'mpeg' | 'mpegts' (ffmpeg -f)
        for chunk in s.iter_chunks(): ...
        s.close()
    """

    def __init__(self, cloud: HikConnectCloud, cam: CloudCamera,
                 stream_type=STREAM_SUB, timeout=15.0, playback=None):
        self.cloud = cloud
        self.cam = cam
        self.stream_type = stream_type
        self.playback = playback      # None = live, else (start_dt, end_dt)
        self.timeout = timeout
        self.input_format = None
        self._vtm = None
        self._packets = None
        self._pending = []
        self._closed = False
        self._depay = None

    def start(self, probe_seconds=6.0):
        from pyezvizapi.stream import VtmStreamClient
        if self.playback:
            url = self.cloud.playback_url(self.cam, *self.playback,
                                          stream_type=self.stream_type)
        else:
            url = self.cloud.live_url(self.cam, self.stream_type)
        self._vtm = VtmStreamClient(url, timeout=self.timeout)
        try:
            info = self._vtm.start()
            if info.result not in (0, None):
                if self.playback and info.result == 5402:
                    raise StreamError('No recording at that time')
                raise StreamError(f'VTM refused the stream (code {info.result})')
            self._packets = self._media_chunks()
            # Prime: wait until we actually have decodable video, so callers
            # can pick the right ffmpeg demuxer and fall back if needed.
            deadline = time.monotonic() + probe_seconds
            for chunk in self._packets:
                if chunk:
                    self._pending.append(chunk)
                    if self.input_format is not None and self._has_video:
                        return self
                if time.monotonic() > deadline:
                    break
            raise StreamError('Cloud stream sent no decodable video')
        except Exception:
            self.close()
            raise

    @property
    def _has_video(self):
        d = self._depay
        return d is None or d.saw_slice

    def _media_chunks(self):
        from pyezvizapi.stream import StreamTransport, VtmChannel, detect_transport
        transport = StreamTransport.UNKNOWN
        for pkt in self._vtm.iter_packets():
            if self._closed:
                return
            if pkt.channel not in (VtmChannel.STREAM, VtmChannel.ENCRYPTED_STREAM) \
                    or not pkt.body:
                continue
            if pkt.encrypted:
                raise StreamError('Stream is encrypted (video encryption on the '
                                  'device) — not supported yet')
            t = detect_transport(pkt.body)
            if transport == StreamTransport.UNKNOWN and t != StreamTransport.UNKNOWN:
                transport = t
                if t == StreamTransport.RTP:
                    self._depay = _RtpVideoDepacketizer()
                else:
                    self.input_format = {StreamTransport.MPEG_PS: 'mpeg',
                                         StreamTransport.MPEG_TS: 'mpegts'}.get(t)
            # Lock onto the first detected transport: MPEG-PS continuation
            # packets can start with a byte that merely looks like RTP v2.
            active = transport
            if active == StreamTransport.RTP:
                out = b''.join(self._depay.feed(pkt.body))
                if self.input_format is None and self._depay.codec:
                    self.input_format = self._depay.codec
                yield out              # b'' lets start() check its deadline
            elif active in (StreamTransport.MPEG_PS, StreamTransport.MPEG_TS):
                yield pkt.body
            else:
                yield b''

    def iter_chunks(self) -> Iterator[bytes]:
        while self._pending:
            yield self._pending.pop(0)
        if self._packets is not None:
            for chunk in self._packets:
                if chunk:
                    yield chunk

    def close(self):
        self._closed = True
        vtm, self._vtm = self._vtm, None
        if vtm is not None:
            try:
                vtm.close()
            except Exception:
                pass


def open_live(cloud: HikConnectCloud, cam: CloudCamera, sub=True,
              timeout=15.0) -> CloudLiveStream:
    """Open a live stream, preferring the requested profile and falling back
    to the other one (some NVR channels send undecodable data on main, some
    cameras have no substream)."""
    order = [STREAM_SUB, STREAM_MAIN] if sub else [STREAM_MAIN, STREAM_SUB]
    last = None
    for st in order:
        s = CloudLiveStream(cloud, cam, st, timeout=timeout)
        try:
            return s.start()
        except AuthError:
            raise
        except Exception as e:
            last = e
    raise StreamError(str(last) if last else 'Cloud stream failed')


def open_playback(cloud: HikConnectCloud, cam: CloudCamera, start, end,
                  timeout=15.0) -> CloudLiveStream:
    """Replay a device recording (start/end = device local datetimes).
    Main profile first (that is what NVRs record), sub as fallback."""
    last = None
    for st in (STREAM_MAIN, STREAM_SUB):
        s = CloudLiveStream(cloud, cam, st, timeout=timeout, playback=(start, end))
        try:
            return s.start()
        except AuthError:
            raise
        except Exception as e:
            last = e
            if 'No recording' in str(e):
                break
    raise StreamError(str(last) if last else 'Cloud playback failed')
