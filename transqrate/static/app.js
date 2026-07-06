/* transQrate UI - dependency-free single page app */
"use strict";

const $main = document.getElementById("main");
let pollTimer = null;
let state = { profiles: [], logJob: "app" };

/* ------------------------------------------------------------- utilities */

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (e) { /* noop */ }
    throw new Error(msg);
  }
  const ct = res.headers.get("content-type") || "";
  return ct.includes("json") ? res.json() : res.text();
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

function basename(p) { return String(p || "").split("/").pop(); }

function fmtBytes(n) {
  if (n == null) return "–";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = Number(n);
  while (Math.abs(v) >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v < 10 && i > 0 ? 2 : 1)} ${units[i]}`;
}

function fmtEta(s) {
  if (s == null || s < 0) return "–";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${Math.floor(s % 60)}s`;
  return `${Math.floor(s)}s`;
}

function savedCell(job) {
  if (job.size_in == null || job.size_out == null) return "–";
  const saved = job.size_in - job.size_out;
  const pct = (saved / job.size_in * 100).toFixed(1);
  const cls = saved >= 0 ? "saved-pos" : "saved-neg";
  return `<span class="${cls}">${fmtBytes(saved)} (${pct}%)</span>`;
}

function badge(status) { return `<span class="badge ${esc(status)}">${esc(status)}</span>`; }

function toast(msg, isErr = false) {
  document.querySelectorAll(".toast").forEach(t => t.remove());
  const el = document.createElement("div");
  el.className = "toast" + (isErr ? " err" : "");
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

/* ---------------------------------------------------------------- router */

const pages = { dashboard, sources, profiles, logs, settings };

async function route() {
  stopPolling();
  const page = (location.hash.replace("#/", "") || "dashboard").split("?")[0];
  document.querySelectorAll("#nav a").forEach(a =>
    a.classList.toggle("active", a.dataset.page === page));
  const fn = pages[page] || pages.dashboard;
  try {
    await fn();
  } catch (e) {
    $main.innerHTML = `<h1>Error</h1><p class="error-text">${esc(e.message)}</p>`;
  }
}
window.addEventListener("hashchange", route);

/* ------------------------------------------------------------- dashboard */

async function dashboard() {
  $main.innerHTML = "<h1>Dashboard</h1><div id='dash'></div>";
  await refreshDashboard();
  pollTimer = setInterval(refreshDashboard, 2500);
}

async function refreshDashboard() {
  const d = await api("/api/dashboard");
  const t = d.status.totals;
  const saved = (t.bytes_in || 0) - (t.bytes_out || 0);
  const pct = t.bytes_in ? (saved / t.bytes_in * 100).toFixed(1) : "0";
  const c = d.status.counts;
  const el = document.getElementById("dash");
  if (!el) return;
  el.innerHTML = `
    <div class="tiles">
      <div class="tile"><div class="label">Active</div>
        <div class="value">${d.active.length}</div>
        <div class="sub">${d.pending_total} queued</div></div>
      <div class="tile"><div class="label">Completed</div>
        <div class="value">${t.done || 0}</div>
        <div class="sub">${c.failed || 0} failed &middot; ${c.skipped || 0} skipped</div></div>
      <div class="tile"><div class="label">Space saved</div>
        <div class="value">${fmtBytes(saved)}</div>
        <div class="sub">${pct}% of ${fmtBytes(t.bytes_in || 0)}</div></div>
    </div>
    <h2>Active jobs</h2>
    ${d.active.length ? d.active.map(activeCard).join("") :
      `<div class="card empty">No job is running. Queue files via Sources &rarr; Scan now.</div>`}
    ${d.pending.length ? `<h2>Queue (${d.pending_total})</h2>
      <div class="tablewrap"><table><thead><tr><th>File</th><th>Profile</th><th></th></tr></thead><tbody>
      ${d.pending.map(j => `<tr><td class="wrap">${esc(j.input_path)}</td>
        <td>${esc(j.profile_name)}</td>
        <td><button class="small danger" data-cancel="${j.id}">Cancel</button></td></tr>`).join("")}
      </tbody></table></div>` : ""}
    <h2>Recent
      <button class="small danger" id="clear-history" style="margin-left:10px">Delete all</button>
    </h2>
    <div class="tablewrap"><table>
      <thead><tr><th>File</th><th>Status</th><th>Profile</th><th>ICQ</th><th>VMAF</th>
        <th>Size in &rarr; out</th><th>Saved</th><th></th></tr></thead>
      <tbody>${d.recent.length ? d.recent.map(recentRow).join("") :
        `<tr><td colspan="8" class="empty">Nothing transcoded yet.</td></tr>`}</tbody>
    </table></div>`;
  bindJobButtons(el);
}

function activeCard(j) {
  const analyzing = j.status === "analyzing";
  const pct = analyzing ? 100 : (j.progress || 0);
  return `<div class="card">
    <div class="row-top">
      <div class="title">${esc(basename(j.input_path))}</div>
      <div class="actions">${badge(j.status)}
        <button class="small danger" data-cancel="${j.id}">Cancel</button>
        <button class="small" data-log="${j.id}">Log</button></div>
    </div>
    <div class="meta">
      <span>${esc(j.profile_name)}</span>
      ${j.chosen_icq != null ? `<span>ICQ ${j.chosen_icq}</span>` : ""}
      ${j.vmaf_score != null ? `<span>VMAF ${j.vmaf_score}</span>` : ""}
      ${analyzing ? "<span>searching quality (VMAF samples)&hellip;</span>" :
        `<span>${(j.progress || 0).toFixed(1)}%</span>
         <span>${j.fps ? j.fps.toFixed(0) + " fps" : ""}</span>
         <span>${esc(j.speed || "")}</span>
         <span>ETA ${fmtEta(j.eta_s)}</span>`}
    </div>
    <div class="progress${analyzing ? " indeterminate" : ""}"><div style="width:${pct}%"></div></div>
  </div>`;
}

function recentRow(j) {
  return `<tr>
    <td class="wrap" title="${esc(j.input_path)}">${esc(basename(j.input_path))}</td>
    <td>${badge(j.status)}${j.error ? ` <span class="error-text" title="${esc(j.error)}">!</span>` : ""}</td>
    <td>${esc(j.profile_name || "")}</td>
    <td class="num">${j.chosen_icq ?? "–"}</td>
    <td class="num">${j.vmaf_score ?? "–"}</td>
    <td class="num">${j.size_in != null ? fmtBytes(j.size_in) + " → " + fmtBytes(j.size_out) : "–"}</td>
    <td class="num">${savedCell(j)}</td>
    <td><div class="actions">
      ${["failed", "cancelled", "skipped"].includes(j.status) ?
        `<button class="small" data-retry="${j.id}">Retry</button>` : ""}
      <button class="small" data-log="${j.id}">Log</button>
      <button class="small danger" data-del-job="${j.id}" title="Delete entry and its log">&#10005;</button>
    </div></td></tr>`;
}

function bindJobButtons(root) {
  root.querySelectorAll("[data-cancel]").forEach(b => b.onclick = async () => {
    try { await api(`/api/jobs/${b.dataset.cancel}/cancel`, { method: "POST" }); toast("Cancel requested"); }
    catch (e) { toast(e.message, true); }
  });
  root.querySelectorAll("[data-retry]").forEach(b => b.onclick = async () => {
    try { await api(`/api/jobs/${b.dataset.retry}/retry`, { method: "POST" }); toast("Job re-queued"); }
    catch (e) { toast(e.message, true); }
  });
  root.querySelectorAll("[data-log]").forEach(b => b.onclick = () => {
    state.logJob = b.dataset.log;
    location.hash = "#/logs";
  });
  root.querySelectorAll("[data-del-job]").forEach(b => b.onclick = async () => {
    if (!confirm("Delete this entry and its log?")) return;
    try { await api(`/api/jobs/${b.dataset.delJob}`, { method: "DELETE" }); refreshDashboard(); }
    catch (e) { toast(e.message, true); }
  });
  const clearBtn = root.querySelector("#clear-history");
  if (clearBtn) clearBtn.onclick = async () => {
    if (!confirm("Delete ALL finished entries and their logs?")) return;
    try {
      const r = await api("/api/jobs", { method: "DELETE" });
      toast(`${r.deleted} entries deleted`);
      refreshDashboard();
    } catch (e) { toast(e.message, true); }
  };
}

/* --------------------------------------------------------------- sources */

async function sources() {
  const [srcs, profs] = await Promise.all([api("/api/sources"), api("/api/profiles")]);
  state.profiles = profs;
  $main.innerHTML = `<h1>Sources</h1>
    <div id="src-form-slot"></div>
    <button class="primary" id="add-src">Add source folder</button>
    <h2>Configured folders</h2>
    <div class="tablewrap"><table>
      <thead><tr><th>Folder</th><th>Profile</th><th>Output</th><th>Originals</th><th>Watch</th>
        <th>Done</th><th>Saved</th><th>Active</th><th></th></tr></thead>
      <tbody>${srcs.length ? srcs.map(sourceRow).join("") :
        `<tr><td colspan="9" class="empty">No source folders yet. Add one to get started.</td></tr>`}
      </tbody></table></div>
    <div id="src-files-slot"></div>
    <p class="inline-note">Paths are container paths - mount your media into the container
      (e.g. <code>/media/movies</code>). Leaving output empty transcodes in place and replaces
      the original file. Finished files carry a <code>TRANSQRATE</code> metadata tag and are
      never picked up twice.</p>`;
  document.getElementById("add-src").onclick = () => sourceForm(null);
  document.querySelectorAll("[data-scan]").forEach(b => b.onclick = async () => {
    b.disabled = true;
    try {
      const r = await api(`/api/sources/${b.dataset.scan}/scan`, { method: "POST" });
      toast(`Scan finished: ${r.queued} queued, ${r.skipped} skipped, ${r.errors} errors`);
      route();
    } catch (e) { toast(e.message, true); b.disabled = false; }
  });
  document.querySelectorAll("[data-files]").forEach(b => b.onclick = () =>
    sourceDetails(srcs.find(s => s.id == b.dataset.files)));
  document.querySelectorAll("[data-edit-src]").forEach(b => b.onclick = () =>
    sourceForm(srcs.find(s => s.id == b.dataset.editSrc)));
  document.querySelectorAll("[data-del-src]").forEach(b => b.onclick = async () => {
    if (!confirm("Remove this source folder? Jobs and history are kept.")) return;
    try { await api(`/api/sources/${b.dataset.delSrc}`, { method: "DELETE" }); route(); }
    catch (e) { toast(e.message, true); }
  });
}

function sourceRow(s) {
  return `<tr>
    <td class="wrap">${esc(s.path)}</td>
    <td>${esc(s.profile_name)}</td>
    <td class="wrap">${s.output_path ? esc(s.output_path) : "<i>in place</i>"}</td>
    <td>${!s.output_path ? "replaced" : (s.delete_original ? "delete" : "keep")}</td>
    <td>${s.watch ? "yes" : "no"}</td>
    <td class="num">${s.stats.done}</td>
    <td class="num">${fmtBytes(s.stats.saved)}</td>
    <td class="num">${s.stats.active}</td>
    <td><div class="actions">
      <button class="small primary" data-scan="${s.id}">Scan now</button>
      <button class="small" data-files="${s.id}">Details</button>
      <button class="small" data-edit-src="${s.id}">Edit</button>
      <button class="small danger" data-del-src="${s.id}">Delete</button>
    </div></td></tr>`;
}

async function sourceDetails(src) {
  const slot = document.getElementById("src-files-slot");
  slot.innerHTML = `<div class="panel"><h3 class="panel-title">Files in ${esc(src.path)}</h3>
    <div class="empty">Loading&hellip;</div></div>`;
  let d;
  try { d = await api(`/api/sources/${src.id}/files`); }
  catch (e) { toast(e.message, true); slot.innerHTML = ""; return; }
  const active = ["pending", "analyzing", "running", "cancelling"];
  const locked = f => active.includes(f.state) || f.state === "ignored";
  slot.innerHTML = `<div class="panel">
    <h3 class="panel-title">Files in ${esc(src.path)} (${d.files.length})</h3>
    <div class="tablewrap"><table>
      <thead><tr><th><input type="checkbox" id="files-all" title="select all"></th>
        <th>File</th><th>Size</th><th>Status</th><th>Saved</th></tr></thead>
      <tbody>${d.files.length ? d.files.map(f => `<tr>
        <td><input type="checkbox" class="file-check" value="${esc(f.path)}"
             ${locked(f) ? "disabled" : ""}></td>
        <td class="wrap">${esc(f.rel)}</td>
        <td class="num">${fmtBytes(f.size)}</td>
        <td>${badge(f.state)}${f.note ?
          ` <span class="inline-note">${esc(f.note)}</span>` : ""}</td>
        <td class="num">${f.saved != null ? fmtBytes(f.saved) : "–"}</td>
      </tr>`).join("") : `<tr><td colspan="5" class="empty">No files found.</td></tr>`}
      </tbody></table></div>
    <div class="form-foot">
      <button class="primary" id="requeue-btn" disabled>Re-queue selected</button>
      <button id="files-close">Close</button>
      <span class="hint">Re-queueing resets a file and transcodes it again,
        even if it is already tagged as done.</span>
    </div></div>`;
  const checks = [...slot.querySelectorAll(".file-check:not(:disabled)")];
  const btn = slot.querySelector("#requeue-btn");
  const sync = () => {
    const n = checks.filter(c => c.checked).length;
    btn.disabled = !n;
    btn.textContent = n ? `Re-queue selected (${n})` : "Re-queue selected";
  };
  checks.forEach(c => c.onchange = sync);
  slot.querySelector("#files-all").onchange = ev => {
    checks.forEach(c => { c.checked = ev.target.checked; });
    sync();
  };
  slot.querySelector("#files-close").onclick = () => { slot.innerHTML = ""; };
  btn.onclick = async () => {
    const paths = checks.filter(c => c.checked).map(c => c.value);
    btn.disabled = true;
    try {
      const r = await api(`/api/sources/${src.id}/requeue`, {
        method: "POST", body: JSON.stringify({ paths }),
      });
      toast(`${r.queued} file(s) queued for transcoding`);
      sourceDetails(src);
    } catch (e) { toast(e.message, true); btn.disabled = false; }
  };
}

function sourceForm(src) {
  const slot = document.getElementById("src-form-slot");
  const s = src || { path: "", profile_id: state.profiles[0]?.id, output_path: "",
    delete_original: 1, watch: 0, enabled: 1 };
  slot.innerHTML = `<form class="panel" id="src-form">
    <div class="grid">
      <label class="field"><span>Source folder (container path)</span>
        <input type="text" name="path" required value="${esc(s.path)}" placeholder="/media/movies">
        <button type="button" class="small" style="margin-top:6px" data-browse="path">Browse&hellip;</button>
      </label>
      <label class="field"><span>Profile</span>
        <select name="profile_id">${state.profiles.map(p =>
          `<option value="${p.id}" ${p.id == s.profile_id ? "selected" : ""}>${esc(p.name)}</option>`).join("")}
        </select>
      </label>
      <label class="field"><span>Output folder (empty = replace in place)</span>
        <input type="text" name="output_path" value="${esc(s.output_path || "")}" placeholder="optional">
        <button type="button" class="small" style="margin-top:6px" data-browse="output_path">Browse&hellip;</button>
      </label>
      <div class="field"><span>&nbsp;</span>
        <label class="check"><input type="checkbox" name="delete_original" ${s.delete_original ? "checked" : ""}>
          Delete original after transcoding</label>
        <label class="check" style="margin-top:8px"><input type="checkbox" name="watch" ${s.watch ? "checked" : ""}>
          Watch folder (queue new files automatically)</label>
        <label class="check" style="margin-top:8px"><input type="checkbox" name="enabled" ${s.enabled ? "checked" : ""}>
          Enabled</label>
        <div class="inline-note">In-place sources (no output folder) always replace the original.</div>
      </div>
    </div>
    <div class="form-foot">
      <button type="submit" class="primary">${src ? "Save changes" : "Add source"}</button>
      <button type="button" id="src-cancel">Close</button>
    </div>
  </form>`;
  slot.querySelectorAll("[data-browse]").forEach(b => b.onclick = () =>
    browseModal(slot.querySelector(`[name=${b.dataset.browse}]`)));
  document.getElementById("src-cancel").onclick = () => (slot.innerHTML = "");
  document.getElementById("src-form").onsubmit = async ev => {
    ev.preventDefault();
    const f = new FormData(ev.target);
    const body = {
      path: f.get("path").trim(),
      profile_id: Number(f.get("profile_id")),
      output_path: f.get("output_path").trim() || null,
      delete_original: f.get("delete_original") === "on",
      watch: f.get("watch") === "on",
      enabled: f.get("enabled") === "on",
    };
    try {
      await api(src ? `/api/sources/${src.id}` : "/api/sources", {
        method: src ? "PUT" : "POST", body: JSON.stringify(body),
      });
      toast(src ? "Source updated" : "Source added");
      route();
    } catch (e) { toast(e.message, true); }
  };
}

async function browseModal(input) {
  const backdrop = document.createElement("div");
  backdrop.className = "modal-backdrop";
  document.body.appendChild(backdrop);
  const close = () => backdrop.remove();
  backdrop.onclick = ev => { if (ev.target === backdrop) close(); };

  async function show(path) {
    let d;
    try { d = await api(`/api/browse?path=${encodeURIComponent(path)}`); }
    catch (e) { toast(e.message, true); return; }
    backdrop.innerHTML = `<div class="modal">
      <header>${esc(d.path)}</header>
      <div class="dirlist">
        ${d.parent ? `<a href="#" data-dir="${esc(d.parent)}">&#8617; ..</a>` : ""}
        ${d.dirs.map(n => `<a href="#" data-dir="${esc(d.path === "/" ? "/" + n : d.path + "/" + n)}">&#128193; ${esc(n)}</a>`).join("") ||
          `<div class="empty">No subfolders</div>`}
      </div>
      <footer><button id="browse-close">Cancel</button>
        <button class="primary" id="browse-pick">Select this folder</button></footer>
    </div>`;
    backdrop.querySelectorAll("[data-dir]").forEach(a => a.onclick = ev => {
      ev.preventDefault(); show(a.dataset.dir);
    });
    backdrop.querySelector("#browse-close").onclick = close;
    backdrop.querySelector("#browse-pick").onclick = () => { input.value = d.path; close(); };
  }
  await show(input.value.trim() || "/");
}

/* -------------------------------------------------------------- profiles */

async function profiles() {
  const profs = await api("/api/profiles");
  state.profiles = profs;
  $main.innerHTML = `<h1>Profiles</h1>
    <div id="prof-form-slot"></div>
    <button class="primary" id="add-prof">New profile</button>
    <h2>Transcoding profiles</h2>
    ${profs.map(profileCard).join("")}
    <p class="inline-note">ICQ (Intelligent Constant Quality) is QSV's quality mode - lower
      values mean higher quality and bigger files. In VMAF mode, transQrate encodes short
      samples of each file at several ICQ values and binary-searches for the highest ICQ
      that still reaches your VMAF target (inspired by ab-av1). The shown command assumes
      an example file with a 5.1 and a stereo audio stream plus a text subtitle - audio
      bitrates are computed per stream (channels &times; kbps) from the actual file at
      job time.</p>`;
  document.getElementById("add-prof").onclick = () => profileForm(null);
  document.querySelectorAll("[data-edit-prof]").forEach(b => b.onclick = () =>
    profileForm(profs.find(p => p.id == b.dataset.editProf)));
  document.querySelectorAll("[data-del-prof]").forEach(b => b.onclick = async () => {
    if (!confirm("Delete this profile?")) return;
    try { await api(`/api/profiles/${b.dataset.delProf}`, { method: "DELETE" }); route(); }
    catch (e) { toast(e.message, true); }
  });
}

function profileCard(p) {
  const quality = p.quality_mode === "vmaf"
    ? `VMAF target ${p.vmaf_target} (auto ICQ)` : `ICQ ${p.icq}`;
  const audio = p.audio_codec === "copy"
    ? "audio copy" : `${p.audio_codec} @ ${p.audio_kbps_per_channel}k/channel`;
  return `<div class="card">
    <div class="row-top">
      <div class="title">${esc(p.name)}</div>
      <div class="actions">
        <button class="small" data-edit-prof="${p.id}">Edit</button>
        <button class="small danger" data-del-prof="${p.id}">Delete</button>
      </div>
    </div>
    <div class="meta">
      <span>${esc(p.video_codec)} (${esc(p.preset)})</span>
      <span>${esc(quality)}</span>
      <span>${p.max_resolution && p.max_resolution !== "source" ?
        "&le; " + esc(p.max_resolution) : "source resolution"}</span>
      ${p.bit_depth === "8" ? "<span>8-bit</span>" : ""}
      <span>${esc(audio)}${p.audio_max_channels ? `, max ${
        { 8: "7.1", 7: "6.1", 6: "5.1", 2: "stereo", 1: "mono" }[p.audio_max_channels] ||
        p.audio_max_channels + "ch"}` : ""}</span>
      <span>${esc(p.container)}</span>
    </div>
    <details class="cmd-details">
      <summary>ffmpeg command</summary>
      <pre class="cmdline">${esc(p.command || "")}</pre>
    </details>
  </div>`;
}

function profileForm(prof) {
  const slot = document.getElementById("prof-form-slot");
  const p = prof || {
    name: "", video_codec: "av1_qsv", preset: "veryslow", quality_mode: "icq",
    icq: 22, vmaf_target: 95, audio_codec: "libopus", audio_kbps_per_channel: 64,
    audio_max_channels: 0, max_resolution: "source", bit_depth: "source",
    container: "mkv", extra_video_args: "",
  };
  const resolutions = [["source", "Keep source"], ["2160p", "4K (2160p)"],
    ["1080p", "1080p"], ["720p", "720p"], ["480p", "480p"]];
  const channelCaps = [[0, "Keep all channels"], [8, "max 7.1"], [6, "max 5.1"],
    [2, "stereo"], [1, "mono"]];
  const presets = ["veryslow", "slower", "slow", "medium", "fast", "faster", "veryfast"];
  slot.innerHTML = `<form class="panel" id="prof-form">
    <div class="grid">
      <label class="field"><span>Name</span>
        <input type="text" name="name" required value="${esc(p.name)}"></label>
      <label class="field"><span>Video codec</span>
        <input type="text" name="video_codec" value="${esc(p.video_codec)}"></label>
      <label class="field"><span>Preset</span>
        <select name="preset">${presets.map(x =>
          `<option ${x === p.preset ? "selected" : ""}>${x}</option>`).join("")}</select></label>
      <label class="field"><span>Quality mode</span>
        <select name="quality_mode">
          <option value="icq" ${p.quality_mode === "icq" ? "selected" : ""}>Fixed ICQ</option>
          <option value="vmaf" ${p.quality_mode === "vmaf" ? "selected" : ""}>VMAF target (auto ICQ)</option>
        </select></label>
      <label class="field"><span>ICQ (fixed mode)</span>
        <input type="number" name="icq" min="1" max="51" value="${p.icq}"></label>
      <label class="field"><span>VMAF target (vmaf mode)</span>
        <input type="number" name="vmaf_target" min="1" max="100" step="0.5" value="${p.vmaf_target}"></label>
      <label class="field"><span>Output resolution (downscale only)</span>
        <select name="max_resolution">${resolutions.map(([v, t]) =>
          `<option value="${v}" ${v === p.max_resolution ? "selected" : ""}>${t}</option>`).join("")}
        </select></label>
      <label class="field" title="Keep source is recommended: AV1 compresses 10-bit material more efficiently (smoother gradients, less banding in HDR) - forcing 8-bit typically produces an even LARGER file at the same quality. Use force 8-bit only when a playback device cannot handle 10-bit.">
        <span>Bit depth <span class="info-hint">&#9432;</span></span>
        <select name="bit_depth">
          <option value="source" ${p.bit_depth !== "8" ? "selected" : ""}>Keep source (HDR-safe, recommended)</option>
          <option value="8" ${p.bit_depth === "8" ? "selected" : ""}>Force 8-bit</option>
        </select></label>
      <label class="field"><span>Audio</span>
        <select name="audio_codec">
          <option value="libopus" ${p.audio_codec === "libopus" ? "selected" : ""}>Opus (re-encode)</option>
          <option value="copy" ${p.audio_codec === "copy" ? "selected" : ""}>Copy original</option>
        </select></label>
      <label class="field"><span>Audio kbps per channel</span>
        <input type="number" name="audio_kbps_per_channel" min="16" max="512" value="${p.audio_kbps_per_channel}"></label>
      <label class="field"><span>Audio channel limit (downmix only)</span>
        <select name="audio_max_channels">${channelCaps.map(([v, t]) =>
          `<option value="${v}" ${v === (p.audio_max_channels || 0) ? "selected" : ""}>${t}</option>`).join("")}
        </select></label>
      <label class="field"><span>Container</span>
        <select name="container">
          <option ${p.container === "mkv" ? "selected" : ""}>mkv</option>
          <option ${p.container === "mp4" ? "selected" : ""}>mp4</option>
        </select></label>
      <label class="field"><span>Extra ffmpeg video args</span>
        <input type="text" name="extra_video_args" value="${esc(p.extra_video_args)}"
          placeholder="-look_ahead_depth 100 -extbrc 1"></label>
    </div>
    <div class="field" style="margin-top:14px">
      <span style="font-size:12px;color:var(--ink-2);font-weight:500">Resulting ffmpeg command</span>
      <pre class="cmdline" id="cmd-preview">&hellip;</pre>
    </div>
    <div class="form-foot">
      <button type="submit" class="primary">${prof ? "Save changes" : "Create profile"}</button>
      <button type="button" id="prof-cancel">Close</button>
      <span class="hint">Opus keeps the source channel count; bitrate = channels &times; kbps.</span>
    </div>
  </form>`;
  const form = document.getElementById("prof-form");
  const readForm = () => {
    const f = new FormData(form);
    return {
      name: (f.get("name") || "profile").toString().trim() || "profile",
      video_codec: f.get("video_codec").trim() || "av1_qsv",
      preset: f.get("preset"),
      quality_mode: f.get("quality_mode"),
      icq: Number(f.get("icq")) || p.icq || 22,
      vmaf_target: Number(f.get("vmaf_target")) || p.vmaf_target || 95,
      audio_codec: f.get("audio_codec"),
      audio_kbps_per_channel: Number(f.get("audio_kbps_per_channel")) || 64,
      audio_max_channels: Number(f.get("audio_max_channels")) || 0,
      max_resolution: f.get("max_resolution"),
      bit_depth: f.get("bit_depth"),
      container: f.get("container"),
      extra_video_args: f.get("extra_video_args").trim(),
    };
  };
  const syncQualityFields = () => {
    const vmafMode = form.elements.quality_mode.value === "vmaf";
    form.elements.icq.disabled = vmafMode;
    form.elements.vmaf_target.disabled = !vmafMode;
  };
  form.elements.quality_mode.addEventListener("change", syncQualityFields);
  syncQualityFields();
  let previewTimer = null;
  const updatePreview = () => {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(async () => {
      try {
        const r = await api("/api/profiles/preview", {
          method: "POST", body: JSON.stringify(readForm()),
        });
        const el = document.getElementById("cmd-preview");
        if (el) el.textContent = r.command;
      } catch (e) { /* invalid intermediate state - ignore */ }
    }, 300);
  };
  form.addEventListener("input", updatePreview);
  form.addEventListener("change", updatePreview);
  updatePreview();
  document.getElementById("prof-cancel").onclick = () => (slot.innerHTML = "");
  form.onsubmit = async ev => {
    ev.preventDefault();
    const body = { ...readForm(), name: new FormData(form).get("name").trim() };
    try {
      await api(prof ? `/api/profiles/${prof.id}` : "/api/profiles", {
        method: prof ? "PUT" : "POST", body: JSON.stringify(body),
      });
      toast(prof ? "Profile updated" : "Profile created");
      route();
    } catch (e) { toast(e.message, true); }
  };
}

/* ------------------------------------------------------------------ logs */

async function logs() {
  const d = await api("/api/logs");
  $main.innerHTML = `<h1>Logs</h1>
    <div class="logs-layout">
      <div class="loglist" id="loglist">
        <a href="#" data-logsel="app">Application log</a>
        ${d.jobs.map(j => `<a href="#" data-logsel="${j.id}">
          #${j.id} &middot; ${esc(basename(j.input_path))}<br>
          <small>${esc(j.status)}${j.finished_at ? " · " + esc(j.finished_at) : ""}</small></a>`).join("")}
      </div>
      <div>
        <div class="form-foot" style="margin:0 0 10px">
          <label class="check"><input type="checkbox" id="log-follow" checked> Follow</label>
          <label class="check">Lines
            <select id="log-lines" style="width:auto">
              <option>500</option>
              <option selected>2000</option>
              <option>10000</option>
              <option value="0">All</option>
            </select>
          </label>
          <span class="hint" id="log-name"></span>
        </div>
        <div class="logview" id="logview">Loading&hellip;</div>
        <div class="inline-note" style="margin-top:10px">Currently running command</div>
        <pre class="cmdline" id="log-cmd" style="margin-top:4px">&ndash;</pre>
      </div>
    </div>`;
  document.querySelectorAll("[data-logsel]").forEach(a => a.onclick = ev => {
    ev.preventDefault();
    state.logJob = a.dataset.logsel;
    highlight();
    refreshLog(true);
  });
  const highlight = () => document.querySelectorAll("[data-logsel]").forEach(a =>
    a.classList.toggle("active", a.dataset.logsel === String(state.logJob)));
  if (!d.jobs.some(j => String(j.id) === String(state.logJob)) && state.logJob !== "app") {
    state.logJob = d.jobs.length ? String(d.jobs[0].id) : "app";
  }
  highlight();
  document.getElementById("log-lines").onchange = () => refreshLog(true);
  await refreshLog(true);
  pollTimer = setInterval(() => {
    if (document.getElementById("log-follow")?.checked) refreshLog(false);
  }, 3000);
}

async function refreshLog(jump) {
  const view = document.getElementById("logview");
  if (!view) return;
  const which = state.logJob;
  const lines = document.getElementById("log-lines")?.value ?? "2000";
  const url = which === "app" ? `/api/logs/app?tail=${lines}` : `/api/jobs/${which}/log?tail=${lines}`;
  document.getElementById("log-name").textContent =
    which === "app" ? "transqrate.log" : `job_${which}.log`;
  try {
    const text = await api(url);
    const stick = jump || view.scrollTop + view.clientHeight >= view.scrollHeight - 40;
    view.textContent = text || "(empty)";
    if (stick) view.scrollTop = view.scrollHeight;
  } catch (e) {
    view.textContent = "Could not load log: " + e.message;
  }
  const cmdEl = document.getElementById("log-cmd");
  if (cmdEl) {
    if (which === "app") {
      cmdEl.textContent = "\u2013 (application log)";
    } else {
      try {
        const j = await api(`/api/jobs/${which}`);
        cmdEl.textContent = j.current_cmd || "\u2013 (no command running)";
      } catch (e) { cmdEl.textContent = "\u2013"; }
    }
  }
}

/* -------------------------------------------------------------- settings */

async function settings() {
  const d = await api("/api/settings");
  const grouped = new Set(Object.values(d.groups || {}).flat());
  const rest = Object.keys(d.values).filter(k => !grouped.has(k));
  const groups = { ...(d.groups || { Settings: Object.keys(d.values) }) };
  if (rest.length) groups.Other = rest;
  const field = k => `<label class="field"><span>${esc(d.meta[k] || k)}</span>
    <input type="text" name="${esc(k)}" value="${esc(d.values[k])}">
    <div class="inline-note">${esc(k)}</div></label>`;
  $main.innerHTML = `<h1>Settings</h1>
    <form id="settings-form">
      ${Object.entries(groups).filter(([, keys]) => keys.length).map(([name, keys]) => `
        <div class="panel">
          <h3 class="panel-title">${esc(name)}</h3>
          <div class="grid">${keys.map(field).join("")}</div>
        </div>`).join("")}
      <div class="form-foot">
        <button type="submit" class="primary">Save settings</button>
        <span class="hint">Worker count changes take effect after a container restart.</span>
      </div>
    </form>`;
  document.getElementById("settings-form").onsubmit = async ev => {
    ev.preventDefault();
    const body = Object.fromEntries(new FormData(ev.target).entries());
    try { await api("/api/settings", { method: "PUT", body: JSON.stringify(body) }); toast("Settings saved"); }
    catch (e) { toast(e.message, true); }
  };
}

/* boot */
route();
