'use strict';

const WS_SCHEME = location.protocol === 'https:' ? 'wss' : 'ws';
const wsUrl = (path) => `${WS_SCHEME}://${location.host}${path}`;
const $ = (id) => document.getElementById(id);

// Replaced by the real values from GET /api/config as soon as it resolves;
// these are just what's shown for the instant before that first fetch lands.
let config = {
  joyDeadmanMs: 300, joyRateHz: 20, micRate: 16000, speakerRate: 22050,
  camFps: 8, camJpegQuality: 0.6,
};
let latest = null;

async function loadConfig() {
  const res = await fetch('/api/config');
  if (res.status === 401) { window.location = '/login'; return; }
  if (!res.ok) return; // keep the defaults above
  const c = await res.json();
  config = {
    joyDeadmanMs: c.joy_deadman_ms, joyRateHz: c.joy_rate_hz,
    micRate: c.mic_sample_rate, speakerRate: c.speaker_sample_rate,
    camFps: c.camera_max_fps, camJpegQuality: c.camera_jpeg_quality,
  };
  $('deadman-ms').textContent = config.joyDeadmanMs;
}

// --------------------------------------------------------------- state feed

function connectState() {
  const ws = new WebSocket(wsUrl('/ws/state'));
  ws.onmessage = (ev) => {
    latest = JSON.parse(ev.data);
    render(latest);
  };
  ws.onclose = () => {
    setBadge($('badge-link'), 'panel disconnected', 'bad');
    setTimeout(connectState, 1500);
  };
}

function setBadge(el, text, cls) {
  el.textContent = text;
  el.className = 'badge' + (cls ? ' ' + cls : '');
}

function render(s) {
  const dialogClass = { IDLE: '', LISTENING: 'good', THINKING: 'warn', SPEAKING: 'good', DEGRADED: 'warn', ESTOP: 'bad' };
  setBadge($('badge-dialog'), s.dialog_state, dialogClass[s.dialog_state] || '');
  setBadge($('badge-backend'), `backend: ${s.backend}`, s.backend === 'ros' ? 'good' : 'warn');

  // sources
  document.querySelectorAll('.seg[data-stream]').forEach((seg) => {
    const stream = seg.dataset.stream;
    const active = s.sources[stream];
    const busy = s.sources.transitioning !== null;
    seg.querySelectorAll('button').forEach((b) => {
      b.classList.toggle('on', b.dataset.backend === active);
      b.disabled = busy;
    });
  });
  if (s.sources.transitioning) {
    $('sources-hint').textContent = `switching ${s.sources.transitioning}…`;
  }

  // media stats
  $('v-camfps').textContent = `${s.camera_fps_in.toFixed(1)} fps`;
  $('v-mickbps').textContent = `${s.mic_kbps_in.toFixed(1)} kbps`;
  const open = Object.entries(s.media).filter(([, v]) => v).map(([k]) => k);
  $('v-channels').textContent = open.length ? open.join(', ') : 'none';

  // system
  $('v-cpu').textContent = `${s.system.cpu_percent.toFixed(0)} %`;
  $('v-mem').textContent = `${(s.system.mem_used_mb / 1000).toFixed(2)} / ${(s.system.mem_total_mb / 1000).toFixed(2)} GB`;
  $('v-temp').textContent = s.system.temp_c === null ? '—' : `${s.system.temp_c.toFixed(1)} °C`;
  $('v-uptime').textContent = fmtDuration(s.system.uptime_s);

  renderPerception(s.perception);

  $('nodes').innerHTML = s.nodes.map((n) =>
    `<div class="row"><span class="k">${n.name}</span><span class="v">${n.state}</span></div>`).join('');

  // head
  $('v-pan').textContent = `${s.head.pan_deg.toFixed(1)}°`;
  $('v-tilt').textContent = `${s.head.tilt_deg.toFixed(1)}°`;
  $('v-limit').textContent = s.head.at_limit ? 'yes' : 'no';
  $('v-src').textContent = s.head.active_source;
  $('v-estop').textContent = s.head.estop ? 'ENGAGED' : 'clear';
  $('btn-estop').classList.toggle('engaged', s.head.estop);
  $('btn-estop').textContent = s.head.estop ? 'RELEASE E-STOP' : 'E-STOP';
}

// ------------------------------------------------- link & dialog polling
//
// These come from model_conn's and intelligence's status files (see
// dialog_status.py / link_status.py), not the 4 Hz /ws/state feed -- reading
// them is file I/O, which doesn't belong in that loop. A slower poll here is
// the deliberate tradeoff: badges lag by up to POLL_MS instead of never
// reflecting reality at all.
const POLL_MS = 3000;

async function pollLinkStatus() {
  try {
    const res = await fetch('/api/link/status');
    if (res.status === 401) { window.location = '/login'; return; }
    if (!res.ok) return;
    const { link } = await res.json();
    setBadge($('badge-link'),
      link.up ? `link: ${link.active_path} ${link.rtt_ms ? link.rtt_ms.toFixed(0) + 'ms' : ''}` : 'link: down',
      link.up ? 'good' : 'warn');
    $('v-linkup').textContent = link.up ? 'up' : 'down';
    $('v-linkpath').textContent = link.active_path;
    $('v-rtt').textContent = link.rtt_ms === null ? '—' : `${link.rtt_ms.toFixed(1)} ms`;
    $('v-fails').textContent = link.consecutive_failures;
  } catch { /* transient fetch failure; next poll retries */ }
}

async function pollDialogStatus() {
  const el = $('v-dlg-reply');
  if (!el) return; // dialog tab markup not present (shouldn't happen, but don't throw)
  try {
    const res = await fetch('/api/dialog/status');
    if (res.status === 401) { window.location = '/login'; return; }
    if (!res.ok) return;
    const d = await res.json();
    $('v-dlg-prompt').textContent = d.last_prompt || '—';
    el.textContent = d.last_reply || '—';
    $('v-dlg-source').textContent = d.chat_source;
    $('v-dlg-asr').textContent = d.asr_engine || '—';
    $('v-dlg-tts').textContent = d.tts_engine || '—';
    $('v-dlg-updated').textContent = d.updated_at ? new Date(d.updated_at * 1000).toLocaleTimeString() : '—';
  } catch { /* transient fetch failure; next poll retries */ }
}

// ------------------------------------------------------------- perception

const STATE_LABEL = {
  scanning: ['scanning', ''],
  engaging: ['confirming…', 'warn'],
  engaged: ['ENGAGED', 'good'],
  suspended: ['suspended — holding', 'warn'],
};

function renderPerception(p) {
  if (!p) return;
  if (!p.available) {
    $('v-det-backend').textContent = 'unavailable';
    return;
  }

  const [label, cls] = STATE_LABEL[p.state] || [p.state, ''];
  const stateEl = $('v-eng-state');
  stateEl.textContent = label;
  stateEl.style.color = cls === 'good' ? 'var(--good)' : cls === 'warn' ? 'var(--warn)' : '';

  $('v-eng-target').textContent = p.target_id === null ? '—' : `#${p.target_id}`;
  $('v-eng-gesture').textContent = p.last_gesture;
  $('v-eng-release').textContent = p.release_reason || '—';
  $('v-eng-aim').textContent = `${p.aim_x.toFixed(2)}, ${p.aim_y.toFixed(2)}`;

  $('v-det-backend').textContent = p.detector;
  $('v-det-people').textContent = p.person_count;
  $('v-det-ms').textContent = p.inference_ms ? `${p.inference_ms.toFixed(0)} ms` : '—';
  $('v-det-fps').textContent = p.fps ? `${p.fps.toFixed(1)} fps` : '—';
  $('v-det-dropped').textContent = p.dropped_frames;

  drawOverlay(p);
}

function drawOverlay(p) {
  const canvas = $('vision-overlay');
  const stage = canvas.parentElement;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (!w || !h) return;
  if (canvas.width !== w || canvas.height !== h) {
    canvas.width = w; canvas.height = h;
  }

  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, w, h);
  // Tracks arriving without a local stream means the robot's own camera is the
  // source: still show the overlay rather than the "start the camera" hint.
  stage.classList.toggle('live', !!camStream || p.tracks.length > 0);

  for (const t of p.tracks) {
    const x = t.x1 * w, y = t.y1 * h;
    const bw = (t.x2 - t.x1) * w, bh = (t.y2 - t.y1) * h;

    if (t.engaged) {
      ctx.strokeStyle = '#3fb8af'; ctx.lineWidth = 3; ctx.setLineDash([]);
    } else if (t.confirmed) {
      ctx.strokeStyle = '#7d8a99'; ctx.lineWidth = 1.5; ctx.setLineDash([]);
    } else {
      // Unconfirmed: seen once, not yet trusted enough to be a lock candidate.
      ctx.strokeStyle = '#4a5563'; ctx.lineWidth = 1; ctx.setLineDash([4, 4]);
    }
    ctx.strokeRect(x, y, bw, bh);

    const tag = `#${t.track_id}${t.gesture !== 'none' ? ' ✋' : ''}${t.engaged ? ' LOCKED' : ''}`;
    ctx.setLineDash([]);
    ctx.font = '600 12px ui-monospace, monospace';
    const tw = ctx.measureText(tag).width + 10;
    ctx.fillStyle = t.engaged ? '#3fb8af' : 'rgba(14,17,22,.8)';
    ctx.fillRect(x, Math.max(0, y - 18), tw, 18);
    ctx.fillStyle = t.engaged ? '#0b0e12' : '#d8e0e8';
    ctx.fillText(tag, x + 5, Math.max(12, y - 5));
  }

  // Aim point, in the same normalised frame the head is driven from.
  if (p.engaged && (p.aim_x || p.aim_y)) {
    const ax = (p.aim_x + 1) / 2 * w;
    const ay = (1 - p.aim_y) / 2 * h;
    ctx.strokeStyle = '#3fb8af';
    ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.arc(ax, ay, 9, 0, Math.PI * 2); ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(ax - 14, ay); ctx.lineTo(ax + 14, ay);
    ctx.moveTo(ax, ay - 14); ctx.lineTo(ax, ay + 14);
    ctx.stroke();
  }
}

$('btn-release').addEventListener('click', () => post('/api/perception/release'));
$('btn-reset-tracks').addEventListener('click', () => post('/api/perception/reset'));

function fmtDuration(sec) {
  const s = Math.floor(sec % 60), m = Math.floor((sec / 60) % 60), h = Math.floor(sec / 3600);
  return h ? `${h}h ${m}m` : m ? `${m}m ${s}s` : `${s}s`;
}

// ------------------------------------------------------------------- tabs

$('tabs').addEventListener('click', (e) => {
  const btn = e.target.closest('button');
  if (!btn || btn.disabled) return;
  document.querySelectorAll('#tabs button').forEach((b) => b.classList.remove('active'));
  document.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));
  btn.classList.add('active');
  $('tab-' + btn.dataset.tab).classList.add('active');
  // Open the joystick socket with the tab, not on pointer-down: a WebSocket
  // handshake takes longer than a quick flick of the stick, and a control that
  // silently drops the first input is worse than one that takes a moment to arm.
  if (btn.dataset.tab === 'head') joyConnect(); else joyDisconnect();
});

// ---------------------------------------------------------------- commands

async function post(path, body) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  if (res.status === 401) { window.location = '/login'; return null; }
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    console.warn(path, err.detail || res.status);
  }
  return res.json().catch(() => ({}));
}

document.querySelectorAll('.seg[data-stream] button').forEach((btn) => {
  btn.addEventListener('click', () =>
    post('/api/sources/set', { stream: btn.closest('.seg').dataset.stream, backend: btn.dataset.backend }));
});

$('btn-estop').addEventListener('click', () =>
  post('/api/system/estop', { engaged: !(latest && latest.head.estop) }));
$('btn-center').addEventListener('click', () => post('/api/head/center'));
$('btn-tone').addEventListener('click', () => post('/api/media/test-tone'));
$('btn-logout').addEventListener('click', async () => {
  await post('/api/auth/logout');
  window.location = '/login';
});

// -------------------------------------------------------------- joystick

// Held open while the Head tab is in use. The browser stops sending the moment
// the pointer is released; the server-side deadman is the backstop for the cases
// the browser cannot report -- a closed lid, a dropped network, a killed tab.
const pad = $('pad'), knob = $('knob');
let joyWs = null, joyTimer = null, axes = [0, 0];

function joyConnect() {
  if (joyWs && joyWs.readyState <= WebSocket.OPEN) return;
  joyWs = new WebSocket(wsUrl('/ws/joy'));
  joyWs.onclose = () => { joyWs = null; };
}

function joyDisconnect() {
  joyRelease();
  if (joyWs) { joyWs.close(); joyWs = null; }
}

function joySend() {
  if (joyWs && joyWs.readyState === WebSocket.OPEN) {
    joyWs.send(JSON.stringify({ axes, buttons: [] }));
  }
}

function padPos(ev) {
  const r = pad.getBoundingClientRect();
  const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
  const max = r.width / 2 - 27;
  let dx = ev.clientX - cx, dy = ev.clientY - cy;
  const dist = Math.hypot(dx, dy);
  if (dist > max) { dx = (dx / dist) * max; dy = (dy / dist) * max; }
  knob.style.transform = `translate(calc(-50% + ${dx}px), calc(-50% + ${dy}px))`;
  // Screen y grows downward; tilt up should be positive.
  axes = [dx / max, -dy / max];
}

pad.addEventListener('pointerdown', (ev) => {
  pad.setPointerCapture(ev.pointerId);
  pad.classList.add('live');
  joyConnect(); // no-op if the tab already armed it
  padPos(ev);
  joySend();
  // The interval is a heartbeat, not the transport: it keeps the stream alive
  // while the stick is held still, which is what feeds the server's deadman.
  joyTimer = setInterval(joySend, 1000 / config.joyRateHz);
});

pad.addEventListener('pointermove', (ev) => {
  if (!pad.classList.contains('live')) return;
  padPos(ev);
  joySend(); // send on the event, so short movements are never dropped
});

function joyRelease() {
  if (!pad.classList.contains('live')) return;
  pad.classList.remove('live');
  clearInterval(joyTimer);
  knob.style.transform = 'translate(-50%, -50%)';
  axes = [0, 0];
  joySend(); // explicit stop, rather than waiting for the deadman
}

pad.addEventListener('pointerup', joyRelease);
pad.addEventListener('pointercancel', joyRelease);
window.addEventListener('blur', joyRelease);

// --------------------------------------------------------------- camera

let camStream = null, camWs = null, camTimer = null;

$('btn-cam').addEventListener('click', async () => {
  if (camStream) return stopCamera();
  try {
    camStream = await navigator.mediaDevices.getUserMedia({ video: { width: 640, height: 480 } });
  } catch (err) {
    alert(secureContextHint('camera', err));
    return;
  }
  const video = $('preview');
  video.srcObject = camStream;
  video.style.display = 'block';
  // Same stream feeds the Vision tab, so the overlay lines up with what the
  // detector is actually seeing.
  $('vision-video').srcObject = camStream;
  camWs = new WebSocket(wsUrl('/ws/camera'));
  camWs.binaryType = 'arraybuffer';

  const canvas = document.createElement('canvas');
  canvas.width = 640; canvas.height = 480;
  const ctx = canvas.getContext('2d');

  camTimer = setInterval(() => {
    if (!camWs || camWs.readyState !== WebSocket.OPEN || video.readyState < 2) return;
    ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
    canvas.toBlob(async (blob) => {
      if (blob && camWs && camWs.readyState === WebSocket.OPEN) {
        camWs.send(await blob.arrayBuffer());
      }
    }, 'image/jpeg', config.camJpegQuality);
  }, 1000 / config.camFps);

  $('btn-cam').textContent = 'Stop camera';
});

function stopCamera() {
  clearInterval(camTimer);
  if (camWs) camWs.close();
  if (camStream) camStream.getTracks().forEach((t) => t.stop());
  camStream = camWs = camTimer = null;
  $('preview').style.display = 'none';
  $('vision-video').srcObject = null;
  $('btn-cam').textContent = 'Start camera';
}

// ------------------------------------------------------------------ mic

let micStream = null, micWs = null, micCtx = null;

$('btn-mic').addEventListener('click', async () => {
  if (micStream) return stopMic();
  try {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    });
  } catch (err) {
    alert(secureContextHint('microphone', err));
    return;
  }
  micWs = new WebSocket(wsUrl('/ws/mic'));
  micWs.binaryType = 'arraybuffer';

  micCtx = new AudioContext({ sampleRate: config.micRate });
  await micCtx.audioWorklet.addModule('/static/pcm-worklet.js');
  const src = micCtx.createMediaStreamSource(micStream);
  const node = new AudioWorkletNode(micCtx, 'pcm-worklet');
  node.port.onmessage = (ev) => {
    if (micWs && micWs.readyState === WebSocket.OPEN) micWs.send(ev.data);
  };
  src.connect(node);
  // Keep the graph pulling without echoing the mic to the local speaker.
  node.connect(micCtx.createGain()).connect(micCtx.destination);

  $('btn-mic').textContent = 'Stop mic';
});

function stopMic() {
  if (micWs) micWs.close();
  if (micCtx) micCtx.close();
  if (micStream) micStream.getTracks().forEach((t) => t.stop());
  micStream = micWs = micCtx = null;
  $('btn-mic').textContent = 'Start mic';
}

// -------------------------------------------------------------- speaker

let spkWs = null, spkCtx = null, spkCursor = 0;

$('btn-spk').addEventListener('click', () => {
  if (spkWs) return stopSpeaker();
  spkCtx = new AudioContext({ sampleRate: config.speakerRate });
  spkCursor = spkCtx.currentTime;
  spkWs = new WebSocket(wsUrl('/ws/speaker'));
  spkWs.binaryType = 'arraybuffer';
  spkWs.onmessage = (ev) => playPcm(new Int16Array(ev.data));
  $('btn-spk').textContent = 'Disconnect speaker';
});

function playPcm(int16) {
  const buf = spkCtx.createBuffer(1, int16.length, spkCtx.sampleRate);
  const ch = buf.getChannelData(0);
  for (let i = 0; i < int16.length; i++) ch[i] = int16[i] / 0x8000;
  const src = spkCtx.createBufferSource();
  src.buffer = buf;
  src.connect(spkCtx.destination);
  // Schedule back to back; never behind the clock, or chunks overlap.
  spkCursor = Math.max(spkCursor, spkCtx.currentTime + 0.02);
  src.start(spkCursor);
  spkCursor += buf.duration;
}

function stopSpeaker() {
  if (spkWs) spkWs.close();
  if (spkCtx) spkCtx.close();
  spkWs = spkCtx = null;
  $('btn-spk').textContent = 'Connect speaker';
}

function secureContextHint(what, err) {
  if (!window.isSecureContext) {
    return `The browser refused ${what} access because this page is not a secure ` +
      `context. Serve the panel over HTTPS (run neo --webapp devcert) or open it ` +
      `on localhost.`;
  }
  return `Could not open the ${what}: ${err.name}`;
}

// ------------------------------------------------------------------ boot

loadConfig().then(() => { $('deadman-ms').textContent = config.joyDeadmanMs; });
connectState();
pollLinkStatus();
pollDialogStatus();
setInterval(pollLinkStatus, POLL_MS);
setInterval(pollDialogStatus, POLL_MS);
