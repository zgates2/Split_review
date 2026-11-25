/* G2 review UI: synced multi-camera playback, frame-accurate marks, sub-task segments. */
"use strict";

const $ = (id) => document.getElementById(id);
const DEFAULT_OUT = "/data/g2/lerobot_v2.1";
const PALETTE = ["#5b8def", "#35c07a", "#e5a13a", "#b072f0", "#2fc4c4", "#ef6f9c", "#a3b84b", "#e7794a"];

/* ------------------------------------------------------------------ helpers */
function el(tag, props, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else if (k === "style") e.style.cssText = v;
    else if (k.startsWith("on")) e[k] = v;
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat()) if (c != null) e.append(c);
  return e;
}

async function request(url, method = "GET", body) {
  const opt = { method };
  if (body !== undefined) {
    opt.headers = { "Content-Type": "application/json" };
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(url, opt);
  if (!r.ok) {
    let msg = `${r.status} ${r.statusText}`;
    try {
      const j = await r.json();
      if (j.detail) msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch (_) { /* non-json error body */ }
    throw new Error(msg);
  }
  return r.json();
}

function toast(msg, isErr, ms) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast show" + (isErr ? " err" : "");
  clearTimeout(t._t);
  t._t = setTimeout(() => { t.className = "toast"; }, ms || (isErr ? 6000 : 2600));
}
const errMsg = (e) => (e && e.message) || String(e);
const clamp = (x, lo, hi) => Math.min(Math.max(x, lo), hi);
const fmt = (t) => (t || 0).toFixed(2);

/* -------------------------------------------------------------------- state */
const S = {
  template: [], datasets: [], collections: [],
  src: null,                 // {kind: "ds"|"col", key, name}
  eps: [], review: {}, filter: "", query: "",
  ep: null, curItem: null, dsKey: null,
  marks: [], segments: [],   // segments.length === marks.length + 1
  keyframes: [], duration: 0, fps: 30, length: 0,
  snapOn: false, dirty: false, saving: false, note: "",
  playing: false, rate: 1, curTime: 0, stopAt: null, selMark: -1, raf: 0, tok: 0,
  exporting: false,
};

const itemKey = (e) => (S.src && S.src.kind === "col" ? e.ord : e.episode_index);
const quant = (t) => Math.round(t * S.fps) / S.fps;
const segColor = (i) => PALETTE[i % PALETTE.length];

/* --------------------------------------------------------------------- boot */
async function boot() {
  bindEvents();
  try {
    const [tpl, dss, cols] = await Promise.all([
      request("/api/template"), request("/api/datasets"), request("/api/collections"),
    ]);
    S.template = tpl.prompts;
    S.datasets = dss;
    S.collections = cols;
    renderDatasetSelect();
    if (!dss.length && !cols.length) { showEmpty(); return; }

    const saved = localStorage.getItem("split_src");
    const all = [...cols.map((c) => `col:${c.key}`), ...dss.map((d) => `ds:${d.key}`)];
    await selectSource(all.includes(saved) ? saved : all[0], true);

    const m = /[#&]ep=(\d+)/.exec(location.hash || "");
    if (m) await openEpisode(parseInt(m[1], 10), true);
    resumeExport();
  } catch (e) { toast(errMsg(e), true); }
}

function showEmpty() {
  $("empty").hidden = false;
  $("emptyTitle").textContent = "还没有数据集";
  $("emptyText").textContent = "先登记一个 LeRobot v3 数据集（或包含多个数据集的父目录）。";
  $("emptyAdd").hidden = false;
  renderOverall({ total: 0 });
}

function renderDatasetSelect() {
  const sel = $("dsSelect");
  sel.innerHTML = "";
  if (!S.datasets.length && !S.collections.length) {
    sel.append(el("option", { value: "", text: "（无数据集）" }));
    return;
  }
  if (S.collections.length) {
    sel.append(el("optgroup", { label: "集合（目录）" },
      S.collections.map((c) => el("option", { value: `col:${c.key}`, text: `${c.name} · ${c.n_items} 条` }))));
  }
  if (S.datasets.length) {
    sel.append(el("optgroup", { label: "单个数据集" },
      S.datasets.map((d) => el("option", { value: `ds:${d.key}`, text: `${d.name} · ${d.n_episodes} 条` }))));
  }
  if (S.src) sel.value = `${S.src.kind}:${S.src.key}`;
}

/* ------------------------------------------------------------ source / list */
function confirmLeave() {
  return !S.dirty || confirm("当前 episode 有未保存的修改，确定放弃吗？");
}

async function fetchList(kind, key) {
  if (kind === "col") {
    const d = await request(`/api/collections/${key}/items`);
    return { name: `${d.collection.name}（集合）`, eps: d.items, review: d.review };
  }
  const [info, d] = await Promise.all([
    request(`/api/datasets/${key}`), request(`/api/datasets/${key}/episodes`),
  ]);
  return { name: info.name, eps: d.episodes, review: d.review };
}

async function selectSource(val, silent) {
  if (!silent && !confirmLeave()) { renderDatasetSelect(); return; }
  const i = val.indexOf(":");
  const kind = val.slice(0, i), key = val.slice(i + 1);
  try {
    const d = await fetchList(kind, key);
    S.src = { kind, key, name: d.name };
    S.eps = d.eps;
    S.review = d.review;
    localStorage.setItem("split_src", val);
    clearEpisode();
    $("dsSelect").value = val;
    renderOverall(S.review);
    renderList();
  } catch (e) { toast(errMsg(e), true); renderDatasetSelect(); }
}

async function reloadList() {
  if (!S.src) return;
  const d = await fetchList(S.src.kind, S.src.key);
  S.eps = d.eps;
  S.review = d.review;
  renderOverall(S.review);
  renderList();
}

function clearEpisode() {
  S.tok++;
  S.loadingKey = null;
  setBusy(false);
  setPlaying(false);
  S.ep = null; S.curItem = null; S.dirty = false;
  for (const id of ["epHead", "videos", "transport", "timeline", "panels", "footer"]) $(id).hidden = true;
  $("videos").innerHTML = "";
  $("empty").hidden = false;
  $("emptyAdd").hidden = true;
  $("emptyTitle").textContent = "请选择左侧 episode";
  $("emptyText").textContent = "点击左侧列表中的一条开始审查；首次打开需要生成预览，约 5–10 秒。";
  updateDirty();
}

function renderOverall(rv) {
  const total = rv.total || 0, done = rv.done || 0, skipped = rv.skipped || 0;
  const fresh = Math.max(0, total - done - skipped);
  $("overallFill").style.width = total ? `${((done + skipped) / total) * 100}%` : "0";
  $("overallText").textContent = total
    ? `已审 ${done} · 跳过 ${skipped} · 未审 ${fresh} / 共 ${total}`
    : "";
}

function epLabel(e) {
  if (S.src.kind === "col") return `${e.dataset_name || "episode"}`;
  return `#${String(e.episode_index).padStart(4, "0")}`;
}

function renderList() {
  const counts = { "": S.eps.length, new: 0, done: 0, skipped: 0 };
  for (const e of S.eps) if (e.status in counts) counts[e.status]++;
  $("cAll").textContent = counts[""];
  $("cNew").textContent = counts.new;
  $("cDone").textContent = counts.done;
  $("cSkipped").textContent = counts.skipped;

  const ul = $("epList");
  ul.innerHTML = "";
  const q = S.query.trim().toLowerCase();
  const list = S.eps.filter((e) =>
    (!S.filter || e.status === S.filter) &&
    (!q || `${epLabel(e)} ${e.episode_index}`.toLowerCase().includes(q)));
  if (!list.length) {
    ul.append(el("li", { class: "none", text: S.eps.length ? "没有符合条件的 episode" : "（空）" }));
    return;
  }
  let activeLi = null;
  for (const e of list) {
    const active = S.ep && itemKey(e) === S.curItem;
    const meta = `${e.duration_s.toFixed(1)}s${e.n_segments ? ` · ${e.kept}/${e.n_segments}段` : ""}`;
    const cls = (active ? "active " : "") + (itemKey(e) === S.loadingKey ? "loading" : "");
    const li = el("li", { class: cls.trim(), title: e.dataset_name || "" },
      el("span", { class: "idx" },
        el("span", { class: `status ${e.status}` }),
        el("span", { class: "name", text: epLabel(e) })),
      el("span", { class: "meta", text: meta }));
    li.onclick = () => openEpisode(itemKey(e));
    if (active) activeLi = li;
    ul.append(li);
  }
  if (activeLi) activeLi.scrollIntoView({ block: "nearest" });
}

function visibleKeys() {
  const q = S.query.trim().toLowerCase();
  return S.eps
    .filter((e) => (!S.filter || e.status === S.filter) &&
      (!q || `${epLabel(e)} ${e.episode_index}`.toLowerCase().includes(q)))
    .map(itemKey);
}

function stepEpisode(dir) {
  const keys = visibleKeys();
  const i = keys.indexOf(S.curItem);
  const next = keys[i < 0 ? 0 : i + dir];
  if (next === undefined) return toast(dir > 0 ? "已是最后一条" : "已是第一条");
  openEpisode(next);
}

/* ---------------------------------------------------------------- episode */
function setBusy(on) {
  $("busyBar").hidden = !on;
  $("stage").classList.toggle("busy", on);
}

async function openEpisode(key, force) {
  if (!S.src) return;
  if (S.loadingKey === key) return;          // this one is already loading
  if (!force && !confirmLeave()) return;
  const tok = ++S.tok;
  setPlaying(false);
  S.loadingKey = key;
  setBusy(true);
  renderList();
  // The first open of an episode probes keyframes and can take ~10 s: say so.
  const slow = setTimeout(() => toast("正在加载，首次打开需要探测关键帧，约 10 秒…", false, 9000), 1200);
  const url = S.src.kind === "col"
    ? `/api/collections/${S.src.key}/items/${key}`
    : `/api/datasets/${S.src.key}/episodes/${key}`;
  let ep;
  try {
    ep = await request(url);
  } catch (e) {
    return toast(errMsg(e), true);
  } finally {
    clearTimeout(slow);
    if (tok === S.tok) { S.loadingKey = null; setBusy(false); }
  }
  if (tok !== S.tok) return;   // a newer open superseded this one
  $("toast").className = "toast";

  S.ep = ep;
  S.curItem = key;
  S.dsKey = ep.dataset_key || S.src.key;
  S.duration = ep.duration_s;
  S.fps = ep.fps;
  S.length = ep.length;
  S.keyframes = ep.keyframes || [];
  S.template = ep.template || S.template;
  S.snapOn = !!ep.snap_default;
  S.note = ep.note || "";
  S.selMark = -1;
  S.stopAt = null;

  if (ep.segments && ep.segments.length) {
    // The saved segments are what export will use, so they are the source of truth.
    S.segments = ep.segments.map((s) => ({ prompt: s.prompt || "", keep: !!s.keep }));
    S.marks = ep.segments.slice(1).map((s) => s.start_frame / S.fps);
  } else {
    S.marks = [];
    S.segments = [{ prompt: S.template[0] || (ep.tasks && ep.tasks[0]) || "", keep: true }];
  }
  syncBounds();
  S.dirty = false;

  $("empty").hidden = true;
  for (const id of ["epHead", "videos", "transport", "timeline", "panels", "footer"]) $(id).hidden = false;
  $("epTitle").textContent = (S.src.kind === "col" && ep.dataset_name) ? ep.dataset_name : `episode #${ep.episode_index}`;
  $("epMeta").textContent = `${ep.duration_s.toFixed(2)}s · ${ep.length} 帧 · ${ep.cameras.length} 相机 · ${ep.fps}fps`;
  $("noteInput").value = S.note;
  $("chkSnap").checked = S.snapOn;
  history.replaceState(null, "", `#ep=${key}`);

  renderVideos(ep.cameras, ep.episode_index);
  renderTimeline();
  renderSegments();
  renderList();
  updateDirty();
  seek(0);
  requestAnimationFrame(drawRuler);
}

/* Recompute segment boundaries from marks; segments[i] spans edges[i]..edges[i+1]. */
function syncBounds() {
  const edges = [0, ...S.marks, S.duration];
  S.segments.forEach((s, i) => {
    s.seq = i;
    s.start = edges[i];
    s.end = edges[i + 1];
    s.start_frame = i === 0 ? 0 : Math.round(edges[i] * S.fps);
    s.end_frame = i === S.segments.length - 1 ? S.length : Math.round(edges[i + 1] * S.fps);
  });
}

function segAt(t) {
  const i = S.segments.findIndex((s) => t >= s.start - 1e-6 && t < s.end);
  return i < 0 ? S.segments.length - 1 : i;
}

/* ------------------------------------------------------------------ videos */
function masterVideo() { return document.querySelector("#videos video"); }
function allVideos() { return [...document.querySelectorAll("#videos video")]; }

function renderVideos(cameras, index) {
  const box = $("videos");
  box.innerHTML = "";
  const n = cameras.length;
  box.className = "videos " + (n <= 1 ? "n1" : n === 2 ? "n2" : "nx");
  box.style.setProperty("--rows", Math.max(1, n - 1));

  cameras.forEach((cam, i) => {
    const cover = el("div", { class: "cover" }, el("div", { class: "spin" }), el("span", { text: "正在生成预览…（首次约 5–10 秒）" }));
    const v = el("video", { muted: true, playsinline: true, preload: "auto" });
    v.muted = true;
    v.playbackRate = S.rate;
    const load = () => {
      cover.className = "cover";
      cover.replaceChildren(el("div", { class: "spin" }), el("span", { text: "正在生成预览…（首次约 5–10 秒）" }));
      cover.hidden = false;
      v.src = `/api/media/${S.dsKey}/${index}?camera=${encodeURIComponent(cam)}&_=${Date.now()}`;
    };
    v.addEventListener("loadeddata", () => { cover.hidden = true; });
    v.addEventListener("error", () => {
      cover.className = "cover err";
      cover.hidden = false;
      cover.replaceChildren(el("span", { text: "预览加载失败" }), el("button", { class: "btn small", text: "重试", onclick: load }));
    });
    box.append(el("div", { class: "cam" },
      v,
      el("span", { class: "tag", text: cam.replace("observation.images.", "") + (i === 0 ? " · 主轨" : "") }),
      cover));
    load();
  });

  const master = masterVideo();
  master.addEventListener("ended", () => setPlaying(false));
  master.addEventListener("seeked", () => { if (!S.playing) updateTime(master.currentTime); });
}

function setPlaying(p) {
  S.playing = p;
  $("btnPlay").textContent = p ? "⏸" : "▶";
  allVideos().forEach((v) => { if (p) v.play().catch(() => {}); else v.pause(); });
  if (p && !S.raf) S.raf = requestAnimationFrame(tick);
  if (!p) S.stopAt = null;
}

function tick() {
  S.raf = 0;
  if (!S.playing) return;
  const m = masterVideo();
  if (m) {
    const t = m.currentTime;
    for (const v of allVideos()) if (v !== m && Math.abs(v.currentTime - t) > 0.15) v.currentTime = t;
    updateTime(t);
    if (S.stopAt != null && t >= S.stopAt - 0.5 / S.fps) {
      const end = S.stopAt;
      setPlaying(false);
      seek(end);
      return;
    }
  }
  S.raf = requestAnimationFrame(tick);
}

function seek(t) {
  t = clamp(t, 0, S.duration);
  allVideos().forEach((v) => { v.currentTime = t; });
  updateTime(t);
}

function togglePlay() {
  if (!S.ep) return;
  const m = masterVideo();
  if (!S.playing && m && m.ended) seek(0);
  setPlaying(!S.playing);
}

function stepFrames(n) {
  if (!S.ep) return;
  setPlaying(false);
  seek(quant(S.curTime) + n / S.fps + 0.0001);
}

function playSegment(i) {
  const s = S.segments[i];
  seek(s.start + 0.0001);
  setPlaying(true);
  S.stopAt = s.end;   // after setPlaying: pausing clears stopAt
}

/* -------------------------------------------------------------- time / head */
function updateTime(t) {
  S.curTime = t;
  if (!S.duration) return;
  $("timeLabel").textContent = `${fmt(t)} / ${fmt(S.duration)} s`;
  $("frameLabel").textContent = `帧 ${Math.min(S.length, Math.round(t * S.fps))} / ${S.length}`;
  $("playhead").style.left = `${(clamp(t, 0, S.duration) / S.duration) * 100}%`;
  highlightCurrent(segAt(t));
}

let lastCur = -1;
function highlightCurrent(i) {
  if (i === lastCur) return;
  lastCur = i;
  document.querySelectorAll(".seg").forEach((n, k) => n.classList.toggle("current", k === i));
  document.querySelectorAll(".band").forEach((n, k) => n.classList.toggle("current", k === i));
}

/* ---------------------------------------------------------------- timeline */
function timeFromX(clientX) {
  const r = $("tlInner").getBoundingClientRect();
  return clamp((clientX - r.left) / r.width, 0, 1) * S.duration;
}

function fmtClock(s) {
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60), r = Math.round(s % 60);
  return `${m}:${String(r).padStart(2, "0")}`;
}

function drawRuler() {
  const cv = $("ruler");
  const w = cv.clientWidth, h = cv.clientHeight;
  if (!w || !S.duration) return;
  const dpr = window.devicePixelRatio || 1;
  cv.width = w * dpr; cv.height = h * dpr;
  const c = cv.getContext("2d");
  c.setTransform(dpr, 0, 0, dpr, 0, 0);
  c.clearRect(0, 0, w, h);
  const pxPer = w / S.duration;

  if (S.keyframes.length && w / S.keyframes.length >= 3) {
    c.fillStyle = "rgba(29,35,48,.28)";
    for (const k of S.keyframes) c.fillRect(Math.round(k * pxPer), h - 6, 1, 6);
  }
  const steps = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800];
  const step = steps.find((s) => s * pxPer >= 70) || 1800;
  c.font = "10px ui-monospace, monospace";
  c.textBaseline = "top";
  for (let s = 0; s <= S.duration + 1e-6; s += step) {
    const x = Math.round(s * pxPer);
    c.fillStyle = "#aab2c3";
    c.fillRect(x, 0, 1, 9);
    c.fillStyle = "#6a7285";
    c.textAlign = x > w - 34 ? "right" : "left";
    c.fillText(fmtClock(s), x + (x > w - 34 ? -3 : 3), 1);
  }
}

function renderTimeline() {
  const bands = $("tlBands");
  bands.innerHTML = "";
  S.segments.forEach((s, i) => {
    const dropped = !s.keep;
    bands.append(el("div", {
      class: "band" + (dropped ? " dropped" : ""),
      style: `left:${(s.start / S.duration) * 100}%;width:${((s.end - s.start) / S.duration) * 100}%;--c:${segColor(i)}`,
      title: `段${i + 1} ${fmt(s.start)}–${fmt(s.end)}s${s.prompt ? " · " + s.prompt : ""}${dropped ? "（丢弃）" : ""}`,
    }, el("span", { class: "bn", text: i + 1 }), el("span", { text: s.prompt || "未设置" })));
  });

  const marks = $("tlMarks");
  marks.innerHTML = "";
  S.marks.forEach((t, i) => {
    const m = el("div", {
      class: "tl-mark" + (i === S.selMark ? " sel" : ""),
      style: `left:${(t / S.duration) * 100}%`,
      title: `${fmt(t)}s（帧 ${Math.round(t * S.fps)}）· 拖动微调 · 双击删除`,
    });
    m.addEventListener("pointerdown", (ev) => startMarkDrag(ev, i));
    m.addEventListener("dblclick", (ev) => { ev.stopPropagation(); removeMark(i); });
    marks.append(m);
  });
  lastCur = -1;
  highlightCurrent(segAt(S.curTime));
}

function selectMark(i) {
  S.selMark = i;
  document.querySelectorAll(".tl-mark").forEach((n, k) => n.classList.toggle("sel", k === i));
}

function startMarkDrag(ev, i) {
  ev.preventDefault();
  ev.stopPropagation();
  selectMark(i);
  const step = 1 / S.fps;
  const move = (e) => {
    let t = timeFromX(e.clientX);
    if (S.snapOn) t = snapTime(t);
    const lo = (i > 0 ? S.marks[i - 1] : 0) + step;
    const hi = (i < S.marks.length - 1 ? S.marks[i + 1] : S.duration) - step;
    t = clamp(quant(t), lo, hi);
    if (t === S.marks[i]) return;
    S.marks[i] = t;
    syncBounds();
    renderTimeline();
    renderSegments();
    markDirty();
    seek(t);
  };
  const up = () => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
}

function startScrub(ev) {
  if (!S.ep || ev.target.classList.contains("tl-mark")) return;
  ev.preventDefault();
  seek(timeFromX(ev.clientX));
  const move = (e) => seek(timeFromX(e.clientX));
  const up = () => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
}

function snapTime(t) {
  if (!S.keyframes.length) return t;
  let best = S.keyframes[0], bd = Infinity;
  for (const k of S.keyframes) {
    const d = Math.abs(k - t);
    if (d < bd) { bd = d; best = k; }
  }
  return best;
}

/* --------------------------------------------------------------- mark edit */
function addMarkAt(t) {
  if (!S.ep) return;
  const s = quant(S.snapOn ? snapTime(t) : t);
  const f = Math.round(s * S.fps);
  if (f <= 0 || f >= S.length) return toast("不能把标记放在开头或结尾");
  if (S.marks.some((m) => Math.round(m * S.fps) === f)) return toast("该位置已有标记");
  let k = S.marks.findIndex((m) => m > s);
  if (k < 0) k = S.marks.length;            // splitting segment k
  const base = S.segments[k];
  const appending = k === S.segments.length - 1;
  S.marks.splice(k, 0, s);
  S.segments.splice(k + 1, 0, {
    prompt: (appending && S.template[k + 1]) || base.prompt,
    keep: base.keep,
  });
  syncBounds();
  selectMark(k);
  renderTimeline(); renderSegments(); markDirty();
  toast(`已在 ${fmt(s)}s（帧 ${f}）切开 → 共 ${S.segments.length} 段`);
}

function removeMark(i) {
  if (i < 0 || i >= S.marks.length) return;
  S.marks.splice(i, 1);
  S.segments.splice(i + 1, 1);              // merge into the left segment
  S.selMark = -1;
  syncBounds();
  renderTimeline(); renderSegments(); markDirty();
}

/* ---------------------------------------------------------------- segments */
function promptOptions(current) {
  const set = new Set([...S.template, ...S.segments.map((s) => s.prompt), current]);
  if (S.ep && S.ep.tasks) S.ep.tasks.forEach((t) => set.add(t));
  set.delete("");
  return [...set];
}

function renderSegments() {
  const box = $("segments");
  box.innerHTML = "";
  S.segments.forEach((seg, i) => {
    const sel = el("select", { class: "input" });
    sel.append(el("option", { value: "", text: "（未设置 prompt）" }));
    for (const p of promptOptions(seg.prompt)) {
      const n = S.template.indexOf(p);
      const o = el("option", { value: p, text: n >= 0 && n < 9 ? `${n + 1} · ${p}` : p });
      if (p === seg.prompt) o.selected = true;
      sel.append(o);
    }
    sel.append(el("option", { value: "__custom", text: "自定义…" }));
    if (!seg.prompt) sel.value = "";
    sel.onchange = () => {
      if (sel.value === "__custom") {
        const v = prompt("输入该段子任务 prompt：", seg.prompt);
        if (!v || !v.trim()) { renderSegments(); return; }
        seg.prompt = v.trim();
      } else seg.prompt = sel.value;
      renderSegments(); renderTimeline(); markDirty();
    };

    const warn = seg.keep && !seg.prompt;
    box.append(el("div", { class: "seg" + (seg.keep ? "" : " dropped") + (warn ? " warn" : ""), style: `--c:${segColor(i)}` },
      el("div", { class: "cbar" }),
      el("div", { class: "info" },
        el("b", { text: `段 ${i + 1}` }),
        el("span", { text: `${fmt(seg.start)}–${fmt(seg.end)}s · ${(seg.end - seg.start).toFixed(1)}s · ${seg.end_frame - seg.start_frame}帧` })),
      el("div", {},
        sel,
        warn ? el("div", { class: "seg-warn", text: "未设置 prompt：保存后该段不会被导出" }) : null),
      el("div", { class: "actions" },
        el("button", { class: "btn small", text: "跳转", title: "定位到该段开头", onclick: () => seek(seg.start + 0.0001) }),
        el("button", { class: "btn small", text: "▶ 试播", title: "只播放该段", onclick: () => playSegment(i) }),
        el("button", {
          class: "btn small " + (seg.keep ? "keep-on" : "keep-off"),
          text: seg.keep ? "✓ 保留" : "✕ 已丢弃",
          title: "点击切换保留 / 丢弃 (D)",
          onclick: () => { seg.keep = !seg.keep; renderSegments(); renderTimeline(); markDirty(); },
        }))));
  });
  lastCur = -1;
  highlightCurrent(segAt(S.curTime));

  const kept = S.segments.filter((s) => s.keep && s.prompt);
  const secs = kept.reduce((a, s) => a + (s.end - s.start), 0);
  $("segSummary").textContent = `保留 ${kept.length}/${S.segments.length} 段 · ${secs.toFixed(1)}s`;
  $("tplHint").textContent = S.template.length ? "" : "还没有子任务模板，可点右上「子任务模板」设置";
}

/* -------------------------------------------------------------------- save */
function markDirty() { S.dirty = true; updateDirty(); }
function updateDirty() { $("dirtyBadge").hidden = !S.dirty; }

async function save() {
  if (!S.ep || S.saving) return false;
  const noPrompt = S.segments.filter((s) => s.keep && !s.prompt).length;
  if (noPrompt && !confirm(`有 ${noPrompt} 个标为保留的段没有设置 prompt，保存后它们不会被导出。继续保存？`)) return false;
  const allDropped = S.segments.every((s) => !s.keep || !s.prompt);
  S.saving = true;
  try {
    const res = await request(`/api/datasets/${S.dsKey}/episodes/${S.ep.episode_index}`, "PUT", {
      marks: S.marks,
      snap: S.snapOn,
      prompts: S.segments.map((s) => s.prompt),
      keeps: S.segments.map((s) => s.keep),
      status: allDropped ? "skipped" : "done",
      note: $("noteInput").value.trim(),
    });
    S.segments = res.segments.map((s) => ({ prompt: s.prompt || "", keep: !!s.keep }));
    S.marks = res.segments.slice(1).map((s) => s.start_frame / S.fps);
    syncBounds();
    S.dirty = false;
    renderTimeline(); renderSegments(); updateDirty();
    const kept = S.segments.filter((s) => s.keep).length;
    toast(allDropped ? "已保存：整条跳过" : `已保存：${kept}/${S.segments.length} 段保留`);
    await reloadList();
    return true;
  } catch (e) {
    toast(`保存失败：${errMsg(e)}`, true);
    return false;
  } finally { S.saving = false; }
}

async function saveAndNext() {
  if (!(await save())) return;
  const keys = S.eps.map(itemKey);
  const at = keys.indexOf(S.curItem);
  const pending = (e) => e.status === "new";
  const next = S.eps.slice(at + 1).find(pending) || S.eps.find(pending);
  if (next) await openEpisode(itemKey(next), true);
  else toast("全部审完了 🎉");
}

function discardAll() {
  if (!S.segments.length) return;
  const allDropped = S.segments.every((s) => !s.keep);
  S.segments.forEach((s) => { s.keep = allDropped; });
  renderSegments(); renderTimeline(); markDirty();
  toast(allDropped ? "已恢复全部段" : "整条已丢弃，保存后记为「跳过」");
}

/* ------------------------------------------------------------------ modals */
function openModal(id) { $(id).hidden = false; const f = $(id).querySelector("input, textarea"); if (f) setTimeout(() => f.focus(), 30); }
function closeModals() { document.querySelectorAll(".modal").forEach((m) => { m.hidden = true; }); }
const modalOpen = () => [...document.querySelectorAll(".modal")].some((m) => !m.hidden);

async function addDataset() {
  const path = $("dsPath").value.trim();
  if (!path) return toast("请输入路径", true);
  $("addGo").disabled = true;
  try {
    let val;
    try {
      const r = await request("/api/datasets", "POST", { path });
      toast(`已登记 ${r.summary.name}（${r.summary.n_episodes} 条）`);
      val = `ds:${r.key}`;
    } catch (e1) {
      // Not a single dataset: try it as a parent directory of datasets.
      try {
        const r = await request("/api/collections", "POST", { path });
        toast(`已登记集合 ${r.name}：${r.datasets} 个数据集 / ${r.episodes} 条${r.skipped_count ? `（跳过 ${r.skipped_count} 个）` : ""}`);
        val = `col:${r.key}`;
      } catch (e2) { throw new Error(errMsg(e1) === errMsg(e2) ? errMsg(e1) : `${errMsg(e1)}；${errMsg(e2)}`); }
    }
    [S.datasets, S.collections] = await Promise.all([request("/api/datasets"), request("/api/collections")]);
    closeModals();
    $("dsPath").value = "";
    await selectSource(val, true);
    renderDatasetSelect();
  } catch (e) { toast(errMsg(e), true); }
  finally { $("addGo").disabled = false; }
}

async function saveTemplate() {
  const prompts = $("tplText").value.split("\n").map((s) => s.trim()).filter(Boolean);
  try {
    const r = await request("/api/template", "PUT", { prompts });
    S.template = r.prompts;
    closeModals();
    if (S.ep) renderSegments();
    toast(`模板已保存（${r.prompts.length} 项）`);
  } catch (e) { toast(errMsg(e), true); }
}

/* ------------------------------------------------------------------ export */
const safeName = (s) => s.trim().replace(/[^\w一-鿿.-]+/g, "_");

function openExport() {
  if (!S.src) return toast("先选数据集", true);
  if (S.exporting) return toast("已有导出任务在运行");
  const done = S.eps.filter((e) => e.status === "done").length;
  if (!done) return toast("还没有「已审」的 episode，先保存几条再导出", true);
  const unsaved = S.dirty ? "（当前 episode 有未保存的修改，不会包含在内）" : "";
  $("expSummary").textContent = `将导出 ${done} 条已审 episode 中保留的段。${unsaved}`;
  $("expName").value = `split_${new Date().toISOString().slice(0, 10).replace(/-/g, "")}`;
  $("expOut").value = localStorage.getItem("split_out_dir") || DEFAULT_OUT;
  updateExpPreview();
  openModal("expModal");
}

function updateExpPreview() {
  const out = $("expOut").value.trim().replace(/\/+$/, "") || DEFAULT_OUT;
  $("expPreview").textContent = `${out}/${safeName($("expName").value) || "<目录名>"}/task_xx_…`;
}

async function startExport() {
  const name = safeName($("expName").value);
  const out = $("expOut").value.trim() || DEFAULT_OUT;
  if (!name) return toast("请填写目录名", true);
  localStorage.setItem("split_out_dir", out);
  $("expGo").disabled = true;
  try {
    const job = await request("/api/export", "POST", { dataset: S.src.key, name, out });
    closeModals();
    toast(`导出已启动 #${job.id} → ${job.output}`, false, 5000);
    pollExport(job.id);
  } catch (e) { toast(errMsg(e), true); }
  finally { $("expGo").disabled = false; }
}

function setExportUi(frac, label, title) {
  $("exportFill").style.width = frac == null ? "0" : `${frac * 100}%`;
  $("exportLabel").textContent = label;
  $("btnExport").title = title || "";
}

function pollExport(id) {
  S.exporting = true;
  const tick = async () => {
    try {
      const j = await request(`/api/export/${id}`);
      if (j.status === "running") {
        setExportUi(j.progress_frac, `导出 ${(j.progress_frac * 100).toFixed(0)}%`, j.progress_msg);
        setTimeout(tick, 1500);
        return;
      }
      S.exporting = false;
      setExportUi(null, "导出子任务数据集");
      if (j.status === "done") {
        const n = (j.report && j.report.tasks || []).length;
        toast(`导出完成：${n} 个子任务数据集 → ${j.output}`, false, 10000);
      } else toast(`导出失败：${j.progress_msg || "未知错误"}`, true, 12000);
    } catch (e) {
      S.exporting = false;
      setExportUi(null, "导出子任务数据集");
      toast(errMsg(e), true);
    }
  };
  tick();
}

async function resumeExport() {
  // Reattach to an export still in flight after a page reload (ignore stale rows).
  try {
    const { exports } = await request("/api/exports");
    const live = exports.find((x) => x.status === "running" && x.updated_at &&
      Date.now() - Date.parse(x.updated_at) < 120000);
    if (live) pollExport(live.id);
  } catch (_) { /* optional */ }
}

/* ------------------------------------------------------------------ events */
function bindEvents() {
  $("dsSelect").onchange = (e) => e.target.value && selectSource(e.target.value);
  $("btnAdd").onclick = () => openModal("addModal");
  $("emptyAdd").onclick = () => openModal("addModal");
  $("addGo").onclick = addDataset;
  $("dsPath").addEventListener("keydown", (e) => { if (e.key === "Enter") addDataset(); });

  $("btnTpl").onclick = () => { $("tplText").value = S.template.join("\n"); openModal("tplModal"); };
  $("tplSave").onclick = saveTemplate;
  $("btnHelp").onclick = () => openModal("helpModal");
  $("btnExport").onclick = openExport;
  $("expGo").onclick = startExport;
  $("expName").addEventListener("input", updateExpPreview);
  $("expOut").addEventListener("input", updateExpPreview);
  $("expName").addEventListener("keydown", (e) => { if (e.key === "Enter") startExport(); });
  document.querySelectorAll("[data-close]").forEach((b) => { b.onclick = closeModals; });
  document.querySelectorAll(".modal").forEach((m) => m.addEventListener("pointerdown", (e) => { if (e.target === m) closeModals(); }));

  $("filters").onclick = (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    document.querySelectorAll("#filters .chip").forEach((c) => c.classList.remove("active"));
    b.classList.add("active");
    S.filter = b.dataset.filter;
    renderList();
  };
  $("epSearch").addEventListener("input", (e) => { S.query = e.target.value; renderList(); });

  $("tlInner").addEventListener("pointerdown", startScrub);
  $("btnPlay").onclick = togglePlay;
  $("btnPrevFrame").onclick = () => stepFrames(-1);
  $("btnNextFrame").onclick = () => stepFrames(1);
  $("btnMark").onclick = () => addMarkAt(S.curTime);
  $("speedSel").onchange = (e) => {
    S.rate = parseFloat(e.target.value);
    allVideos().forEach((v) => { v.playbackRate = S.rate; });
  };
  $("chkSnap").onchange = (e) => { S.snapOn = e.target.checked; markDirty(); };
  $("noteInput").addEventListener("input", markDirty);

  $("btnSave").onclick = save;
  $("btnSaveNext").onclick = saveAndNext;
  $("btnPrev").onclick = () => stepEpisode(-1);
  $("btnNext").onclick = () => stepEpisode(1);
  $("btnDiscardAll").onclick = discardAll;

  document.addEventListener("keydown", onKey);
  window.addEventListener("resize", () => { if (S.ep) drawRuler(); });
  window.addEventListener("beforeunload", (e) => { if (S.dirty) { e.preventDefault(); e.returnValue = ""; } });
}

function onKey(e) {
  if (e.key === "Escape") { closeModals(); return; }
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.target.matches("input, textarea, select") || modalOpen()) return;
  if (e.key === "?") { openModal("helpModal"); return; }
  if (!S.ep) return;

  const step = e.shiftKey ? 5 : 1;
  const k = e.key;
  if (e.code === "Space") { e.preventDefault(); togglePlay(); }
  else if (k === "m" || k === "M") addMarkAt(S.curTime);
  else if (k === "ArrowLeft") { e.preventDefault(); seek(S.curTime - step); }
  else if (k === "ArrowRight") { e.preventDefault(); seek(S.curTime + step); }
  else if (k === ",") stepFrames(-1);
  else if (k === ".") stepFrames(1);
  else if (k === "Home") seek(0);
  else if (k === "End") seek(S.duration);
  else if (k === "[") stepEpisode(-1);
  else if (k === "]") stepEpisode(1);
  else if (k === "d" || k === "D") {
    const s = S.segments[segAt(S.curTime)];
    if (s) { s.keep = !s.keep; renderSegments(); renderTimeline(); markDirty(); }
  } else if (k === "s" || k === "S") save();
  else if (k === "n" || k === "N") saveAndNext();
  else if (/^[1-9]$/.test(k)) {
    const p = S.template[parseInt(k, 10) - 1];
    if (!p) return toast(`模板第 ${k} 行不存在`);
    S.segments[segAt(S.curTime)].prompt = p;
    renderSegments(); renderTimeline(); markDirty();
  } else if (k === "Delete" || k === "Backspace") {
    let i = S.selMark;
    if (i < 0) {
      let best = Infinity;
      S.marks.forEach((m, n) => { const d = Math.abs(m - S.curTime); if (d < best && d < 0.5) { best = d; i = n; } });
    }
    if (i >= 0) removeMark(i); else toast("选中一个标记，或把播放头移到标记附近再删除");
  }
}

boot();
