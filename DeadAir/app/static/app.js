"use strict";
/* DeadAir front end - plain JS, talks only to the local server. */

const $ = (id) => document.getElementById(id);
const EXTS = [".mp4", ".mov", ".mkv", ".webm", ".avi"];
const PRESETS = {
  natural:    { threshold: "-35", minsil: "0.7", padding: "0.25" },
  balanced:   { threshold: "-30", minsil: "0.5", padding: "0.15" },
  aggressive: { threshold: "-25", minsil: "0.3", padding: "0.1" },
};

const state = { video: null, detection: null, busy: false, ffmpegOk: true };

/* ---------- formatting ---------- */
function clock(sec) {
  const cs = Math.round(Math.max(0, sec) * 100);
  const c = cs % 100, s = Math.floor(cs / 100) % 60, m = Math.floor(cs / 6000) % 60, h = Math.floor(cs / 360000);
  const p = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${p(m)}:${p(s)}.${p(c)}` : `${p(m)}:${p(s)}.${p(c)}`;
}
const secs = (n) => `${n.toFixed(2)}s`;

/* ---------- UI state ---------- */
function showBanner(msg) { const b = $("banner"); b.textContent = msg || ""; b.hidden = !msg; }
function setStatus(id, msg, isError = false) { const el = $(id); el.textContent = msg || ""; el.classList.toggle("error", !!isError); }
function setBar(id, frac) { const bar = $(id); bar.hidden = frac == null; if (frac != null) bar.firstElementChild.style.width = `${Math.round(frac * 100)}%`; }

function refresh() {
  const hasVideo = !!state.video && state.video.has_audio;
  const canExport = !!state.detection && state.detection.has_changes && !state.detection.fully_silent;
  $("detect").disabled = state.busy || !hasVideo || !state.ffmpegOk;
  $("export").disabled = state.busy || !canExport;
  for (const id of ["preset", "threshold", "minsil", "padding"]) $(id).disabled = state.busy;
  $("drop").classList.toggle("disabled", state.busy);
  $("open-file").disabled = $("open-folder").disabled = false;
}

function invalidateDetection() {
  state.detection = null;
  $("results").hidden = true;
  $("done").hidden = true;
  setStatus("detect-status", "");
  setStatus("export-status", "");
  refresh();
}

/* ---------- API ---------- */
async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  let data = {};
  try { data = await res.json(); } catch (_) { /* non-JSON error */ }
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

async function runJob(jobId, onProgress) {
  for (;;) {
    const job = await api(`/api/jobs/${jobId}`);
    onProgress(job.progress);
    if (job.state === "done") return job.result;
    if (job.state === "error") throw new Error(job.error || "Processing failed.");
    await new Promise((r) => setTimeout(r, 300));
  }
}

/* ---------- upload ---------- */
function upload(file) {
  const dot = file.name.lastIndexOf(".");
  const ext = dot >= 0 ? file.name.slice(dot).toLowerCase() : "";
  if (!EXTS.includes(ext)) { showBanner("Unsupported file type. Use MP4, MOV, MKV, WebM or AVI."); return; }
  showBanner("");
  state.busy = true; state.video = null; invalidateDetection(); $("info").hidden = true; refresh();
  setBar("upload-progress", 0);

  const xhr = new XMLHttpRequest();
  xhr.open("POST", `/api/upload?filename=${encodeURIComponent(file.name)}`);
  xhr.setRequestHeader("Content-Type", "application/octet-stream");
  xhr.upload.onprogress = (e) => { if (e.lengthComputable) setBar("upload-progress", e.loaded / e.total); };
  xhr.onload = () => {
    state.busy = false; setBar("upload-progress", null);
    let data = {};
    try { data = JSON.parse(xhr.responseText); } catch (_) { /* ignore */ }
    if (xhr.status !== 200) { showBanner(data.error || "Couldn't load that video."); refresh(); return; }
    state.video = data;
    showInfo(data);
    if (!data.has_audio) showBanner("This video has no audio track, so there is no silence to detect.");
    refresh();
  };
  xhr.onerror = () => { state.busy = false; setBar("upload-progress", null); showBanner("Upload failed. Is DeadAir still running?"); refresh(); };
  xhr.send(file);   // raw body: no multipart parsing needed, streamed straight to disk
}

function showInfo(v) {
  $("i-name").textContent = v.filename;
  $("i-duration").textContent = clock(v.duration);
  $("i-size").textContent = v.size_human;
  $("i-res").textContent = `${v.width} x ${v.height}`;
  $("i-fps").textContent = v.fps ? `${Number(v.fps.toFixed(3))} fps` : "unknown";
  $("info").hidden = false;
}

/* ---------- detection ---------- */
function settings() {
  return { threshold_db: parseFloat($("threshold").value), min_silence: parseFloat($("minsil").value), padding: parseFloat($("padding").value) };
}

async function detect() {
  state.busy = true; state.detection = null; $("results").hidden = true; $("done").hidden = true;
  showBanner(""); setStatus("detect-status", "Analyzing audio..."); setBar("detect-progress", 0); refresh();
  try {
    const { job_id } = await api("/api/detect", { video_id: state.video.id, ...settings() });
    const result = await runJob(job_id, (p) => setBar("detect-progress", p));
    state.detection = result;
    renderResults(result);
    setStatus("detect-status", "");
  } catch (err) {
    setStatus("detect-status", err.message, true);
  } finally {
    state.busy = false; setBar("detect-progress", null); refresh();
  }
}

function renderResults(r) {
  $("results").hidden = false;
  const summary = $("summary"), list = $("list");
  list.innerHTML = "";
  summary.classList.remove("ok");
  $("total").hidden = r.silences.length === 0;
  $("timeline-wrap").hidden = false;

  if (r.fully_silent) {
    summary.textContent = "The entire video was detected as silence, so there is nothing to export. Try a lower threshold such as -40 dB.";
    $("total").hidden = true;
  } else if (!r.has_changes) {
    summary.textContent = "No silence detected. No changes are necessary, so there's nothing to export.";
    summary.classList.add("ok");
  } else {
    const n = r.silences.length;
    summary.textContent = `${n} silence section${n === 1 ? "" : "s"} found`;
  }

  for (const s of r.silences) {
    const li = document.createElement("li");
    const range = document.createElement("span"); range.textContent = `${clock(s.start)} \u2192 ${clock(s.end)}`;
    const dur = document.createElement("span"); dur.className = "dur"; dur.textContent = secs(s.duration);
    li.append(range, dur); list.append(li);
  }
  $("total-val").textContent = secs(r.total_silence);
  renderTimeline(r);
}

function renderTimeline(r) {
  const tl = $("timeline"); tl.innerHTML = "";
  const parts = [
    ...r.keep.map((k) => ({ ...k, type: "kept" })),
    ...r.silences.map((s) => ({ ...s, type: "cut" })),
  ].sort((a, b) => a.start - b.start);
  for (const p of parts) {
    const seg = document.createElement("div");
    seg.className = `seg ${p.type}`;
    seg.style.width = `${((p.end - p.start) / r.duration) * 100}%`;
    seg.title = `${p.type === "kept" ? "Kept" : "Removed"}: ${clock(p.start)} \u2192 ${clock(p.end)} (${secs(p.end - p.start)})`;
    tl.append(seg);
  }
  const ruler = $("ruler"); ruler.innerHTML = "";
  for (let i = 0; i <= 4; i++) {
    const t = document.createElement("span");
    t.style.left = `${i * 25}%`; t.textContent = clock((r.duration * i) / 4).replace(/\.\d\d$/, "");
    ruler.append(t);
  }
  $("lg-kept").textContent = secs(r.final_duration);
  $("lg-cut").textContent = secs(r.total_silence);
}

/* ---------- export ---------- */
async function exportVideo() {
  state.busy = true; $("done").hidden = true; showBanner("");
  setStatus("export-status", "Exporting... (re-encoding for frame-accurate cuts)"); setBar("export-progress", 0); refresh();
  try {
    const { job_id } = await api("/api/export", { video_id: state.video.id });
    const r = await runJob(job_id, (p) => { setBar("export-progress", p); setStatus("export-status", `Exporting... ${Math.round(p * 100)}%`); });
    $("d-orig").textContent = secs(r.original_duration);
    $("d-final").textContent = secs(r.final_duration);
    $("d-removed").textContent = secs(r.removed);
    $("d-path").textContent = `${r.output_path} (${r.output_size_human})`;
    $("d-warn").hidden = !r.warnings.length; $("d-warn").textContent = r.warnings.join(" ");
    $("done").hidden = false;
    setStatus("export-status", "");
  } catch (err) {
    setStatus("export-status", err.message, true);
  } finally {
    state.busy = false; setBar("export-progress", null); refresh();
  }
}

async function openTarget(target) {
  try { await api("/api/open", { target }); } catch (err) { setStatus("export-status", err.message, true); }
}

/* ---------- wiring ---------- */
function applyPreset(name) {
  const p = PRESETS[name]; if (!p) return;
  $("threshold").value = p.threshold; $("minsil").value = p.minsil; $("padding").value = p.padding;
}
function syncPresetLabel() {
  const cur = { threshold: $("threshold").value, minsil: $("minsil").value, padding: $("padding").value };
  const match = Object.keys(PRESETS).find((k) => Object.keys(cur).every((f) => PRESETS[k][f] === cur[f]));
  const sel = $("preset"); sel.querySelector('[value="custom"]').hidden = !!match; sel.value = match || "custom";
}

$("preset").addEventListener("change", () => { applyPreset($("preset").value); syncPresetLabel(); invalidateDetection(); });
for (const id of ["threshold", "minsil", "padding"]) $(id).addEventListener("change", () => { syncPresetLabel(); invalidateDetection(); });
$("detect").addEventListener("click", detect);
$("export").addEventListener("click", exportVideo);
$("open-file").addEventListener("click", () => openTarget("file"));
$("open-folder").addEventListener("click", () => openTarget("folder"));

const drop = $("drop"), fileInput = $("file");
$("choose").addEventListener("click", (e) => { e.stopPropagation(); fileInput.click(); });
drop.addEventListener("click", () => fileInput.click());
drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); } });
fileInput.addEventListener("change", () => { if (fileInput.files[0]) upload(fileInput.files[0]); fileInput.value = ""; });
for (const ev of ["dragenter", "dragover"]) drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); });
for (const ev of ["dragleave", "drop"]) drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); });
drop.addEventListener("drop", (e) => { const f = e.dataTransfer.files[0]; if (f && !state.busy) upload(f); });
// A stray drop elsewhere on the page shouldn't navigate the browser away to the video file.
for (const ev of ["dragover", "drop"]) window.addEventListener(ev, (e) => e.preventDefault());

(async function init() {
  try {
    const st = await api("/api/status");
    state.ffmpegOk = st.ffmpeg && st.ffprobe;
    if (!state.ffmpegOk) showBanner("FFmpeg/FFprobe not found. Install FFmpeg and add it to PATH (see the README), then restart DeadAir.");
  } catch (_) { showBanner("Can't reach the DeadAir server."); }
  refresh();
})();
