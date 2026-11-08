"""On-demand video sessions for the web viewer.

A session is one Hik-Connect relay stream (live view or a recording) piped
through ffmpeg into H.264 video the browser can play, either as fragmented MP4
pushed over a WebSocket (low delay, "fmp4") or as HLS files in a private temp
directory (fallback for browsers without Media Source). Nothing
runs until somebody asks for it: a session starts on request and is closed
when no client has touched it for a while, so cameras are only streamed while
a browser is actually looking at them.

Browsers cannot reliably play H.265, so video is transcoded to H.264.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

import hik_cloud

log = logging.getLogger('hik_web')

FFMPEG = shutil.which('ffmpeg') or 'ffmpeg'
FFPROBE = shutil.which('ffprobe') or 'ffprobe'

LIVE_IDLE_SECONDS = 25        # no client touched a live session → close
PLAYBACK_IDLE_SECONDS = 120   # recordings may be paused for a while
MAX_CLIP_SECONDS = 30 * 60
CLIP_KEEP_SECONDS = 30 * 60   # a finished clip stays downloadable this long


class Session:
    def __init__(self, sid, kind, cam, root, **info):
        self.id = sid
        self.kind = kind                  # 'live' | 'playback'
        self.cam = cam
        self.dir = os.path.join(root, sid)
        os.makedirs(self.dir)
        self.info = info
        self.touched = time.monotonic()
        self.closed = threading.Event()
        self.proc = None
        self.stream = None
        self.error = None
        self.hub = None                   # Fmp4Hub when the output is fMP4 over WebSocket

    def touch(self):
        self.touched = time.monotonic()

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    @property
    def idle_limit(self):
        return LIVE_IDLE_SECONDS if self.kind == 'live' else PLAYBACK_IDLE_SECONDS

    def log_tail(self, n=600):
        try:
            with open(os.path.join(self.dir, 'ffmpeg.log'), 'rb') as f:
                return f.read()[-n:].decode('utf-8', 'replace').strip()
        except OSError:
            return ''


class ClipJob:
    """A clip being copied out of the relay. ``progress`` is an estimate: the
    relay delivers recordings at about real-time speed."""

    def __init__(self, cid, filename, seconds):
        self.id = cid
        self.filename = filename
        self.seconds = seconds
        self.state = 'preparing'          # 'preparing' | 'ready' | 'error'
        self.error = None
        self.path = None
        self.size = 0
        self.started = time.monotonic()
        self.finished = None

    @property
    def progress(self):
        if self.state == 'ready':
            return 1.0
        return min(0.98, (time.monotonic() - self.started) / max(1.0, self.seconds + 3))


def _ffmpeg_cmd(fmt, outdir, *, live, speed=1.0, max_height=None, dar=None,
                mode='hls', crf=24, maxrate=None, bufsize=None, threads=None):
    """ffmpeg: raw/PS cloud stream on stdin → H.264 for the browser.

    mode 'hls':  fMP4 HLS files in ``outdir`` (1 s segments).
    mode 'fmp4': fragmented MP4 on stdout, a fragment per second (starting with
                 a keyframe), for the WebSocket player.
    """
    cmd = [FFMPEG, '-hide_banner', '-loglevel', 'warning', '-nostdin']
    if live:
        # Raw elementary stream: the format is known and the first chunk holds
        # the parameter sets, so a tiny probe is enough (cameras send 25 fps).
        cmd += ['-f', fmt, '-probesize', '2048', '-analyzeduration', '0']
    else:
        # Recordings arrive as MPEG-PS, faster than real time → pace them.
        cmd += ['-readrate', f'{speed:g}', '-f', fmt,
                '-probesize', '500000', '-analyzeduration', '1000000']
    if threads:
        cmd += ['-threads', str(threads)]         # decoder: do not spawn a thread per host core
    cmd += ['-i', 'pipe:0', '-map', '0:v:0', '-an', '-sn']
    filters = []
    if dar:
        # Square pixels at the camera's true shape (a no-op when already right).
        filters.append(f"scale='trunc(ih*{dar:.4f}/2)*2':ih,setsar=1")
    if max_height:
        filters.append(f"scale=-2:'min({int(max_height)},ih)'")
    if filters:
        cmd += ['-vf', ','.join(filters)]
    # One keyframe per second (25 fps), and in fMP4 mode one fragment per keyframe.
    # Measured on real camera footage: a tighter bitrate cap or more frequent
    # keyframes make the picture "pump" (sharp keyframe, then mushy frames) and the
    # ultrafast preset turns deblocking off. ipratio/pbratio 1.0 and qcomp 0.8 keep
    # the quality even from frame to frame; the cap is only a safety net.
    cmd += ['-c:v', 'libx264', '-preset', 'veryfast', '-tune', 'zerolatency',
            '-pix_fmt', 'yuv420p', '-profile:v', 'high',
            '-x264-params', 'ipratio=1.0:pbratio=1.0:qcomp=0.8',
            '-crf', str(crf)]
    if threads:
        cmd += ['-threads', str(threads)]
    if maxrate:
        cmd += ['-maxrate', maxrate, '-bufsize', bufsize or maxrate]
    cmd += ['-g', '25', '-keyint_min', '25', '-sc_threshold', '0',
            '-force_key_frames', 'expr:gte(t,n_forced*1)']
    if mode == 'fmp4':
        return cmd + ['-flush_packets', '1', '-f', 'mp4',
                      '-movflags', 'empty_moov+default_base_moof+frag_keyframe', 'pipe:1']
    seg = ['-f', 'hls', '-hls_time', '1', '-hls_segment_type', 'fmp4',
           '-hls_fmp4_init_filename', 'init.mp4',
           '-hls_segment_filename', os.path.join(outdir, 'seg%05d.m4s')]
    if live:
        seg += ['-hls_list_size', '6',
                '-hls_flags', 'delete_segments+independent_segments+omit_endlist']
    else:
        seg += ['-hls_list_size', '0', '-hls_playlist_type', 'event',
                '-hls_flags', 'independent_segments']
    return cmd + seg + [os.path.join(outdir, 'index.m3u8')]


# ── fragmented-MP4 fan-out ──────────────────────────────────────────────────
def _read_exact(f, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = f.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def _mp4_boxes(f):
    """Yield (type, raw bytes) for each top-level MP4 box read from ``f``."""
    while True:
        head = _read_exact(f, 8)
        if head is None:
            return
        size, kind = int.from_bytes(head[:4], 'big'), head[4:8]
        if size == 1:                                   # 64-bit size
            ext = _read_exact(f, 8)
            if ext is None:
                return
            head += ext
            size = int.from_bytes(ext, 'big')
        body = _read_exact(f, size - len(head))
        if body is None:
            return
        yield kind, head + body


def _mime_from_init(init):
    """'video/mp4; codecs="avc1.640028"' from the avcC box of an init segment."""
    i = init.find(b'avcC')
    if i < 0 or i + 8 > len(init):
        return 'video/mp4; codecs="avc1.640028"'
    return 'video/mp4; codecs="avc1.%02x%02x%02x"' % tuple(init[i + 5:i + 8])


def _put(q, item):
    try:
        q.put_nowait(item)
    except asyncio.QueueFull:                           # viewer too slow: drop it
        while not q.empty():
            q.get_nowait()
        q.put_nowait(None)


class Fmp4Hub:
    """The fMP4 output of one session, shared by every WebSocket viewer.

    Each fragment starts with a keyframe, so a viewer that joins late gets the
    init segment plus the latest fragment and shows a picture straight away."""

    def __init__(self):
        self.init = None
        self.mime = None
        self.last = None
        self.ended = False
        self.ready = threading.Event()
        self._lock = threading.Lock()
        self._subs = set()                              # (loop, queue)

    def subscribe(self, loop, q):
        with self._lock:
            self._subs.add((loop, q))
            return self.last

    def unsubscribe(self, loop, q):
        with self._lock:
            self._subs.discard((loop, q))

    def set_init(self, init):
        self.init, self.mime = init, _mime_from_init(init)
        self.ready.set()

    def publish(self, frag):
        with self._lock:
            self.last = frag
            subs = list(self._subs)
        for loop, q in subs:
            loop.call_soon_threadsafe(_put, q, frag)

    def end(self):
        with self._lock:
            self.ended = True
            subs = list(self._subs)
        self.ready.set()
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(_put, q, None)
            except RuntimeError:                        # loop already closed
                pass


class SessionManager:
    def __init__(self, root=None):
        self.root = root or tempfile.mkdtemp(prefix='hikweb-')
        os.makedirs(self.root, exist_ok=True)
        self._sessions = {}          # sid → Session
        self._live = {}              # (camera key, quality, mode) → sid
        self._clips = {}             # clip id → ClipJob
        self._key_locks = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        threading.Thread(target=self._reaper, daemon=True, name='hikweb-reaper').start()

    # ── lookup ──────────────────────────────────────────────────────────────
    def get(self, sid):
        with self._lock:
            s = self._sessions.get(sid)
        if s is not None:
            s.touch()
        return s

    def _key_lock(self, k):
        with self._lock:
            return self._key_locks.setdefault(k, threading.Lock())

    # ── live ────────────────────────────────────────────────────────────────
    def live(self, cloud, cam, code, quality, dar=None, mode='hls'):
        """Shared live session per (camera, quality, mode): a second viewer
        joins the running one instead of opening another relay stream."""
        k = (cam.key, quality, mode)
        with self._key_lock(k):
            with self._lock:
                sid = self._live.get(k)
                s = self._sessions.get(sid) if sid else None
                if s is not None and s.running and not s.closed.is_set():
                    s.touch()               # under the lock: the reaper cannot close it now
                    return s
            if s is not None:
                self.close(s)
            s = self._open('live', cloud, cam, code, quality=quality, dar=dar,
                           mode=mode, max_height=540 if quality == 'sub' else 1080)
            self._live[k] = s.id
            return s

    # ── playback ────────────────────────────────────────────────────────────
    def playback(self, cloud, cam, code, begin, end, speed=1.0, dar=None, fast=False):
        return self._open('playback', cloud, cam, code, quality='main', dar=dar,
                          playback=(begin, end), speed=speed,
                          max_height=540 if fast else 1080)


    # ── internals ───────────────────────────────────────────────────────────
    def _open(self, kind, cloud, cam, code, *, quality, playback=None,
              speed=1.0, max_height=None, dar=None, mode='hls'):
        s = Session(uuid.uuid4().hex[:12], kind, cam, self.root,
                    quality=quality, speed=speed, mode=mode)
        try:
            if kind == 'live':
                s.stream = hik_cloud.open_live(cloud, cam, sub=(quality == 'sub'),
                                               verify_code=code)
            else:
                s.stream = hik_cloud.open_playback(cloud, cam, *playback,
                                                   verify_code=code)
            s.info['stream_type'] = s.stream.stream_type
            small = bool(max_height and max_height <= 540)
            cmd = _ffmpeg_cmd(s.stream.input_format, s.dir, live=(kind == 'live'),
                              speed=speed, max_height=max_height, dar=dar, mode=mode,
                              crf=24 if small else 25,
                              maxrate='3000k' if small else '8000k',
                              bufsize='6000k' if small else '16000k',
                              threads=1 if small else 2)
            with open(os.path.join(s.dir, 'ffmpeg.log'), 'wb') as logf:
                s.proc = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE, cwd=s.dir, stderr=logf,
                    stdout=subprocess.PIPE if mode == 'fmp4' else subprocess.DEVNULL)
            try:
                os.setpriority(os.PRIO_PROCESS, s.proc.pid, 5)    # yield to the web server and to neighbours
            except OSError:
                pass
            if mode == 'fmp4':
                s.hub = Fmp4Hub()
                threading.Thread(target=self._pump_fmp4, args=(s,), daemon=True,
                                 name=f'hikweb-fmp4-{s.id}').start()
            threading.Thread(target=self._feed, args=(s,), daemon=True,
                             name=f'hikweb-feed-{s.id}').start()
            with self._lock:
                self._sessions[s.id] = s
            log.info('%s session %s: %s (%s, %s)', kind, s.id, cam.name, quality, mode)
            return s
        except BaseException:
            self._teardown(s)
            raise

    @staticmethod
    def _pump_fmp4(s):
        """Split ffmpeg's fMP4 stdout into the init segment and fragments."""
        hub, init, frag = s.hub, bytearray(), bytearray()
        try:
            for kind, data in _mp4_boxes(s.proc.stdout):
                if hub.init is None:
                    init += data
                    if kind == b'moov':
                        hub.set_init(bytes(init))
                elif kind == b'moof':
                    frag = bytearray(data)
                elif kind == b'mdat' and frag:
                    frag += data
                    hub.publish(bytes(frag))
                    frag = bytearray()
        except (OSError, ValueError):
            pass
        finally:
            hub.end()

    @staticmethod
    def _feed(s):
        try:
            for chunk in s.stream.iter_chunks():
                if s.closed.is_set():
                    break
                s.proc.stdin.write(chunk)
                s.proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        except Exception as e:                      # relay dropped, etc.
            s.error = str(e)
            log.info('session %s feed ended: %s', s.id, e)
        finally:
            try:
                s.stream.close()
            except Exception:
                pass
            try:
                s.proc.stdin.close()
            except Exception:
                pass

    @staticmethod
    def _teardown(s):
        s.closed.set()
        if s.stream is not None:
            try:
                s.stream.close()
            except Exception:
                pass
        if s.proc is not None and s.proc.poll() is None:
            s.proc.terminate()
            try:
                s.proc.wait(2)
            except subprocess.TimeoutExpired:
                s.proc.kill()
        shutil.rmtree(s.dir, ignore_errors=True)

    def close(self, s):
        self._unregister(s)
        self._teardown(s)
        log.info('closed %s session %s', s.kind, s.id)

    def _unregister(self, s):
        with self._lock:
            self._sessions.pop(s.id, None)
            k = (s.cam.key, s.info.get('quality'), s.info.get('mode'))
            if self._live.get(k) == s.id:
                self._live.pop(k, None)

    def _reap_if_stale(self, s, now):
        """Close an idle session. The check and the removal happen under one lock,
        so a viewer that joins at that moment either keeps it alive or starts a
        new one; it is never handed a session that is about to disappear."""
        with self._lock:
            idle = now - s.touched
            stale = idle > s.idle_limit or (s.kind == 'live' and not s.running and idle > 5)
            if not stale or self._sessions.get(s.id) is not s:
                return
            self._sessions.pop(s.id, None)
            k = (s.cam.key, s.info.get('quality'), s.info.get('mode'))
            if self._live.get(k) == s.id:
                self._live.pop(k, None)
        self._teardown(s)
        log.info('closed idle %s session %s', s.kind, s.id)

    def _reaper(self):
        while not self._stop.wait(3):
            now = time.monotonic()
            with self._lock:
                candidates = list(self._sessions.values())
            for s in candidates:
                self._reap_if_stale(s, now)
            self._expire_clips(now)

    def _expire_clips(self, now):
        with self._lock:
            old = [j for j in self._clips.values()
                   if j.finished and now - j.finished > CLIP_KEEP_SECONDS]
            for j in old:
                del self._clips[j.id]
        for j in old:
            if j.path:
                try:
                    os.unlink(j.path)
                except OSError:
                    pass

    def shutdown(self):
        self._stop.set()
        with self._lock:
            sessions = list(self._sessions.values())
        for s in sessions:
            self.close(s)
        shutil.rmtree(self.root, ignore_errors=True)

    # ── clip download ───────────────────────────────────────────────────────
    def start_clip(self, cloud, cam, code, begin, end):
        """Copy a recording span into an MP4 (no re-encode) in the background.
        The relay opens synchronously, so a missing verification code or a
        missing recording is raised here; the rest is polled via the job."""
        seconds = (end - begin).total_seconds()
        if seconds > MAX_CLIP_SECONDS:
            raise ValueError(f'Clips are limited to {MAX_CLIP_SECONDS // 60} minutes')
        stream = hik_cloud.open_playback(cloud, cam, begin, end, verify_code=code)
        name = re.sub(r'[^A-Za-z0-9._-]+', '_', cam.name).strip('_') or 'camera'
        job = ClipJob(uuid.uuid4().hex[:12], f'{name}_{begin:%Y%m%d-%H%M%S}.mp4',
                      seconds)
        with self._lock:
            self._clips[job.id] = job
        threading.Thread(target=self._build_clip, args=(job, stream), daemon=True,
                         name=f'hikweb-clip-{job.id}').start()
        return job

    def clip(self, cid):
        with self._lock:
            return self._clips.get(cid)

    def _build_clip(self, job, stream):
        fd, raw = tempfile.mkstemp(suffix='.mp4', prefix='clip-', dir=self.root)
        os.close(fd)
        cmd = [FFMPEG, '-hide_banner', '-loglevel', 'warning', '-nostdin', '-y',
               '-f', stream.input_format, '-probesize', '500000',
               '-analyzeduration', '1000000', '-i', 'pipe:0',
               '-map', '0:v:0', '-an', '-c:v', 'copy', '-movflags', '+faststart', raw]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            try:
                for chunk in stream.iter_chunks():
                    proc.stdin.write(chunk)
            except (BrokenPipeError, OSError):
                pass
            finally:
                stream.close()
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            err = proc.stderr.read().decode('utf-8', 'replace')
            proc.wait(60)
            if proc.returncode != 0 or os.path.getsize(raw) < 1024:
                raise RuntimeError('Could not build the clip: ' + err.strip()[-300:])
            job.path = self._apple_tag(raw)
            job.size = os.path.getsize(job.path)
            job.state = 'ready'
        except Exception as e:
            log.info('clip %s failed: %s', job.id, e)
            job.error, job.state = str(e), 'error'
            if proc.poll() is None:
                proc.kill()
            try:
                os.unlink(raw)
            except OSError:
                pass
        finally:
            job.finished = time.monotonic()

    def _apple_tag(self, path):
        """HEVC in MP4 needs the 'hvc1' tag for QuickTime / Safari / phones."""
        codec = subprocess.run(
            [FFPROBE, '-v', 'error', '-select_streams', 'v:0', '-show_entries',
             'stream=codec_name', '-of', 'csv=p=0', path],
            capture_output=True, text=True).stdout.strip()
        if codec != 'hevc':
            return path
        tagged = path + '.tagged.mp4'
        r = subprocess.run([FFMPEG, '-hide_banner', '-loglevel', 'error', '-y', '-i', path,
                            '-c', 'copy', '-tag:v', 'hvc1', '-movflags', '+faststart', tagged],
                           capture_output=True)
        if r.returncode == 0 and os.path.getsize(tagged) > 1024:
            os.replace(tagged, path)
        else:
            try:
                os.unlink(tagged)
            except OSError:
                pass
        return path
