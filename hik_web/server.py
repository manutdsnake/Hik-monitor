"""Web viewer for Hik-Connect cameras: live grid, playback, clip download.

    python -m hik_web [--host 127.0.0.1] [--port 8765]

Binds to localhost by default. To reach it from other devices, put it behind
Tailscale (``tailscale serve``) or bind to the Tailscale address, and set
HIK_WEB_PASSWORD so the page asks for a password.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hmac
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from aiohttp import web

import hik_cloud
from .account import Account
from .streams import SessionManager

log = logging.getLogger('hik_web')
STATIC = os.path.join(os.path.dirname(__file__), 'static')
_SAFE_NAME = re.compile(r'^[A-Za-z0-9_.-]+$')


def _json_error(status, message, **extra):
    return web.json_response({'error': message, **extra}, status=status)


def _stream_error(e):
    """Map a cloud/stream exception to an HTTP response."""
    if isinstance(e, hik_cloud.EncryptedStreamError):
        return _json_error(409, str(e), needs_code=True, wrong=bool(e.wrong),
                           serial=e.serial, device=e.device_name)
    if isinstance(e, ValueError):
        return _json_error(400, str(e))
    if isinstance(e, hik_cloud.HikCloudError):
        return _json_error(502, str(e))
    log.exception('unexpected error')
    return _json_error(500, f'{type(e).__name__}: {e}')


def _parse_time(value):
    try:
        return datetime.strptime(value[:19], '%Y-%m-%dT%H:%M:%S')
    except (TypeError, ValueError):
        raise ValueError('Bad time, expected YYYY-MM-DDTHH:MM:SS') from None


def _camera_or_404(request, key):
    cam = request.app['account'].camera(key)
    if cam is None:
        raise web.HTTPNotFound(text='Unknown camera')
    return cam


# ── auth ────────────────────────────────────────────────────────────────────
@web.middleware
async def auth_middleware(request, handler):
    password = request.app['password']
    if password:
        sent = ''
        header = request.headers.get('Authorization', '')
        if header.startswith('Basic '):
            try:
                sent = base64.b64decode(header[6:]).decode().partition(':')[2]
            except Exception:
                sent = ''
        if not hmac.compare_digest(sent.encode(), password.encode()):
            return web.Response(status=401, text='Password required',
                                headers={'WWW-Authenticate': 'Basic realm="Hik Monitor"'})
    return await handler(request)


# ── pages ───────────────────────────────────────────────────────────────────
async def index(request):
    return web.FileResponse(os.path.join(STATIC, 'index.html'),
                            headers={'Cache-Control': 'no-cache'})


# ── API ─────────────────────────────────────────────────────────────────────
async def api_cameras(request):
    account = request.app['account']
    try:
        cams = await asyncio.to_thread(account.cameras, request.query.get('refresh') == '1')
    except Exception as e:
        return _stream_error(e)
    return web.json_response([
        {'key': c.key, 'name': c.name, 'device': c.device_name,
         'serial': c.device_serial, 'channel': c.channel_no,
         'has_code': bool(account.verify_code(c.device_serial)),
         'empty': account.is_empty(c)}
        for c in cams.values()])


async def api_snapshot(request):
    account = request.app['account']
    cam = _camera_or_404(request, request.match_info['key'])
    try:
        data = await asyncio.to_thread(account.snapshot, cam)
    except Exception as e:
        return _stream_error(e)
    return web.Response(body=data, content_type='image/jpeg',
                        headers={'Cache-Control': 'max-age=60'})


async def api_live(request):
    account, mgr = request.app['account'], request.app['sessions']
    body = await request.json()
    cam = _camera_or_404(request, str(body.get('key', '')))
    quality = 'main' if body.get('quality') == 'main' else 'sub'
    mode = 'fmp4' if body.get('transport') == 'mse' else 'hls'
    try:
        t0 = time.monotonic()
        dar = await asyncio.to_thread(account.aspect, cam)
        t1 = time.monotonic()
        s = await asyncio.to_thread(mgr.live, account.cloud, cam,
                                    account.verify_code(cam.device_serial), quality,
                                    dar, mode)
        if time.monotonic() - t0 > 3:
            log.info('slow live start for %s: shape %.1f s, open %.1f s', cam.name, t1 - t0,
                     time.monotonic() - t1)
    except Exception as e:
        if '6106' in str(e):                # the relay: no camera on this channel
            account.mark_empty(cam)
        return _stream_error(e)
    out = {'id': s.id, 'stream_type': s.info.get('stream_type')}
    out['ws' if mode == 'fmp4' else 'url'] = (
        f'/ws/live/{s.id}' if mode == 'fmp4' else f'/hls/{s.id}/index.m3u8')
    return web.json_response(out)


async def api_records(request):
    account = request.app['account']
    cam = _camera_or_404(request, request.query.get('key', ''))
    try:
        day = datetime.strptime(request.query.get('date', ''), '%Y-%m-%d')
    except ValueError:
        return _json_error(400, 'Bad date, expected YYYY-MM-DD')
    try:
        recs = await asyncio.to_thread(
            account.cloud.search_records, cam, day,
            day + timedelta(hours=23, minutes=59, seconds=59))
    except Exception as e:
        return _stream_error(e)
    return web.json_response({
        'now': datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
        'records': [{'begin': r['begin'], 'end': r['end'], 'type': r.get('type', '')}
                    for r in recs]})


async def api_playback(request):
    account, mgr = request.app['account'], request.app['sessions']
    body = await request.json()
    cam = _camera_or_404(request, str(body.get('key', '')))
    try:
        begin, end = _parse_time(body.get('begin')), _parse_time(body.get('end'))
        if end <= begin:
            raise ValueError('End must be after start')
        # The relay delivers a recording at real-time speed only (tested: it ignores
        # every speed parameter, and the NVR allows just two playbacks at once), so
        # only slow motion is possible; fast movement is done by jumping.
        speed = min(1.0, max(0.25, float(body.get('speed') or 1)))
        dar = await asyncio.to_thread(account.aspect, cam)
        s = await asyncio.to_thread(mgr.playback, account.cloud, cam,
                                    account.verify_code(cam.device_serial),
                                    begin, end, speed, dar, bool(body.get('fast')))
    except Exception as e:
        return _stream_error(e)
    return web.json_response({'id': s.id, 'url': f'/hls/{s.id}/index.m3u8'})


async def api_clip_start(request):
    account, mgr = request.app['account'], request.app['sessions']
    body = await request.json()
    cam = _camera_or_404(request, str(body.get('key', '')))
    try:
        begin, end = _parse_time(body.get('begin')), _parse_time(body.get('end'))
        if end <= begin:
            raise ValueError('End must be after start')
        job = await asyncio.to_thread(mgr.start_clip, account.cloud, cam,
                                      account.verify_code(cam.device_serial), begin, end)
    except Exception as e:
        return _stream_error(e)
    return web.json_response({'id': job.id, 'filename': job.filename})


async def api_clip_status(request):
    job = request.app['sessions'].clip(request.match_info['cid'])
    if job is None:
        return _json_error(404, 'That clip is no longer available')
    return web.json_response({'state': job.state, 'progress': round(job.progress, 3),
                              'error': job.error, 'size': job.size,
                              'filename': job.filename})


async def api_clip_file(request):
    job = request.app['sessions'].clip(request.match_info['cid'])
    if job is None or job.state != 'ready' or not job.path or not os.path.exists(job.path):
        return _json_error(404, 'That clip is not ready or has expired')
    return web.FileResponse(job.path, headers={
        'Content-Type': 'video/mp4',
        'Content-Disposition': f'attachment; filename="{job.filename}"'})


async def api_verify_code(request):
    account = request.app['account']
    body = await request.json()
    serial, code = str(body.get('serial', '')).strip(), str(body.get('code', '')).strip()
    if not serial or not code:
        return _json_error(400, 'serial and code are required')
    if not any(c.device_serial == serial for c in account.cameras().values()):
        return _json_error(404, 'Unknown device')
    await asyncio.to_thread(account.set_verify_code, serial, code)
    # Streams already running used the old code → drop them so they restart.
    mgr = request.app['sessions']
    for s in [s for s in list(mgr._sessions.values()) if s.cam.device_serial == serial]:
        await asyncio.to_thread(mgr.close, s)
    return web.json_response({'ok': True})


async def api_session_touch(request):
    s = request.app['sessions'].get(request.match_info['sid'])
    return web.json_response({'ok': bool(s)}, status=200 if s else 404)


async def api_session_close(request):
    mgr = request.app['sessions']
    s = mgr.get(request.match_info['sid'])
    if s is not None and s.kind == 'playback':      # live sessions are shared
        await asyncio.to_thread(mgr.close, s)
    return web.json_response({'ok': True})


async def ws_live(request):
    """Low-delay live video: the session's fMP4 fragments over a WebSocket.
    First a JSON message with the codec string, then the init segment, then
    one binary message per fragment (each starts with a keyframe)."""
    s = request.app['sessions'].get(request.match_info['sid'])
    if s is None or s.hub is None:
        raise web.HTTPNotFound()
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=0)
    await ws.prepare(request)
    hub, loop = s.hub, asyncio.get_running_loop()
    await asyncio.to_thread(hub.ready.wait, 15)
    if hub.init is None:
        await ws.send_json({'error': 'The stream ended before it produced video.',
                            'detail': s.log_tail()})
        await ws.close()
        return ws
    q = asyncio.Queue(maxsize=40)
    last = hub.subscribe(loop, q)

    async def watch_client():
        async for _ in ws:                  # nothing is expected from the viewer
            pass
        _put_closed(q)

    def _put_closed(queue):
        try:
            queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

    watcher = asyncio.create_task(watch_client())
    try:
        await ws.send_json({'mime': hub.mime})
        await ws.send_bytes(hub.init)
        if last:
            await ws.send_bytes(last)
        while True:
            frag = await q.get()
            if frag is None or ws.closed:
                break
            s.touch()
            await ws.send_bytes(frag)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        hub.unsubscribe(loop, q)
        watcher.cancel()
    if not ws.closed:
        await ws.close()
    return ws


async def hls_file(request):
    mgr = request.app['sessions']
    s = mgr.get(request.match_info['sid'])
    name = request.match_info['name']
    if s is None or not _SAFE_NAME.match(name):
        raise web.HTTPNotFound()
    path = os.path.join(s.dir, name)
    if name in ('index.m3u8', 'init.mp4'):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 20
        while not os.path.exists(path):
            if s.closed.is_set() or not s.running:
                return _json_error(502, 'Stream ended before it produced video',
                                   detail=s.log_tail())
            if loop.time() > deadline:
                return _json_error(504, 'Timed out waiting for video')
            await asyncio.sleep(0.1)
    if not os.path.exists(path):
        raise web.HTTPNotFound()
    headers = {'Cache-Control': 'no-store'}
    if name.endswith('.m3u8'):
        headers['Content-Type'] = 'application/vnd.apple.mpegurl'
    elif name.endswith(('.m4s', '.mp4')):
        headers['Content-Type'] = 'video/mp4'
    return web.FileResponse(path, headers=headers)


# ── app ─────────────────────────────────────────────────────────────────────
def make_app(account=None, password=None):
    app = web.Application(middlewares=[auth_middleware])
    app['account'] = account or Account()
    app['sessions'] = SessionManager()
    app['password'] = password if password is not None else os.environ.get('HIK_WEB_PASSWORD', '')

    def _warm_up():
        # Sign in and list the cameras now, and fetch their placeholder pictures
        # in parallel, so the first page load does not pay for it. (Picture
        # shapes are remembered on disk, so streams do not wait for this.)
        account = app['account']
        t0 = time.monotonic()
        try:
            cams = list(account.cameras().values())
            def prime(cam):
                if account.is_empty(cam):
                    return
                try:
                    account.aspect(cam)
                    account.snapshot(cam)
                except Exception:
                    pass                       # e.g. an empty channel
            with ThreadPoolExecutor(max_workers=6) as pool:
                list(pool.map(prime, cams))
        except Exception as e:
            log.warning('warm-up: %s', e)
        log.info('warm-up finished in %.1f s', time.monotonic() - t0)

    async def _on_start(app):
        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(max_workers=32, thread_name_prefix='hikweb'))
        app['warm'] = asyncio.get_running_loop().run_in_executor(None, _warm_up)

    async def _on_cleanup(app):
        await asyncio.to_thread(app['sessions'].shutdown)

    app.on_startup.append(_on_start)
    app.on_cleanup.append(_on_cleanup)
    app.router.add_get('/', index)
    app.router.add_static('/static', STATIC, follow_symlinks=False)
    app.router.add_get('/api/cameras', api_cameras)
    app.router.add_get('/api/snapshot/{key}', api_snapshot)
    app.router.add_post('/api/live', api_live)
    app.router.add_get('/api/records', api_records)
    app.router.add_post('/api/playback', api_playback)
    app.router.add_post('/api/clips', api_clip_start)
    app.router.add_get('/api/clips/{cid}', api_clip_status)
    app.router.add_get('/api/clips/{cid}/file', api_clip_file)
    app.router.add_post('/api/verify-code', api_verify_code)
    app.router.add_post('/api/session/{sid}/touch', api_session_touch)
    app.router.add_delete('/api/session/{sid}', api_session_close)
    app.router.add_get('/ws/live/{sid}', ws_live)
    app.router.add_get('/hls/{sid}/{name}', hls_file)
    return app


def main(argv=None):
    ap = argparse.ArgumentParser(prog='hik_web', description=__doc__.split('\n\n')[0])
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--config', help='config file (default ~/.hikvision_monitor.json)')
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    app = make_app(Account(args.config))
    if args.host not in ('127.0.0.1', 'localhost', '::1') and not app['password']:
        log.warning('Listening on %s without HIK_WEB_PASSWORD — anyone who can '
                    'reach this port can watch your cameras', args.host)
    web.run_app(app, host=args.host, port=args.port, print=lambda *_: None,
                access_log=None)
    return 0
