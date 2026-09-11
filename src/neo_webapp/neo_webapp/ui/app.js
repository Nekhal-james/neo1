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

  renderAudio(s.audio, s.media);
  renderVoice(s.voice);

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
  $('v-mood').textContent = s.emotion
    ? `${s.emotion.label.toLowerCase()} (${s.emotion.intensity.toFixed(2)})`
    : '—';
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

async function pollModelStatus() {
  const statusEl = $('v-mdl-status');
  if (!statusEl) return;
  try {
    const res = await fetch('/api/model/status');
    if (res.status === 401) { window.location = '/login'; return; }
    if (!res.ok) return;
    const m = await res.json();

    statusEl.textContent = m.summary;
    statusEl.style.color = m.serving ? 'var(--good)' : 'var(--muted)';
    $('v-mdl-name').textContent = m.model_name || '—';
    $('v-mdl-endpoint').textContent = m.endpoint || '—';
    $('v-mdl-tls').textContent = m.reachable ? (m.tls_enabled ? 'mTLS' : 'plain HTTP') : '—';
    $('v-mdl-configured').textContent = m.configured_model || 'not set';

    // A model-name mismatch produces a degraded reply with no visible cause,
    // so it gets said out loud rather than left to be discovered.
    const warn = $('mdl-warning');
    warn.hidden = m.warnings.length === 0;
    warn.textContent = m.warnings.join(' ');
    $('v-mdl-configured').style.color = m.mismatch ? 'var(--bad)' : '';
  } catch { /* transient fetch failure; next poll retries */ }
}

// ------------------------------------------------------------------- ask

const askInput = $('ask-input');
const askBtn = $('btn-ask');

async function askNeo() {
  const text = askInput.value.trim();
  if (!text) return;
  const out = $('ask-answer');

  askBtn.disabled = true;
  out.hidden = false;
  out.className = 'answer';
  out.textContent = 'Thinking…';

  try {
    const res = await fetch('/api/dialog/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    if (res.status === 401) { window.location = '/login'; return; }
    const body = await res.json().catch(() => ({}));

    if (!res.ok) {
      out.className = 'answer bad';
      out.textContent = body.detail || `request failed (${res.status})`;
    } else {
      // Degraded is a normal outcome, not an error -- the host is usually a
      // daily-driver laptop. Amber, not red.
      out.className = 'answer ' + (body.source === 'degraded' ? '' : 'good');
      out.textContent = body.reply;
      const meta = document.createElement('span');
      meta.className = 'meta';
      meta.textContent = body.source === 'degraded'
        ? 'degraded — no model host reachable'
        : `${body.source} · ${body.host} · ${body.latency_ms.toFixed(0)} ms`;
      out.appendChild(meta);
      askInput.value = '';
      pollDialogStatus();
    }
  } catch {
    out.className = 'answer bad';
    out.textContent = 'could not reach the panel';
  } finally {
    askBtn.disabled = false;
    askInput.focus();
  }
}

if (askBtn) {
  askBtn.addEventListener('click', askNeo);
  askInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') askNeo(); });
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
  renderPalm(p);
  $('v-eng-release').textContent = p.release_reason || '—';
  $('v-eng-aim').textContent = `${p.aim_x.toFixed(2)}, ${p.aim_y.toFixed(2)}`;
  $('v-eng-wants').textContent = (p.engage_gesture || '').replace('_', ' ') || '—';

  const facingEl = $('v-eng-facing');
  facingEl.textContent = p.target_facing === 'unknown' && !p.engaged ? '—' : p.target_facing;
  // Amber while facing away: the release countdown is running.
  facingEl.style.color = p.target_facing === 'away' ? 'var(--warn)' : '';

  $('v-det-backend').textContent = p.detector;
  $('v-det-people').textContent = p.person_count;
  $('v-det-ms').textContent = p.inference_ms ? `${p.inference_ms.toFixed(0)} ms` : '—';
  $('v-det-fps').textContent = p.fps ? `${p.fps.toFixed(1)} fps` : '—';
  $('v-det-dropped').textContent = p.dropped_frames;

  drawOverlay(p);
}

// Which link of the palm test broke, for whoever matters most right now. A palm
// that will not register is otherwise a guessing game.
function renderPalm(p) {
  const el = $('v-eng-palm');
  const palm = p.palm;
  if (!palm) {
    el.textContent = p.person_count ? '—' : 'nobody in view';
    el.style.color = '';
  } else {
    const detail = [];
    if (palm.forearm_tilt_deg !== null) detail.push(`tilt ${palm.forearm_tilt_deg.toFixed(0)}°`);
    detail.push(`elbow ${palm.elbow_score.toFixed(2)}`, `wrist ${palm.wrist_score.toFixed(2)}`);
    const verdict = palm.verdict.replace('_', ' ');
    el.textContent = `#${palm.track_id} ${verdict}${palm.reason ? ' — ' + palm.reason : ''} (${detail.join(', ')})`;
    el.style.color = palm.verdict === 'open_palm' ? 'var(--good)'
      : palm.verdict === 'none' ? '' : 'var(--warn)';
  }
  $('v-eng-hold').textContent = p.engaged
    ? 'locked'
    : `${Math.round((p.hold_progress || 0) * 100)} %`;
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
    // The person box, faint, behind the head. At desk range it is usually
    // clipped by the frame edge -- which is the whole reason the head is what
    // gets tracked.
    if (t.px1 !== null && t.px1 !== undefined) {
      ctx.strokeStyle = 'rgba(125,138,153,.45)';
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 3]);
      ctx.strokeRect(t.px1 * w, t.py1 * h, (t.px2 - t.px1) * w, (t.py2 - t.py1) * h);
      ctx.setLineDash([]);
    }

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

    const palm = t.gesture === 'open_palm' ? ' PALM' : t.gesture !== 'none' ? ' hand' : '';
    const away = t.facing === 'away' ? ' back turned' : '';
    const tag = `#${t.track_id}${palm}${away}${t.engaged ? ' LOCKED' : ''}`;
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

// ---------------------------------------------------------- identify object

$('btn-identify').addEventListener('click', async () => {
  const btn = $('btn-identify');
  const out = $('identify-answer');
  btn.disabled = true;
  out.hidden = false;
  out.className = 'answer';
  // The first question also pays for loading the object model off disk, so
  // saying nothing here reads as a hang.
  out.textContent = 'Looking…';

  try {
    const res = await fetch('/api/perception/identify', { method: 'POST' });
    if (res.status === 401) { window.location = '/login'; return; }
    const body = await res.json().catch(() => ({}));

    if (!res.ok) {
      out.className = 'answer bad';
      out.textContent = body.detail || `identification failed (${res.status})`;
    } else if (!body.best) {
      out.textContent = "I don't recognise anything being held up.";
    } else {
      const others = body.guesses.slice(1, 3).map((g) => g.label);
      out.className = 'answer good';
      out.textContent = `That looks like a ${body.best}.`
        + (others.length ? `  (also saw: ${others.join(', ')})` : '');
    }
  } catch {
    out.className = 'answer bad';
    out.textContent = 'could not reach the panel';
  } finally {
    btn.disabled = false;
  }
});

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
  if (btn.dataset.tab === 'data' && !campus.loaded) loadCampus();
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

// ------------------------------------------------------------- password

const pwDialog = $('password-dialog');

function resetPasswordDialog() {
  $('password-form').reset();
  $('pw-error').textContent = '';
  $('pw-recovery-code').hidden = true;
  $('pw-recovery-code').textContent = '';
  $('pw-recovery-note').hidden = true;
}

function closePasswordDialog() {
  resetPasswordDialog();
  pwDialog.close();
}

async function loadRecoveryStatus() {
  const el = $('v-recovery');
  try {
    const res = await fetch('/api/auth/recovery');
    if (!res.ok) { el.textContent = '—'; return; }
    const { configured } = await res.json();
    el.textContent = configured ? 'set' : 'not set';
    el.style.color = configured ? 'var(--good)' : 'var(--warn)';
  } catch { el.textContent = '—'; }
}

$('btn-password').addEventListener('click', () => {
  resetPasswordDialog();
  loadRecoveryStatus();
  pwDialog.showModal();
  $('pw-current').focus();
});

$('pw-recovery').addEventListener('click', async () => {
  const err = $('pw-error');
  err.style.color = '';
  const current = $('pw-current').value;
  if (!current) {
    err.textContent = 'Enter the current password first.';
    $('pw-current').focus();
    return;
  }
  const btn = $('pw-recovery');
  btn.disabled = true;
  err.textContent = '';
  try {
    const res = await fetch('/api/auth/recovery', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ current_password: current }),
    });
    if (res.status === 401) { window.location = '/login'; return; }
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      err.textContent = body.detail || `could not make a recovery code (${res.status})`;
      return;
    }
    $('pw-current').value = '';
    $('pw-recovery-code').textContent = body.recovery_code;
    $('pw-recovery-code').hidden = false;
    $('pw-recovery-note').hidden = false;
    loadRecoveryStatus();
  } catch {
    err.textContent = 'could not reach the panel';
  } finally {
    btn.disabled = false;
  }
});

$('pw-cancel').addEventListener('click', closePasswordDialog);

$('password-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const err = $('pw-error');
  const current = $('pw-current').value;
  const next = $('pw-new').value;
  err.style.color = '';
  if (next !== $('pw-confirm').value) {
    err.textContent = 'The two new passwords do not match.';
    return;
  }
  const btn = $('pw-submit');
  btn.disabled = true;
  err.textContent = '';
  try {
    const res = await fetch('/api/auth/password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ current_password: current, new_password: next }),
    });
    if (res.status === 401) { window.location = '/login'; return; }
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      err.textContent = body.detail || `could not change the password (${res.status})`;
      return;
    }
    $('password-form').reset();
    err.style.color = 'var(--good)';
    err.textContent = 'Password changed. Other browsers have been signed out.';
    setTimeout(closePasswordDialog, 1800);
  } catch {
    err.textContent = 'could not reach the panel';
  } finally {
    btn.disabled = false;
  }
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

// ----------------------------------------------------------------- audio

// Availability comes from the 4 Hz state snapshot rather than its own poll:
// the server refreshes it on a 1 Hz task precisely so that reading it here is
// free. /api/audio/status exists for the first paint and for curl.
function renderAudio(a, media) {
  if (!a) return;

  $('v-asr-engine').textContent = a.asr_engine || '—';
  $('v-asr-status').textContent = a.asr_available ? 'ready' : 'unavailable';
  $('v-asr-listening').textContent = a.listening ? 'yes — mic channel open' : 'no';
  $('v-asr-bytes').textContent = a.audio_bytes ? `${(a.audio_bytes / 1024).toFixed(0)} KB` : '—';
  $('v-asr-count').textContent = String(a.utterances || 0);

  $('v-tts-engine').textContent = a.tts_engine || '—';
  $('v-tts-status').textContent = a.tts_available ? 'ready' : 'unavailable';
  // Only the state snapshot knows about channels; a direct /api/audio/*
  // response does not, and must leave this field alone rather than flashing
  // "not connected" at someone whose speaker is fine.
  if (media) {
    $('v-tts-speaker').textContent = media.speaker ? 'connected' : 'not connected';
  }

  // The reason, not just the flag: "unavailable" alone is indistinguishable
  // from a bug, and every reason the server sends names something fixable.
  showReason('asr-warning', a.asr_available, a.asr_reason);
  showReason('tts-warning', a.tts_available, a.tts_reason);

  $('v-asr-partial').textContent = a.partial || '—';
  $('v-asr-final').textContent = (a.last && a.last.text) || '—';
  $('v-asr-conf').textContent = a.last && a.last.text
    ? `${(a.last.confidence || 0).toFixed(2)} · ${a.last.engine || '?'}`
    : '—';

  if (a.last_error) showReason('asr-warning', false, a.last_error);
}

function showReason(id, ok, reason) {
  const el = $(id);
  if (!el) return;
  if (ok || !reason) { el.hidden = true; return; }
  el.hidden = false;
  el.textContent = reason;
}

const sayInput = $('say-input');
const sayBtn = $('btn-say');

async function speak(text) {
  if (!text) return;
  const out = $('say-answer');
  sayBtn.disabled = true;
  out.hidden = false;
  out.className = 'answer';
  out.textContent = 'Synthesizing…';

  try {
    const res = await fetch('/api/audio/say', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    if (res.status === 401) { window.location = '/login'; return; }
    const body = await res.json().catch(() => ({}));

    if (!res.ok) {
      out.className = 'answer bad';
      out.textContent = body.detail || `request failed (${res.status})`;
      return;
    }
    // Audio with nobody connected is produced and dropped. Saying "spoke"
    // for something the operator never heard is the confusing outcome.
    out.className = 'answer ' + (body.speaker_connected ? 'good' : '');
    out.textContent = body.speaker_connected
      ? `Spoke ${body.duration_s.toFixed(1)}s at ${body.sample_rate} Hz.`
      : `Synthesized ${body.duration_s.toFixed(1)}s — but no speaker is connected, `
        + `so nobody heard it. Connect the speaker on the Sources tab.`;
  } catch (err) {
    out.className = 'answer bad';
    out.textContent = String(err);
  } finally {
    sayBtn.disabled = false;
  }
}

sayBtn.addEventListener('click', () => speak(sayInput.value.trim()));
sayInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') speak(sayInput.value.trim());
});

$('btn-say-heard').addEventListener('click', () => {
  const heard = $('v-asr-final').textContent;
  if (heard && heard !== '—') speak(heard);
});

$('btn-asr-reset').addEventListener('click', () => post('/api/audio/reset'));
$('btn-audio-reload').addEventListener('click', async () => {
  const btn = $('btn-audio-reload');
  btn.disabled = true;
  try {
    const view = await post('/api/audio/reload');
    if (view) renderAudio(view, null);
  } finally {
    btn.disabled = false;
  }
});

async function loadAudioStatus() {
  const res = await fetch('/api/audio/status');
  if (res.status === 401) { window.location = '/login'; return; }
  if (res.ok) renderAudio(await res.json(), null);
}

// ----------------------------------------------------------------- voice

// The spoken loop. The server owns the turn and the half-duplex gate; this
// renders what it reports and flips the one switch.
const PHASE_TEXT = {
  idle: 'idle — mic closed',
  listening: 'listening',
  thinking: 'thinking…',
  speaking: 'speaking',
};
// While a toggle request is in flight, the 4 Hz snapshot still carries the old
// value; without this the checkbox flicks back under the operator's finger.
let voiceTogglePending = false;

function renderVoice(v) {
  if (!v) return;
  let phase = PHASE_TEXT[v.phase] || v.phase;
  if (v.phase === 'speaking' && v.gated) phase += ' — mic muted so it cannot hear itself';
  $('v-voice-phase').textContent = phase;
  if (!voiceTogglePending) $('voice-enabled').checked = v.enabled;

  const t = v.last || {};
  $('v-voice-heard').textContent = t.heard || '—';
  $('v-voice-reply').textContent = t.reply || '—';
  const parts = [];
  if (t.source) parts.push(t.source);
  if (t.think_ms) parts.push(`thinking ${t.think_ms.toFixed(0)} ms`);
  if (t.first_audio_ms) parts.push(`first audio ${t.first_audio_ms.toFixed(0)} ms`);
  if (t.spoken_s) {
    parts.push(`spoke ${t.spoken_s.toFixed(1)} s in ${t.sentences} sentence${t.sentences === 1 ? '' : 's'}`);
  }
  $('v-voice-timing').textContent = parts.length ? parts.join(' · ') : '—';
  showReason('voice-warning', !t.error, t.error);
  $('btn-talk').textContent = micStream ? 'Stop talking' : 'Start talking';
}

async function setVoiceEnabled(enabled) {
  voiceTogglePending = true;
  let body = null;
  try {
    const res = await fetch('/api/voice', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    });
    if (res.status === 401) { window.location = '/login'; return; }
    if (res.ok) body = await res.json();
  } finally {
    voiceTogglePending = false;
  }
  if (body) renderVoice(body);
}

$('voice-enabled').addEventListener('change', (e) => setVoiceEnabled(e.target.checked));

// One button for the whole thing. Clicking the Sources-tab buttons from inside
// this handler keeps it within the user gesture that getUserMedia and a fresh
// AudioContext both insist on.
$('btn-talk').addEventListener('click', async () => {
  if (micStream) { stopMic(); return; }
  if (!spkWs) $('btn-spk').click();
  $('btn-mic').click();
  await setVoiceEnabled(true);
});

$('btn-ask-voice').addEventListener('click', async () => {
  const text = askInput.value.trim();
  if (!text) return;
  const out = $('ask-answer');
  const btn = $('btn-ask-voice');
  btn.disabled = true;
  askBtn.disabled = true;
  out.hidden = false;
  out.className = 'answer';
  out.textContent = 'Thinking…';

  try {
    const res = await fetch('/api/voice/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    if (res.status === 401) { window.location = '/login'; return; }
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      out.className = 'answer bad';
      out.textContent = body.detail || `request failed (${res.status})`;
      return;
    }
    // Same colours as the typed Ask: degraded is amber, not red.
    out.className = !body.reply ? 'answer bad'
      : (body.error || body.source === 'degraded') ? 'answer' : 'answer good';
    out.textContent = body.reply || body.error || '(no reply)';
    const meta = document.createElement('span');
    meta.className = 'meta';
    const bits = [body.source || '?', `thinking ${(body.think_ms || 0).toFixed(0)} ms`];
    if (body.first_audio_ms) bits.push(`first audio ${body.first_audio_ms.toFixed(0)} ms`);
    if (body.error && body.reply) bits.push(body.error);
    meta.textContent = bits.join(' · ');
    out.appendChild(meta);
    askInput.value = '';
    pollDialogStatus();
  } catch {
    out.className = 'answer bad';
    out.textContent = 'could not reach the panel';
  } finally {
    btn.disabled = false;
    askBtn.disabled = false;
  }
});

// ------------------------------------------------------------------ data

// The campus files are edited as text and saved verbatim. Each file keeps its
// own draft, so switching between Rooms, Graph and Coverage never loses an edit,
// and `modifiedNs` is the version a draft started from: the server refuses a
// save over a file that changed underneath it.
const campus = {
  loaded: false,
  file: 'rooms',
  saved: {},      // file -> text on disk
  modifiedNs: {}, // file -> version the draft is based on
  drafts: {},     // file -> text in the editor
  samples: {},
};

const ROOM_TEMPLATE = `
- code:
  name:
  type: classroom
  block:
  floor: 0
  aliases: []
  landmarks: []
`;

const editor = $('campus-editor');

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function campusDirty(file) {
  return (campus.drafts[file] ?? '') !== (campus.saved[file] ?? '');
}

// Reads every file from disk. A draft with unsaved edits is kept unless
// `discard` names its file -- that is what Revert does.
async function loadCampus(discard) {
  if (campus.loaded) campus.drafts[campus.file] = editor.value;
  const res = await fetch('/api/campus');
  if (res.status === 401) { window.location = '/login'; return; }
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    $('campus-state').textContent = body.detail || `could not load (${res.status})`;
    return;
  }
  campus.samples = body.samples;
  for (const [name, f] of Object.entries(body.files)) {
    const keep = campus.loaded && name !== discard && campusDirty(name);
    campus.saved[name] = f.text;
    campus.modifiedNs[name] = f.modified_ns;
    if (!keep) campus.drafts[name] = f.text;
  }
  campus.loaded = true;
  showCampusFile(campus.file, false);
  renderCampusSummary(body.summary);
  renderCampusIssues(body);
}

function showCampusFile(file, stashCurrent = true) {
  if (stashCurrent) campus.drafts[campus.file] = editor.value;
  campus.file = file;
  editor.value = campus.drafts[file] ?? '';
  document.querySelectorAll('#campus-files button').forEach((b) =>
    b.classList.toggle('on', b.dataset.file === file));
  document.querySelectorAll('.format').forEach((f) => { f.hidden = f.id !== `format-${file}`; });
  $('campus-sample').textContent = campus.samples[file] || '';
  $('btn-campus-template').hidden = file !== 'rooms';
  updateCampusState();
}

function updateCampusState() {
  campus.drafts[campus.file] = editor.value;
  const state = $('campus-state');
  const dirty = campusDirty(campus.file);
  const others = ['rooms', 'graph', 'coverage'].filter((f) => f !== campus.file && campusDirty(f));
  state.textContent = `${campus.file}.yaml · ${dirty ? 'unsaved changes' : (campus.modifiedNs[campus.file] ? 'saved' : 'not created yet')}`
    + (others.length ? ` · also unsaved: ${others.join(', ')}` : '');
  state.style.color = dirty ? 'var(--warn)' : '';
  $('btn-campus-example').disabled = editor.value.trim() !== '';
}

function renderCampusSummary(s) {
  if (!s) return;
  $('v-campus-rooms').textContent = String(s.rooms);
  $('v-campus-graph').textContent = s.nodes ? `${s.nodes} nodes, ${s.edges} edges` : 'none';
  const tbody = $('campus-coverage').querySelector('tbody');
  tbody.replaceChildren();
  if (!s.blocks.length) {
    const tr = el('tr');
    const td = el('td', 'muted', 'No blocks yet.');
    td.colSpan = 4;
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }
  const label = { complete: 'complete', partial: 'partial', not_surveyed: 'not surveyed', unlisted: 'not in coverage' };
  for (const b of s.blocks) {
    const tr = el('tr');
    tr.appendChild(el('td', '', b.block));
    tr.appendChild(el('td', `st-${b.status}`, label[b.status] || b.status));
    tr.appendChild(el('td', '', b.floors.length ? b.floors.join(', ') : '—'));
    tr.appendChild(el('td', '', String(b.rooms)));
    tbody.appendChild(tr);
  }
}

// Issues for every file are shown, not just the open one: saving rooms.yaml is
// checked against the graph and coverage too, and a problem there blocks it.
function renderCampusIssues(result, headline) {
  const list = $('campus-issues');
  list.replaceChildren();
  if (headline) list.appendChild(el('li', result.ok ? 'ok' : 'err', headline));
  const items = [...(result.errors || []), ...(result.warnings || [])];
  for (const i of items) {
    const li = el('li', i.severity === 'error' ? 'err' : 'warn');
    const where = `${i.file}.yaml · ${i.where}${i.line ? ` · line ${i.line}` : ''}`;
    li.appendChild(el('span', 'where', where));
    li.appendChild(document.createTextNode(i.message));
    if (i.line) {
      li.classList.add('jump');
      li.title = 'Go to this line';
      li.addEventListener('click', () => {
        if (campus.file !== i.file) showCampusFile(i.file);
        jumpToLine(i.line);
      });
    }
    list.appendChild(li);
  }
}

function jumpToLine(line) {
  const lines = editor.value.split('\n');
  const start = lines.slice(0, line - 1).reduce((n, l) => n + l.length + 1, 0);
  editor.focus();
  editor.setSelectionRange(start, start + (lines[line - 1] || '').length);
  const lineHeight = parseFloat(getComputedStyle(editor).lineHeight) || 18;
  editor.scrollTop = Math.max(0, (line - 4) * lineHeight);
}

async function campusPost(path, body) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (res.status === 401) { window.location = '/login'; return null; }
  return { res, body: await res.json().catch(() => ({})) };
}

$('campus-files').addEventListener('click', (e) => {
  const btn = e.target.closest('button');
  if (btn) showCampusFile(btn.dataset.file);
});

editor.addEventListener('input', updateCampusState);
editor.addEventListener('keydown', (e) => {
  // YAML is indented with spaces; a Tab that leaves the editor is useless here.
  if (e.key === 'Tab' && !e.shiftKey) {
    e.preventDefault();
    editor.setRangeText('  ', editor.selectionStart, editor.selectionEnd, 'end');
    updateCampusState();
  }
  if ((e.ctrlKey || e.metaKey) && e.key === 's') {
    e.preventDefault();
    saveCampus();
  }
});

$('btn-campus-validate').addEventListener('click', async () => {
  const out = await campusPost('/api/campus/validate', { file: campus.file, text: editor.value });
  if (!out) return;
  if (!out.res.ok) {
    renderCampusIssues({ ok: false }, out.body.detail || `check failed (${out.res.status})`);
    return;
  }
  const n = out.body.errors.length, w = out.body.warnings.length;
  renderCampusIssues(out.body, n ? `${n} error${n === 1 ? '' : 's'} — fix before saving`
    : w ? `No errors, ${w} warning${w === 1 ? '' : 's'}` : 'No problems found');
  renderCampusSummary(out.body.summary);
});

async function saveCampus() {
  const file = campus.file;
  const text = editor.value;
  const btn = $('btn-campus-save');
  btn.disabled = true;
  try {
    const out = await campusPost('/api/campus/save', { file, text, modified_ns: campus.modifiedNs[file] });
    if (!out) return;
    const { res, body } = out;
    if (!res.ok) {
      const d = body.detail;
      if (d && typeof d === 'object') renderCampusIssues(d, d.message);
      else renderCampusIssues({ ok: false }, d || `save failed (${res.status})`);
      return;
    }
    campus.saved[file] = text;
    campus.modifiedNs[file] = body.modified_ns;
    const w = body.warnings.length;
    renderCampusIssues(body, `Saved ${file}.yaml at ${new Date().toLocaleTimeString()}`
      + (body.backup ? ', previous version backed up' : '')
      + (w ? ` · ${w} warning${w === 1 ? '' : 's'}` : ''));
    renderCampusSummary(body.summary);
  } finally {
    btn.disabled = false;
    updateCampusState();
  }
}

$('btn-campus-save').addEventListener('click', saveCampus);

// Reloads the open file from disk, so it is also how to pick up a version
// someone else saved.
$('btn-campus-revert').addEventListener('click', async () => {
  campus.drafts[campus.file] = editor.value;
  if (campusDirty(campus.file) && !confirm(`Discard your changes to ${campus.file}.yaml and reload it?`)) return;
  await loadCampus(campus.file);
});

$('btn-campus-template').addEventListener('click', () => {
  const text = editor.value.replace(/\s*$/, '\n');
  editor.value = (text.trim() ? text : '') + ROOM_TEMPLATE;
  updateCampusState();
  // Put the cursor after "code: " in the new entry.
  const pos = editor.value.lastIndexOf('- code: ') + '- code: '.length;
  editor.focus();
  editor.setSelectionRange(pos, pos);
  editor.scrollTop = editor.scrollHeight;
});

$('btn-campus-example').addEventListener('click', () => {
  if (editor.value.trim()) return;
  editor.value = campus.samples[campus.file] || '';
  updateCampusState();
});

async function tryCampusQuestion() {
  const text = $('campus-q').value.trim();
  if (!text) return;
  campus.drafts[campus.file] = editor.value;
  const drafts = {};
  for (const f of ['rooms', 'graph', 'coverage']) if (campusDirty(f)) drafts[f] = campus.drafts[f];

  const out = await campusPost('/api/campus/try', { text, drafts });
  if (!out) return;
  const { res, body } = out;
  $('campus-try-out').hidden = false;
  if (!res.ok) {
    $('v-try-matched').textContent = body.detail || `failed (${res.status})`;
    return;
  }
  $('v-try-normalized').textContent = body.normalized || '—';

  let matched;
  if (body.hits.length) {
    matched = body.hits.map((h) => `${h.code} (${h.matched} "${h.key}")`).join(', ');
    if (body.ambiguous) matched += ' — ambiguous, Neo asks which';
  } else if (body.listing.length) {
    matched = `listing ${body.blocks.join(', ')} block: ${body.listing.join(', ')}`
      + (body.listing_total > body.listing.length ? ` (+${body.listing_total - body.listing.length})` : '');
  } else if (body.unknown_codes.length) {
    matched = `nothing — not in the directory: ${body.unknown_codes.join(', ')}`;
  } else {
    matched = 'nothing';
  }
  if (body.used_drafts.length) matched += `  · using unsaved ${body.used_drafts.join(', ')}`;
  const m = $('v-try-matched');
  m.textContent = matched;
  m.style.color = body.hits.length || body.listing.length ? 'var(--good)' : 'var(--warn)';

  $('v-try-offline').textContent = body.offline_reply || '(no directory answer: the degraded reply)';
  $('v-try-context').textContent = body.context;
}

$('btn-campus-try').addEventListener('click', tryCampusQuestion);
$('campus-q').addEventListener('keydown', (e) => { if (e.key === 'Enter') tryCampusQuestion(); });

window.addEventListener('beforeunload', (e) => {
  if (!campus.loaded) return;
  campus.drafts[campus.file] = editor.value;
  if (['rooms', 'graph', 'coverage'].some(campusDirty)) e.preventDefault();
});

// ------------------------------------------------------------------ boot

loadConfig().then(() => { $('deadman-ms').textContent = config.joyDeadmanMs; });
connectState();
pollLinkStatus();
pollDialogStatus();
pollModelStatus();
loadAudioStatus();
setInterval(pollLinkStatus, POLL_MS);
setInterval(pollDialogStatus, POLL_MS);
setInterval(pollModelStatus, POLL_MS);
