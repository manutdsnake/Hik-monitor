"""
Lightweight ONVIF client — no external deps (raw UDP WS-Discovery + SOAP over
requests). Used to discover IP cameras on the LAN and fetch their live RTSP URLs.

ONVIF cameras deliver STANDARD H.264/H.265, which ffmpeg decodes cleanly — unlike
the proprietary live stream of some OEM NVRs. This is the universal path to
supporting cameras from any manufacturer.
"""
import socket
import uuid
import re
import base64
import hashlib
import datetime
from urllib.parse import urlparse, urlunparse

try:
    import requests
except ImportError:
    requests = None

WSD_ADDR = ('239.255.255.250', 3702)

_PROBE = '''<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
 xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
 xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
 <e:Header>
  <w:MessageID>uuid:{mid}</w:MessageID>
  <w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
  <w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
 </e:Header>
 <e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body>
</e:Envelope>'''


def discover(timeout=5):
    """WS-Discovery broadcast. Returns a list of dicts: {host, xaddr, port}.
    Filters out non-camera responders (e.g. Windows/NAS WSD on port 5357)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    s.settimeout(timeout)
    try:
        s.sendto(_PROBE.format(mid=uuid.uuid4()).encode(), WSD_ADDR)
    except OSError:
        s.close()
        return []
    import time
    found = {}
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            data, addr = s.recvfrom(65535)
        except socket.timeout:
            break
        except OSError:
            break
        txt = data.decode(errors='replace')
        for xaddr in re.findall(r'<[^>]*XAddrs>([^<]+)<', txt):
            # XAddrs may list several URLs (IPv4 + IPv6); take the first http(s).
            for url in xaddr.split():
                if not url.lower().startswith('http'):
                    continue
                if ':5357/' in url:        # Windows/NAS WS-Discovery, not a camera
                    continue
                p = urlparse(url)
                host = p.hostname
                if not host or ':' in host:  # skip IPv6
                    continue
                if host not in found:
                    found[host] = {'host': host, 'xaddr': url,
                                   'port': p.port or 80}
                break
    s.close()
    return list(found.values())


class ONVIFCamera:
    """Minimal ONVIF Media client: device info + live stream URI."""
    DEV_NS = 'http://www.onvif.org/ver10/device/wsdl'
    MEDIA_NS = 'http://www.onvif.org/ver10/media/wsdl'

    def __init__(self, host, port=80, username='', password='', xaddr=None):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.device_url = xaddr or f'http://{host}:{port}/onvif/device_service'
        self.media_url = None
        self.timeout = 8

    # ── WS-Security UsernameToken (PasswordDigest) ───────────────────────────
    def _security_header(self):
        if not self.username:
            return ''
        nonce = uuid.uuid4().bytes
        created = datetime.datetime.now(datetime.timezone.utc).strftime(
            '%Y-%m-%dT%H:%M:%SZ')
        digest = base64.b64encode(
            hashlib.sha1(nonce + created.encode() +
                         self.password.encode()).digest()).decode()
        nonce_b64 = base64.b64encode(nonce).decode()
        return (
            '<s:Header><Security s:mustUnderstand="1" '
            'xmlns="http://docs.oasis-open.org/wss/2004/01/'
            'oasis-200401-wss-wssecurity-secext-1.0.xsd">'
            '<UsernameToken><Username>%s</Username>'
            '<Password Type="http://docs.oasis-open.org/wss/2004/01/'
            'oasis-200401-wss-username-token-profile-1.0#PasswordDigest">%s'
            '</Password>'
            '<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/'
            'oasis-200401-wss-soap-message-security-1.0#Base64Binary">%s</Nonce>'
            '<Created xmlns="http://docs.oasis-open.org/wss/2004/01/'
            'oasis-200401-wss-wssecurity-utility-1.0.xsd">%s</Created>'
            '</UsernameToken></Security></s:Header>'
            % (self.username, digest, nonce_b64, created))

    def _soap(self, url, body):
        env = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
            'xmlns:tds="%s" xmlns:trt="%s" '
            'xmlns:tt="http://www.onvif.org/ver10/schema">'
            '%s<s:Body>%s</s:Body></s:Envelope>'
            % (self.DEV_NS, self.MEDIA_NS, self._security_header(), body))
        r = requests.post(url, data=env.encode(),
                          headers={'Content-Type':
                                   'application/soap+xml; charset=utf-8'},
                          timeout=self.timeout)
        return r.text

    def get_device_information(self):
        t = self._soap(self.device_url, '<tds:GetDeviceInformation/>')
        def g(tag):
            m = re.search(rf'<[^>]*{tag}>([^<]*)<', t)
            return m.group(1) if m else ''
        return {'manufacturer': g('Manufacturer'), 'model': g('Model'),
                'firmware': g('FirmwareVersion'), 'serial': g('SerialNumber')}

    def _ensure_media_url(self):
        if self.media_url:
            return
        t = self._soap(self.device_url,
                       '<tds:GetCapabilities><tds:Category>Media'
                       '</tds:Category></tds:GetCapabilities>')
        # Find the Media capability's XAddr.
        m = re.search(r'<tt:Media>.*?<tt:XAddr>([^<]+)<', t, re.S)
        self.media_url = m.group(1) if m else \
            f'http://{self.host}:{self.port}/onvif/media_service'

    def get_profiles(self):
        return [p['token'] for p in self.get_profiles_detail()]

    def get_profiles_detail(self):
        """Return [{'token','width','height','source'}] for each media profile.
        Multi-lens cameras expose one profile per lens (sometimes mislabelled with
        the same source token), so we surface them all."""
        self._ensure_media_url()
        t = self._soap(self.media_url, '<trt:GetProfiles/>')
        out = []
        # Split the response into per-profile blocks (each starts at a Profiles tag).
        blocks = re.split(r'(?=<[^>]*[:\s]Profiles[ >])', t)
        for b in blocks:
            mtok = re.search(r'[:\s]Profiles[^>]*token="([^"]+)"', b)
            if not mtok:
                continue
            res = re.search(r'Resolution>\s*<[^>]*Width>(\d+)<.*?Height>(\d+)<', b, re.S)
            src = re.search(r'SourceToken>([^<]+)<', b)
            out.append({'token': mtok.group(1),
                        'width':  int(res.group(1)) if res else 0,
                        'height': int(res.group(2)) if res else 0,
                        'source': src.group(1) if src else ''})
        return out

    _ptz_url_cache = None

    def get_ptz_url(self):
        """Return the PTZ service XAddr if the camera supports PTZ, else None."""
        if self._ptz_url_cache is not None:
            return self._ptz_url_cache or None
        url = ''
        try:
            env = ('<?xml version="1.0"?><s:Envelope '
                   'xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
                   'xmlns:tds="%s">%s<s:Body><tds:GetServices>'
                   '<tds:IncludeCapability>false</tds:IncludeCapability>'
                   '</tds:GetServices></s:Body></s:Envelope>'
                   % (self.DEV_NS, self._security_header()))
            t = requests.post(self.device_url, data=env.encode(),
                              headers={'Content-Type': 'application/soap+xml'},
                              timeout=self.timeout).text
            m = re.search(r'Namespace>http://www\.onvif\.org/ver20/ptz/wsdl'
                          r'</[^>]*Namespace>\s*<[^>]*XAddr>([^<]+)<', t)
            url = m.group(1) if m else ''
        except Exception:
            url = ''
        self._ptz_url_cache = url
        return url or None

    def ptz(self, token, pan=0.0, tilt=0.0, zoom=0.0):
        """ContinuousMove (pan/tilt/zoom velocities -1..1), or Stop when all 0."""
        url = self.get_ptz_url()
        if not url:
            return False
        if pan == 0 and tilt == 0 and zoom == 0:
            body = ('<tptz:Stop xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl">'
                    '<tptz:ProfileToken>%s</tptz:ProfileToken>'
                    '<tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>true</tptz:Zoom>'
                    '</tptz:Stop>' % token)
        else:
            body = ('<tptz:ContinuousMove xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl">'
                    '<tptz:ProfileToken>%s</tptz:ProfileToken>'
                    '<tptz:Velocity>'
                    '<tt:PanTilt x="%.2f" y="%.2f" xmlns:tt="http://www.onvif.org/ver10/schema"/>'
                    '<tt:Zoom x="%.2f" xmlns:tt="http://www.onvif.org/ver10/schema"/>'
                    '</tptz:Velocity></tptz:ContinuousMove>'
                    % (token, pan, tilt, zoom))
        try:
            self._soap(url, body)
            return True
        except Exception:
            return False

    _img_url_cache = None

    def get_imaging_url(self):
        """Imaging service XAddr if supported, else None."""
        if self._img_url_cache is not None:
            return self._img_url_cache or None
        url = ''
        try:
            env = ('<?xml version="1.0"?><s:Envelope '
                   'xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
                   'xmlns:tds="%s">%s<s:Body><tds:GetServices>'
                   '<tds:IncludeCapability>false</tds:IncludeCapability>'
                   '</tds:GetServices></s:Body></s:Envelope>'
                   % (self.DEV_NS, self._security_header()))
            t = requests.post(self.device_url, data=env.encode(),
                              headers={'Content-Type': 'application/soap+xml'},
                              timeout=self.timeout).text
            m = re.search(r'Namespace>http://www\.onvif\.org/ver20/imaging/wsdl'
                          r'</[^>]*Namespace>\s*<[^>]*XAddr>([^<]+)<', t)
            url = m.group(1) if m else ''
        except Exception:
            url = ''
        self._img_url_cache = url
        return url or None

    def get_ir_cut_filter(self, source_token):
        url = self.get_imaging_url()
        if not url:
            return None
        try:
            t = self._soap(url,
                '<timg:GetImagingSettings xmlns:timg="http://www.onvif.org/ver20/imaging/wsdl">'
                '<timg:VideoSourceToken>%s</timg:VideoSourceToken>'
                '</timg:GetImagingSettings>' % source_token)
            m = re.search(r'IrCutFilter>([^<]+)<', t)
            return m.group(1) if m else None
        except Exception:
            return None

    def set_ir_cut_filter(self, source_token, mode):
        """mode: 'AUTO' (auto day/night), 'ON' (day/colour), 'OFF' (night/IR)."""
        url = self.get_imaging_url()
        if not url:
            return False
        body = ('<timg:SetImagingSettings xmlns:timg="http://www.onvif.org/ver20/imaging/wsdl">'
                '<timg:VideoSourceToken>%s</timg:VideoSourceToken>'
                '<timg:ImagingSettings>'
                '<tt:IrCutFilter xmlns:tt="http://www.onvif.org/ver10/schema">%s</tt:IrCutFilter>'
                '</timg:ImagingSettings></timg:SetImagingSettings>' % (source_token, mode))
        try:
            t = self._soap(url, body)
            return 'Fault' not in t
        except Exception:
            return False

    def get_video_source_count(self):
        """Number of physical video sources (lenses). Dual-lens cameras report
        2 even when they (non-conformantly) reuse one source token."""
        self._ensure_media_url()
        try:
            t = self._soap(self.media_url, '<trt:GetVideoSources/>')
            n = len(re.findall(r'[:\s]VideoSources token=', t))
            return max(1, n)
        except Exception:
            return 1

    def get_stream_uri(self, profile_token):
        self._ensure_media_url()
        body = (
            '<trt:GetStreamUri><trt:StreamSetup>'
            '<tt:Stream>RTP-Unicast</tt:Stream>'
            '<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport>'
            '</trt:StreamSetup><trt:ProfileToken>%s</trt:ProfileToken>'
            '</trt:GetStreamUri>' % profile_token)
        t = self._soap(self.media_url, body)
        m = re.search(r'<tt:Uri>([^<]+)<', t)
        if not m:
            return None
        uri = m.group(1).replace('&amp;', '&')
        return self._inject_credentials(uri)

    def _inject_credentials(self, uri):
        """Add user:pass@ to the RTSP URL if the camera didn't embed creds and
        we have them (needed for cameras that require RTSP auth)."""
        if not self.username:
            return uri
        p = urlparse(uri)
        if p.username or 'user=' in uri or 'password=' in uri:
            return uri   # creds already present (inline or path-embedded)
        netloc = f'{self.username}:{self.password}@{p.hostname}'
        if p.port:
            netloc += f':{p.port}'
        return urlunparse(p._replace(netloc=netloc))

    def stream_urls(self):
        """Return [(label, rtsp_url), ...] for all profiles. Label carries the
        resolution so the user can tell streams/lenses apart in the camera list."""
        out = []
        for i, p in enumerate(self.get_profiles_detail()):
            try:
                url = self.get_stream_uri(p['token'])
                if not url:
                    continue
                if p['width']:
                    label = f"{p['width']}×{p['height']}"
                else:
                    label = f'stream{i}'
                out.append((label, url))
            except Exception:
                pass
        return out
