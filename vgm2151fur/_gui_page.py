"""The GUI's one page, embedded because the package ships only .py files.

Vanilla HTML/CSS/JS: no build step, nothing to install. The page polls
`/api/jobs` once a second instead of holding a stream open, which keeps the
server trivial. Every button maps onto one endpoint in `gui.py`.
"""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>vgm2151fur</title>
<style>
:root {
  --bg: #14161a; --panel: #1c2026; --edge: #2b323b; --ink: #d7dde4;
  --dim: #8a94a0; --hot: #e8b34b; --ok: #7fc06e; --bad: #e06c60;
  --sel: #2a3540; --mono: "Cascadia Mono", Consolas, Menlo, monospace;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 14px/1.45 var(--mono);
}
header {
  display: flex; gap: 14px; align-items: baseline;
  padding: 8px 14px; border-bottom: 1px solid var(--edge);
}
header h1 { font-size: 16px; margin: 0; letter-spacing: .5px; }
header .sub { color: var(--dim); }
#furnace.ok { color: var(--ok); }
#furnace.bad { color: var(--bad); }
main {
  display: grid; grid-template-columns: minmax(320px, 1fr) 340px;
  gap: 10px; padding: 10px 14px 0;
}
section.panel {
  background: var(--panel); border: 1px solid var(--edge);
  border-radius: 6px; padding: 10px;
}
h2 {
  font-size: 12px; text-transform: uppercase; letter-spacing: 1px;
  color: var(--dim); margin: 0 0 8px;
}
.row { display: flex; gap: 6px; align-items: center; margin-bottom: 8px; }
.row input[type=text], .row input[type=number] {
  flex: 1; background: var(--bg); border: 1px solid var(--edge);
  color: var(--ink); padding: 5px 7px; border-radius: 4px; font: inherit;
}
button {
  background: var(--sel); color: var(--ink); border: 1px solid var(--edge);
  padding: 5px 11px; border-radius: 4px; font: inherit; cursor: pointer;
}
button:hover { border-color: var(--hot); }
button.primary { background: #3d4c2e; border-color: #55702f; }
button:disabled { opacity: .4; cursor: default; }
.packs, .tracks { max-height: 40vh; overflow: auto; margin-top: 8px; }
.packitem, .track {
  display: flex; gap: 8px; align-items: center; padding: 4px 6px;
  border-bottom: 1px solid var(--edge); cursor: pointer;
}
.packitem:hover { background: var(--sel); }
.packitem .meta, .track .meta { color: var(--dim); font-size: 12px; }
.track .name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.track.nofur .name { color: var(--dim); font-style: italic; }
.track .nofurmark { color: var(--dim); font-size: 11px; white-space: nowrap; }
.track button { padding: 2px 7px; font-size: 12px; }
#loose { margin-top: 8px; }
#loose .track {}
.actions {
  display: flex; gap: 6px; margin-top: 10px; flex-wrap: wrap;
}
.notice { color: var(--dim); font-size: 12px; margin-top: 6px; }
.optgrid { display: grid; grid-template-columns: auto 1fr; gap: 6px 8px; align-items: center; }
.optgrid label { color: var(--dim); font-size: 12px; }
.optgrid input, .optgrid select {
  background: var(--bg); border: 1px solid var(--edge); color: var(--ink);
  padding: 4px 6px; border-radius: 4px; font: inherit; width: 100%;
}
.optgrid .wide { grid-column: 1 / 3; }
.checks { display: flex; flex-wrap: wrap; gap: 4px 10px; grid-column: 1 / 3; }
.checks label { color: var(--ink); font-size: 12px; display: flex; gap: 4px; align-items: center; }
fieldset {
  border: 1px solid var(--edge); border-radius: 4px; margin: 0 0 8px;
}
legend { color: var(--dim); font-size: 11px; letter-spacing: 1px; }
#jobsbar { padding: 10px 14px 96px; }
#jobchips { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 8px; }
.chip {
  border: 1px solid var(--edge); border-radius: 4px; padding: 3px 8px;
  background: var(--panel); cursor: pointer; font-size: 12px;
}
.chip.running { border-color: var(--hot); }
.chip.done { color: var(--ok); }
.chip.error, .chip.cancelled { color: var(--bad); }
.chip.open { outline: 1px solid var(--hot); }
#jobdetail {
  background: var(--panel); border: 1px solid var(--edge);
  border-radius: 6px; padding: 10px;
}
#jobdetail .status-error { color: var(--bad); }
#jobdetail .status-done { color: var(--ok); }
#jobdetail .status-running { color: var(--hot); }
#jobdetail table { border-collapse: collapse; width: 100%; font-size: 12px; }
#jobdetail td, #jobdetail th {
  border-bottom: 1px solid var(--edge); padding: 3px 6px; text-align: left;
}
#jobdetail td.num { text-align: right; }
.variants { color: var(--dim); }
.warns { color: var(--hot); font-size: 12px; margin-top: 6px; }
#reporttext { white-space: pre-wrap; font-size: 12px; margin: 6px 0 0; }
#log {
  background: #101216; border: 1px solid var(--edge); border-radius: 6px;
  height: 150px; overflow: auto; padding: 8px 10px; margin-top: 10px;
  white-space: pre-wrap; font-size: 12px; color: var(--dim);
}
#playerbar {
  position: fixed; left: 0; right: 0; bottom: 0;
  display: flex; gap: 12px; align-items: center;
  background: var(--panel); border-top: 1px solid var(--edge);
  padding: 8px 14px;
}
#playname { color: var(--dim); font-size: 12px; max-width: 40%; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
#playerbar audio { flex: 1; height: 32px; }
dialog {
  background: var(--panel); color: var(--ink); border: 1px solid var(--edge);
  border-radius: 6px; min-width: 480px; max-width: 720px; padding: 14px;
}
dialog::backdrop { background: rgba(0,0,0,.6); }
.chiprow {
  display: flex; gap: 8px; align-items: center;
  border-bottom: 1px solid var(--edge); padding: 4px 2px;
}
.chiprow .cname { flex: 1; }
.chiprow input[type=number] {
  width: 90px; background: var(--bg); border: 1px solid var(--edge);
  color: var(--ink); padding: 3px 6px; border-radius: 4px; font: inherit;
}
#drophint {
  position: fixed; inset: 0; display: none; align-items: center;
  justify-content: center; background: rgba(20,22,26,.85); z-index: 9;
  font-size: 18px; color: var(--hot);
}
body.dropping #drophint { display: flex; }
.errline { color: var(--bad); font-size: 12px; margin-top: 6px; min-height: 1em; }
</style>
</head>
<body>
<header>
  <h1>vgm2151fur</h1><span class="sub" id="ver"></span>
  <span class="sub">furnace: <span id="furnace"></span></span>
  <span class="sub" style="margin-left:auto">drop or paste a path to open it</span>
</header>

<main>
  <section class="panel" id="browse">
    <h2>browse</h2>
    <div class="row">
      <input type="text" id="root" placeholder="folder of packs, or one pack">
      <button onclick="scan()">scan</button>
    </div>
    <div class="errline" id="browseerr"></div>
    <div id="packs" class="packs"></div>
    <div id="tracks" class="tracks"></div>
    <div id="loose"></div>
    <div class="actions">
      <button class="primary" id="btnconv" onclick="convertSel()">convert selected</button>
      <button id="btnrep" onclick="reportSel()">report selected</button>
      <button id="btnselall" onclick="selectAll(true)">select all</button>
      <button id="btnselnone" onclick="selectAll(false)">none</button>
    </div>
    <div class="notice" id="selinfo"></div>
  </section>

  <aside class="panel">
    <h2>options</h2>
    <fieldset><legend>output &amp; grid</legend>
      <div class="optgrid">
        <label class="wide">output folder (blank: &lt;first track's folder&gt;/fur)</label>
        <input type="text" class="wide" id="o_dest">
        <label>ticks / row</label><input type="number" id="o_speed" min="1" placeholder="auto">
        <label>max BPM</label><input type="number" id="o_max_bpm" step="any" placeholder="auto">
        <label>chips</label>
        <select id="o_chips">
          <option value="all">all chips</option>
          <option value="melody-only">melody only (no samples)</option>
          <option value="pcm-only">sample chips only</option>
        </select>
        <label>workers</label><input type="number" id="o_workers" min="1" placeholder="auto">
      </div>
    </fieldset>
    <fieldset><legend>condense ladder</legend>
      <div class="optgrid">
        <label>optimize pick</label>
        <select id="o_optimize">
          <option value="none">none — 1x only</option>
          <option value="lossless">lossless (coarsest safe)</option>
          <option value="all">all — largest factor</option>
        </select>
        <label>largest factor</label>
        <select id="o_condense_max"><option>2</option><option>3</option><option selected>4</option></select>
        <label>factors</label><input type="text" id="o_factors" placeholder="1, 1.5, 2 …">
        <div class="checks wide" id="ladder_checks"></div>
      </div>
    </fieldset>
    <fieldset><legend>furnace passes</legend>
      <div class="checks">
        <label><input type="checkbox" id="o_normalize"> normalize (-2.5 dBFS fit)</label>
        <label><input type="checkbox" id="o_verify"> verify variants</label>
      </div>
    </fieldset>
    <fieldset><legend>loops &amp; samples</legend>
      <div class="checks">
        <label><input type="checkbox" id="o_loop_find"> move loop onto the bar</label>
        <label><input type="checkbox" id="o_lfo_loop"> LFO / tail loop search</label>
        <label><input type="checkbox" id="o_lossy_samples"> lossy sample clustering</label>
        <label><input type="checkbox" id="o_dirs_layout"> dirs layout (default/ xN optimised/)</label>
        <label><input type="checkbox" id="o_c352_quad"> C352 quad output</label>
      </div>
    </fieldset>
    <fieldset><legend>render &amp; compare</legend>
      <div class="optgrid">
        <label>loops to render</label><input type="number" id="o_render_loops" min="1">
        <label>compare channel</label><input type="number" id="o_compare_channel" min="0">
      </div>
    </fieldset>
    <div class="actions">
      <button onclick="saveOptions()">remember these</button>
      <span class="notice">saved to ~/.vgm2151fur.json (dest &amp; lossy stay per-session)</span>
    </div>
  </aside>
</main>

<section id="jobsbar">
  <h2>jobs</h2>
  <div id="jobchips"></div>
  <div id="jobdetail"><span class="notice">no job selected</span></div>
  <div id="log"></div>
</section>

<div id="playerbar">
  <span id="playname">nothing playing</span>
  <audio id="player" controls></audio>
</div>

<dialog id="volumesdlg">
  <h2>chip volumes</h2>
  <div class="notice" id="volfur"></div>
  <div id="volrows" style="max-height:50vh; overflow:auto"></div>
  <div class="row" style="margin-top:10px">
    <input type="number" id="volfactor" step="any" placeholder="×factor">
    <button onclick="applyVolumes('factor')">multiply checked</button>
    <input type="number" id="volvalue" step="any" placeholder="set value">
    <button onclick="applyVolumes('value')">set checked</button>
  </div>
  <div class="row">
    <button onclick="checkAllChips(true)">all</button>
    <button onclick="checkAllChips(false)">none</button>
    <span style="flex:1"></span>
    <button onclick="volumesdlg.close()">close</button>
  </div>
  <div class="errline" id="volerr"></div>
</dialog>

<dialog id="comparedlg">
  <h2>compare fur against source</h2>
  <div class="notice" id="cmppaths"></div>
  <div class="row">
    <label style="color:var(--dim)">channel</label>
    <input type="number" id="cmpchannel" min="0" style="width:70px">
    <button class="primary" onclick="runCompare()">compare</button>
  </div>
  <div class="notice">exports the .fur back to VGM through Furnace and lists
  sustained pitch divergences on that channel. The job shows up below.</div>
  <div class="actions"><button onclick="comparedlg.close()">close</button></div>
</dialog>

<div id="drophint">drop to open</div>

<script>
"use strict";
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

const S = {
  options: {}, view: null, selVgm: new Set(), furByPath: {},
  jobs: [], openJobId: null, currentPack: null,
};

async function api(path, body) {
  const res = await fetch(path, body === undefined
    ? {method: "GET"} : {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body)});
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.status);
  return data;
}

function opt(id) { return $(id); }

// ---- options form -------------------------------------------------
const BOOL_OPTS = ["dirs_layout", "lfo_loop", "loop_find", "lossy_samples",
  "normalize", "verify", "c352_quad"];
const TEXT_OPTS = ["dest", "speed", "max_bpm", "factors", "workers",
  "render_loops", "compare_channel"];
const PICK_OPTS = ["optimize", "chips"];

function fillOptions(opts) {
  for (const key of BOOL_OPTS) opt("o_" + key).checked = !!opts[key];
  for (const key of TEXT_OPTS) opt("o_" + key).value = opts[key] !== undefined ? opts[key] : "";
  for (const key of PICK_OPTS) opt("o_" + key).value = opts[key] || "all";
  ladderChecks();
}

function readOptions() {
  const o = {};
  for (const key of BOOL_OPTS) o[key] = opt("o_" + key).checked;
  for (const key of TEXT_OPTS) o[key] = opt("o_" + key).value.trim();
  for (const key of PICK_OPTS) o[key] = opt("o_" + key).value;
  o.render_loops = parseInt(o.render_loops, 10) || 1;
  o.compare_channel = parseInt(o.compare_channel, 10) || 0;
  return o;
}

async function saveOptions() {
  try { await api("/api/save_options", readOptions()); }
  catch (err) { $("browseerr").textContent = err.message; return; }
  $("browseerr").textContent = "";
}

const LADDER = [1, 1.5, 2, 2.5, 3, 3.5, 4];
function ladderChecks() {
  const max = parseInt(opt("o_condense_max").value, 10) || 4;
  const custom = opt("o_factors").value.trim();
  const box = $("ladder_checks");
  if (custom) {
    box.innerHTML = '<span class="notice">custom factors override the ladder</span>';
    return;
  }
  box.innerHTML = LADDER.map(f => {
    const kept = f <= max || f === 1;
    return `<label style="opacity:${kept ? 1 : .35}">x${f}</label>`;
  }).join("");
}

// ---- browse -------------------------------------------------------
async function scan() {
  await openRoot(opt("root").value.trim());
}

async function openRoot(root) {
  $("browseerr").textContent = "";
  try {
    const data = await api("/api/scan", {root});
    opt("root").value = root;
    renderView(data);
  } catch (err) { $("browseerr").textContent = err.message; }
}

function renderView(data) {
  S.view = data;
  S.currentPack = data.kind === "pack" ? data.path : null;
  $("packs").innerHTML = "";
  $("tracks").innerHTML = "";
  $("loose").innerHTML = "";
  S.selVgm.clear();
  if (data.kind === "packs") {
    const box = $("packs");
    box.innerHTML = data.packs.map(p => `
      <div class="packitem" data-pack="${esc(p.path)}">
        <span>${esc(p.name)}</span>
        <span class="meta">${p.tracks} tracks · ${esc(p.chips)}${p.playlist ? " · .m3u" : ""}</span>
      </div>`).join("")
      + (data.truncated ? '<div class="notice">list truncated — scan a deeper folder</div>' : "");
  } else if (data.kind === "pack") {
    opt("root").value = data.path;
    renderPack(data);
  }
  selInfo();
}

function renderPack(pack) {
  $("tracks").innerHTML = pack.tracks.map((t, i) => `
    <div class="track ${t.fur ? "" : "nofur"}">
      <input type="checkbox" data-vgm="${esc(t.path)}"
        ${S.selVgm.has(t.path) ? "checked" : ""} onchange="checkTrack(this)">
      <span class="name" title="${esc(t.path)}">${i + 1}. ${esc(t.name)}</span>
      <span class="nofurmark">${t.fur ? esc("…/" + t.fur.split(/[\\/]/).pop()) : "no .fur yet"}</span>
      ${t.fur ? `
        <button data-act="volumes" data-fur="${esc(t.fur)}">volumes</button>
        <button data-act="render" data-fur="${esc(t.fur)}">render</button>
        <button data-act="compare" data-src="${esc(t.path)}" data-fur="${esc(t.fur)}">compare</button>
        ${t.wav ? `<button data-act="play" data-token="${t.wav}" data-name="${esc(t.name)}">play</button>` : ""}
      ` : ""}
    </div>`).join("");
  $("packs").innerHTML = `<div class="notice">${esc(pack.name)} · ${esc(pack.chips)}
    ${pack.playlist_order ? "· playlist order" : ""} · output: <span title="${esc(pack.fur_dir)}">${esc(pack.fur_dir)}</span></div>`;
}

async function openPack(path) {
  $("browseerr").textContent = "";
  try { renderView(await api("/api/pack", {path})); }
  catch (err) { $("browseerr").textContent = err.message; }
}

async function openLoose(data) {
  S.view = data;
  S.currentPack = null;
  $("packs").innerHTML = "";
  $("tracks").innerHTML = "";
  S.selVgm.clear();
  const all = [...data.vgms.map(p => ({path: p, fur: null, name: p.split(/[\\/]/).pop()})),
               ...data.furs.map(p => ({path: null, fur: p, name: p.split(/[\\/]/).pop()}))];
  $("loose").innerHTML = `
    <div class="notice">${data.vgms.length} .vgm/.vgz and ${data.furs.length} .fur matched</div>` +
    all.map(t => t.path ? `
      <div class="track"><input type="checkbox" data-vgm="${esc(t.path)}" onchange="checkTrack(this)">
        <span class="name">${esc(t.name)}</span><span class="meta">vgm</span></div>` : `
      <div class="track"><input type="checkbox" data-fur="${esc(t.fur)}" onchange="checkFur(this)">
        <span class="name">${esc(t.name)}</span><span class="meta">fur</span>
        <button data-act="volumes" data-fur="${esc(t.fur)}">volumes</button>
        <button data-act="render" data-fur="${esc(t.fur)}">render</button>
      </div>`).join("");
  selInfo();
}

function checkTrack(box) {
  if (box.checked) S.selVgm.add(box.dataset.vgm);
  else S.selVgm.delete(box.dataset.vgm);
  selInfo();
}

function checkFur(box) { /* furs act through their own row buttons */ }

function selectAll(on) {
  for (const box of document.querySelectorAll("input[data-vgm]")) {
    box.checked = on;
    if (on) S.selVgm.add(box.dataset.vgm); else S.selVgm.delete(box.dataset.vgm);
  }
  selInfo();
}

function selInfo() {
  const n = S.selVgm.size;
  $("selinfo").textContent =
    n ? `${n} track(s) selected for convert / report` : "no tracks selected";
  $("btnconv").disabled = $("btnrep").disabled = n === 0;
}

// ---- jobs ---------------------------------------------------------
async function convertSel() {
  const files = [...S.selVgm];
  if (!files.length) return;
  try {
    await api("/api/convert", {files, options: readOptions(),
      dest: opt("o_dest").value.trim(), pack: S.currentPack});
    pollJobs();
  } catch (err) { $("browseerr").textContent = err.message; }
}

async function reportSel() {
  const files = [...S.selVgm];
  if (!files.length) return;
  try {
    const o = readOptions();
    await api("/api/report", {files, options: {speed: o.speed, max_bpm: o.max_bpm}});
    pollJobs();
  } catch (err) { $("browseerr").textContent = err.message; }
}

async function renderFur(fur) {
  try {
    await api("/api/render", {files: [fur],
      loops: parseInt(opt("o_render_loops").value, 10) || 1});
    pollJobs();
  } catch (err) { $("browseerr").textContent = err.message; }
}

async function cancelJob(id) {
  await api("/api/cancel", {id});
  pollJobs();
}

function statusIcon(s) {
  return {queued: "…", running: "▶", done: "✓", error: "✗",
    cancelled: "⨯"}[s] || s;
}

function renderJobs() {
  const chips = S.jobs.map(j => `
    <span class="chip ${j.status} ${j.id === S.openJobId ? "open" : ""}"
      data-job="${j.id}">${statusIcon(j.status)} ${esc(j.label)} · ${esc(j.kind)}</span>`).join("");
  $("jobchips").innerHTML = chips || '<span class="notice">no jobs yet — select tracks and convert</span>';
  const open = S.jobs.find(j => j.id === S.openJobId);
  $("jobdetail").innerHTML = open ? jobDetail(open) :
    '<span class="notice">no job selected</span>';
  const lines = open ? open.log : [];
  const logEl = $("log");
  logEl.textContent = lines.join("\n") +
    (lines.length ? "\n" : "") +
    S.jobs.filter(j => j.error && (!open || j.id !== open.id))
      .map(j => `ERROR in ${j.label}: ${j.error}`).join("\n");
  logEl.scrollTop = logEl.scrollHeight;
}

function jobDetail(job) {
  const head = `
    <div><span class="status-${esc(job.status)}">${esc(job.status)}</span>
      ${job.error ? " — " + esc(job.error) : ""}
      <button style="float:right" data-act="cancel" data-id="${job.id}">cancel</button></div>`;
  const rows = Object.entries(job.rows);
  if (job.kind === "report") {
    const text = rows.map(([, r]) => r.text).filter(Boolean).join("\n\n");
    const body = rows.map(([path, r]) =>
      `<div class="status-${esc(r.status)}">${statusIcon(r.status)} ${esc(path.split(/[\\/]/).pop())}${r.detail ? " — " + esc(r.detail) : ""}</div>`).join("");
    return head + body + (text ? `<pre id="reporttext">${esc(text)}</pre>` : "");
  }
  if (job.kind === "compare") {
    const runs = (job.extra && job.extra.runs) || [];
    return head
      + `<div class="notice">${esc(job.files[0])} vs ${esc((job.extra || {}).exported || "")}</div>`
      + (runs.length
        ? `<table><tr><th>time</th><th>worst</th></tr>${runs.map(r =>
            `<tr><td>${esc(r.text)}</td><td class="num">${r.st} st</td></tr>`).join("")}</table>`
        : '<div class="status-done">no divergence listed</div>');
  }
  const trs = rows.map(([path, r]) => {
    const name = path.split(/[\\/]/).pop();
    let line = `<div class="status-${esc(r.status)}">${statusIcon(r.status)} ${esc(name)}`
      + (r.detail ? ` — ${esc(r.detail)}` : "") + "</div>";
    if (r.warnings && r.warnings.length)
      line += `<div class="warns">${r.warnings.map(esc).join(" · ")}</div>`;
    if (r.variants && r.variants.length) {
      line += `<table class="variants"><tr><th>file</th><th>x</th><th>bpm</th><th>loss</th><th>keys</th><th></th></tr>`
        + r.variants.map(v =>
          `<tr${v.pick ? ' style="color:var(--ok)"' : ""}><td>${esc(v.file)}</td>
           <td class="num">x${v.factor}</td><td class="num">${v.bpm}</td>
           <td class="num">${v.loss}</td><td class="num">${v.miss !== null && v.miss !== undefined ? v.miss : ""}</td>
           <td>${v.pick ? "pick" : (v.lossless ? "" : "dropped")}</td></tr>`).join("") + "</table>";
    }
    if (r.wav)
      line += ` <button data-act="play" data-token="${r.wav}"
        data-name="${esc(name)}">play wav</button>`;
    return line;
  }).join("<hr style='border-color:var(--edge)'>");
  return head + trs;
}

function showJob(id) { S.openJobId = id; renderJobs(); }

async function pollJobs() {
  try {
    const data = await api("/api/jobs");
    S.jobs = data.jobs;
    if (!S.openJobId && S.jobs.length) S.openJobId = S.jobs[0].id;
    renderJobs();
  } catch (err) { /* the page is closing, or a reload raced the server */ }
}

// ---- wav player ---------------------------------------------------
function playWav(token, name) {
  const audio = $("player");
  audio.src = "/api/wav?token=" + token;
  audio.play().catch(() => {});
  $("playname").textContent = name;
}

// ---- volumes dialog ------------------------------------------------
let volPath = null;

async function openVolumes(fur) {
  volPath = fur;
  $("volerr").textContent = "";
  $("volfur").textContent = fur;
  const box = $("volrows");
  box.innerHTML = '<span class="notice">reading…</span>';
  $("volumesdlg").showModal();
  try {
    const data = await api("/api/chips", {files: [fur]});
    const entry = data.files[0];
    if (entry.error) { $("volerr").textContent = entry.error; box.innerHTML = ""; return; }
    box.innerHTML = entry.chips.map(c => `
      <div class="chiprow">
        <input type="checkbox" checked data-chip="${c.index}">
        <span>#${c.index}</span><span class="cname">${esc(c.name)} <span class="meta">(${esc(c.id)})</span></span>
        <input type="number" step="0.01" min="0" max="4" value="${c.volume}" data-vol="${c.index}">
      </div>`).join("");
  } catch (err) { $("volerr").textContent = err.message; box.innerHTML = ""; }
}

function checkAllChips(on) {
  for (const box of document.querySelectorAll("#volrows input[type=checkbox]"))
    box.checked = on;
}

async function applyVolumes(mode) {
  const targets = [...document.querySelectorAll("#volrows input[type=checkbox]")]
    .filter(b => b.checked).map(b => parseInt(b.dataset.chip, 10));
  const body = {files: [volPath]};
  if (mode === "factor") {
    const f = parseFloat($("volfactor").value);
    if (isNaN(f)) { $("volerr").textContent = "give a factor"; return; }
    body.factor = f;
  } else {
    const v = parseFloat($("volvalue").value);
    if (isNaN(v)) { $("volerr").textContent = "give a value"; return; }
    body.value = v;
  }
  if (targets.length) body.targets = targets;
  try {
    const data = await api("/api/volumes", body);
    const result = data.results[0];
    if (result.error) { $("volerr").textContent = result.error; return; }
    for (const c of result.chips) {
      const input = document.querySelector(`#volrows input[data-vol="${c.index}"]`);
      if (input) input.value = c.volume;
    }
    $("volerr").textContent = "";
  } catch (err) { $("volerr").textContent = err.message; }
}

// ---- compare dialog -------------------------------------------------
let cmpSource = null, cmpFur = null;

async function openCompare(source, fur) {
  cmpSource = source; cmpFur = fur;
  $("cmppaths").textContent = `${source}  ⇔  ${fur}`;
  $("cmpchannel").value = S.options.compare_channel || 0;
  $("comparedlg").showModal();
}

async function runCompare() {
  try {
    await api("/api/compare", {source: cmpSource, fur: cmpFur,
      channel: parseInt($("cmpchannel").value, 10) || 0});
    $("comparedlg").close();
    pollJobs();
  } catch (err) { $("cmppaths").textContent = err.message; }
}

// ---- drop & paste ----------------------------------------------------
function cleanDropped(text) {
  return text.trim().replace(/^"|"$/g, "").replace(/^file:\/\/\/?/i, "");
}

async function openPath(raw) {
  const path = cleanDropped(raw);
  if (!path) return;
  $("browseerr").textContent = "";
  try {
    const data = await api("/api/open_path", {path});
    if (data.kind === "paths") openLoose(data);
    else {
      opt("root").value = path;
      renderView(data);
    }
  } catch (err) { $("browseerr").textContent = err.message; }
}

document.addEventListener("dragover", ev => {
  ev.preventDefault();
  document.body.classList.add("dropping");
});
document.addEventListener("dragleave", () => document.body.classList.remove("dropping"));
document.addEventListener("drop", ev => {
  ev.preventDefault();
  document.body.classList.remove("dropping");
  const text = ev.dataTransfer.getData("text/plain") || ev.dataTransfer.getData("text/uri-list");
  if (text) openPath(text);
});
document.addEventListener("paste", ev => {
  const text = ev.clipboardData.getData("text/plain");
  if (text && /[\\/]/.test(text) && !ev.target.closest("input, textarea")) openPath(text);
});

// ---- delegated clicks ---------------------------------------------
// Paths and names ride in data-* attributes, never inline JS strings: pack
// folders with apostrophes must not break the markup.
document.addEventListener("click", ev => {
  const el = ev.target.closest("[data-act]");
  if (!el) return;
  const act = el.dataset.act;
  if (act === "volumes") openVolumes(el.dataset.fur);
  else if (act === "render") renderFur(el.dataset.fur);
  else if (act === "compare") openCompare(el.dataset.src, el.dataset.fur);
  else if (act === "play") playWav(el.dataset.token, el.dataset.name);
  else if (act === "cancel") cancelJob(parseInt(el.dataset.id, 10));
});
$("packs").addEventListener("click", ev => {
  const item = ev.target.closest(".packitem");
  if (item) openPack(item.dataset.pack);
});
$("jobchips").addEventListener("click", ev => {
  const chip = ev.target.closest(".chip");
  if (chip) showJob(parseInt(chip.dataset.job, 10));
});

// ---- boot -----------------------------------------------------------
(async function boot() {
  try {
    const state = await api("/api/state");
    S.options = state.options;
    $("ver").textContent = "v" + state.version;
    const furn = $("furnace");
    if (state.furnace) {
      furn.textContent = state.furnace;
      furn.className = "ok";
    } else {
      furn.textContent = "not found — render, compare and normalize stay off";
      furn.className = "bad";
    }
    fillOptions(state.options);
    if (state.browse_root) {
      opt("root").value = state.browse_root;
      openRoot(state.browse_root);
    }
  } catch (err) { $("browseerr").textContent = err.message; }
  await pollJobs();
  setInterval(pollJobs, 1000);
})();
</script>
</body>
</html>
"""