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
import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Iterator, Optional
from urllib.parse import urlparse

import logging
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
FPS_WAIT_SECONDS = 1.0     # longest a live start waits to learn the camera's frame rate


# ── Errors ──────────────────────────────────────────────────────────────────
log = logging.getLogger('hik_cloud')


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


class EncryptedStreamError(StreamError):
    """The device encrypts its video and we have no verification code, or the
    one we have is wrong (``wrong``). ``serial`` / ``device_name`` identify the
    device so the UI can ask for its code."""
    needs_verify_code = True

    def __init__(self, message, wrong=False):
        super().__init__(message)
        self.wrong = wrong
        self.serial = ''
        self.device_name = ''


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
        self._resources = None
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
            self._resources = None

    def ensure_login(self):
        if not self.session_id:
            self.login()

    def _request(self, method, path, *, params=None, data=None, retry=True):
        self.ensure_login()
        url = path if path.startswith('http') else f'{self.base_url}{path}'
        t0 = time.monotonic()
        r = self._http.request(method, url, params=params, data=data,
                               headers=self._headers(), timeout=self.timeout)
        if time.monotonic() - t0 > 2.0:
            log.info('slow Hik-Connect call %s: %.1f s', path.split('?')[0], time.monotonic() - t0)
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

    def _resource_list(self, max_age=120.0):
        """The account's stream resources. One answer covers every camera, so
        opening several at once asks the cloud once instead of once each."""
        with self._lock:
            hit = self._resources
            if hit and time.monotonic() - hit[0] < max_age:
                return hit[1]
        pl = self.request_json('GET', '/v3/userdevices/v1/resources/pagelist',
                               params={'groupId': -1, 'limit': 50, 'offset': 0,
                                       'filter': 'VTM'})
        with self._lock:
            self._resources = (time.monotonic(), pl)
        return pl

    def live_url(self, cam: CloudCamera, stream_type=STREAM_SUB):
        """Build the ysproto:// VTM URL for one camera channel."""
        from pyezvizapi.stream import build_vtm_url

        pl = self._resource_list()
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


def _key16(code):
    """Verification code → 16-byte AES key (zero-padded / truncated)."""
    b = (code or '').encode()
    return b[:16] if len(b) >= 16 else b + b'\0' * (16 - len(b))


def _decrypt_capture(raw, validate_code):
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad

    if len(raw) <= 48:
        raise HikCloudError('Encrypted snapshot too small')
    dec = AES.new(_key16(validate_code), AES.MODE_CBC, _key16('01234567')).decrypt(raw[48:])
    try:
        dec = unpad(dec, AES.block_size)
    except ValueError:
        pass
    if not dec.startswith(_JPEG_MAGIC):
        raise HikCloudError('Snapshot decrypt failed — wrong verify code?')
    return dec


# ── Video encryption (device "stream encryption" / verification code) ───────
# A device with video encryption on still sends normal RTP / MPEG-PS framing,
# but for the *flagged* frames the first 4096 bytes of every video NAL (after
# the codec's NAL header; parameter sets included) are AES-128-ECB encrypted,
# key = verification code zero-padded to 16 bytes. Which frames are flagged
# (an NVR we tested: every keyframe and ~1 in 4 of the rest):
#   RTP      the frame's packets carry an RTP header extension
#   MPEG-PS  PES_scrambling_control != 0 on the frame's video PES packets
# The cipher layout matches pyezvizapi.stream.decrypt_hikvision_ps_video.
_ENC_PREFIX = 4096
_H264_PROFILES = {44, 66, 77, 83, 86, 88, 100, 110, 118, 122, 128, 134, 135,
                  138, 139, 244}


def _decrypt_nal_bytes(nal, key, hdr):
    """Decrypt the encrypted prefix of one NAL (no start code); the first
    ``hdr`` bytes (the NAL header) are not encrypted."""
    from Crypto.Cipher import AES
    n = min(len(nal) - hdr, _ENC_PREFIX)
    n -= n % 16
    if n <= 0:
        return bytes(nal)
    body = AES.new(key, AES.MODE_ECB).decrypt(bytes(nal[hdr:hdr + n]))
    return bytes(nal[:hdr]) + body + bytes(nal[hdr + n:])


def _judge_param_set(nal, codec=None):
    """For a HEVC VPS / H.264 SPS (no start code) return ``(codec, looks_clear)``
    — ``looks_clear`` is False for scrambled bytes, which is how we tell an
    encrypted stream (and a wrong code) from a clear one. None for any other
    NAL type, which cannot be judged."""
    if codec in (None, 'hevc') and len(nal) >= 6 and nal[0] & 0x81 == 0 \
            and (nal[0] >> 1) & 0x3F == 32 and nal[1] & 7:
        return 'hevc', nal[4:6] == b'\xff\xff'        # vps_reserved_0xffff_16bits
    if codec in (None, 'h264') and len(nal) >= 4 and nal[0] & 0x80 == 0 \
            and nal[0] & 0x1F == 7:
        return 'h264', nal[1] in _H264_PROFILES and nal[2] & 0x03 == 0
    return None


_G711_PTS = (0, 8)          # RTP payload types: PCMU (µ-law), PCMA (A-law)


# ── RTP video depacketizer (H.264 + H.265) ─────────────────────────────────
# Idea and parameter-set handling adapted from hikcloudstream (Apache-2.0).
# The VTM stream mixes several RTP payload types (video, G.711 audio, a Hik
# private PT 112). We lock onto the PT that carries video parameter sets and
# ignore the rest. Sub streams are usually H.264, main streams H.265.
class _RtpVideoDepacketizer:
    _PS = {'h264': (7, 8), 'hevc': (32, 33, 34)}

    def __init__(self, key=None):
        self.codec = None        # 'h264' | 'hevc' once detected
        self.pt = None
        self.saw_slice = False
        self.key = key           # 16-byte AES key (device verification code) or None
        self.encrypted = None    # None = not judged yet, else is this stream scrambled?
        self._frag = None
        self._seq = None
        self._ps = {}
        self._just_sent_ps = False
        self._ts = None          # RTP timestamp of the current frame
        self._frame_ts = []      # the first frames' RTP timestamps, for fps
        self._frame_flagged = False
        self.on_audio = None     # callable(payload type, G.711 bytes), set once streaming
        self._audio_aes = None

    @property
    def fps(self):
        """The camera's frame rate, from the RTP timestamps (a 90 kHz clock), or
        None until a few frames have been seen. Raw H.264/H.265 carries no
        timing, and ffmpeg assumes 25 fps: a camera that sends 12.5 would play
        at double speed."""
        t = self._frame_ts
        if len(t) < 5:
            return None
        steps = sorted(d for d in ((b - a) & 0xFFFFFFFF for a, b in zip(t, t[1:]))
                       if 0 < d < 90000)       # no repeats, wraps or pauses
        if len(steps) < 3:
            return None
        fps = 90000 / steps[len(steps) // 2]
        return round(fps, 2) if 1 <= fps <= 60 else None

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
        if pt in _G711_PTS:
            if self.on_audio is not None:
                self._audio(pt, pkt[0] & 0x10, p)
            return []
        if self.pt is None:
            # Never detect video on static (G.711 audio…) or Hik-private (112)
            # payload types: their first byte can look like an SPS by chance.
            if pt < 96 or pt == 112:
                return []
            codec = self._detect(p)
            if codec is None:
                return []
            self.codec, self.pt = codec, pt
        elif pt != self.pt:
            return []
        if self._seq is not None and seq != (self._seq + 1) & 0xFFFF:
            self._frag = None            # lost a packet → drop partial NAL
        self._seq = seq
        ts = bytes(pkt[4:8])
        if ts != self._ts:                       # new frame
            self._ts, self._frame_flagged = ts, False
            if len(self._frame_ts) < 12:
                self._frame_ts.append(int.from_bytes(ts, 'big'))
        if pkt[0] & 0x10:                        # RTP header extension → flagged frame
            self._frame_flagged = True
        nals = self._depay_hevc(p) if self.codec == 'hevc' else self._depay_h264(p)
        if self._frame_flagged and self.encrypted is not False:
            nals = list(map(self._decrypt, nals))
        return [o for o in map(self._inject, nals) if o]

    def _audio(self, pt, flagged, p):
        """Pass on one G.711 packet (8 kHz, 8-bit samples). With video encryption
        on, the camera also scrambles its sound: every whole 16-byte block of the
        packet is AES-ECB encrypted with the same key (measured: the decrypted
        bytes are smooth audio, the raw ones are noise). Follow the video's
        verdict: until the video has proved the stream is scrambled and the code
        is right, hold the sound back rather than play noise."""
        if flagged and self.key:
            if self.encrypted is None:
                return
            if self.encrypted:
                if self._audio_aes is None:
                    from Crypto.Cipher import AES
                    self._audio_aes = AES.new(self.key, AES.MODE_ECB)
                n = len(p) // 16 * 16
                p = self._audio_aes.decrypt(bytes(p[:n])) + bytes(p[n:])
        elif flagged and self.encrypted is not False:
            return                                   # scrambled, and no code to unlock it
        try:
            self.on_audio(pt, bytes(p))
        except Exception:
            pass

    def _decrypt(self, nal):
        """Decrypt one NAL (start code included) of a flagged frame. The first
        parameter set decides whether the stream really is scrambled and
        whether our code is right; NALs that cannot be judged pass through."""
        hdr = 2 if self.codec == 'hevc' else 1
        raw = nal[4:]
        if self.encrypted is None:
            j = _judge_param_set(raw, self.codec)
            if j is None:
                return nal
            if j[1]:                             # clear → the flag meant something else
                self.encrypted = False
                return nal
            if not self.key:
                raise EncryptedStreamError(
                    'Video encryption is on — verification code needed')
            j = _judge_param_set(_decrypt_nal_bytes(raw, self.key, hdr), self.codec)
            if not (j and j[1]):
                raise EncryptedStreamError('Wrong verification code', wrong=True)
            self.encrypted = True
        return _START_CODE + _decrypt_nal_bytes(raw, self.key, hdr)

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


# ── MPEG-PS decryptor (recordings replayed from the NVR) ─────────────────────
_PS_BOUNDARY = re.compile(b'\x00\x00\x01[\xb9-\xff]')   # a PS start code, never a NAL


class _PsDecryptor:
    """Streaming decryptor for the MPEG-PS the NVR replays recordings as. Video
    PES packets whose PES_scrambling_control is set hold encrypted NALs; those
    are collected into runs (a NAL may span adjacent PES packets) and decrypted
    with pyezvizapi's routine. Everything else passes through untouched."""

    def __init__(self, key=None):
        self.key = key
        self.encrypted = None            # None = not judged yet
        self.codec = None
        self._buf = bytearray()
        self._run = bytearray()          # trailing flagged video PES run
        self._saw_clear = False          # a video PES without the scrambling flag went by

    @property
    def ready(self):
        """True once we know whether this stream is scrambled (so a missing or
        wrong code has already been reported)."""
        return self.encrypted is not None or self._saw_clear

    @staticmethod
    def _packet_end(buf, pos):
        """End offset of the PS packet at ``pos``; None = incomplete."""
        if len(buf) - pos < 4:
            return None
        if buf[pos:pos + 3] != b'\x00\x00\x01':      # resync to the next start code
            nxt = buf.find(b'\x00\x00\x01', pos + 1)
            if nxt >= 0:
                return nxt
            return len(buf) - 2 if len(buf) - pos > 2 else None
        sid = buf[pos + 3]
        if sid == 0xBA:                                 # pack header
            if len(buf) - pos < 14:
                return None
            end = pos + 14 + (buf[pos + 13] & 7) if buf[pos + 4] & 0xC0 == 0x40 \
                else pos + 12                           # MPEG-2 : MPEG-1
            return end if end <= len(buf) else None
        if sid == 0xB9:
            return pos + 4
        if len(buf) - pos < 6:
            return None
        ln = int.from_bytes(buf[pos + 4:pos + 6], 'big')
        if ln:
            end = pos + 6 + ln
            return end if end <= len(buf) else None
        m = _PS_BOUNDARY.search(buf, pos + 6)           # unbounded video PES
        return m.start() if m else None

    def feed(self, data):
        buf = self._buf
        buf += data
        out = bytearray()
        pos = 0
        while True:
            end = self._packet_end(buf, pos)
            if end is None:
                break
            pkt = bytes(buf[pos:end])
            pos = end
            sid = pkt[3] if len(pkt) > 3 else 0
            video = 0xE0 <= sid <= 0xEF and len(pkt) >= 9
            if video and (pkt[6] >> 4) & 3 and self.encrypted is not False:
                self._run += pkt                        # wait for the end of the run
                continue
            if video and not (pkt[6] >> 4) & 3:
                self._saw_clear = True
            if self._run:
                out += self._flush_run()
            out += pkt
        del buf[:pos]
        return bytes(out)

    @staticmethod
    def _first_nal(run):
        """First NAL (after its start code) of the run's first PES payload."""
        start = 9 + run[8]
        i = run.find(b'\x00\x00\x01', start, start + 8)
        return bytes(run[i + 3:i + 3 + 64]) if i >= 0 else b''

    def _decrypt_run(self, run):
        from pyezvizapi.stream import decrypt_hikvision_ps_video
        return decrypt_hikvision_ps_video(
            run, self.key, nalu_header_size=2 if self.codec == 'hevc' else 1)

    def _flush_run(self):
        run = bytes(self._run)
        self._run.clear()
        if self.encrypted is None:
            j = _judge_param_set(self._first_nal(run))
            if j is None:
                return run                              # cannot judge → pass through
            self.codec = j[0]
            if j[1]:
                self.encrypted = False
                return run
            if not self.key:
                raise EncryptedStreamError(
                    'Video encryption is on — verification code needed')
            dec = self._decrypt_run(run)
            j = _judge_param_set(self._first_nal(dec), self.codec)
            if not (j and j[1]):
                raise EncryptedStreamError('Wrong verification code', wrong=True)
            self.encrypted = True
            return dec
        return self._decrypt_run(run)


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
                 stream_type=STREAM_SUB, timeout=15.0, playback=None,
                 verify_code=None):
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
        self._ps = None
        self._key = _key16(verify_code) if verify_code else None

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
            ready_at = None
            for chunk in self._packets:
                if chunk:
                    self._pending.append(chunk)
                if self.input_format is not None and self._has_video:
                    # A live RTP stream also needs its frame rate (see ``fps``):
                    # a few frames after the keyframe tell it, but never hold the
                    # picture back for long.
                    ready_at = ready_at or time.monotonic()
                    if (self.playback or self._depay is None or self.fps
                            or time.monotonic() - ready_at > FPS_WAIT_SECONDS):
                        return self
                if time.monotonic() > deadline:
                    break
            raise StreamError('Cloud stream sent no decodable video')
        except Exception as e:
            if isinstance(e, EncryptedStreamError):
                e.serial, e.device_name = self.cam.device_serial, self.cam.device_name
            self.close()
            raise

    @property
    def fps(self):
        """Frames per second of a live RTP stream, or None if not (yet) known."""
        return self._depay.fps if self._depay is not None else None

    def set_audio_sink(self, fn):
        """Receive the live sound as ``fn(payload_type, g711_bytes)``, one call per
        RTP packet (0 = µ-law, 8 = A-law; 8 kHz mono). Only live RTP streams carry
        it here (the NVR records video only). Returns False when there is none."""
        if self._depay is None:
            return False
        self._depay.on_audio = fn
        return True

    @property
    def _has_video(self):
        d = self._depay
        if d is not None:
            return d.saw_slice
        # MPEG-PS: ready once we know whether it is scrambled, so a missing or
        # wrong verification code is raised here, not later in the feed thread.
        return self._ps is None or self._ps.ready

    def _media_chunks(self):
        from pyezvizapi.exceptions import PyEzvizError
        try:
            yield from self._media_chunks_inner()
        except PyEzvizError as e:
            # At the end of a recording the relay sends a control packet on
            # channel 0x02, which pyezvizapi cannot decode. That is the normal
            # end of the stream, not a failure.
            if not (self.playback and 'Unknown VTM channel: 0x02' in str(e)):
                raise

    def _media_chunks_inner(self):
        from pyezvizapi.stream import StreamTransport, VtmChannel, detect_transport
        transport = StreamTransport.UNKNOWN
        for pkt in self._vtm.iter_packets():
            if self._closed:
                return
            if pkt.channel not in (VtmChannel.STREAM, VtmChannel.ENCRYPTED_STREAM) \
                    or not pkt.body:
                continue
            if pkt.encrypted:
                raise StreamError('Stream uses end-to-end (ECDH) encryption — '
                                  'not supported')
            t = detect_transport(pkt.body)
            if transport == StreamTransport.UNKNOWN and t != StreamTransport.UNKNOWN:
                transport = t
                if t == StreamTransport.RTP:
                    self._depay = _RtpVideoDepacketizer(self._key)
                else:
                    self.input_format = {StreamTransport.MPEG_PS: 'mpeg',
                                         StreamTransport.MPEG_TS: 'mpegts'}.get(t)
                    if t == StreamTransport.MPEG_PS:
                        self._ps = _PsDecryptor(self._key)
            # Lock onto the first detected transport: MPEG-PS continuation
            # packets can start with a byte that merely looks like RTP v2.
            active = transport
            if active == StreamTransport.RTP:
                out = b''.join(self._depay.feed(pkt.body))
                if self.input_format is None and self._depay.codec:
                    self.input_format = self._depay.codec
                yield out              # b'' lets start() check its deadline
            elif active == StreamTransport.MPEG_PS:
                yield self._ps.feed(pkt.body)   # b'' while a frame is held back
            elif active == StreamTransport.MPEG_TS:
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
              timeout=15.0, verify_code=None) -> CloudLiveStream:
    """Open a live stream, preferring the requested profile and falling back
    to the other one (some NVR channels send undecodable data on main, some
    cameras have no substream)."""
    order = [STREAM_SUB, STREAM_MAIN] if sub else [STREAM_MAIN, STREAM_SUB]
    last = None
    for st in order:
        s = CloudLiveStream(cloud, cam, st, timeout=timeout,
                            verify_code=verify_code)
        try:
            return s.start()
        except (AuthError, EncryptedStreamError):
            raise
        except Exception as e:
            last = e
    raise StreamError(str(last) if last else 'Cloud stream failed')


def open_playback(cloud: HikConnectCloud, cam: CloudCamera, start, end,
                  timeout=15.0, verify_code=None) -> CloudLiveStream:
    """Replay a device recording (start/end = device local datetimes).
    Main profile first (that is what NVRs record), sub as fallback."""
    last = None
    for st in (STREAM_MAIN, STREAM_SUB):
        s = CloudLiveStream(cloud, cam, st, timeout=timeout, playback=(start, end),
                            verify_code=verify_code)
        try:
            return s.start()
        except (AuthError, EncryptedStreamError):
            raise
        except Exception as e:
            last = e
            if 'No recording' in str(e):
                break
    raise StreamError(str(last) if last else 'Cloud playback failed')
