'use strict';

/* ── helpers ─────────────────────────────────────────────────────────────── */
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const pad = n => String(n).padStart(2, '0');
const fmtClock = d => `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
const fmtDate = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const iso = d => `${fmtDate(d)}T${fmtClock(d)}`;
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const fmtOsd = d => `${pad(d.getDate())} ${MONTHS[d.getMonth()]} ${d.getFullYear()}  ${fmtClock(d)}`;

const store = {
  get(k, d) { try { const v = JSON.parse(localStorage.getItem(k)); return v ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ } },
};

async function api(path, body, method) {
  const init = body === undefined ? (method ? { method } : {}) : {
    method: method || 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  };
  const r = await fetch(path, init);
  let data = null;
  try { data = await r.json(); } catch { /* not JSON */ }
  if (!r.ok) {
    throw Object.assign(new Error((data && data.error) || r.statusText || 'Request failed'),
      { status: r.status, data: data || {} });
  }
  return data;
}

let toastTimer;
function toast(msg) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 3200);
}

/** Turn relay/server errors into something a person can act on. */
function friendly(e) {
  const m = (e && e.message) || String(e);
  if (e && e.data && e.data.auth) { checkStatus(); return 'The server cannot sign in to Hik-Connect.'; }
  if (/6106/.test(m)) return 'No camera is connected to this channel.';
  if (/No recording/i.test(m)) return 'There is no recording at that time.';
  if (/offline/i.test(m)) return 'The device is offline.';
  return m;
}

function hlsError(d) {
  try {
    const body = JSON.parse(d.response && d.response.text);
    if (body && body.error) return Object.assign(new Error(body.error), { data: body, status: d.response.code });
  } catch { /* ignore */ }
  if (d.response && d.response.code === 404) return new Error('The stream ended.');
  return new Error('The video connection dropped.');
}

function newHls(extra) {
  return new Hls({
    lowLatencyMode: false, enableWorker: true, backBufferLength: 20,
    manifestLoadingMaxRetry: 1, levelLoadingMaxRetry: 2, fragLoadingMaxRetry: 3, ...extra,
  });
}

/** Save the picture currently shown in a <video> as a JPEG download. */
function saveVideoFrame(video, baseName, when) {
  if (!video.videoWidth) { toast('Nothing to save yet.'); return; }
  const c = document.createElement('canvas');
  c.width = video.videoWidth; c.height = video.videoHeight;
  c.getContext('2d').drawImage(video, 0, 0);
  c.toBlob(b => {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(b);
    a.download = `${baseName.replace(/[^\w.-]+/g, '_')}_${fmtDate(when)}_${fmtClock(when).replace(/:/g, '-')}.jpg`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  }, 'image/jpeg', 0.92);
}

/* ── digital zoom: mouse wheel zooms in at the pointer, drag moves ───────── */
class Zoom {
  /** box: the element that clips the picture; video: the element that is scaled.
   *  enabled(): whether zooming is allowed right now. onChange(zoomed) is told
   *  when the picture goes from whole to zoomed or back. */
  constructor(box, video, { enabled = () => true, onChange = () => {} } = {}) {
    Object.assign(this, { box, video, enabled, onChange });
    this.s = 1; this.x = 0; this.y = 0;
    this.moved = false;
    this.reset_ = document.createElement('button');
    this.reset_.type = 'button';
    this.reset_.className = 'zoom-reset';
    this.reset_.hidden = true;
    this.reset_.addEventListener('click', ev => { ev.stopPropagation(); this.reset(); });
    this.reset_.addEventListener('pointerdown', ev => ev.stopPropagation());
    box.append(this.reset_);
    box.addEventListener('wheel', ev => this.onWheel(ev), { passive: false });
    box.addEventListener('pointerdown', ev => this.onDown(ev));
    box.addEventListener('dblclick', ev => { if (this.s > 1) { ev.stopPropagation(); ev.preventDefault(); this.reset(); } }, true);
    // A drag that moved the picture is not a click (a click on a live tile changes the layout).
    box.addEventListener('click', ev => { if (this.moved) { ev.stopPropagation(); ev.preventDefault(); this.moved = false; } }, true);
  }

  get zoomed() { return this.s > 1; }

  onWheel(ev) {
    if (!this.enabled() || ev.ctrlKey) return;      // ctrl+wheel stays the browser's page zoom
    ev.preventDefault();
    const r = this.box.getBoundingClientRect();
    const px = ev.clientX - r.left, py = ev.clientY - r.top;
    const step = Math.exp(-Math.sign(ev.deltaY) * Math.min(Math.abs(ev.deltaY), 120) / 400);
    this.zoomAt(px, py, this.s * step);
  }

  zoomAt(px, py, s) {
    s = Math.min(8, Math.max(1, s));
    this.x = px - (px - this.x) * (s / this.s);
    this.y = py - (py - this.y) * (s / this.s);
    this.set(s);
  }

  onDown(ev) {
    if (this.s <= 1 || ev.button !== 0) return;
    const sx = ev.clientX, sy = ev.clientY, ox = this.x, oy = this.y;
    let dragging = false;
    const move = e => {
      if (!dragging && Math.hypot(e.clientX - sx, e.clientY - sy) < 4) return;
      dragging = true; this.moved = true;
      this.box.classList.add('panning');
      this.x = ox + e.clientX - sx; this.y = oy + e.clientY - sy;
      this.set(this.s);
    };
    const up = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      this.box.classList.remove('panning');
      if (!dragging) this.moved = false;
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  }

  set(s) {
    const W = this.box.clientWidth, H = this.box.clientHeight;
    const was = this.s > 1;
    this.s = s;
    this.x = Math.min(0, Math.max(W * (1 - s), this.x));       // keep the picture covering the box
    this.y = Math.min(0, Math.max(H * (1 - s), this.y));
    if (s <= 1) { this.x = this.y = 0; }
    this.video.style.transformOrigin = '0 0';
    this.video.style.transform = s > 1 ? `translate(${this.x}px, ${this.y}px) scale(${s})` : '';
    this.box.classList.toggle('zoomed', s > 1);
    this.reset_.hidden = s <= 1;
    this.reset_.textContent = `Reset zoom (${s.toFixed(1)}×)`;
    if (was !== s > 1) this.onChange(s > 1);
  }

  reset() { this.set(1); }
}

/* ── verification code dialog (one prompt per device) ────────────────────── */
const codePrompts = new Map();
function askCode(serial, device, wrong) {
  if (codePrompts.has(serial)) return codePrompts.get(serial);
  const dlg = $('#code-dialog'), form = $('#code-form'), input = $('#code-input'), err = $('#code-error');
  $('#code-title').textContent = wrong ? 'That code did not work' : 'Verification code needed';
  $('#code-text').textContent = `${device ? `The device “${device}”` : 'This device'} encrypts its video. ` +
    'Enter its verification code (on the device label, or the one you set in the Hik-Connect app). ' +
    'It is saved on the server.';
  input.value = ''; err.hidden = true;
  const p = new Promise(resolve => {
    const done = ok => { form.removeEventListener('submit', onSubmit); dlg.removeEventListener('close', onClose); resolve(ok); };
    const onClose = () => { if (dlg.returnValue !== 'ok') done(false); };
    const onSubmit = async ev => {
      if (ev.submitter && ev.submitter.value === 'cancel') return;
      ev.preventDefault();
      const btn = $('#code-ok'); btn.disabled = true;
      try {
        await api('/api/verify-code', { serial, code: input.value.trim() });
        dlg.returnValue = 'ok'; dlg.close('ok'); done(true);
      } catch (e) {
        err.textContent = friendly(e); err.hidden = false;
      } finally { btn.disabled = false; }
    };
    form.addEventListener('submit', onSubmit);
    dlg.addEventListener('close', onClose);
  });
  codePrompts.set(serial, p);
  p.finally(() => codePrompts.delete(serial));
  dlg.returnValue = '';
  dlg.showModal();
  input.focus();
  return p;
}

/* ── low-delay player: fragmented MP4 over a WebSocket into Media Source ──── */
// Falls back to HLS (hls.js / native) where Media Source is missing, or with ?hls=1.
const USE_MSE = !!window.MediaSource && !/[?&]hls=1/.test(location.search) &&
  MediaSource.isTypeSupported('video/mp4; codecs="avc1.640028"');

/* Live sound: the camera's G.711 (8 kHz) packets, played with Web Audio and held
   back by about as long as the video is behind, so picture and sound line up. */
const ULAW = new Float32Array(256), ALAW = new Float32Array(256);
for (let i = 0; i < 256; i++) {
  let u = ~i & 0xFF, e = (u >> 4) & 7, m = u & 0x0F;
  let v = (((m << 3) + 0x84) << e) - 0x84;
  ULAW[i] = ((u & 0x80) ? -v : v) / 32768;
  let a = i ^ 0x55; e = (a >> 4) & 7; m = a & 0x0F;
  v = e === 0 ? (m << 4) + 8 : ((m << 4) + 0x108) << (e - 1);
  ALAW[i] = ((a & 0x80) ? v : -v) / 32768;
}

class LiveSound {
  constructor() {
    const AC = window.AudioContext || window.webkitAudioContext;
    this.ctx = new AC();
    this.gain = this.ctx.createGain();
    this.gain.connect(this.ctx.destination);
    this.next = 0;
    this.delay = 1.2;                         // seconds; follows the video's lag
    this.got = 0;
  }

  /** The video is `lag` seconds behind real time. */
  setLag(lag) { this.delay += (Math.min(4, Math.max(0.25, lag)) - this.delay) * 0.3; }

  push(buf) {
    // buf: 'AUD', payload type, samples
    const bytes = new Uint8Array(buf, 4), table = new Uint8Array(buf, 3, 1)[0] === 8 ? ALAW : ULAW;
    if (!bytes.length) return;
    this.got++;
    const ab = this.ctx.createBuffer(1, bytes.length, 8000), ch = ab.getChannelData(0);
    for (let i = 0; i < bytes.length; i++) ch[i] = table[bytes[i]];
    const now = this.ctx.currentTime, target = now + this.delay;
    // Re-anchor after a gap, or when the schedule has drifted from the video.
    if (this.next < now + 0.02 || Math.abs(this.next - target) > 0.3) this.next = target;
    const src = this.ctx.createBufferSource();
    src.buffer = ab; src.connect(this.gain); src.start(this.next);
    this.next += ab.duration;
  }

  close() { try { this.ctx.close(); } catch { /* already closed */ } }
}

const isAudio = buf => buf.byteLength > 4 && (() => { const b = new Uint8Array(buf, 0, 3); return b[0] === 65 && b[1] === 85 && b[2] === 68; })();

class MsePlayer {
  constructor(video, path, onError) {
    this.video = video;
    this.onError = onError;
    this.sound = null;
    this.queue = [];
    this.sb = null;
    this.dead = false;
    this.started = false;
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    this.ws = new WebSocket(`${proto}//${location.host}${path}`);
    this.ws.binaryType = 'arraybuffer';
    this.ws.onopen = () => { if (this.sound) this.ws.send(JSON.stringify({ audio: true })); };
    this.ws.onmessage = ev => this.onMessage(ev);
    this.ws.onclose = () => this.fatal(new Error('The video connection dropped.'));
  }

  onMessage(ev) {
    if (this.dead) return;
    if (typeof ev.data === 'string') {
      const m = JSON.parse(ev.data);
      if (m.error) this.fatal(Object.assign(new Error(m.error), { data: m }));
      else this.open(m.mime);
      return;
    }
    if (isAudio(ev.data)) { if (this.sound) this.sound.push(ev.data); return; }
    this.queue.push(ev.data);
    // A hidden or busy tab can fall behind. Every fragment starts with a keyframe,
    // so dropping the old ones only makes the picture jump forward.
    if (this.queue.length > 8) this.queue.splice(0, this.queue.length - 2);
    this.pump();
  }

  open(mime) {
    const ms = this.ms = new MediaSource();
    this.url = URL.createObjectURL(ms);
    this.video.src = this.url;
    ms.addEventListener('sourceopen', () => {
      if (this.sb || this.dead) return;
      try { this.sb = ms.addSourceBuffer(mime); } catch (e) { this.fatal(e); return; }
      this.sb.mode = 'sequence';             // play fragments back to back, whatever their timestamps
      this.sb.addEventListener('updateend', () => this.pump());
      this.sb.addEventListener('error', () => this.fatal(new Error('The video could not be decoded.')));
      this.pump();
    }, { once: true });
  }

  pump() {
    const sb = this.sb, v = this.video;
    if (!sb || sb.updating || this.dead) return;
    if (v.buffered.length) {
      const start = v.buffered.start(0), end = v.buffered.end(v.buffered.length - 1);
      if (v.currentTime - start > 12) {      // keep memory bounded
        try { sb.remove(start, v.currentTime - 4); return; } catch { /* retry next time */ }
      }
      // Stay near the live edge without visible jumps: speed up a little when a second or
      // more behind, and only skip ahead after a real stall (a hidden tab, a network gap).
      const ahead = end - v.currentTime;
      if (ahead > 3 && !v.paused) v.currentTime = end - 0.5;
      else v.playbackRate = ahead > 1.8 ? 1.08 : 1;
      // How far the picture is behind the sound: a fragment is about a second of
      // frames sent once complete, so its first frame arrives that old and then
      // waits behind what is buffered: (end - 1 s) - now + 1 s, plus encoding.
      if (this.sound) this.sound.setLag(Math.min(ahead, 3) + 0.2);
    }
    const next = this.queue.shift();
    if (!next) return;
    try {
      sb.appendBuffer(next);
    } catch (e) {
      if (e.name === 'QuotaExceededError') this.queue.unshift(next); else this.fatal(e);
      return;
    }
    if (!this.started) { this.started = true; v.play().catch(() => {}); }
  }

  /** Turn the camera's sound on or off (call from a click: browsers only start audio then). */
  setSound(on) {
    if (on && !this.sound) {
      this.sound = new LiveSound();
      if (this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({ audio: true }));
    } else if (!on && this.sound) {
      this.sound.close(); this.sound = null;
      if (this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({ audio: false }));
    }
  }

  fatal(e) {
    if (this.dead) return;
    this.destroy();
    this.onError(e);
  }

  destroy() {
    this.dead = true;
    if (this.sound) { this.sound.close(); this.sound = null; }
    try { this.ws.onclose = null; this.ws.close(); } catch { /* already closed */ }
    this.queue = [];
    if (this.url) URL.revokeObjectURL(this.url);
    const v = this.video;
    v.removeAttribute('src');
    v.load();
  }
}

/* ── live tiles ──────────────────────────────────────────────────────────── */
class Tile {
  constructor(cam) {
    this.cam = cam;
    this.quality = 'sub';
    this.sid = null;
    this.hls = null;
    this.token = 0;
    this.state = 'idle';
    this.retries = 0;
    const el = this.el = document.createElement('div');
    el.className = 'tile'; el.tabIndex = 0; el.dataset.key = cam.key; el.dataset.state = 'idle';
    el.innerHTML = `
      <img class="poster" alt="" hidden>
      <video muted playsinline></video>
      <div class="osd top"><span class="ch"></span><span class="name"></span><span class="live">LIVE</span></div>
      <div class="osd bottom"><time></time></div>
      <div class="status"><div class="spin"></div><div class="msg"></div><button class="ghost retry" hidden>Try again</button></div>
      <div class="tools">
        <button type="button" data-act="sound" aria-pressed="false" title="Hear this camera (one camera at a time)"${USE_MSE ? '' : ' hidden'}>Sound</button>
        <button type="button" data-act="frame" title="Save this frame as a picture">Save frame</button>
        <button type="button" data-act="full" title="Full screen">Full screen</button>
      </div>`;
    $('.ch', el).textContent = `CH${cam.channel}`;
    $('.name', el).textContent = cam.name;
    this.video = $('video', el);
    this.msg = $('.msg', el);
    this.retry = $('.retry', el);
    this.retry.addEventListener('click', ev => { ev.stopPropagation(); this.start(this.quality); });
    $('[data-act="frame"]', el).addEventListener('click', ev => { ev.stopPropagation(); this.saveFrame(); });
    this.soundBtn = $('[data-act="sound"]', el);
    this.soundBtn.addEventListener('click', ev => { ev.stopPropagation(); live.setSound(this.soundOn ? null : cam.key); });
    this.soundOn = false;
    $('[data-act="full"]', el).addEventListener('click', ev => { ev.stopPropagation(); this.fullscreen(); });
    el.setAttribute('aria-label', cam.name);
    this.posterLoaded = false;
    // Zoom in on the big picture (focus view or full screen), not on the small tiles.
    this.zoom = new Zoom(el, this.video, {
      enabled: () => el.classList.contains('main') || document.fullscreenElement === el,
    });
  }

  loadPoster() {
    this.posterLoaded = true;
    const img = $('.poster', this.el);
    img.onload = () => { if (this.state !== 'playing') img.hidden = false; };
    img.onerror = () => { img.hidden = true; };
    img.src = `/api/snapshot/${encodeURIComponent(this.cam.key)}`;
  }

  setState(state, message = '') {
    this.state = state;
    this.el.dataset.state = state;
    this.msg.textContent = message;
    this.retry.hidden = !(state === 'error' || state === 'locked');
    this.retry.textContent = state === 'locked' ? 'Enter code' : 'Try again';
    if (state === 'playing') { const p = $('.poster', this.el); p.hidden = true; p.classList.remove('frozen'); }
  }

  async start(quality = this.quality) {
    const token = ++this.token;
    this.freezeFrame();
    this.teardown();
    this.quality = quality;
    this.setState('connecting', 'Connecting…');
    try {
      const r = await api('/api/live', { key: this.cam.key, quality, transport: USE_MSE ? 'mse' : 'hls' });
      if (token !== this.token) return;
      this.sid = r.id;
      this.attach(r, token);
    } catch (e) {
      if (token === this.token) this.fail(e, token);
    }
  }

  /** Keep showing the last picture while a new stream (e.g. HD) connects. */
  freezeFrame() {
    const v = this.video;
    if (this.state !== 'playing' || !v.videoWidth) return;
    try {
      const c = document.createElement('canvas');
      c.width = Math.min(960, v.videoWidth);
      c.height = Math.round(c.width * v.videoHeight / v.videoWidth);
      c.getContext('2d').drawImage(v, 0, 0, c.width, c.height);
      const img = $('.poster', this.el);
      img.src = c.toDataURL('image/jpeg', 0.7);
      img.classList.add('frozen');
      img.hidden = false;
    } catch { /* keep the old poster */ }
  }

  attach(r, token) {
    const v = this.video;
    v.onplaying = () => { if (token === this.token) { this.retries = 0; this.setState('playing'); } };
    if (r.ws) {
      this.mse = new MsePlayer(v, r.ws, e => { if (token === this.token) this.fail(e, token); });
      if (this.soundOn) this.mse.setSound(true);
    } else if (window.Hls && Hls.isSupported()) {
      const h = this.hls = newHls({ liveSyncDurationCount: 2, liveMaxLatencyDurationCount: 6, maxBufferLength: 8 });
      h.on(Hls.Events.MANIFEST_PARSED, () => v.play().catch(() => {}));
      h.on(Hls.Events.ERROR, (_, d) => { if (d.fatal && token === this.token) this.fail(hlsError(d), token); });
      h.loadSource(r.url);
      h.attachMedia(v);
    } else if (v.canPlayType('application/vnd.apple.mpegurl')) {
      v.src = r.url;
      v.play().catch(() => {});
    } else {
      this.fail(new Error('This browser cannot play the video.'), token);
    }
  }

  async fail(e, token) {
    this.teardown();
    const d = e.data || {};
    if (d.needs_code) {
      this.setState('locked', 'This device needs its verification code.');
      const ok = await askCode(d.serial, d.device, d.wrong);
      if (ok && token === this.token) this.start(this.quality);
      return;
    }
    if (/6106/.test(e.message || '')) live.markEmpty(this.cam.key);
    else if (this.retries < 3 && token === this.token) {
      // A dropped connection (server restart, idle stream closed as we joined, a hiccup
      // at the relay): try again a few times before showing an error.
      this.retries++;
      this.setState('connecting', 'Reconnecting…');
      setTimeout(() => { if (token === this.token) this.start(this.quality); }, 700 * this.retries);
      return;
    }
    this.setState('error', friendly(e));
  }

  teardown() {
    if (this.mse) { this.mse.destroy(); this.mse = null; }
    if (this.hls) { this.hls.destroy(); this.hls = null; }
    this.sid = null;
    const v = this.video;
    v.onplaying = null;
    v.removeAttribute('src');
    v.load();
  }

  stop() {
    this.token++;
    this.teardown();
    this.setState('idle');
    const p = $('.poster', this.el);
    p.classList.remove('frozen');
    if (p.getAttribute('src') && p.src.startsWith('data:')) this.loadPoster();   // back to the real snapshot
  }

  saveFrame() {
    saveVideoFrame(this.video, this.cam.name, new Date());
  }

  setSound(on) {
    this.soundOn = on;
    this.soundBtn.setAttribute('aria-pressed', String(on));
    this.soundBtn.textContent = on ? 'Sound on' : 'Sound';
    this.el.classList.toggle('sound', on);
    if (this.mse) this.mse.setSound(on);
    clearTimeout(this.soundCheck);
    if (on) {
      // Some cameras have no microphone: say so instead of staying silent.
      this.soundCheck = setTimeout(() => {
        const snd = this.mse && this.mse.sound;
        if (this.soundOn && this.state === 'playing' && snd && !snd.got) toast(`${this.cam.name} is not sending any sound.`);
      }, 5000);
    }
  }

  fullscreen() {
    const el = this.el;
    if (el.requestFullscreen) el.requestFullscreen().catch(() => {});
    else if (this.video.webkitEnterFullscreen) this.video.webkitEnterFullscreen();
  }
}

/* ── live view: sidebar, grid, focus ─────────────────────────────────────── */
const live = {
  cams: [],
  hidden: new Set(store.get('hik.hidden', [])),
  empty: new Set(store.get('hik.empty', [])),
  layout: store.get('hik.layout', 'grid'),
  profile: store.get('hik.profile', 'fast'),     // 'fast': sub-streams everywhere · 'quality': full HD in the focus view
  focus: null,
  tiles: new Map(),
  active: false,

  async init() {
    try {
      this.cams = await api('/api/cameras');
    } catch (e) {
      $('#live-empty').textContent = `Could not load cameras: ${friendly(e)}`;
      $('#live-empty').hidden = false;
      return;
    }
    for (const cam of this.cams) this.tiles.set(cam.key, new Tile(cam));
    for (const cam of this.cams) if (cam.empty) this.markEmpty(cam.key, true);     // the server already knows
    this.renderSide();
    this.render();
    rec.init();
  },

  visible() { return this.cams.filter(c => !this.hidden.has(c.key)); },

  renderSide() {
    const ul = $('#cams');
    ul.replaceChildren();
    for (const cam of this.cams) {
      const li = document.createElement('li');
      const b = document.createElement('button');
      b.className = 'cam'; b.type = 'button';
      b.setAttribute('aria-pressed', String(!this.hidden.has(cam.key)));
      b.innerHTML = '<span class="dot"></span><span class="name"></span><span class="ch"></span>';
      $('.name', b).textContent = cam.name;
      $('.ch', b).textContent = this.empty.has(cam.key) ? 'no camera' : `CH${cam.channel}`;
      b.addEventListener('click', () => this.toggle(cam.key));
      li.append(b); ul.append(li);
    }
    $('#side-note').textContent = this.cams.length ? 'Cameras that are turned off do not stream.' : '';
  },

  /** A channel the relay says has no camera: stop showing it (it can be turned back on). */
  markEmpty(key, quiet = false) {
    if (this.empty.has(key)) return;
    this.empty.add(key);
    store.set('hik.empty', [...this.empty]);
    if (!this.hidden.has(key)) {
      this.hidden.add(key);
      store.set('hik.hidden', [...this.hidden]);
      const cam = this.cams.find(c => c.key === key);
      if (!quiet) toast(`${cam ? cam.name : 'A channel'} has no camera connected, so it was turned off.`);
      if (!quiet) { this.renderSide(); this.render(); }
    }
  },

  toggle(key) {
    if (this.hidden.has(key)) this.hidden.delete(key); else this.hidden.add(key);
    store.set('hik.hidden', [...this.hidden]);
    this.renderSide();
    this.render();
  },

  setLayout(layout, focus) {
    this.layout = layout;
    if (focus !== undefined) this.focus = focus;
    store.set('hik.layout', layout);
    $$('#layout button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.layout === layout)));
    this.render();
  },

  render() {
    const grid = $('#grid');
    const vis = this.visible();
    $('#live-empty').hidden = vis.length > 0 || !this.cams.length;
    if (this.layout === 'focus' && !vis.some(c => c.key === this.focus)) this.focus = vis.length ? vis[0].key : null;
    const focusing = this.layout === 'focus' && this.focus;
    grid.classList.toggle('focus', !!focusing);
    const n = vis.length;
    const cols = n <= 1 ? 1 : n <= 4 ? 2 : n <= 9 ? 3 : 4;
    grid.style.setProperty('--cols', cols);
    grid.style.setProperty('--rows', Math.max(1, Math.ceil(n / cols)));
    grid.style.setProperty('--others', Math.max(1, n - 1));

    const ordered = focusing
      ? [vis.find(c => c.key === this.focus), ...vis.filter(c => c.key !== this.focus)]
      : vis;
    grid.replaceChildren(...ordered.map(c => this.tiles.get(c.key).el));
    this.fit();
    for (const cam of this.cams) {
      const t = this.tiles.get(cam.key);
      const shown = !this.hidden.has(cam.key);
      const main = focusing && cam.key === this.focus;
      t.el.classList.toggle('main', main);
      if (!main && t.zoom.zoomed && document.fullscreenElement !== t.el) t.zoom.reset();
      t.el.onclick = () => this.tileClicked(cam.key);
      t.el.onkeydown = ev => { if (ev.key === 'Enter' && ev.target === t.el) this.tileClicked(cam.key); };
      if (!shown) { if (t.soundOn) this.setSound(null); if (t.state !== 'idle') t.stop(); continue; }
      if (!t.posterLoaded) t.loadPoster();
      const q = main && this.profile === 'quality' ? 'main' : 'sub';
      if (this.active && (t.state === 'idle' || t.quality !== q)) t.start(q);
    }
  },

  /** Grid layout: pick the column count that gives the biggest 16:9 tiles for this window. */
  fit() {
    const grid = $('#grid');
    const n = this.visible().length;
    const narrow = matchMedia('(max-width: 820px)').matches;
    const clear = () => ['gridTemplateColumns', 'gridTemplateRows', 'justifyContent', 'alignContent']
      .forEach(k => { grid.style[k] = ''; });
    if (grid.classList.contains('focus') || narrow || !n) { clear(); return; }
    const gap = 6, W = grid.clientWidth, H = grid.clientHeight;
    if (!W || !H) return;
    let best = null;
    for (let c = 1; c <= n; c++) {
      const r = Math.ceil(n / c);
      const w = Math.floor(Math.min((W - gap * (c - 1)) / c, ((H - gap * (r - 1)) / r) * 16 / 9));
      if (w > 0 && (!best || w > best.w)) best = { c, r, w };
    }
    if (!best) return;
    const h = Math.round(best.w * 9 / 16);
    grid.style.gridTemplateColumns = `repeat(${best.c}, ${best.w}px)`;
    grid.style.gridTemplateRows = `repeat(${best.r}, ${h}px)`;
    grid.style.justifyContent = 'center';
    grid.style.alignContent = 'center';
  },

  setProfile(profile) {
    this.profile = profile;
    store.set('hik.profile', profile);
    $$('#profile button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.profile === profile)));
    this.render();
  },

  tileClicked(key) {
    if (this.layout === 'focus' && this.focus === key) this.setLayout('grid');
    else this.setLayout('focus', key);
  },

  start() { this.active = true; this.render(); },
  stop() { this.active = false; this.setSound(null); this.tiles.forEach(t => { if (t.state !== 'idle') t.stop(); }); },

  /** Sound from one camera at a time (key), or none (null). */
  soundKey: null,
  setSound(key) {
    this.soundKey = key;
    this.tiles.forEach((t, k) => { if (t.soundOn !== (k === key)) t.setSound(k === key); });
  },

  keepAlive() {
    this.tiles.forEach(t => {
      if (!t.sid) return;
      fetch(`/api/session/${t.sid}/touch`, { method: 'POST' })
        .then(r => { if (r.status === 404 && t.sid) t.start(t.quality); })
        .catch(() => {});
    });
  },
};

/* ── recordings ──────────────────────────────────────────────────────────── */
const DAY_MS = 86400000;
const SPAN_MS = 15 * 60 * 1000;
const GAP_MIN_MS = 60 * 1000;        // shorter breaks are normal (the NVR splits files)
const rec = {
  key: null, date: null, blocks: [], serverNow: null,
  cursor: null, speed: 1, tok: 0,
  sid: null, hls: null, spanBegin: null, spanEnd: null, blockEnd: null,
  clipStart: null, clipEnd: null, clipBusy: false,
  loaded: false, zoom: store.get('hik.zoom', 24), winStart: null,

  init() {
    const sel = $('#rec-cam');
    sel.replaceChildren(...live.cams.map(c => new Option(`${c.name}  (CH${c.channel})`, c.key)));
    const saved = store.get('hik.recCam', null);
    this.key = live.cams.some(c => c.key === saved) ? saved : (live.cams[0] && live.cams[0].key);
    sel.value = this.key || '';
    this.date = fmtDate(new Date());
    $('#rec-date').value = this.date;
    sel.addEventListener('change', () => { this.key = sel.value; store.set('hik.recCam', this.key); this.reset(); });
    $('#rec-date').addEventListener('change', ev => { if (ev.target.value) { this.date = ev.target.value; this.reset(); } });
    $('#day-prev').addEventListener('click', () => this.shiftDay(-1));
    $('#day-next').addEventListener('click', () => this.shiftDay(1));
    $$('#speed button').forEach(b => b.addEventListener('click', () => this.setSpeed(Number(b.dataset.speed))));
    $$('button[data-skip]').forEach(b => b.addEventListener('click', () => this.skip(Number(b.dataset.skip))));
    $$('#zoom button').forEach(b => b.addEventListener('click', () => this.setZoom(Number(b.dataset.zoom))));
    $('#pan-left').addEventListener('click', () => this.pan(-1));
    $('#pan-right').addEventListener('click', () => this.pan(1));
    $('#day-start').addEventListener('click', () => this.dayStartJump());
    $('#latest').addEventListener('click', () => this.latest());
    $('#save-frame').addEventListener('click', () => this.saveFrame());
    $('#bm-add').addEventListener('click', () => this.addBookmark());
    $('#bm-form').addEventListener('submit', ev => { ev.preventDefault(); this.saveBookmark(); });
    $('#bm-cancel').addEventListener('click', () => { $('#bm-form').hidden = true; });
    $('#goto').addEventListener('submit', ev => { ev.preventDefault(); this.gotoTime($('#goto-time').value); });
    $$('#zoom button').forEach(b => b.setAttribute('aria-pressed', String(Number(b.dataset.zoom) === this.zoom)));
    this.initKeys();
    this.initTimeline();
    this.initClip();
    const v = $('#pb-video');
    v.addEventListener('timeupdate', () => this.onTime());
    v.addEventListener('ended', () => this.onEnded());
    v.addEventListener('loadedmetadata', () => { v.playbackRate = this.speed; });
    v.addEventListener('playing', () => { v.playbackRate = this.speed; this.message(''); });
    this.zoomer = new Zoom($('#player'), v, { onChange: z => { v.controls = !z; } });
    this.renderTicks();
  },

  get dayStart() { return new Date(`${this.date}T00:00:00`); },

  message(text, isError = false) {
    const m = $('#pb-msg');
    m.textContent = text;
    m.classList.toggle('err', isError);
  },

  reset() {
    this.stopSpan();
    if (this.zoomer) this.zoomer.reset();
    this.cursor = null;
    this.clipStart = this.clipEnd = null;
    $('#clip-start').value = $('#clip-end').value = '';
    this.message('Pick a time on the timeline to start playing.');
    this.loadDay();
  },

  shiftDay(n) {
    const d = this.dayStart; d.setDate(d.getDate() + n);
    this.date = fmtDate(d);
    $('#rec-date').value = this.date;
    this.reset();
  },

  async loadDay() {
    if (!this.key) return;
    const key = this.key, date = this.date;
    this.blocks = [];
    this.renderTimeline();
    this.loadBookmarks();
    try {
      const r = await api(`/api/records?key=${encodeURIComponent(key)}&date=${date}`);
      if (key !== this.key || date !== this.date) return;
      this.serverNow = new Date(r.now);
      // Join recordings that touch each other into one continuous block.
      const blocks = [];
      for (const x of r.records) {
        const b = new Date(x.begin), e = new Date(x.end);
        const last = blocks[blocks.length - 1];
        if (last && b - last.e <= 3000) last.e = e > last.e ? e : last.e; else blocks.push({ b, e });
      }
      this.blocks = blocks;
      this.gaps = this.findGaps(blocks);
      this.loaded = true;
      if (!blocks.length) this.message('No recordings on this day.');
    } catch (e) {
      this.message(friendly(e), true);
    }
    this.renderTimeline();
    this.renderGaps();
  },

  /* gaps: stretches inside the recorded part of the day with no footage */
  gaps: [],
  findGaps(blocks) {
    const out = [];
    for (let i = 1; i < blocks.length; i++) {
      const b = blocks[i - 1].e, e = blocks[i].b;
      if (e - b >= GAP_MIN_MS) out.push({ b, e });
    }
    return out;
  },

  renderGaps() {
    const el = $('#tl-gaps');
    el.replaceChildren();
    if (!this.blocks.length) return;
    const n = this.gaps.length;
    const dur = ms => { const m = Math.round(ms / 60000); return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${pad(m % 60)} min`; };
    const hm = d => `${pad(d.getHours())}:${pad(d.getMinutes())}`;
    if (!n) { el.textContent = 'No gaps in the recording on this day.'; return; }
    const total = this.gaps.reduce((t, g) => t + (g.e - g.b), 0);
    el.append(`${n === 1 ? 'One gap' : `${n} gaps`} in the recording (${dur(total)} missing): `);
    this.gaps.slice(0, 12).forEach((g, i) => {
      const b = document.createElement('button');
      b.type = 'button'; b.className = 'link';
      b.textContent = `${hm(g.b)}–${hm(g.e)}`;
      b.title = `No recording for ${dur(g.e - g.b)}. Click to play from just before it.`;
      b.addEventListener('click', () => this.playAt(new Date(Math.max(this.dayStart.getTime(), g.b.getTime() - 20000))));
      if (i) el.append(', ');
      el.append(b);
    });
    if (n > 12) el.append(`, and ${n - 12} more`);
  },

  /* bookmarks */
  bookmarks: [],
  async loadBookmarks() {
    const key = this.key;
    try {
      const list = await api(`/api/bookmarks?key=${encodeURIComponent(key)}`);
      if (key !== this.key) return;
      this.bookmarks = list.map(b => ({ ...b, t: new Date(b.time) }));
    } catch { this.bookmarks = []; }
    this.renderBookmarks();
    this.renderTimeline();
  },

  addBookmark() {
    if (!this.cursor) { toast('Start playing first, then bookmark the moment.'); return; }
    this.bmTime = new Date(this.cursor.getTime());
    $('#bm-when').textContent = `${this.date} ${fmtClock(this.bmTime)}`;
    $('#bm-note').value = '';
    $('#bm-form').hidden = false;
    $('#bm-note').focus();
  },

  async saveBookmark() {
    if (!this.bmTime) return;
    try {
      await api('/api/bookmarks', { key: this.key, time: iso(this.bmTime), note: $('#bm-note').value.trim() });
      $('#bm-form').hidden = true;
      toast(`Bookmarked ${fmtClock(this.bmTime)}.`);
      this.loadBookmarks();
    } catch (e) { toast(friendly(e)); }
  },

  async deleteBookmark(b) {
    try {
      await api(`/api/bookmarks/${b.id}`, undefined, 'DELETE');
      this.loadBookmarks();
    } catch (e) { toast(friendly(e)); }
  },

  /** Play a bookmark, switching to its day first if needed. */
  async gotoBookmark(b) {
    if (fmtDate(b.t) !== this.date) {
      this.date = fmtDate(b.t);
      $('#rec-date').value = this.date;
      this.stopSpan();
      this.cursor = null;
      await this.loadDay();
    }
    this.playAt(new Date(Math.max(this.dayStart.getTime(), b.t.getTime() - 5000)));
  },

  renderBookmarks() {
    const ul = $('#bm-list');
    ul.replaceChildren();
    $('#bm-empty').hidden = this.bookmarks.length > 0;
    for (const b of [...this.bookmarks].reverse()) {
      const li = document.createElement('li');
      const go = document.createElement('button');
      go.type = 'button'; go.className = 'bm-go';
      go.innerHTML = '<time></time><span class="note"></span>';
      $('time', go).textContent = `${fmtDate(b.t) === fmtDate(new Date()) ? 'Today' : fmtDate(b.t)} ${fmtClock(b.t)}`;
      $('.note', go).textContent = b.note || 'No note';
      if (!b.note) $('.note', go).classList.add('muted');
      go.title = 'Play from 5 seconds before this moment';
      go.addEventListener('click', () => this.gotoBookmark(b));
      const del = document.createElement('button');
      del.type = 'button'; del.className = 'icon bm-del'; del.textContent = '×';
      del.setAttribute('aria-label', `Delete the bookmark at ${fmtClock(b.t)}`);
      del.addEventListener('click', () => this.deleteBookmark(b));
      li.append(go, del);
      ul.append(li);
    }
  },

  enter() {
    if (!this.loaded) this.loadDay(); else this.renderTimeline();
  },

  /* timeline: the visible window is the whole day, or 6 h / 1 h around the playhead */
  get win() {
    const ds = this.dayStart.getTime(), len = Math.min(DAY_MS, this.zoom * 3600000);
    if (len >= DAY_MS) return { s: new Date(ds), len: DAY_MS };
    const start = Math.min(Math.max((this.winStart || this.dayStart).getTime(), ds), ds + DAY_MS - len);
    return { s: new Date(start), len };
  },

  frac(d) { const w = this.win; return Math.min(1, Math.max(0, (d - w.s) / w.len)); },

  setZoom(z) {
    this.zoom = z;
    store.set('hik.zoom', z);
    $$('#zoom button').forEach(b => b.setAttribute('aria-pressed', String(Number(b.dataset.zoom) === z)));
    const c = this.cursor || this.serverNow || this.dayStart;
    this.winStart = new Date(c.getTime() - Math.min(DAY_MS, z * 3600000) / 2);
    this.renderTimeline();
  },

  pan(dir) {
    if (this.zoom >= 24) return;
    const w = this.win;
    this.winStart = new Date(w.s.getTime() + dir * w.len / 2);
    this.renderTimeline();
  },

  /** Keep the playhead inside a zoomed window while playing. True if the window moved. */
  follow() {
    if (this.zoom >= 24 || !this.cursor) return false;
    const w = this.win, f = (this.cursor - w.s) / w.len;
    if (f > 0.1 && f < 0.9) return false;
    const ds = this.dayStart.getTime();
    const next = Math.min(Math.max(this.cursor.getTime() - w.len / 2, ds), ds + DAY_MS - w.len);
    if (next === w.s.getTime()) return false;          // already as centred as the day allows
    this.winStart = new Date(next);
    return true;
  },

  renderTicks() {
    const t = $('#tl-ticks');
    t.replaceChildren();
    const w = this.win, step = this.zoom >= 24 ? 3 * 3600000 : this.zoom >= 6 ? 3600000 : 600000;
    const ds = this.dayStart.getTime();
    const first = ds + Math.ceil((w.s.getTime() - ds) / step) * step;       // aligned to local clock time
    for (let ms = first; ms <= w.s.getTime() + w.len; ms += step) {
      const d = new Date(ms), f = (ms - w.s.getTime()) / w.len;
      const s = document.createElement('span');
      s.style.left = `${f * 100}%`;
      s.textContent = ms - this.dayStart.getTime() >= DAY_MS ? '24:00' : `${pad(d.getHours())}:${pad(d.getMinutes())}`;
      t.append(s);
    }
  },

  renderTimeline() {
    const track = $('#tl-track');
    track.replaceChildren();
    this.renderTicks();
    const w = this.win, we = new Date(w.s.getTime() + w.len);
    const place = (el, s, e, minPct = 0) => {
      el.style.left = `${this.frac(s) * 100}%`;
      el.style.width = `${Math.max(minPct, (this.frac(e) - this.frac(s)) * 100)}%`;
      track.append(el);
    };
    for (const b of this.blocks) {
      const s = b.b < w.s ? w.s : b.b, e = b.e > we ? we : b.e;
      if (e <= s) continue;
      const el = document.createElement('div');
      el.className = 'span';
      place(el, s, e, 0.15);
    }
    for (const g of this.gaps) {
      const s = g.b < w.s ? w.s : g.b, e = g.e > we ? we : g.e;
      if (e <= s) continue;
      const el = document.createElement('div');
      el.className = 'gap';
      place(el, s, e, 0.15);
    }
    for (const b of this.bookmarks) {
      if (fmtDate(b.t) !== this.date || b.t < w.s || b.t > we) continue;
      const m = document.createElement('div');
      m.className = 'bm'; m.style.left = `${this.frac(b.t) * 100}%`;
      m.title = `${fmtClock(b.t)}${b.note ? ` ${b.note}` : ''}`;
      track.append(m);
    }
    if (this.clipStart && this.clipEnd && this.clipEnd > this.clipStart) {
      const s = this.clipStart < w.s ? w.s : this.clipStart, e = this.clipEnd > we ? we : this.clipEnd;
      if (e > s) { const c = document.createElement('div'); c.className = 'clip'; place(c, s, e); }
    }
    if (this.serverNow && fmtDate(this.serverNow) === this.date && this.serverNow >= w.s && this.serverNow <= we) {
      const n = document.createElement('div');
      n.className = 'now'; n.style.left = `${this.frac(this.serverNow) * 100}%`;
      track.append(n);
    }
    const hover = document.createElement('div'); hover.className = 'hover'; hover.id = 'tl-hover';
    const head = document.createElement('div'); head.className = 'head'; head.id = 'tl-head';
    track.append(hover, head);
    this.renderHead();
  },

  renderHead() {
    const c = this.cursor;
    $('#pb-clock').textContent = c ? fmtClock(c) : '--:--:--';
    $('#tl').setAttribute('aria-valuenow', c ? String(Math.round((c - this.dayStart) / 1000)) : '0');
    if (c && this.follow()) { this.renderTimeline(); return; }      // the window moved: redraw it
    const head = $('#tl-head');
    if (!head) return;
    const w = this.win;
    const inWin = c && c >= w.s && c <= new Date(w.s.getTime() + w.len);
    head.style.display = inWin ? '' : 'none';
    if (inWin) head.style.left = `${this.frac(c) * 100}%`;
  },

  timeAt(clientX) {
    const r = $('#tl-track').getBoundingClientRect(), w = this.win;
    const f = Math.min(1, Math.max(0, (clientX - r.left) / r.width));
    return new Date(w.s.getTime() + Math.floor(f * w.len / 1000) * 1000);
  },

  initTimeline() {
    const tl = $('#tl');
    const tip = document.createElement('div');
    tip.className = 'tl-tip'; tl.append(tip);
    let dragging = false;
    const show = ev => {
      const t = this.timeAt(ev.clientX);
      const r = $('#tl-track').getBoundingClientRect();
      tip.textContent = fmtClock(t);
      tip.style.left = `${ev.clientX - r.left}px`;
      tip.style.display = 'block';
      const h = $('#tl-hover');
      if (h) { h.style.display = 'block'; h.style.left = `${ev.clientX - r.left}px`; }
      return t;
    };
    const hide = () => {
      tip.style.display = 'none';
      const h = $('#tl-hover'); if (h) h.style.display = 'none';
    };
    tl.addEventListener('pointerdown', ev => { dragging = true; tl.setPointerCapture(ev.pointerId); show(ev); });
    tl.addEventListener('pointermove', ev => { if (ev.pointerType === 'mouse' || dragging) show(ev); });
    tl.addEventListener('pointerleave', () => { if (!dragging) hide(); });
    tl.addEventListener('pointerup', ev => {
      if (!dragging) return;
      dragging = false; hide();
      this.playAt(this.timeAt(ev.clientX));
    });
    tl.addEventListener('pointercancel', () => { dragging = false; hide(); });
    tl.addEventListener('keydown', ev => {
      const step = ev.shiftKey ? 300 : 30;
      if (ev.key === 'ArrowRight') { ev.preventDefault(); this.skip(step); }
      if (ev.key === 'ArrowLeft') { ev.preventDefault(); this.skip(-step); }
    });
  },

  /* playback */
  runAt(t) {
    return this.blocks.find(b => b.b <= t && t < b.e) || this.blocks.find(b => b.b > t) || null;
  },

  skip(sec) {
    const base = this.cursor || this.serverNow || new Date();
    const target = new Date(base.getTime() + sec * 1000);
    const v = $('#pb-video');
    // Inside the part of this span that has already arrived: just seek.
    if (this.sid && this.spanBegin && v.seekable.length) {
      const t = (target - this.spanBegin) / 1000;
      if (t >= 0 && t <= v.seekable.end(v.seekable.length - 1) - 0.5) { v.currentTime = t; return; }
    }
    this.playAt(target);
  },

  gotoTime(str) {
    const t = this.clipTime(str);
    if (!t || isNaN(t)) { toast('Type a time like 14:30 or 14:30:15.'); return; }
    this.playAt(t);
  },

  /** Jump to the first footage of the selected day. */
  dayStartJump() {
    const first = this.blocks.find(b => b.e > this.dayStart);
    if (!first) { toast('No recordings on this day.'); return; }
    this.playAt(new Date(Math.max(first.b.getTime(), this.dayStart.getTime())));
  },

  latest() {
    const last = this.blocks[this.blocks.length - 1];
    if (!last) { toast('No recordings on this day.'); return; }
    this.playAt(new Date(Math.max(last.b.getTime(), last.e.getTime() - 30000)));
  },

  saveFrame() {
    const cam = live.cams.find(c => c.key === this.key);
    saveVideoFrame($('#pb-video'), cam ? cam.name : 'camera', this.cursor || new Date());
  },

  initKeys() {
    document.addEventListener('keydown', ev => {
      if ($('#rec').hidden || ev.ctrlKey || ev.metaKey || ev.altKey) return;
      if (ev.target.closest && ev.target.closest('input, select, textarea, dialog')) return;
      const v = $('#pb-video');
      const speeds = $$('#speed button').map(b => Number(b.dataset.speed));
      const key = ev.key;
      const actions = {
        ' ': () => { if (this.sid) { if (v.paused) v.play(); else v.pause(); } else if (this.cursor) this.playAt(this.cursor); },
        'k': () => actions[' ']() ,
        'ArrowLeft': () => this.skip(ev.shiftKey ? -60 : -10),
        'ArrowRight': () => this.skip(ev.shiftKey ? 60 : 10),
        ',': () => { v.pause(); v.currentTime = Math.max(0, v.currentTime - 0.04); },
        '.': () => { v.pause(); v.currentTime += 0.04; },
        '[': () => { const i = speeds.indexOf(this.speed); if (i > 0) this.setSpeed(speeds[i - 1]); },
        ']': () => { const i = speeds.indexOf(this.speed); if (i < speeds.length - 1) this.setSpeed(speeds[i + 1]); },
        'g': () => $('#goto-time').focus(),
        'i': () => $('#clip-start-now').click(),
        'o': () => $('#clip-end-now').click(),
        's': () => this.saveFrame(),
        'b': () => this.addBookmark(),
        '0': () => this.zoomer.reset(),
        'f': () => { const el = $('#player'); if (document.fullscreenElement) document.exitFullscreen(); else if (el.requestFullscreen) el.requestFullscreen().catch(() => {}); },
      };
      const act = actions[key];
      if (act) { ev.preventDefault(); act(); }
    });
  },

  setSpeed(s) {
    this.speed = s;
    $$('#speed button').forEach(b => b.setAttribute('aria-pressed', String(Number(b.dataset.speed) === s)));
    if (this.sid && this.cursor) this.playAt(this.cursor);
  },

  async playAt(t, quiet = false) {
    const run = this.runAt(t);
    if (!run) { toast('There is no recording after that time.'); return; }
    if (run.b > t && !quiet && run.b - t > 3000) toast(`No recording at ${fmtClock(t)}. Playing from ${fmtClock(run.b)}.`);
    const begin = new Date(Math.max(t.getTime(), run.b.getTime(), this.dayStart.getTime()));
    const end = new Date(Math.min(run.e.getTime(), begin.getTime() + SPAN_MS));
    if (end - begin < 2000) { toast('That is the end of the recording.'); return; }
    this.cursor = begin;
    this.renderHead();
    await this.startSpan(begin, end, run.e);
  },

  async startSpan(begin, end, blockEnd, retried = false) {
    this.stopSpan();
    const tok = ++this.tok;
    this.message('Connecting…');
    let r;
    try {
      r = await api('/api/playback', { key: this.key, begin: iso(begin), end: iso(end), speed: this.speed, fast: live.profile === 'fast' });
    } catch (e) {
      if (tok !== this.tok) return;
      const d = e.data || {};
      if (d.needs_code && !retried) {
        this.message('This device needs its verification code.', true);
        if (await askCode(d.serial, d.device, d.wrong)) return this.startSpan(begin, end, blockEnd, true);
        return;
      }
      this.message(friendly(e), true);
      return;
    }
    if (tok !== this.tok) { fetch(`/api/session/${r.id}`, { method: 'DELETE' }).catch(() => {}); return; }
    this.sid = r.id; this.spanBegin = begin; this.spanEnd = end; this.blockEnd = blockEnd;
    const v = $('#pb-video');
    if (window.Hls && Hls.isSupported()) {
      const h = this.hls = newHls({ startPosition: 0, maxBufferLength: 20 });
      h.on(Hls.Events.MANIFEST_PARSED, () => v.play().catch(() => {}));
      h.on(Hls.Events.ERROR, (_, d) => { if (d.fatal && tok === this.tok) { this.stopSpan(); this.message(friendly(hlsError(d)), true); } });
      h.loadSource(r.url);
      h.attachMedia(v);
    } else {
      v.src = r.url;
      v.play().catch(() => {});
    }
  },

  stopSpan() {
    this.tok++;
    if (this.hls) { this.hls.destroy(); this.hls = null; }
    if (this.sid) fetch(`/api/session/${this.sid}`, { method: 'DELETE', keepalive: true }).catch(() => {});
    this.sid = null;
    const v = $('#pb-video');
    v.removeAttribute('src');
    v.load();
  },

  onTime() {
    if (!this.spanBegin) return;
    this.cursor = new Date(this.spanBegin.getTime() + $('#pb-video').currentTime * 1000);
    this.renderHead();
  },

  onEnded() {
    if (this.spanEnd && this.blockEnd && this.spanEnd < this.blockEnd) { this.playAt(this.spanEnd, true); return; }
    // The end of a recorded stretch: carry on after the gap, if there is more today.
    const next = this.blockEnd && this.blocks.find(b => b.b >= this.blockEnd);
    if (next) {
      const m = Math.round((next.b - this.blockEnd) / 60000);
      toast(`Skipped a gap with no recording (${fmtClock(this.blockEnd)} to ${fmtClock(next.b)}${m ? `, ${m} min` : ''}).`);
      this.playAt(next.b, true);
    } else toast('End of recording.');
  },

  keepAlive() {
    if (!this.sid) return;
    fetch(`/api/session/${this.sid}/touch`, { method: 'POST' }).catch(() => {});
  },

  /* clips */
  clipTime(str) { return str ? new Date(`${this.date}T${str.length === 5 ? str + ':00' : str}`) : null; },

  initClip() {
    const s = $('#clip-start'), e = $('#clip-end');
    const sync = () => { this.clipStart = this.clipTime(s.value); this.clipEnd = this.clipTime(e.value); this.renderTimeline(); };
    s.addEventListener('input', sync);
    e.addEventListener('input', sync);
    $('#clip-start-now').addEventListener('click', () => {
      if (!this.cursor) { toast('Start playing first, then use the playhead.'); return; }
      s.value = fmtClock(this.cursor);
      if (!e.value || this.clipTime(e.value) <= this.cursor) e.value = fmtClock(new Date(this.cursor.getTime() + 60000));
      sync();
    });
    $('#clip-end-now').addEventListener('click', () => {
      if (!this.cursor) { toast('Start playing first, then use the playhead.'); return; }
      e.value = fmtClock(this.cursor);
      if (!s.value || this.clipTime(s.value) >= this.cursor) s.value = fmtClock(new Date(this.cursor.getTime() - 60000));
      sync();
    });
    $('#clip').addEventListener('submit', ev => { ev.preventDefault(); this.downloadClip(); });
  },

  clipStatus(text, isError = false) {
    const el = $('#clip-status');
    el.textContent = text;
    el.classList.toggle('err', isError);
  },

  async downloadClip(retried = false) {
    if (this.clipBusy) return;
    const b = this.clipTime($('#clip-start').value), e = this.clipTime($('#clip-end').value);
    if (!b || !e || e <= b) { this.clipStatus('The clip must end after it starts.', true); return; }
    if (e - b > 30 * 60000) { this.clipStatus('Clips can be at most 30 minutes long.', true); return; }
    this.clipBusy = true;
    const btn = $('#clip-save'), bar = $('#clip-bar'), fill = $('i', bar);
    btn.disabled = true; bar.hidden = false; fill.style.width = '2%';
    this.clipStatus('Opening the recording…');
    try {
      const job = await api('/api/clips', { key: this.key, begin: iso(b), end: iso(e) });
      this.clipStatus('Preparing the clip…');
      for (;;) {
        await new Promise(r => setTimeout(r, 1000));
        const s = await api(`/api/clips/${job.id}`);
        fill.style.width = `${Math.round(s.progress * 100)}%`;
        if (s.state === 'error') throw new Error(s.error || 'The clip could not be made.');
        if (s.state === 'ready') {
          const a = document.createElement('a');
          a.href = `/api/clips/${job.id}/file`; a.download = job.filename;
          document.body.append(a); a.click(); a.remove();
          this.clipStatus(`Downloaded ${job.filename} (${(s.size / 1048576).toFixed(1)} MB).`);
          break;
        }
        this.clipStatus(`Preparing the clip… ${Math.round(s.progress * 100)}%`);
      }
    } catch (err) {
      const d = err.data || {};
      if (d.needs_code && !retried) {
        this.clipBusy = false; btn.disabled = false; bar.hidden = true;
        if (await askCode(d.serial, d.device, d.wrong)) return this.downloadClip(true);
        this.clipStatus('The clip needs the device verification code.', true);
        return;
      }
      this.clipStatus(friendly(err), true);
    }
    this.clipBusy = false; btn.disabled = false;
    setTimeout(() => { bar.hidden = true; fill.style.width = '0'; }, 1500);
  },
};

/* ── shell: tabs, layout, sidebar, lifecycle ─────────────────────────────── */
function showView(name) {
  const isLive = name === 'live';
  $('#tab-live').setAttribute('aria-selected', String(isLive));
  $('#tab-rec').setAttribute('aria-selected', String(!isLive));
  $('#live').hidden = !isLive;
  $('#rec').hidden = isLive;
  $('#layout').style.visibility = isLive ? '' : 'hidden';
  if (isLive) { rec.stopSpan(); live.start(); } else { live.stop(); rec.enter(); }
  document.body.classList.remove('side-open');
  store.set('hik.view', name);
}

$('#tab-live').addEventListener('click', () => showView('live'));
$('#tab-rec').addEventListener('click', () => showView('rec'));
$$('#layout button').forEach(b => b.addEventListener('click', () => live.setLayout(b.dataset.layout)));
$$('#profile button').forEach(b => b.addEventListener('click', () => live.setProfile(b.dataset.profile)));
new ResizeObserver(() => live.fit()).observe($('#live'));
$('#menu').addEventListener('click', () => {
  const open = document.body.classList.toggle('side-open');
  $('#menu').setAttribute('aria-expanded', String(open));
});
$('main').addEventListener('click', () => document.body.classList.remove('side-open'));

setInterval(() => {
  const now = fmtOsd(new Date());
  $$('.tile time').forEach(t => { t.textContent = now; });
}, 1000);
setInterval(() => { live.keepAlive(); rec.keepAlive(); }, 10000);

// Stop streaming when the page has been hidden for a while; resume when it is back.
let hideTimer = null, suspended = false;
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    hideTimer = setTimeout(() => {
      suspended = true;
      if (live.active) live.stop();
      rec.stopSpan();
    }, 20000);
  } else {
    clearTimeout(hideTimer);
    if (suspended) {
      suspended = false;
      if (!$('#live').hidden) live.start();
    }
  }
});
window.addEventListener('pagehide', () => { rec.stopSpan(); });

// A window left open on a screen nobody is watching would stream all day. After
// IDLE_MS without a mouse move, key or touch, stop the video and offer to resume.
const IDLE_MS = 30 * 60 * 1000;
let lastInput = Date.now(), idlePaused = null;
['pointermove', 'pointerdown', 'keydown', 'wheel', 'touchstart'].forEach(t =>
  document.addEventListener(t, () => { lastInput = Date.now(); }, { passive: true, capture: true }));
setInterval(() => {
  if (idlePaused || document.hidden || Date.now() - lastInput < IDLE_MS) return;
  const onLive = !$('#live').hidden;
  if (onLive ? !live.active : !rec.sid) return;
  idlePaused = onLive ? 'live' : 'rec';
  if (onLive) live.stop(); else { $('#pb-video').pause(); rec.stopSpan(); }
  $('#idle').hidden = false;
  $('#idle-resume').focus();
}, 15000);
$('#idle-resume').addEventListener('click', () => {
  $('#idle').hidden = true;
  const what = idlePaused; idlePaused = null;
  if (what === 'live') live.start();
  else if (rec.cursor) rec.playAt(rec.cursor, true);
});

// Tell the viewer when the server can no longer sign in to Hik-Connect, instead of
// leaving every camera to fail on its own.
async function checkStatus() {
  try {
    const st = await api('/api/status');
    const b = $('#login-banner');
    b.hidden = st.login_ok;
    if (!st.login_ok) $('#login-msg').textContent = st.error || 'Signing in failed.';
  } catch { /* the server itself is unreachable: the tiles already say so */ }
}
setInterval(checkStatus, 60000);

(async () => {
  $$('#layout button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.layout === live.layout)));
  $$('#profile button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.profile === live.profile)));
  await live.init();
  checkStatus();
  showView(store.get('hik.view', 'live') === 'rec' ? 'rec' : 'live');
})();
