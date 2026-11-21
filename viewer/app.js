/* D Studio — annotation UI.
 * No build step, no framework: plain DOM + canvas. Served by tools/serve.py.
 * All user-facing text goes through I18N.t() (viewer/i18n.js).
 */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const I18N = window.I18N || { t: (k) => k, onChange: () => {}, lang: 'en' };
  const t = (key, params) => I18N.t(key, params);

  const DATA_BASE = '../data/';
  const API = '/api/';
  const TRAIL_FRAMES = 18;
  const RIPPLE_MS = 400;
  const KEY_CHIP_MS = 700;
  const IDLE_GAP_S = 2.0;
  const MIN_SEG_S = 0.5;
  const DRAFT_DELAY_MS = 1500;

  const S = {
    datasets: [],
    dataDir: '',
    id: null,
    info: null,          // /api/datasets entry of the open recording
    meta: null,
    geo: null,
    xs: null, ys: null, bs: null,
    events: [],
    evT: null,           // Float64Array of event times (s), for binary search
    appSpans: [],
    cuts: [],            // [{t, clip, gap, clamped}] clip boundaries (s)
    masks: [],           // [{t0, t1, app, auto}] privacy masks / auto-pauses (s)
    segments: [],
    selected: -1,
    dirty: false,
    loaded: false,
    cursorInVideo: false,
    opts: { cursor: true, trail: true, events: true },
    bins: [],
    empty: null,         // {key, params} shown over the video, or null
    save: { key: 'save.saved', dirty: false },
    mediaKey: '',
    loadSeq: 0,
  };

  const video = $('video');
  const overlay = $('overlay');
  const wrap = $('video-wrap');
  const octx = overlay.getContext('2d');
  const timeline = $('timeline');
  const tctx = timeline.getContext('2d');
  const tip = $('tl-tip');
  const TL = { W: 0, H: 96, laneSeg: 58 };

  let saveTimer = null;
  let lastFrame = -1;
  let tlDrag = false;

  // ---------------------------------------------------------------- utilities
  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
  const fmtTime = (s) => {
    s = Math.max(0, s || 0);
    const m = Math.floor(s / 60);
    const sec = s - m * 60;
    return `${String(m).padStart(2, '0')}:${sec.toFixed(3).padStart(6, '0')}`;
  };
  const parseJSONL = (text) => {
    const out = [];
    for (const line of String(text || '').split('\n')) {
      const s = line.trim();
      if (s.length < 2) continue;
      try { out.push(JSON.parse(s)); } catch (e) { /* skip a torn line */ }
    }
    return out;
  };
  const enc = (s) => encodeURIComponent(s);
  const dsUrl = (id, rel) => `${DATA_BASE}${enc(id)}/${rel.split('/').map(enc).join('/')}`;

  async function fetchText(path) {
    const res = await fetch(path, { cache: 'no-store' });
    if (!res.ok) throw new Error(`${res.status} ${decodeURIComponent(path)}`);
    return res.text();
  }
  async function fetchJSON(path) {
    return JSON.parse(await fetchText(path));
  }
  const fps = () => {
    const f = S.meta && +S.meta.fps;
    return f > 0 ? f : 30;
  };
  const totalFrames = () => Math.max(0, (S.meta && +S.meta.total_frames) || 0);
  const durationS = () => (S.meta ? totalFrames() / fps() : 0);
  // frame k is on screen during [k/fps, (k+1)/fps) - same rule as the packager
  const frameNow = () => clamp(Math.floor(video.currentTime * fps() + 1e-3), 0, Math.max(0, totalFrames() - 1));

  function lowerBound(arr, x) {
    let lo = 0, hi = arr.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (arr[mid] < x) lo = mid + 1; else hi = mid;
    }
    return lo;
  }

  // ------------------------------------------------------------ status text
  function showEmpty(key, params) {
    S.empty = key ? { key, params: params || null } : null;
    renderEmpty();
  }
  function renderEmpty() {
    const el = $('empty');
    if (!S.empty) { el.classList.add('hidden'); return; }
    el.removeAttribute('data-i18n');
    el.textContent = t(S.empty.key, S.empty.params || undefined);
    el.classList.remove('hidden');
  }
  function setSaveState(key, dirty) {
    S.save = { key, dirty: !!dirty };
    renderSaveState();
  }
  function renderSaveState() {
    const el = $('save-state');
    el.textContent = t(S.save.key);
    el.classList.toggle('dirty', S.save.dirty);
  }

  // -------------------------------------------------------------- data loading
  function currentHashId() {
    const raw = location.hash.replace(/^#/, '');
    if (!raw) return '';
    try { return decodeURIComponent(raw); } catch (e) { return raw; }
  }

  function renderDatasetOptions() {
    const sel = $('dataset-select');
    const keep = S.id;
    sel.innerHTML = '';
    if (!S.datasets.length) {
      const o = document.createElement('option');
      o.textContent = t('ds.none');
      o.value = '';
      sel.appendChild(o);
      sel.disabled = true;
      return;
    }
    sel.disabled = false;
    for (const d of S.datasets) {
      const o = document.createElement('option');
      o.value = d.id;
      const secs = ((+d.total_frames || 0) / (+d.fps || 30)).toFixed(0);
      let label = t('ds.option', { title: d.title || d.id, secs });
      if (d.annotations) label += ` · ${t('ds.annotated', { n: d.annotations })}`;
      o.textContent = label;
      o.title = d.id;
      sel.appendChild(o);
    }
    if (keep && S.datasets.some((d) => d.id === keep)) sel.value = keep;
  }

  async function loadDatasets(force) {
    let list;
    try {
      list = await fetchJSON(API + 'datasets');
    } catch (e) {
      S.datasets = [];
      renderDatasetOptions();
      showEmpty('empty.server', { err: e.message });
      return;
    }
    S.datasets = Array.isArray(list.datasets) ? list.datasets : [];
    S.dataDir = list.data_dir || '';
    renderDatasetOptions();
    if (!S.datasets.length) {
      S.loaded = false;
      S.id = null;
      S.meta = null;
      S.segments = [];
      renderSegmentList();
      updateQuality();
      invalidateTimeline();
      showEmpty('empty.none', { dir: S.dataDir || 'data' });
      return;
    }
    let stored = null;
    try { stored = sessionStorage.getItem('dts:id'); } catch (e) { stored = null; }
    const wanted = currentHashId() || S.id || stored;
    const id = S.datasets.some((d) => d.id === wanted) ? wanted : S.datasets[0].id;
    $('dataset-select').value = id;
    await loadDataset(id, force);
  }

  async function loadDataset(id, force) {
    if (!id) return;
    await flushDraft();
    const seq = ++S.loadSeq;
    S.loaded = false;
    S.id = id;
    S.info = S.datasets.find((d) => d.id === id) || null;
    S.selected = -1;
    S.meta = null;
    S.segments = [];
    S.events = [];
    S.evT = new Float64Array(0);
    S.cuts = [];
    S.masks = [];
    S.appSpans = [];
    S.dirty = false;
    lastFrame = -1;
    hideTip();
    if (currentHashId() !== id) location.hash = enc(id);
    try { sessionStorage.setItem('dts:id', id); } catch (e) { /* ignore */ }
    showEmpty('empty.loading');
    renderSegmentList();
    syncForm();
    setSaveState('save.saved');

    try {
      // fresh summary (file times change while annotating)
      try {
        const fresh = await fetchJSON(`${API}dataset?id=${enc(id)}`);
        if (fresh && fresh.id === id) {
          S.info = fresh;
          const k = S.datasets.findIndex((d) => d.id === id);
          if (k >= 0) S.datasets[k] = fresh;
        }
      } catch (e) { /* keep the list entry */ }
      if (seq !== S.loadSeq) return;
      const meta = await fetchJSON(dsUrl(id, 'metadata.json'));
      if (seq !== S.loadSeq) return;           // user switched again meanwhile
      S.meta = meta && typeof meta === 'object' ? meta : {};
      const total = totalFrames();

      let geo = null;
      try { geo = await fetchJSON(dsUrl(id, 'view/geometry.json')); } catch (e) { geo = null; }
      S.geo = geo && +geo.width > 0 ? geo : { originX: 0, originY: 0, width: 0, height: 0 };

      const [mouseText, eventsText] = await Promise.all([
        fetchText(dsUrl(id, 'view/mouse.jsonl')).catch(() => ''),
        fetchText(dsUrl(id, 'view/events.jsonl')).catch(() => ''),
      ]);
      if (seq !== S.loadSeq) return;
      buildFrameArrays(mouseText, total);
      S.events = parseJSONL(eventsText).filter((e) => e && Number.isFinite(e.t)).sort((a, b) => a.t - b.t);
      S.evT = Float64Array.from(S.events, (e) => e.t / 1000);
      S.appSpans = buildAppSpans();
      S.cuts = buildCuts();
      S.masks = buildMasks();
      buildDensity();

      const narration = await loadNarration(id);
      if (seq !== S.loadSeq) return;
      if (narration && Array.isArray(narration.annotations) && narration.annotations.length) {
        S.segments = toSegments(narration.annotations);
      }
      $('f-summary').value = (narration && typeof narration.summary === 'string') ? narration.summary : '';

      applyCursorDefault();
      const mediaName = (S.info && S.info.video_file) || S.meta.video_file || 'clip.mp4';
      const hasVideo = !S.info || S.info.has_video !== false;
      const key = `${id}/${mediaName}`;
      if (hasVideo && (force || S.mediaKey !== key || !video.currentSrc)) {
        S.mediaKey = key;
        video.src = `${dsUrl(id, mediaName)}?t=${Date.now()}`;
      } else if (!hasVideo) {
        S.mediaKey = '';
        video.removeAttribute('src');
        video.load();
      }
      video.playbackRate = +$('speed').value || 1;
      showEmpty(hasVideo ? null : 'empty.noVideo');
      S.loaded = true;
      selectSegment(S.segments.length ? 0 : -1);
      renderSegmentList();
      updateQuality();
      sizeTimeline();
      if (hasVideo) video.play().catch(() => {});
    } catch (err) {
      if (seq !== S.loadSeq) return;
      showEmpty('empty.loadFailed', { err: err.message, dir: `${S.dataDir || 'data'}/${id}` });
      console.error(err);
    }
  }

  // narration.json vs draft.json: whichever the server saw written last wins
  // (a draft keeps autosaving after an export), then this browser's copy.
  async function loadNarration(id) {
    const info = S.info || {};
    const known = typeof info.narration_mtime === 'number';
    const has = (o) => !!(o && Array.isArray(o.annotations) && o.annotations.length);
    const get = (name) => fetchJSON(dsUrl(id, name)).catch(() => null);
    const narration = (!known || info.narration_mtime > 0) ? await get('narration.json') : null;
    const draft = (!known || info.draft_mtime > 0) ? await get('draft.json') : null;
    const draftNewer = known && info.draft_mtime > info.narration_mtime + 0.5;
    if (has(draft) && (draftNewer || !has(narration))) return draft;
    if (has(narration)) return narration;
    let local = null;
    try { local = JSON.parse(localStorage.getItem(`dts:draft:${id}`) || 'null'); } catch (e) { local = null; }
    if (has(local)) return local;
    return narration || draft || null;
  }

  function toSegments(anns) {
    return anns
      .filter((a) => a && typeof a === 'object')
      .map((a) => ({
        id: 0,
        start: Number.isFinite(+a.start_s) ? +a.start_s : 0,
        end: Number.isFinite(+a.end_s) ? +a.end_s : 0,
        action: String(a.action || ''), intent: String(a.intent || ''),
        app: String(a.app || ''), details: String(a.details || ''),
      }))
      .sort((a, b) => a.start - b.start)
      .map((s, i) => ({ ...s, id: i }));
  }

  function applyCursorDefault() {
    // The recorder now draws the OS cursor into the video (Markov-style); do
    // not paint a second one on top unless the annotator turns it back on.
    let civ = S.meta && S.meta.cursor_in_video;
    if (typeof civ !== 'boolean') civ = !!(S.info && S.info.cursor_in_video);
    S.cursorInVideo = civ;
    S.opts.cursor = !civ;
    $('chk-cursor').checked = !civ;
    $('cursor-note').classList.toggle('hidden', !civ);
  }

  function buildFrameArrays(text, total) {
    S.xs = new Int32Array(total);
    S.ys = new Int32Array(total);
    S.bs = new Uint8Array(total);
    let prev = -1, x = 0, y = 0, b = 0;
    for (const line of String(text || '').split('\n')) {
      if (line.length < 5) continue;
      let o;
      try { o = JSON.parse(line); } catch (e) { continue; }
      const f = o.f;
      if (!(f >= 0 && f < total)) continue;
      for (let k = prev + 1; k < f; k++) { S.xs[k] = x; S.ys[k] = y; S.bs[k] = b; }
      x = o.x | 0; y = o.y | 0; b = o.b | 0;
      S.xs[f] = x; S.ys[f] = y; S.bs[f] = b;
      prev = Math.max(prev, f);
    }
    for (let k = prev + 1; k < total; k++) { S.xs[k] = x; S.ys[k] = y; S.bs[k] = b; }
  }

  function buildAppSpans() {
    const spans = [];
    let cur = null;
    for (const e of S.events) {
      if (e.type !== 'active_app') continue;
      if (cur) { cur.t1 = e.t / 1000; spans.push(cur); }
      cur = { t0: e.t / 1000, t1: durationS(), name: e.name || e.appName || '' };
    }
    if (cur) spans.push(cur);
    return spans;
  }

  function appAt(time) {
    let name = '';
    for (const s of S.appSpans) { if (s.t0 <= time) name = s.name; else break; }
    return name;
  }

  function buildCuts() {
    const clips = (S.meta && Array.isArray(S.meta.clips)) ? S.meta.clips : [];
    const out = [];
    clips.forEach((clip, i) => {
      if (i === 0 || !clip) return;
      const fs = +clip.frame_start;
      if (!Number.isFinite(fs)) return;
      out.push({
        t: fs / fps(),
        clip: (Number.isFinite(+clip.clip_index) ? +clip.clip_index : i) + 1,
        gap: Math.max(0, +clip.gap_before_ms || 0) / 1000,
        clamped: !!clip.gap_before_clamped,
      });
    });
    return out.sort((a, b) => a.t - b.t);
  }

  function maskApp(e) {
    return String(e.appName || e.exeName || e.maskDetail || e.name || '');
  }

  function buildMasks() {
    // Pair privacy_mask_start/end. An auto-pause (trigger non_target_app) cuts
    // time, so start and end usually sit on both sides of one clip boundary.
    const dur = durationS();
    const nextCut = (time) => {
      for (const c of S.cuts) if (c.t >= time - 1e-6) return c.t;
      return dur;
    };
    const prevCut = (time) => {
      let v = 0;
      for (const c of S.cuts) { if (c.t <= time + 1e-6) v = c.t; else break; }
      return v;
    };
    const out = [];
    let open = null;
    for (const e of S.events) {
      if (e.type !== 'privacy_mask_start' && e.type !== 'privacy_mask_end') continue;
      const time = e.t / 1000;
      const auto = (e.trigger || e.maskTrigger) === 'non_target_app';
      if (e.type === 'privacy_mask_start') {
        if (open) { open.t1 = time; out.push(open); }
        open = { t0: time, t1: time, app: maskApp(e), auto };
      } else if (open) {
        open.t1 = Math.max(open.t0, time);
        if (!open.app) open.app = maskApp(e);
        open.auto = open.auto || auto;
        out.push(open);
        open = null;
      } else {
        // the start fell into a cut gap (dropped by the packager)
        out.push({ t0: auto ? prevCut(time) : time, t1: time, app: maskApp(e), auto });
      }
    }
    if (open) {
      open.t1 = open.auto ? nextCut(open.t0) : dur;
      out.push(open);
    }
    return out;
  }

  function buildDensity() {
    const dur = durationS() || 1;
    const n = clamp(Math.round(dur), 60, 2400);
    const bins = new Array(n).fill(0).map(() => ({ click: 0, key: 0, scroll: 0, drag: 0, app: 0, other: 0 }));
    const idx = (time) => clamp(Math.floor((time / dur) * n), 0, n - 1);
    for (const e of S.events) {
      const k = idx(e.t / 1000);
      if (e.type === 'click') bins[k].click++;
      else if (e.type === 'key') bins[k].key++;
      else if (e.type === 'scroll') bins[k].scroll++;
      else if (e.type === 'drag') bins[k].drag++;
      else if (e.type === 'active_app') bins[k].app++;
      else bins[k].other++;
    }
    S.bins = bins;
  }

  // --------------------------------------------------------------- rendering
  function resizeOverlay() {
    const vr = video.getBoundingClientRect();
    const wr = wrap.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(1, Math.round(vr.width)), h = Math.max(1, Math.round(vr.height));
    overlay.style.left = `${vr.left - wr.left}px`;
    overlay.style.top = `${vr.top - wr.top}px`;
    overlay.style.width = `${w}px`;
    overlay.style.height = `${h}px`;
    if (overlay.width !== Math.round(w * dpr) || overlay.height !== Math.round(h * dpr)) {
      overlay.width = Math.round(w * dpr);
      overlay.height = Math.round(h * dpr);
    }
    const geoW = (S.geo && +S.geo.width) || video.videoWidth || 1920;
    const scale = (w * dpr) / geoW;
    const ox = S.geo ? +S.geo.originX || 0 : 0;
    const oy = S.geo ? +S.geo.originY || 0 : 0;
    return { w: overlay.width, h: overlay.height, scale, ox, oy, cssW: w, cssH: h, dpr };
  }

  function renderFrame() {
    if (!S.loaded) return;
    const f = frameNow();
    const now = video.currentTime;
    const R = resizeOverlay();
    octx.clearRect(0, 0, R.w, R.h);
    if (S.xs && f < S.xs.length) {
      const toPx = (x, y) => [(x - R.ox) * R.scale, (y - R.oy) * R.scale];
      const [cx, cy] = toPx(S.xs[f], S.ys[f]);

      if (S.opts.trail) {
        const pts = [];
        for (let k = Math.max(0, f - TRAIL_FRAMES); k <= f; k++) {
          const [px, py] = toPx(S.xs[k], S.ys[k]);
          if (!pts.length || Math.abs(px - pts[pts.length - 1][0]) + Math.abs(py - pts[pts.length - 1][1]) > 1.5) {
            pts.push([px, py]);
          }
        }
        Overlay.drawTrail(octx, pts, R.scale);
      }

      if (S.opts.cursor) {
        const pressed = (S.bs[f] & 1) !== 0;
        Overlay.drawCursor(octx, cx, cy, R.scale * 1.15, pressed);
        if (pressed) {
          octx.save();
          octx.strokeStyle = 'rgba(77,163,255,.85)';
          octx.lineWidth = Math.max(1, 1.5 * R.scale);
          octx.beginPath();
          octx.arc(cx, cy, 9 * R.scale, 0, Math.PI * 2);
          octx.stroke();
          octx.restore();
        }
      }

      if (S.opts.events && S.evT && S.evT.length) {
        // only events in the last ~0.7 s matter: binary search the window
        const from = lowerBound(S.evT, now - Math.max(RIPPLE_MS, KEY_CHIP_MS) / 1000 - 0.05);
        let keyText = '';
        let keyT = 0;
        for (let i = from; i < S.events.length; i++) {
          const e = S.events[i];
          const et = S.evT[i];
          if (et > now) break;
          if (e.type === 'click' && e.isDown && now - et <= RIPPLE_MS / 1000) {
            const [px, py] = toPx(e.x, e.y);
            Overlay.drawRipple(octx, px, py, R.scale, (now - et) / (RIPPLE_MS / 1000), e.button);
          } else if (e.type === 'key' && e.isDown && now - et <= KEY_CHIP_MS / 1000) {
            keyText = keyLabel(e);
            keyT = e.t;
          }
        }
        if (keyText) Overlay.drawKeyChip(octx, keyText, R.w, R.h, R.scale, KEY_CHIP_MS - (now * 1000 - keyT));
      }
    }
    if (f !== lastFrame) {
      lastFrame = f;
      $('frame-label').textContent = `f ${f}`;
      $('time-label').textContent = `${fmtTime(now)} / ${fmtTime(durationS())}`;
      presentTimeline();
    }
  }

  const VK_NAMES = {
    8: 'Backspace', 9: 'Tab', 13: 'Enter', 16: 'Shift', 17: 'Ctrl', 18: 'Alt', 19: 'Pause',
    20: 'CapsLock', 27: 'Esc', 32: 'Space', 33: 'PgUp', 34: 'PgDn', 35: 'End', 36: 'Home',
    37: '←', 38: '↑', 39: '→', 40: '↓', 44: 'PrtSc', 45: 'Ins', 46: 'Del',
    91: 'Win', 92: 'Win', 93: 'Menu', 106: 'Num *', 107: 'Num +', 109: 'Num -', 110: 'Num .', 111: 'Num /',
    144: 'NumLock', 145: 'ScrollLock', 160: 'Shift', 161: 'Shift', 162: 'Ctrl', 163: 'Ctrl', 164: 'Alt', 165: 'Alt',
    186: ';', 187: '=', 188: ',', 189: '-', 190: '.', 191: '/', 192: '`', 219: '[', 220: '\\', 221: ']', 222: "'",
  };

  function keyLabel(e) {
    const mods = [];
    const m = e.modifiers | 0;
    if (m & 2) mods.push('Ctrl');
    if (m & 1) mods.push('Shift');
    if (m & 4) mods.push('Alt');
    if (m & 8) mods.push('Win');
    const vk = e.keyCode | 0;
    let k = typeof e.characters === 'string' ? e.characters : '';
    if (VK_NAMES[vk]) k = VK_NAMES[vk];
    else if (vk >= 112 && vk <= 135) k = `F${vk - 111}`;
    else if (vk >= 96 && vk <= 105) k = `Num ${vk - 96}`;
    else if (!k || k.charCodeAt(0) < 32 || (mods.length && ((vk >= 65 && vk <= 90) || (vk >= 48 && vk <= 57)))) {
      // Ctrl+Z arrives as "\u001a": show the key, not the control character
      k = (vk >= 65 && vk <= 90) || (vk >= 48 && vk <= 57) ? String.fromCharCode(vk) : `VK ${vk}`;
    } else if (k.length === 1) {
      k = k.toUpperCase() === k.toLowerCase() ? k : (mods.length ? k.toUpperCase() : k);
    }
    // a lone modifier press shows once, not as "Ctrl+Ctrl"
    const modName = { Ctrl: 'Ctrl', Shift: 'Shift', Alt: 'Alt', Win: 'Win' }[k];
    const list = modName ? mods.filter((x) => x !== modName) : mods;
    return list.length ? `${list.join('+')}+${k}` : k;
  }

  // Timeline: static layers render once into an offscreen cache; only the
  // playhead is composited per frame. Cheap, and no ghosting of old playheads.
  let tlDirty = true;
  let tlCache = null;

  function sizeTimeline() {
    const rect = timeline.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    TL.W = Math.max(200, rect.width);
    TL.H = 96;
    timeline.width = Math.round(TL.W * dpr);
    timeline.height = Math.round(TL.H * dpr);
    tctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    tlDirty = true;
    presentTimeline();
  }

  function paintTimelineCache(dpr) {
    if (!tlCache) tlCache = document.createElement('canvas');
    tlCache.width = timeline.width;
    tlCache.height = timeline.height;
    const c = tlCache.getContext('2d');
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    const W = TL.W, H = TL.H;
    const dur = durationS() || 1;
    c.fillStyle = '#121821';
    c.fillRect(0, 0, W, H);
    if (!S.meta) return;

    const bins = S.bins;
    if (bins && bins.length) {
      const bw = W / bins.length;
      let max = 1;
      for (const b of bins) max = Math.max(max, b.click + b.key * 0.8 + b.scroll * 0.6 + b.drag * 0.5 + b.other * 0.2);
      const laneH = TL.laneSeg - 14;
      for (let i = 0; i < bins.length; i++) {
        const b = bins[i];
        const v = b.click + b.key * 0.8 + b.scroll * 0.6 + b.drag * 0.5 + b.other * 0.2;
        if (v <= 0) continue;
        const h = Math.max(2, (v / max) * laneH);
        const color = b.click ? '#4da3ff' : b.key ? '#35d19a' : b.scroll ? '#ffc857' : b.drag ? '#b28dff' : '#5a6b82';
        c.fillStyle = color;
        c.globalAlpha = 0.75;
        c.fillRect(i * bw, TL.laneSeg - 6 - h, Math.max(1, bw - 0.5), h);
      }
      c.globalAlpha = 1;
    }

    if (S.appSpans.length) {
      for (const span of S.appSpans) {
        const x = (span.t0 / dur) * W;
        const w = Math.max(1, ((Math.min(span.t1, dur) - span.t0) / dur) * W);
        c.fillStyle = 'rgba(120,190,255,.10)';
        c.fillRect(x, TL.laneSeg - 5, w, 3);
      }
    }

    // privacy masks / auto-pauses: red band on top, tick at the start
    for (const m of S.masks) {
      const x0 = (m.t0 / dur) * W;
      const x1 = (Math.min(m.t1, dur) / dur) * W;
      c.fillStyle = 'rgba(255,107,107,.85)';
      c.fillRect(Math.min(x0, W - 3), 0, Math.max(3, x1 - x0), 5);
      if (x1 - x0 > 3) {
        c.fillStyle = 'rgba(255,107,107,.10)';
        c.fillRect(x0, 5, x1 - x0, TL.laneSeg - 5);
      }
    }

    // recording cuts (pause / auto-pause / stall): full-height dashed line
    if (S.cuts.length) {
      c.save();
      c.strokeStyle = 'rgba(230,237,243,.55)';
      c.lineWidth = 1;
      c.setLineDash([3, 3]);
      for (const cut of S.cuts) {
        const x = Math.round((cut.t / dur) * W) + 0.5;
        c.beginPath(); c.moveTo(x, 6); c.lineTo(x, H); c.stroke();
      }
      c.setLineDash([]);
      c.fillStyle = 'rgba(230,237,243,.85)';
      for (const cut of S.cuts) {
        const x = (cut.t / dur) * W;
        c.beginPath(); c.moveTo(x - 4, 6); c.lineTo(x + 4, 6); c.lineTo(x, 11); c.closePath(); c.fill();
      }
      c.restore();
    }

    for (let i = 0; i < S.segments.length; i++) {
      const s = S.segments[i];
      const x = (s.start / dur) * W;
      const w = Math.max(2, ((s.end - s.start) / dur) * W);
      const active = i === S.selected;
      c.fillStyle = active ? 'rgba(77,163,255,.45)' : 'rgba(77,163,255,.18)';
      c.fillRect(x, TL.laneSeg + 1, Math.max(1, w - 1), H - TL.laneSeg - 8);
      if (active) {
        c.strokeStyle = '#4da3ff';
        c.lineWidth = 1.5;
        c.strokeRect(x + 0.5, TL.laneSeg + 1.5, Math.max(1, w - 2), H - TL.laneSeg - 9);
      }
      if (w > 26) {
        c.fillStyle = 'rgba(230,237,243,.85)';
        c.font = '10px "Segoe UI", system-ui, sans-serif';
        c.fillText(String(i + 1), x + 4, TL.laneSeg + 14);
      }
    }
  }

  function presentTimeline() {
    if (!TL.W) return;
    const dpr = window.devicePixelRatio || 1;
    if (tlDirty || !tlCache) { paintTimelineCache(dpr); tlDirty = false; }
    tctx.setTransform(1, 0, 0, 1, 0, 0);
    tctx.clearRect(0, 0, timeline.width, timeline.height);
    if (tlCache.width && tlCache.height) tctx.drawImage(tlCache, 0, 0);
    tctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (!S.meta) return;
    const x = (video.currentTime / (durationS() || 1)) * TL.W;
    tctx.strokeStyle = '#ff8f5e';
    tctx.lineWidth = 1.5;
    tctx.beginPath(); tctx.moveTo(x, 0); tctx.lineTo(x, TL.H); tctx.stroke();
  }

  function invalidateTimeline() { tlDirty = true; presentTimeline(); }

  // ---------------------------------------------------------- timeline tips
  function tipLines(x, y, width) {
    const dur = durationS();
    if (!S.meta || !dur) return [];
    const time = clamp((x / width) * dur, 0, dur);
    const tol = (5 / width) * dur;   // ±5 px
    const lines = [t('tip.time', { t: fmtTime(time), f: Math.min(totalFrames() - 1, Math.floor(time * fps())) })];
    const app = appAt(time);
    if (app) lines.push(t('tip.app', { app }));
    for (const cut of S.cuts) {
      if (Math.abs(cut.t - time) <= tol) {
        lines.push(t(cut.clamped ? 'tip.cutClamped' : 'tip.cut', { n: cut.clip, gap: cut.gap.toFixed(1) }));
      }
    }
    for (const m of S.masks) {
      if (time >= m.t0 - tol && time <= m.t1 + tol) {
        lines.push(t(m.auto ? 'tip.autoPause' : 'tip.mask', { app: m.app || t('tip.unknownApp') }));
      }
    }
    if (y > TL.laneSeg) {
      const hit = S.segments.findIndex((s) => time >= s.start && time <= s.end);
      if (hit >= 0) {
        const s = S.segments[hit];
        lines.push(`${t('tip.segment', { n: hit + 1 })} · ${fmtTime(s.start)} → ${fmtTime(s.end)}`);
        if (s.action.trim()) lines.push(s.action.trim().slice(0, 120));
      }
    }
    return lines;
  }

  function showTip(ev) {
    const r = timeline.getBoundingClientRect();
    const x = ev.clientX - r.left;
    const y = ev.clientY - r.top;
    const lines = tipLines(x, y, r.width || 1);
    if (!lines.length) { hideTip(); return; }
    tip.textContent = lines.join('\n');
    tip.classList.remove('hidden');
    const tw = tip.offsetWidth;
    tip.style.left = `${clamp(x - tw / 2, 0, Math.max(0, r.width - tw))}px`;
  }

  function hideTip() { tip.classList.add('hidden'); }

  // ---------------------------------------------------------------- segments
  function autoSegment() {
    if (!S.meta || !S.xs) return;
    const dur = durationS();
    const times = [];
    for (let f = 1; f < S.xs.length; f++) {
      if (Math.abs(S.xs[f] - S.xs[f - 1]) + Math.abs(S.ys[f] - S.ys[f - 1]) > 2) times.push(f / fps());
    }
    for (const e of S.events) times.push(e.t / 1000);
    times.sort((a, b) => a - b);

    const clusters = [];
    let start = null, prev = null;
    for (const time of times) {
      if (start === null) { start = time; prev = time; continue; }
      if (time - prev > IDLE_GAP_S) { clusters.push([start, prev]); start = time; }
      prev = time;
    }
    if (start !== null) clusters.push([start, prev]);

    // split clusters on foreground app changes and recording cuts
    const cuts = S.appSpans.map((s) => s.t0).concat(S.cuts.map((c) => c.t))
      .filter((x) => x > 0).sort((a, b) => a - b);
    const pieces = [];
    for (const [a, b] of clusters) {
      const inner = cuts.filter((x) => x > a + 0.2 && x < b - 0.2);
      let s = a;
      for (const x of inner) { if (x > s) { pieces.push([s, x]); s = x; } }
      pieces.push([s, b]);
    }

    const segs = [];
    for (const [a, b] of pieces.sort((p, q) => p[0] - q[0])) {
      let s = Math.max(0, a - 0.15);
      const e = Math.min(dur, b + 0.35);
      if (e - s < MIN_SEG_S) continue;
      if (segs.length && s - segs[segs.length - 1].end < 1.0) s = Math.max(s, segs[segs.length - 1].end);
      if (e - s < MIN_SEG_S) continue;
      segs.push({ id: segs.length, start: +s.toFixed(3), end: +e.toFixed(3), action: '', intent: '', app: appAt(s), details: '' });
    }
    // snap segments to fully cover the timeline where gaps are small
    for (let i = 0; i < segs.length - 1; i++) {
      if (segs[i + 1].start - segs[i].end <= 1.0) segs[i].end = segs[i + 1].start;
    }
    // a recording cut (pause / auto-pause) is a hard border: snap shared
    // borders that landed next to one exactly onto it
    for (const cut of S.cuts) {
      for (let i = 0; i < segs.length - 1; i++) {
        const b = segs[i].end;
        if (segs[i + 1].start !== b || Math.abs(b - cut.t) > 0.5) continue;
        if (cut.t > segs[i].start + 0.1 && cut.t < segs[i + 1].end - 0.1) {
          segs[i].end = segs[i + 1].start = +cut.t.toFixed(3);
        }
      }
    }
    S.segments = segs;
    S.selected = segs.length ? 0 : -1;
    renderSegmentList();
    syncForm();
    invalidateTimeline();
    updateQuality();
    markDirty();
  }

  function newSegment() {
    if (!S.meta) return;
    const dur = durationS();
    const now = clamp(video.currentTime, 0, Math.max(0, dur - 0.5));
    let end = Math.min(dur, now + 5);
    const next = S.segments.find((s) => s.start > now + 0.05);
    if (next) end = Math.min(end, next.start);
    const seg = { id: S.segments.length, start: +now.toFixed(3), end: +Math.max(now + 0.4, end).toFixed(3), action: '', intent: '', app: appAt(now), details: '' };
    S.segments.push(seg);
    S.segments.sort((a, b) => a.start - b.start).forEach((s, i) => (s.id = i));
    S.selected = S.segments.indexOf(seg);
    renderSegmentList(); syncForm(); invalidateTimeline(); updateQuality(); markDirty();
  }

  function splitAtPlayhead() {
    const i = S.selected;
    if (i < 0) return;
    const s = S.segments[i];
    const now = video.currentTime;
    if (now <= s.start + 0.2 || now >= s.end - 0.2) return;
    const right = { ...s, id: s.id + 1, start: +now.toFixed(3), action: '', intent: '', details: s.details };
    s.end = +now.toFixed(3);
    S.segments.splice(i + 1, 0, right);
    S.segments.forEach((x, k) => (x.id = k));
    S.selected = i + 1;
    renderSegmentList(); syncForm(); invalidateTimeline(); updateQuality(); markDirty();
  }

  function deleteSegment() {
    if (S.selected < 0) return;
    S.segments.splice(S.selected, 1);
    S.segments.forEach((x, k) => (x.id = k));
    S.selected = Math.min(S.selected, S.segments.length - 1);
    renderSegmentList(); syncForm(); invalidateTimeline(); updateQuality(); markDirty();
  }

  function selectSegment(i) {
    S.selected = i;
    renderSegmentList();
    syncForm();
    invalidateTimeline();
    if (i >= 0) seek(S.segments[i].start);
  }

  function renderSegmentList() {
    const ul = $('segments');
    ul.innerHTML = '';
    S.segments.forEach((s, i) => {
      const li = document.createElement('li');
      li.className = i === S.selected ? 'active' : '';
      if (!s.action.trim() || s.end <= s.start) li.classList.add('error');
      li.innerHTML = `<span class="idx">${i + 1}</span>
        <span class="body">
          <span class="range">${fmtTime(s.start)} → ${fmtTime(s.end)}</span><br>
          <span class="action ${s.action.trim() ? '' : 'empty'}">${s.action.trim() ? escapeHtml(s.action) : escapeHtml(t('seg.noAction'))}</span>
          <div class="meta">${escapeHtml(s.app || '')}${s.intent ? ' · ' + escapeHtml(s.intent) : ''}</div>
        </span>
        <button class="x" type="button" title="${escapeHtml(t('seg.delete'))}" aria-label="${escapeHtml(t('seg.delete'))}">✕</button>`;
      li.addEventListener('click', (ev) => {
        if (ev.target.classList.contains('x')) { S.selected = i; deleteSegment(); return; }
        selectSegment(i);
      });
      ul.appendChild(li);
    });
    $('seg-count').textContent = String(S.segments.length);
    renderEditorRange();
  }

  function renderEditorRange() {
    const el = $('editor-range');
    const s = S.segments[S.selected];
    if (s) {
      el.textContent = `${fmtTime(s.start)} → ${fmtTime(s.end)}`;
      el.classList.remove('muted');
    } else {
      el.textContent = t('seg.none');
      el.classList.add('muted');
    }
  }

  function refreshRow(i) {
    const li = $('segments').children[i];
    const s = S.segments[i];
    if (!li || !s) return;
    li.classList.toggle('error', !s.action.trim() || s.end <= s.start);
    li.querySelector('.range').textContent = `${fmtTime(s.start)} → ${fmtTime(s.end)}`;
    const act = li.querySelector('.action');
    act.textContent = s.action.trim() || t('seg.noAction');
    act.classList.toggle('empty', !s.action.trim());
    li.querySelector('.meta').textContent = `${s.app || ''}${s.intent ? ' · ' + s.intent : ''}`;
    if (i === S.selected) renderEditorRange();
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  // -------------------------------------------------------------------- form
  const F = ['start', 'end', 'action', 'intent', 'app', 'details'];
  const FIELDS = { start: 'f-start', end: 'f-end', action: 'f-action', intent: 'f-intent', app: 'f-app', details: 'f-details' };

  function syncForm() {
    const s = S.segments[S.selected];
    for (const k of F) {
      const el = $(FIELDS[k]);
      el.value = s ? String(s[k] ?? '') : '';
      el.disabled = !s;
    }
  }

  function readForm() {
    const s = S.segments[S.selected];
    if (!s) return;
    for (const k of F) {
      if (k === 'start' || k === 'end') s[k] = +$(FIELDS[k]).value || 0;
      else s[k] = $(FIELDS[k]).value;
    }
    if (s.end <= s.start) s.end = +(s.start + 0.4).toFixed(3);
    refreshRow(S.selected);
    markDirty();
  }

  function seek(time) {
    if (!S.meta) return;
    video.currentTime = clamp(time, 0, Math.max(0, durationS() - 0.001));
  }

  // ------------------------------------------------------------------ quality
  function validate() {
    const issues = [];
    const blocked = [];
    const dur = durationS();
    let prevEnd = -1;
    S.segments.forEach((s, i) => {
      const n = i + 1;
      if (!(s.end > s.start)) { issues.push(t('v.endBeforeStart', { n })); blocked.push(i); }
      if (s.start < prevEnd - 1e-3) { issues.push(t('v.overlap', { n })); blocked.push(i); }
      if (!s.action.trim()) { issues.push(t('v.noAction', { n })); blocked.push(i); }
      if (s.end - s.start > 60) issues.push(t('v.tooLong', { n, d: (s.end - s.start).toFixed(1) }));
      prevEnd = Math.max(prevEnd, s.end);
    });
    const covered = S.segments.reduce((a, s) => a + Math.max(0, s.end - s.start), 0);
    const cov = dur ? covered / dur : 0;
    if (cov < 0.95) issues.push(t('v.coverage', { p: (cov * 100).toFixed(1) }));
    return { issues, blocked: Array.from(new Set(blocked)), coverage: cov };
  }

  function updateQuality() {
    const el = $('quality');
    if (!S.meta) { el.textContent = ''; el.className = 'quality'; return; }
    const v = validate();
    el.className = 'quality ' + (v.blocked.length ? 'bad' : v.issues.length ? '' : 'good');
    el.textContent = v.issues.length
      ? t('v.issues', { n: v.issues.length, list: v.issues.slice(0, 3).join(t('v.sep')) + (v.issues.length > 3 ? ' …' : '') })
      : t('v.ok', { p: (v.coverage * 100).toFixed(1) });
  }

  // ------------------------------------------------------------- save/export
  function markDirty() {
    if (!S.id || !S.meta) return;
    S.dirty = true;
    setSaveState('save.dirty', true);
    invalidateTimeline();
    updateQuality();
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(() => { saveTimer = null; saveDraft(); }, DRAFT_DELAY_MS);
  }

  function narrationObject() {
    return {
      summary: $('f-summary').value.trim(),
      annotations: S.segments
        .slice()
        .sort((a, b) => a.start - b.start)
        .map((s) => ({
          start_s: +(+s.start).toFixed(3),
          end_s: +(+s.end).toFixed(3),
          action: s.action.trim(),
          intent: s.intent.trim(),
          app: s.app.trim(),
          details: s.details.trim(),
        })),
    };
  }

  function keepLocal(id, payload) {
    try { localStorage.setItem(`dts:draft:${id}`, JSON.stringify(payload)); } catch (e) { /* quota */ }
  }

  async function saveDraft() {
    if (!S.id || !S.meta) return;
    // snapshot now: the user may switch recordings while the request is in flight
    const id = S.id;
    const payload = narrationObject();
    keepLocal(id, payload);
    try {
      setSaveState('save.saving', true);
      await postSave(id, 'draft.json', payload);
      if (S.id === id) {
        S.dirty = false;
        setSaveState('save.draftSaved');
      }
    } catch (e) {
      if (S.id === id) setSaveState('save.offline', true);
    }
  }

  async function flushDraft() {
    if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; }
    if (S.dirty && S.id && S.meta) await saveDraft();
  }

  async function postSave(id, file, payload) {
    const res = await fetch(`${API}save?dataset=${enc(id)}&file=${enc(file)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error(await res.text());
    return res.json().catch(() => ({}));
  }

  async function exportNarration() {
    if (!S.meta || !S.id) return;
    const v = validate();
    if (v.blocked.length) {
      alert(t('alert.exportBlocked', { n: v.blocked.length }));
      return;
    }
    if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; }
    const id = S.id;
    const payload = narrationObject();
    keepLocal(id, payload);
    try {
      await postSave(id, 'narration.json', payload);
      await postSave(id, 'draft.json', payload).catch(() => {});
      S.dirty = false;
      setSaveState('save.exported');
      const info = S.datasets.find((d) => d.id === id);
      if (info) { info.annotations = payload.annotations.length; renderDatasetOptions(); }
    } catch (e) {
      setSaveState('save.exportLocal', true);
    }
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `narration-${id}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  }

  async function importNarration(file) {
    if (!S.meta) return;
    let obj;
    try {
      obj = JSON.parse(await file.text());
    } catch (e) {
      alert(t('alert.importFailed', { err: e.message }));
      return;
    }
    const anns = Array.isArray(obj) ? obj : (obj && Array.isArray(obj.annotations) ? obj.annotations : null);
    if (!anns) { alert(t('alert.importEmpty')); return; }
    if (S.segments.length && !confirm(t('confirm.import'))) return;
    S.segments = toSegments(anns);
    $('f-summary').value = (obj && !Array.isArray(obj) && typeof obj.summary === 'string') ? obj.summary : '';
    S.selected = S.segments.length ? 0 : -1;
    renderSegmentList(); syncForm(); invalidateTimeline(); updateQuality(); markDirty();
  }

  // ------------------------------------------------------------------ wiring
  function setStartAtPlayhead() {
    const s = S.segments[S.selected]; if (!s) return;
    s.start = +video.currentTime.toFixed(3); if (s.end <= s.start) s.end = +(s.start + 0.4).toFixed(3);
    renderSegmentList(); syncForm(); markDirty();
  }
  function setEndAtPlayhead() {
    const s = S.segments[S.selected]; if (!s) return;
    s.end = +video.currentTime.toFixed(3); if (s.end <= s.start) s.start = Math.max(0, +(s.end - 0.4).toFixed(3));
    renderSegmentList(); syncForm(); markDirty();
  }

  function bind() {
    $('dataset-select').addEventListener('change', (e) => { if (e.target.value) loadDataset(e.target.value); });
    $('btn-reload').addEventListener('click', async () => { await flushDraft(); loadDatasets(true); });

    $('btn-play').addEventListener('click', togglePlay);
    $('btn-prev').addEventListener('click', () => stepFrame(-1));
    $('btn-next').addEventListener('click', () => stepFrame(1));
    $('speed').addEventListener('change', (e) => { video.playbackRate = +e.target.value || 1; });

    $('chk-cursor').addEventListener('change', (e) => { S.opts.cursor = e.target.checked; lastFrame = -1; });
    $('chk-trail').addEventListener('change', (e) => { S.opts.trail = e.target.checked; lastFrame = -1; });
    $('chk-events').addEventListener('change', (e) => { S.opts.events = e.target.checked; lastFrame = -1; });

    $('btn-auto').addEventListener('click', () => {
      if (!S.meta) return;
      if (S.segments.length && !confirm(t('confirm.auto'))) return;
      autoSegment();
    });
    $('btn-new').addEventListener('click', newSegment);
    $('btn-split').addEventListener('click', splitAtPlayhead);
    $('btn-del').addEventListener('click', deleteSegment);
    $('btn-export').addEventListener('click', exportNarration);
    $('btn-import').addEventListener('click', () => $('file-import').click());
    $('file-import').addEventListener('change', (e) => {
      if (e.target.files && e.target.files[0]) importNarration(e.target.files[0]);
      e.target.value = '';
    });

    for (const k of F) {
      $(FIELDS[k]).addEventListener('input', readForm);
      $(FIELDS[k]).addEventListener('change', readForm);
    }
    $('f-summary').addEventListener('input', markDirty);
    $('btn-set-start').addEventListener('click', setStartAtPlayhead);
    $('btn-set-end').addEventListener('click', setEndAtPlayhead);

    timeline.addEventListener('mousedown', (e) => {
      if (!S.meta) return;
      const r = timeline.getBoundingClientRect();
      const x = e.clientX - r.left;
      const y = e.clientY - r.top;
      if (y > TL.laneSeg) {
        const time = (x / r.width) * durationS();
        const hit = S.segments.findIndex((s) => time >= s.start && time <= s.end);
        if (hit >= 0) { S.selected = hit; renderSegmentList(); syncForm(); invalidateTimeline(); }
      }
      tlDrag = true;
      seek((x / r.width) * durationS());
    });
    timeline.addEventListener('mousemove', (e) => { if (!tlDrag) showTip(e); });
    timeline.addEventListener('mouseleave', hideTip);
    window.addEventListener('mousemove', (e) => {
      if (!tlDrag) return;
      const r = timeline.getBoundingClientRect();
      seek(clamp((e.clientX - r.left) / r.width, 0, 1) * durationS());
    });
    window.addEventListener('mouseup', () => { tlDrag = false; });

    video.addEventListener('click', togglePlay);
    video.addEventListener('seeked', () => { lastFrame = -1; });
    video.addEventListener('loadedmetadata', () => { sizeTimeline(); lastFrame = -1; });
    video.addEventListener('error', () => {
      if (!S.id || !video.getAttribute('src')) return;
      const name = S.mediaKey.split('/').slice(1).join('/') || 'clip.mp4';
      showEmpty('empty.videoError', { file: name });
    });
    video.addEventListener('play', () => { $('btn-play').textContent = '❚❚'; });
    video.addEventListener('pause', () => { $('btn-play').textContent = '▶'; });
    window.addEventListener('resize', () => { sizeTimeline(); lastFrame = -1; });
    window.addEventListener('hashchange', () => {
      const id = currentHashId();
      if (id && id !== S.id && S.datasets.some((d) => d.id === id)) {
        $('dataset-select').value = id;
        loadDataset(id);
      }
    });

    document.addEventListener('keydown', onKey);
    // closing the tab inside the autosave delay must not lose edits
    window.addEventListener('pagehide', () => {
      if (!S.dirty || !S.id || !S.meta) return;
      const payload = narrationObject();
      keepLocal(S.id, payload);
      try {
        // text/plain keeps sendBeacon a "simple" request; the server accepts it
        // only with a same-origin Origin header
        const blob = new Blob([JSON.stringify(payload)], { type: 'text/plain;charset=UTF-8' });
        if (navigator.sendBeacon) navigator.sendBeacon(`${API}save?dataset=${enc(S.id)}&file=draft.json`, blob);
      } catch (e) { /* best effort */ }
    });

    I18N.onChange(() => {
      renderDatasetOptions();
      renderEmpty();
      renderSaveState();
      renderSegmentList();
      updateQuality();
      hideTip();
    });
  }

  function onKey(e) {
    const tag = (e.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select' || e.target.isContentEditable) {
      if (e.key === 'Escape') e.target.blur();
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey) return;   // leave browser shortcuts alone
    switch (e.key) {
      case ' ': e.preventDefault(); togglePlay(); break;
      case 'ArrowLeft': e.preventDefault(); if (e.shiftKey) seek(video.currentTime - 1); else stepFrame(-1); break;
      case 'ArrowRight': e.preventDefault(); if (e.shiftKey) seek(video.currentTime + 1); else stepFrame(1); break;
      case 's': case 'S': setStartAtPlayhead(); break;
      case 'e': case 'E': setEndAtPlayhead(); break;
      case 'n': case 'N': newSegment(); break;
      case 'Delete': case 'Backspace': e.preventDefault(); deleteSegment(); break;
      case 'Enter': {
        const next = S.segments.find((x) => x.start > video.currentTime + 0.05);
        if (next) selectSegment(S.segments.indexOf(next));
        break;
      }
      case ',': stepFrame(-1); break;
      case '.': stepFrame(1); break;
      default: break;
    }
  }

  function togglePlay() {
    if (!S.meta || !video.getAttribute('src')) return;
    if (video.paused) video.play().catch(() => {}); else video.pause();
  }

  function stepFrame(d) {
    if (!S.meta) return;
    video.pause();
    seek((frameNow() + d) / fps() + 0.0005);
  }

  function loop() {
    if (S.loaded) {
      renderFrame();
      if (!video.paused) presentTimeline();
    }
    requestAnimationFrame(loop);
  }

  // start
  bind();
  renderSaveState();
  renderSegmentList();
  syncForm();
  sizeTimeline();
  loadDatasets().then(() => { loop(); });
})();
