"use strict";

const $ = (id) => document.getElementById(id);
const icon = (name) => `<svg><use href="#i-${name}"/></svg>`;
const cssEscape = (s) => (window.CSS && CSS.escape ? CSS.escape(s) : String(s).replace(/["\\]/g, "\\$&"));

// ---------------------------------------------------------------- talking to the app

// The version of the app this page was served by. When the app is updated underneath an
// open tab, the routes may have changed shape, so the page reloads itself once instead of
// running the old script against the new app.
const PAGE_VERSION = (document.querySelector('meta[name="app-version"]') || {}).content || "";

function checkAppVersion(res) {
  const running = res.headers.get("X-App-Version");
  if (!running || !PAGE_VERSION || running === PAGE_VERSION) return;
  try {
    if (sessionStorage.getItem("nh-reloaded-for") === running) return;
    sessionStorage.setItem("nh-reloaded-for", running);
  } catch (_) { /* no storage: reload anyway, the new page will match */ }
  location.reload();
}

async function api(method, path, body) {
  const opts = { method, headers: { "X-Requested-With": "fetch" } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  checkAppVersion(res);
  let data = {};
  try { data = await res.json(); } catch (_) { /* no body */ }
  if (res.status === 401 && !path.endsWith("/login")) {
    showLogin();
    throw new Error("Not logged in");
  }
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

let toastTimer = null;
function toast(text) {
  const el = $("toast");
  el.textContent = text;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.classList.remove("show"); }, 4000);
}

function confirmDialog(title, text, okLabel) {
  const dlg = $("confirm-dialog");
  $("confirm-title").textContent = title;
  $("confirm-text").textContent = text;
  $("confirm-ok").textContent = okLabel || "OK";
  return new Promise((resolve) => {
    let settled = false;
    const done = (value) => {
      if (settled) return;
      settled = true;
      $("confirm-ok").onclick = null;
      $("confirm-cancel").onclick = null;
      dlg.onclose = null;
      if (dlg.open) dlg.close();
      resolve(value);
    };
    $("confirm-ok").onclick = () => done(true);
    $("confirm-cancel").onclick = () => done(false);
    dlg.onclose = () => done(false);
    dlg.showModal();
  });
}

// ---------------------------------------------------------------- state

const HISTORY_LEN = 120;   // samples kept for the metrics charts (1 per second)
const MAX_CONSOLE_LINES = 3000;
const ID_RE = /^[A-Za-z0-9_-]+$/;   // what the app's routes accept as a server id
const DEFAULT_SEARCH = () => ({ q: "", offset: 0, total: 0, limit: 20 });

const state = {
  user: null,
  servers: [],
  page: "dashboard",   // dashboard | server
  current: null,       // server dict while on the server page
  visit: 0,            // bumped every time the server page is (re)entered; async work checks it
  events: null,        // EventSource for the current server
  statsTimer: null,    // 1 s poll for the current server
  pulseTimer: null,    // slower poll of every server, feeds the dashboard and sidebar
  tab: "console",
  search: DEFAULT_SEARCH(),
  history: null,       // {cpu, mem, tps, players} arrays for the charts
  consoleTail: "",     // output received after the last newline
  commands: [],        // commands sent, for Up/Down in the command box
  commandIndex: 0,
  hasPsutil: true,     // false once /api/stats reports no host figures
  filter: "",
  settings: null,      // the server's settings once the tab has loaded them
  iconFile: null,      // the picture chosen but not saved yet
  updating: false,     // true while a version update is running
};

const STATUS_LABEL = { running: "Online", stopped: "Offline", starting: "Starting", stopping: "Stopping" };
const STATUS_ICON = { running: "signal", stopped: "signal-off", starting: "sync", stopping: "sync" };

// ---------------------------------------------------------------- formatting

function fmtUptime(s) {
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60), sec = Math.floor(s % 60);
  return (d ? `${d}d ` : "") + `${h}h ${m}m ${sec}s`;
}

function fmtMB(mb) {
  return mb >= 1024 ? `${(mb / 1024).toFixed(2)} GB` : `${Math.round(mb)} MB`;
}

function fmtSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function fmtTime(date) {
  return date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function loaderLabel(s) {
  if (s.edition === "bedrock") return "Bedrock";
  const name = s.loader_type ? s.loader_type.charAt(0).toUpperCase() + s.loader_type.slice(1) : "";
  return `${name} ${s.loader_version || ""}`.trim();
}

function monogram(name) {
  const words = String(name).trim().split(/\s+/).filter(Boolean);
  const letters = words.length > 1 ? words[0][0] + words[1][0] : String(name).slice(0, 2);
  return letters.toUpperCase();
}

function levelClass(pct) {
  return pct > 85 ? "danger" : pct > 60 ? "warn" : "ok";
}

const isBusy = (status) => status === "starting" || status === "stopping";
const isLive = (status) => status === "running" || status === "starting";

// ---------------------------------------------------------------- login

function showLogin() {
  leaveServer();
  stopPulse();
  $("app").hidden = true;
  $("login").hidden = false;
  $("login-form").username.focus();
}

$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  $("login-error").textContent = "";
  try {
    state.user = await api("POST", "/api/login", {
      username: form.username.value.trim(),
      password: form.password.value,
    });
    form.password.value = "";
    await showApp();
  } catch (err) {
    $("login-error").textContent = err.message;
  }
});

$("logout").addEventListener("click", async () => {
  try { await api("POST", "/api/logout"); } catch (_) { /* already gone */ }
  showLogin();
});

// ---------------------------------------------------------------- app shell

async function showApp() {
  $("login").hidden = true;
  $("app").hidden = false;
  $("user-name").textContent = state.user.username;
  $("user-role").hidden = !state.user.admin;
  try { await refreshServers(); } catch (err) { toast(err.message); }
  route();
  startPulse();   // keeps retrying the list if the first load failed
}

async function refreshServers() {
  const servers = await api("GET", "/api/servers");
  const focused = document.activeElement && document.activeElement.closest ? document.activeElement.closest("[data-id]") : null;
  state.servers = servers.filter((s) => ID_RE.test(s.id));
  renderSidebar();
  renderTable();
  renderSummary();
  if (focused) {
    const again = document.querySelector(`#server-list a[data-id="${cssEscape(focused.dataset.id)}"], #servers-rows tr[data-id="${cssEscape(focused.dataset.id)}"] a`);
    if (again) again.focus();
  }
  if (state.current) {
    const fresh = state.servers.find((s) => s.id === state.current.id);
    if (fresh) { state.current = fresh; renderHeader(); }
    else { location.hash = "#/"; }
  }
}

// Hash routing: #/ is the dashboard, #/server/<id>[/<tab>] one server.
function route() {
  const hash = location.hash || "#/";
  const m = hash.match(/^#\/server\/([A-Za-z0-9_-]+)(?:\/(console|performance|mods|settings))?$/);
  closeMobileSidebar();
  if (m) showServer(m[1], m[2] || "console");
  else showDashboard();
}
window.addEventListener("hashchange", route);

function setPage(name) {
  const changed = state.page !== name;
  state.page = name;
  $("page-dashboard").hidden = name !== "dashboard";
  $("page-server").hidden = name !== "server";
  for (const a of document.querySelectorAll("#sidebar .nav-link")) {
    const active = name === "dashboard" ? a.dataset.nav === "dashboard" : a.dataset.id === (state.current && state.current.id);
    a.classList.toggle("active", active);
    if (active) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
  $("topbar-server").hidden = name !== "server";
  if (changed) restartPulse();
}

// --- sidebar collapse / off-canvas, after Crafty's sidebar
const mobile = window.matchMedia("(max-width: 991px)");
try {
  document.body.classList.toggle("sidebar-collapsed", localStorage.getItem("nh-sidebar") === "collapsed");
} catch (_) { /* storage blocked */ }

function syncSidebarToggle() {
  const btn = $("sidebar-toggle");
  let label;
  if (mobile.matches) {
    const open = document.body.classList.contains("sidebar-open");
    label = open ? "Close navigation" : "Open navigation";
    btn.setAttribute("aria-expanded", String(open));
  } else {
    const collapsed = document.body.classList.contains("sidebar-collapsed");
    label = collapsed ? "Expand navigation" : "Collapse navigation";
    btn.setAttribute("aria-expanded", String(!collapsed));
  }
  btn.title = label;
  btn.setAttribute("aria-label", label);
}

$("sidebar-toggle").addEventListener("click", () => {
  if (mobile.matches) {
    const open = document.body.classList.toggle("sidebar-open");
    $("backdrop").hidden = !open;
  } else {
    const collapsed = document.body.classList.toggle("sidebar-collapsed");
    try { localStorage.setItem("nh-sidebar", collapsed ? "collapsed" : "expanded"); } catch (_) { /* ignore */ }
  }
  syncSidebarToggle();
});
$("backdrop").addEventListener("click", closeMobileSidebar);
$("sidebar").addEventListener("click", (e) => { if (e.target.closest("a.nav-link")) closeMobileSidebar(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && document.body.classList.contains("sidebar-open")) closeMobileSidebar(); });
mobile.addEventListener("change", () => { closeMobileSidebar(); syncSidebarToggle(); });
$("main").addEventListener("transitionend", (e) => { if (e.propertyName === "padding-left") drawCharts(); });
syncSidebarToggle();

function closeMobileSidebar() {
  document.body.classList.remove("sidebar-open");
  $("backdrop").hidden = true;
  syncSidebarToggle();
}

// ---------------------------------------------------------------- server list + dashboard

function renderSidebar() {
  const list = $("server-list");
  list.innerHTML = "";
  $("no-servers").hidden = state.servers.length > 0;
  for (const s of state.servers) {
    const li = document.createElement("li");
    li.innerHTML = `<a class="nav-link"><span class="icon-dot">${icon("server")}<span class="monogram"></span><span class="dot"></span></span><span class="name"></span><span class="players"></span></a>`;
    const a = li.querySelector("a");
    a.href = `#/server/${s.id}`;
    a.dataset.id = s.id;
    a.querySelector(".monogram").textContent = monogram(s.name);
    a.querySelector(".name").textContent = s.name;
    const active = state.page === "server" && state.current && state.current.id === s.id;
    a.classList.toggle("active", active);
    if (active) a.setAttribute("aria-current", "page");
    list.appendChild(li);
    paintSidebar(s);
  }
}

function paintSidebar(s) {
  const a = $("server-list").querySelector(`a[data-id="${cssEscape(s.id)}"]`);
  if (!a) return;
  a.querySelector(".dot").className = `dot ${s.status}`;
  a.querySelector(".players").textContent = s.status === "running" ? `${s.players}/${s.max_players}` : "";
  a.title = `${s.name} — ${STATUS_LABEL[s.status] || s.status}`;
}

function renderTable() {
  const rows = $("servers-rows");
  rows.innerHTML = "";
  $("dash-empty").hidden = state.servers.length > 0;
  $("servers-table").hidden = state.servers.length === 0;
  for (const s of state.servers) {
    const tr = document.createElement("tr");
    tr.dataset.id = s.id;
    tr.innerHTML = `
      <td class="col-server"><a>${icon("server")}<span class="name"></span></a><span class="sub"></span></td>
      <td class="col-actions"></td>
      <td class="col-cpu"><div class="meter"><div class="bar"><span></span></div><span class="num">—</span></div></td>
      <td class="col-mem"><div class="meter"><div class="bar"><span></span></div><span class="num">—</span></div></td>
      <td class="col-players"></td>
      <td class="col-port"></td>
      <td class="col-version"></td>
      <td class="col-status"><span class="status"></span></td>`;
    tr.querySelector("a").href = `#/server/${s.id}`;
    tr.querySelector(".name").textContent = s.name;
    tr.querySelector(".sub").textContent = s.is_remote ? "on another machine" : "";
    tr.querySelector(".col-port").textContent = s.port || "—";
    tr.querySelector(".col-version").textContent = `${s.mc_version} · ${loaderLabel(s)}`;
    rows.appendChild(tr);
    paintRow(s);
  }
  applyFilter();
}

$("servers-filter").addEventListener("input", (e) => { state.filter = e.target.value.trim().toLowerCase(); applyFilter(); });

function applyFilter() {
  for (const tr of $("servers-rows").children) {
    const s = state.servers.find((x) => x.id === tr.dataset.id);
    tr.hidden = !!state.filter && !(s && s.name.toLowerCase().includes(state.filter));
  }
}

// Fills *el* as <… class="{cls} {status}"><icon/><span>Label</span></…>, the label set as text.
function paintStatus(el, cls, status) {
  const known = STATUS_LABEL[status] ? status : "stopped";
  el.className = `${cls} ${known}`;
  el.innerHTML = `${icon(STATUS_ICON[known])}<span></span>`;
  el.lastElementChild.textContent = STATUS_LABEL[known];
}

function rowActions(s) {
  if (isBusy(s.status)) return `<span class="busy">${icon("sync")}${STATUS_LABEL[s.status]}…</span>`;
  if (s.status === "running") {
    return `<button class="icon-btn danger" data-action="stop" title="Stop" aria-label="Stop">${icon("stop")}</button>` +
      `<button class="icon-btn warn" data-action="restart" title="Restart" aria-label="Restart">${icon("restart")}</button>`;
  }
  return `<button class="icon-btn ok" data-action="start" title="Start" aria-label="Start">${icon("play")}</button>`;
}

function paintRow(s) {
  const tr = $("servers-rows").querySelector(`tr[data-id="${cssEscape(s.id)}"]`);
  if (!tr) return;
  const actions = tr.querySelector(".col-actions");
  if (actions.dataset.status !== s.status) {
    const hadFocus = actions.contains(document.activeElement);
    actions.dataset.status = s.status;
    actions.innerHTML = rowActions(s);
    for (const b of actions.querySelectorAll("button")) {
      b.addEventListener("click", () => power(s.id, b.dataset.action));
    }
    if (hadFocus) (actions.querySelector("button") || tr.querySelector("a")).focus();
  }
  tr.querySelector(".col-players").textContent = s.status === "running" ? `${s.players} / ${s.max_players}` : "—";
  paintStatus(tr.querySelector(".col-status .status"), "status", s.status);
  if (!isLive(s.status)) {
    paintMeter(tr.querySelector(".col-cpu .meter"), null, "—");
    paintMeter(tr.querySelector(".col-mem .meter"), null, "—");
  }
}

function paintMeter(meter, fraction, text) {
  const bar = meter.querySelector(".bar");
  const pct = fraction === null ? 0 : Math.max(0, Math.min(100, fraction * 100));
  bar.querySelector("span").style.width = `${pct}%`;
  bar.className = `bar ${levelClass(pct)}`;
  meter.querySelector(".num").textContent = text;
}

function paintRowStats(id, st) {
  const tr = $("servers-rows").querySelector(`tr[data-id="${cssEscape(id)}"]`);
  if (!tr || !isLive(st.status)) return;
  const cpu = st.cpu_percent;
  paintMeter(tr.querySelector(".col-cpu .meter"), cpu === null ? null : cpu / 100, cpu === null ? "—" : `${cpu.toFixed(1)}%`);
  const mem = st.rss_mb;
  paintMeter(tr.querySelector(".col-mem .meter"), mem === null ? null : mem / st.ram_mb,
    mem === null ? "—" : `${Math.round(mem / st.ram_mb * 100)}% · ${fmtMB(mem)}`);
}

function renderSummary() {
  const online = state.servers.filter((s) => s.status === "running").length;
  $("sum-total").textContent = state.servers.length;
  $("sum-online").textContent = online;
  $("sum-offline").textContent = state.servers.length - online;
  let players = 0, max = 0;
  for (const s of state.servers) {
    if (s.status === "running") { players += s.players; max += s.max_players; }
  }
  $("sum-players").textContent = players;
  $("sum-max").textContent = max;
}

function applyServerPatch(id, patch) {
  const s = state.servers.find((x) => x.id === id);
  if (!s) return;
  const statusChanged = "status" in patch && patch.status !== s.status;
  Object.assign(s, patch);
  paintSidebar(s);
  paintRow(s);
  renderSummary();
  if (state.current && state.current.id === id) {
    state.current = s;
    renderHeader();
    if (statusChanged) paintModsState();
  }
}

// The pulse: one request for every server's numbers, feeding the dashboard table,
// the host card and the sidebar dots. Faster on the dashboard, slower elsewhere.
function startPulse() {
  stopPulse();
  pulse();
  state.pulseTimer = setInterval(pulse, state.page === "dashboard" ? 2000 : 6000);
}
function stopPulse() { clearInterval(state.pulseTimer); state.pulseTimer = null; }
function restartPulse() { if (state.pulseTimer) startPulse(); }

function setLive(ok) {
  const el = $("dash-sync");
  el.classList.toggle("off", !ok);
  el.title = ok ? "Live" : "Not updating — lost the connection to the app";
  el.querySelector(".sync-text").textContent = ok ? "Live" : "Not updating";
}

async function pulse() {
  let data;
  try { data = await api("GET", "/api/stats"); } catch (_) { setLive(false); return; }
  setLive(true);
  const ids = Object.keys(data.servers);
  const known = state.servers.map((s) => s.id);
  if (ids.length !== known.length || ids.some((id) => !known.includes(id))) {
    refreshServers().catch(() => {});
  }
  for (const id of ids) {
    const st = data.servers[id];
    applyServerPatch(id, { status: st.status, players: st.players, max_players: st.max_players });
    paintRowStats(id, st);
  }
  const host = data.host;
  state.hasPsutil = host !== null;
  $("host-cpu").textContent = host ? `${host.cpu_percent.toFixed(0)}%` : "—";
  $("host-mem").textContent = host ? `${host.mem_percent.toFixed(0)} % · ${fmtMB(host.mem_used_mb)} of ${fmtMB(host.mem_total_mb)}` : "—";
  $("host-cpu-line").className = host ? levelClass(host.cpu_percent) : "muted";
  $("host-mem-line").className = host ? levelClass(host.mem_percent) : "muted";
  if (state.current) paintStatsNote();
}

function showDashboard() {
  leaveServer();
  setPage("dashboard");
}

async function power(id, action) {
  try {
    await api("POST", `/api/servers/${id}/${action}`);
    toast({ start: "Starting…", stop: "Stopping…", restart: "Restarting…" }[action]);
    // The request went through, so the process is on its way; the stream or the
    // next pulse brings the real status shortly.
    applyServerPatch(id, { status: action === "stop" ? "stopping" : "starting" });
  } catch (err) { toast(err.message); }
}

// ---------------------------------------------------------------- one server

function leaveServer() {
  state.visit += 1;
  if (state.events) { state.events.close(); state.events = null; }
  clearInterval(state.statsTimer);
  state.statsTimer = null;
  state.current = null;
  state.history = null;
  resetConsole();
  state.search = DEFAULT_SEARCH();
  $("search").value = "";
  $("search-results").innerHTML = "";
  $("search-pager").hidden = true;
  $("mod-list").innerHTML = "";
  resetSettings();
  if ($("versions-dialog").open) $("versions-dialog").close();
}

function showServer(id, tab) {
  const changed = !state.current || state.current.id !== id;
  if (changed) {
    leaveServer();
    state.current = state.servers.find((s) => s.id === id);
    if (!state.current) { location.hash = "#/"; return; }
    state.history = { cpu: [], mem: [], tps: [], players: [] };
  }
  setPage("server");
  renderHeader();
  // Mods are a Java Edition thing; every server has settings.
  $("tab-btn-mods").hidden = !state.current.has_mods;
  if (!state.current.has_mods && tab === "mods") tab = "console";
  switchTab(tab);
  if (!changed) return;

  pollStats();
  state.statsTimer = setInterval(pollStats, 1000);
  openEvents(id, state.visit);
  if (!mobile.matches && tab === "console" && !$("command").disabled) $("command").focus();
}

function renderHeader() {
  const s = state.current;
  $("server-name").textContent = s.name;
  $("topbar-server").textContent = s.name;
  $("server-meta").textContent = `${s.mc_version} · ${loaderLabel(s)} · ${fmtMB(s.ram_mb)}${s.is_remote ? " · on another machine" : ""}`;
  paintStatus($("status-badge"), "badge", s.status);
  const busy = isBusy(s.status);
  $("btn-start").disabled = busy || s.status === "running";
  $("btn-stop").disabled = busy || s.status !== "running";
  $("btn-restart").disabled = busy || s.status !== "running";
  const canCommand = s.status === "running";
  $("command").disabled = !canCommand;
  $("command-send").disabled = !canCommand;
  $("command").placeholder = canCommand ? "Type a command and press Enter"
    : busy ? `${STATUS_LABEL[s.status]}…` : "Start the server to send commands";

  const st = $("d-status");
  st.textContent = STATUS_LABEL[s.status] || s.status;
  st.className = s.status === "running" ? "ok" : "warn";
  $("d-location").textContent = s.is_remote ? "Another machine" : "This computer";
  $("d-version").textContent = s.mc_version || "—";
  $("d-loader").textContent = loaderLabel(s) || "—";
  $("d-port").textContent = s.port || "—";
  $("d-ram").textContent = fmtMB(s.ram_mb);
  $("d-players").textContent = s.status === "running" ? `${s.players} / ${s.max_players}` : "0 / 0";
  $("d-tps-row").hidden = s.edition === "bedrock";
  $("tile-tps").hidden = s.edition === "bedrock";
  $("chart-tps-box").hidden = s.edition === "bedrock";
  $("st-pid-sub").textContent = s.is_remote ? "on the other machine" : "on this computer";
  paintStatsNote();
  for (const a of document.querySelectorAll("#sidebar .nav-link")) {
    const active = a.dataset.id === s.id;
    a.classList.toggle("active", active);
    if (active) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
}

function paintStatsNote() {
  const s = state.current;
  if (!s) return;
  let text = "";
  if (s.is_remote) text = "CPU, memory and uptime are not available for a server running on another machine.";
  else if (!state.hasPsutil) text = "CPU, memory and uptime need the psutil package, which the app could not find.";
  $("stats-note-text").textContent = text;
  $("stats-note").hidden = !text;
}

// Opens the event stream first and only then asks for the console history, buffering
// whatever the stream delivers in between; the part of the buffer the snapshot
// already contains is dropped, so nothing is lost or shown twice. A reconnect after a
// network blip goes through the same path and re-syncs the console.
function openEvents(id, visit) {
  if (state.events) state.events.close();
  const es = new EventSource(`/api/servers/${id}/events`);
  state.events = es;
  let buffer = null;   // output chunks held back while the history loads
  es.onopen = async () => {
    if (state.visit !== visit) return;
    buffer = [];
    let history;
    try { history = await api("GET", `/api/servers/${id}/console?lines=300`); }
    catch (err) { if (state.visit === visit) { toast(err.message); flushBuffer(""); } return; }
    if (state.visit !== visit) return;
    flushBuffer(history.lines.join(""));
  };
  const flushBuffer = (snapshot) => {
    const held = buffer || [];
    buffer = null;
    let drop = 0;
    for (let k = held.length; k > 0; k--) {
      if (snapshot.endsWith(held.slice(0, k).join(""))) { drop = k; break; }
    }
    resetConsole();
    appendConsole(snapshot);
    for (const chunk of held.slice(drop)) appendConsole(chunk);
    scrollConsole();
  };
  es.onmessage = (msg) => {
    if (state.visit !== visit) return;
    const ev = JSON.parse(msg.data);
    if (ev.event === "output") {
      if (buffer) buffer.push(ev.text); else appendConsole(ev.text);
    } else if (ev.event === "status") applyServerPatch(id, { status: ev.status });
    else if (ev.event === "players") applyServerPatch(id, { players: ev.players, max_players: ev.max });
    else if (ev.event === "server-changed") refreshServers().catch(() => {});
  };
}

for (const [btn, action] of [["btn-start", "start"], ["btn-stop", "stop"], ["btn-restart", "restart"]]) {
  $(btn).addEventListener("click", () => { if (state.current) power(state.current.id, action); });
}

// ---------------------------------------------------------------- tabs

for (const btn of document.querySelectorAll("#tabs button")) {
  btn.addEventListener("click", () => {
    if (state.current) location.hash = `#/server/${state.current.id}/${btn.dataset.tab}`;
  });
}

function switchTab(name) {
  state.tab = name;
  for (const btn of document.querySelectorAll("#tabs button")) {
    const active = btn.dataset.tab === name;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", String(active));
  }
  for (const sec of document.querySelectorAll(".tab")) sec.hidden = sec.id !== `tab-${name}`;
  if (name === "performance") drawCharts();
  else if (name === "mods") loadMods();
  else if (name === "settings") loadSettings();
  else if (name === "console") scrollConsole();
}

// ---------------------------------------------------------------- console

const TIME_RE = /^(\[\d{1,2}:\d{2}:\d{2}(?:\.\d+)?\]|\d{2}:\d{2}:\d{2})/;

function lineClass(line) {
  if (line.startsWith("> ")) return "cmd";
  if (/\/FATAL\]|\bFATAL\b/.test(line)) return "fatal";
  if (/\/ERROR\]|\bERROR\b|Exception|\tat /.test(line)) return "error";
  if (/\/WARN\]|\bWARN(ING)?\b/.test(line)) return "warn";
  return "";
}

function lineElement(line) {
  const span = document.createElement("span");
  span.className = `l ${lineClass(line)}`.trim();
  const m = line.match(TIME_RE);
  if (m) {
    const t = document.createElement("span");
    t.className = "t";
    t.textContent = m[0];
    span.appendChild(t);
    span.appendChild(document.createTextNode(line.slice(m[0].length) + "\n"));
  } else {
    span.textContent = line + "\n";
  }
  return span;
}

function resetConsole() {
  state.consoleTail = "";
  $("console").innerHTML = "";
  $("to-bottom").hidden = true;
}

function appendConsole(text) {
  if (!text) return;
  const pre = $("console");
  const stick = atBottom(pre);
  const parts = (state.consoleTail + text).split("\n");
  state.consoleTail = parts.pop();
  const tail = pre.querySelector(".tail");
  if (tail) tail.remove();
  for (const line of parts) pre.appendChild(lineElement(line));
  if (state.consoleTail) {
    const t = document.createElement("span");
    t.className = "l tail";
    t.textContent = state.consoleTail;
    pre.appendChild(t);
  }
  while (pre.childElementCount > MAX_CONSOLE_LINES) pre.removeChild(pre.firstElementChild);
  if (stick) scrollConsole();
  else $("to-bottom").hidden = false;
}

function atBottom(pre) {
  return pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 8;
}

function scrollConsole() {
  const pre = $("console");
  pre.scrollTop = pre.scrollHeight;
  $("to-bottom").hidden = true;
}

$("console").addEventListener("scroll", () => { $("to-bottom").hidden = atBottom($("console")); });
$("to-bottom").addEventListener("click", scrollConsole);

$("command-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("command");
  const command = input.value.trim();
  if (!command || !state.current) return;
  try {
    const res = await api("POST", `/api/servers/${state.current.id}/console`, { command });
    if (!res.echoed) appendConsole(`> ${command}\n`);
    if (state.commands[state.commands.length - 1] !== command) state.commands.push(command);
    state.commandIndex = state.commands.length;
    input.value = "";
  } catch (err) { toast(err.message); }
});

// Up/Down walk through the commands sent earlier, like a shell.
$("command").addEventListener("keydown", (e) => {
  if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
  const n = state.commands.length;
  if (!n) return;
  e.preventDefault();
  state.commandIndex = e.key === "ArrowUp" ? Math.max(0, state.commandIndex - 1) : Math.min(n, state.commandIndex + 1);
  e.target.value = state.commandIndex < n ? state.commands[state.commandIndex] : "";
});

// ---------------------------------------------------------------- stats, details and charts

async function pollStats() {
  if (!state.current) return;
  const id = state.current.id, visit = state.visit;
  let st;
  try { st = await api("GET", `/api/servers/${id}/stats`); } catch (_) { return; }
  if (state.visit !== visit || !st.status) return;
  const live = isLive(st.status);
  const dash = (v, f) => (live && v !== null && v !== undefined ? f(v) : "—");

  // details card
  $("d-cpu").textContent = dash(st.cpu_percent, (v) => `${v.toFixed(1)}%`);
  $("d-mem").textContent = dash(st.rss_mb, (v) => `${fmtMB(v)} / ${fmtMB(st.ram_mb)}`);
  $("d-players").textContent = live ? `${st.players} / ${st.max_players}` : "0 / 0";
  $("d-tps").textContent = dash(st.tps, (v) => v.toFixed(1));
  $("d-uptime").textContent = dash(st.uptime_s, fmtUptime);
  $("d-started").textContent = dash(st.uptime_s, (v) => fmtTime(new Date(Date.now() - v * 1000)));

  // metrics tiles
  $("st-cpu").textContent = dash(st.cpu_percent, (v) => `${v.toFixed(1)} %`);
  $("st-mem").textContent = dash(st.rss_mb, fmtMB);
  $("st-mem-max").textContent = `of ${fmtMB(st.ram_mb)}`;
  $("st-tps").textContent = dash(st.tps, (v) => v.toFixed(1));
  $("st-players").textContent = live ? `${st.players} / ${st.max_players}` : "—";
  $("st-uptime").textContent = dash(st.uptime_s, fmtUptime);
  $("st-pid").textContent = dash(st.pid, String);

  if (state.history) {
    const push = (arr, v) => { arr.push(live ? v : null); if (arr.length > HISTORY_LEN) arr.shift(); };
    push(state.history.cpu, st.cpu_percent);
    push(state.history.mem, st.rss_mb);
    push(state.history.tps, st.tps);
    push(state.history.players, live ? st.players : null);
    if (state.tab === "performance") drawCharts();
  }
}

function drawCharts() {
  if (!state.history || state.page !== "server" || state.tab !== "performance") return;
  const s = state.current;
  const css = getComputedStyle(document.body);
  const token = (name, fallback) => css.getPropertyValue(name).trim() || fallback;
  drawChart($("chart-cpu"), state.history.cpu, { max: 100, color: token("--accent", "#5b8def"), fmt: (v) => `${v.toFixed(0)}%`, what: "CPU usage" });
  drawChart($("chart-mem"), state.history.mem, { max: s.ram_mb, color: token("--ok", "#2ec27e"), fmt: (v) => fmtMB(v), what: "Memory use" });
  drawChart($("chart-tps"), state.history.tps, { max: 20, color: token("--warn", "#e5a50a"), fmt: (v) => v.toFixed(1), what: "Ticks per second" });
  drawChart($("chart-players"), state.history.players, { max: Math.max(1, s.max_players || 1), color: token("--info", "#a086e8"), fmt: (v) => String(v), what: "Players online" });
}

function drawChart(canvas, values, opts) {
  if (!canvas || canvas.offsetParent === null) return;
  const present = values.filter((v) => v !== null && v !== undefined);
  if (present.length) {
    const last = present[present.length - 1], peak = Math.max.apply(null, present);
    canvas.setAttribute("aria-label", `${opts.what} over the last two minutes: now ${opts.fmt(last)}, peak ${opts.fmt(peak)}`);
  }
  const ratio = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (!w || !h) return;
  canvas.width = Math.round(w * ratio);
  canvas.height = Math.round(h * ratio);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const styles = getComputedStyle(document.body);
  const grid = styles.getPropertyValue("--border").trim() || "#30343e";
  const muted = styles.getPropertyValue("--muted").trim() || "#8b90a0";
  const pad = { l: 44, r: 8, t: 6, b: 6 };
  const pw = w - pad.l - pad.r, ph = h - pad.t - pad.b;
  let max = opts.max || 1;
  if (present.length) max = Math.max(max, Math.max.apply(null, present));
  ctx.font = `11px ${styles.getPropertyValue("--sans") || "sans-serif"}`;
  ctx.fillStyle = muted;
  ctx.strokeStyle = grid;
  ctx.lineWidth = 1;
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  for (let i = 0; i <= 2; i++) {
    const y = pad.t + (ph * i) / 2;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke();
    ctx.fillText(opts.fmt(max - (max * i) / 2), pad.l - 6, y);
  }
  if (!present.length) {
    ctx.textAlign = "center";
    ctx.fillText("No data yet", pad.l + pw / 2, pad.t + ph / 2);
    return;
  }
  const step = pw / (HISTORY_LEN - 1);
  const x0 = pad.l + pw - step * (values.length - 1);
  const y = (v) => pad.t + ph - (Math.min(v, max) / max) * ph;
  ctx.beginPath();
  let drawing = false;
  values.forEach((v, i) => {
    if (v === null || v === undefined) { drawing = false; return; }
    const x = x0 + i * step;
    if (!drawing) { ctx.moveTo(x, y(v)); drawing = true; } else ctx.lineTo(x, y(v));
  });
  ctx.strokeStyle = opts.color;
  ctx.lineWidth = 2;
  ctx.lineJoin = "round";
  ctx.stroke();
  // soft fill under the line, one run of samples at a time
  ctx.fillStyle = opts.color;
  ctx.globalAlpha = 0.12;
  let run = [];
  const flush = () => {
    if (run.length < 2) { run = []; return; }
    ctx.beginPath();
    ctx.moveTo(run[0].x, pad.t + ph);
    for (const p of run) ctx.lineTo(p.x, p.y);
    ctx.lineTo(run[run.length - 1].x, pad.t + ph);
    ctx.closePath();
    ctx.fill();
    run = [];
  };
  values.forEach((v, i) => {
    if (v === null || v === undefined) { flush(); return; }
    run.push({ x: x0 + i * step, y: y(v) });
  });
  flush();
  ctx.globalAlpha = 1;
}

window.addEventListener("resize", drawCharts);

// ---------------------------------------------------------------- mods

// Remove buttons and the hint depend on the status; called again whenever it changes.
function paintModsState() {
  if (!state.current) return;
  const stopped = state.current.status === "stopped";
  const list = $("mod-list");
  for (const b of list.querySelectorAll("button")) b.disabled = !stopped;
  const hint = $("mods-hint");
  const count = list.childElementCount;
  hint.textContent = count
    ? (stopped ? "" : "Stop the server to remove mods. Restart it for new mods to load.")
    : "No mods installed yet. Use the Modrinth search to add one.";
  hint.hidden = !hint.textContent;
}

async function loadMods() {
  if (!state.current || !state.current.has_mods) return;
  const id = state.current.id, visit = state.visit;
  let mods;
  try { mods = await api("GET", `/api/servers/${id}/mods`); } catch (err) { toast(err.message); return; }
  if (state.visit !== visit) return;
  const list = $("mod-list");
  list.innerHTML = "";
  $("mods-count").textContent = mods.length;
  for (const m of mods) {
    const li = document.createElement("li");
    const bits = [fmtSize(m.size)];
    if (m.version_number) bits.push(`version ${m.version_number}`);
    if (m.dependents.length) bits.push(`needed by ${m.dependents.length} mod${m.dependents.length > 1 ? "s" : ""}`);
    if (m.modpacks.length) bits.push(`from ${m.modpacks.join(", ")}`);
    li.innerHTML = `<span class="file-icon">${icon("layers")}</span><div class="grow"><div class="title"></div><div class="sub"></div></div>` +
      `<button class="icon-btn danger" title="Remove" aria-label="Remove">${icon("trash")}</button>`;
    li.querySelector(".title").textContent = m.title || m.filename;
    li.querySelector(".sub").textContent = bits.join(" · ");
    li.querySelector("button").addEventListener("click", async () => {
      const warn = m.dependents.length ? `\n\nThese mods need it: ${m.dependents.join(", ")}` : "";
      if (!(await confirmDialog("Remove mod", `Remove ${m.filename}?${warn}`, "Remove"))) return;
      try {
        await api("DELETE", `/api/servers/${id}/mods/${encodeURIComponent(m.filename)}`);
        toast(`Removed ${m.filename}`);
        loadMods();
      } catch (err) { toast(err.message); }
    });
    list.appendChild(li);
  }
  paintModsState();
}

$("search-form").addEventListener("submit", (e) => {
  e.preventDefault();
  state.search.q = $("search").value.trim();
  state.search.offset = 0;
  runSearch();
});
$("prev-page").addEventListener("click", () => { state.search.offset = Math.max(0, state.search.offset - state.search.limit); runSearch(); });
$("next-page").addEventListener("click", () => { state.search.offset += state.search.limit; runSearch(); });

async function runSearch() {
  if (!state.current) return;
  const visit = state.visit;
  const { q, offset, limit } = state.search;
  let res;
  try {
    res = await api("GET", `/api/servers/${state.current.id}/modrinth/search?q=${encodeURIComponent(q)}&offset=${offset}&limit=${limit}`);
  } catch (err) { toast(err.message); return; }
  if (state.visit !== visit) return;
  state.search.total = res.total;
  const list = $("search-results");
  list.innerHTML = "";
  if (!res.hits.length) list.innerHTML = `<li class="muted">Nothing found for this server's Minecraft version and loader.</li>`;
  for (const h of res.hits) {
    const li = document.createElement("li");
    li.innerHTML = `<img alt=""><div class="grow"><div class="title"></div><div class="sub"></div></div>` +
      `<button class="btn primary small">${icon("download")}<span>Install</span></button>`;
    const img = li.querySelector("img");
    if (h.icon_url) img.src = h.icon_url; else img.outerHTML = `<span class="file-icon">${icon("layers")}</span>`;
    li.querySelector(".title").textContent = h.title;
    li.querySelector(".sub").textContent = `${h.author} · ${h.downloads.toLocaleString()} downloads · ${h.description}`;
    li.querySelector("button").addEventListener("click", () => pickVersion(h));
    list.appendChild(li);
  }
  const pager = $("search-pager");
  pager.hidden = res.total <= limit;
  $("prev-page").disabled = offset === 0;
  $("next-page").disabled = offset + limit >= res.total;
  $("page-info").textContent = `${offset + 1}–${Math.min(offset + limit, res.total)} of ${res.total}`;
}

async function pickVersion(hit) {
  if (!state.current) return;
  const serverId = state.current.id;
  const dlg = $("versions-dialog");
  $("versions-title").textContent = hit.title;
  $("versions-error").textContent = "";
  $("install-progress").hidden = true;
  const list = $("versions-list");
  list.innerHTML = `<li class="muted">Looking for compatible versions…</li>`;
  dlg.showModal();
  let versions;
  try {
    versions = await api("GET", `/api/servers/${serverId}/modrinth/${encodeURIComponent(hit.project_id)}/versions`);
  } catch (err) {
    list.innerHTML = "";
    $("versions-error").textContent = err.message;
    return;
  }
  if (!dlg.open) return;
  list.innerHTML = "";
  if (!versions.length) list.innerHTML = `<li class="muted">No version of this mod fits this server's Minecraft version and loader.</li>`;
  for (const v of versions) {
    const li = document.createElement("li");
    li.innerHTML = `<div class="grow"><div class="title"></div><div class="sub"></div></div>` +
      `<button class="btn primary small">${icon("download")}<span>Install</span></button>`;
    li.querySelector(".title").textContent = v.name || v.version_number;
    li.querySelector(".sub").textContent = `${v.version_number} · ${v.filename}`;
    li.querySelector("button").addEventListener("click", () => install(serverId, hit, v, list));
    list.appendChild(li);
  }
}

async function install(serverId, hit, version, list) {
  const dlg = $("versions-dialog");
  const fail = (message) => {
    $("versions-error").textContent = message;
    $("install-progress").hidden = true;
    for (const b of list.querySelectorAll("button")) b.disabled = false;
  };
  for (const b of list.querySelectorAll("button")) b.disabled = true;
  $("versions-error").textContent = "";
  $("install-progress").hidden = false;
  $("install-bar").value = 0;
  $("install-detail").textContent = "Starting…";
  let job;
  try {
    job = await api("POST", `/api/servers/${serverId}/mods/install`, {
      project_id: hit.project_id, version_id: version.version_id,
    });
  } catch (err) { fail(err.message); return; }
  const poll = async () => {
    let j;
    try { j = await api("GET", `/api/jobs/${job.job_id}`); } catch (err) { fail(err.message); return; }
    $("install-bar").value = j.fraction;
    $("install-detail").textContent = j.message;
    if (j.state === "running") { setTimeout(poll, 500); return; }
    if (j.state === "done") {
      toast(`Installed ${j.message}`);
      if (dlg.open) dlg.close();
      if (state.current && state.current.id === serverId) loadMods();
    } else {
      fail(`Install failed: ${j.message}`);
    }
  };
  poll();
}

$("versions-close").addEventListener("click", () => $("versions-dialog").close());


// ---------------------------------------------------------------- settings

function resetSettings() {
  state.settings = null;
  state.iconFile = null;
  state.updating = false;
  $("icon-file").value = "";
  $("icon-save").disabled = true;
  $("motd").value = "";
  $("update-mc").innerHTML = "";
  $("update-loader").innerHTML = "";
  $("update-mc").disabled = true;
  $("update-loader").disabled = true;
  $("update-run").disabled = true;
  $("update-progress").hidden = true;
  $("update-note").hidden = true;
  $("update-current").textContent = "";
}

async function loadSettings() {
  const s = state.current;
  if (!s || state.settings) return;
  const id = s.id, visit = state.visit;
  let data;
  try { data = await api("GET", `/api/servers/${id}/settings`); } catch (err) { toast(err.message); return; }
  if (state.visit !== visit) return;
  state.settings = data;
  const bedrock = data.edition === "bedrock";

  paintIconPreview(data.has_icon ? `/api/servers/${id}/icon?t=${Date.now()}` : null);
  // Bedrock keeps the line shown in the server list as server-name rather than motd.
  $("motd-title").textContent = bedrock ? "Server name" : "Message of the day";
  $("motd-label").textContent = bedrock ? "Server name" : "Message of the day";
  $("motd-note").textContent = bedrock
    ? "The name players see in their server list. It is read when the server starts, so restart it to show a new one."
    : "The line players see under the server's name. It is read when the server starts, so restart it to show a new one.";
  $("motd").value = data.motd;
  $("motd").disabled = !data.properties_readable;
  $("motd-save").disabled = !data.properties_readable;
  $("motd").placeholder = data.properties_readable
    ? "a Niksnaks-Hosting server"
    : "This server's server.properties could not be read.";

  $("update-current").textContent = `Now on ${data.mc_version}`;
  $("update-mc-label").textContent = bedrock ? "Bedrock version" : "Minecraft version";
  $("update-loader-field").hidden = bedrock;
  $("update-loader-label").textContent = `${loaderLabel(s).split(" ")[0] || "Loader"} version`;
  if (!data.can_update) {
    updateNote("A server on another machine has to be updated from the app on that machine.");
    return;
  }
  loadUpdateVersions(id, visit);
}

function updateNote(text) {
  $("update-note-text").textContent = text;
  $("update-note").hidden = !text;
}

function paintIconPreview(src) {
  const box = $("icon-preview");
  const img = box.querySelector("img");
  box.querySelector(".monogram").textContent = monogram(state.current ? state.current.name : "?");
  if (src) {
    img.src = src;
    img.hidden = false;
    img.onerror = () => { img.hidden = true; };
  } else {
    img.removeAttribute("src");
    img.hidden = true;
  }
}

async function loadUpdateVersions(id, visit) {
  const mc = $("update-mc");
  mc.innerHTML = `<option>Looking for newer versions…</option>`;
  let data;
  try { data = await api("GET", `/api/servers/${id}/update/versions`); } catch (err) { mc.innerHTML = ""; updateNote(err.message); return; }
  if (state.visit !== visit || !state.current || state.current.id !== id) return;
  mc.innerHTML = "";
  const bedrock = !data.loader_type;
  if (!data.mc_versions.length) {
    mc.innerHTML = `<option>No newer versions</option>`;
    updateNote(bedrock
      ? "This server is already on the newest Bedrock version."
      : "This server is already on the newest Minecraft version its loader offers.");
    return;
  }
  const backup = state.settings && state.settings.backs_up
    ? "Updating makes a full backup first, then"
    : "No backup can be made of a server on another machine; updating";
  updateNote(bedrock
    ? `${backup} installs the new Bedrock server. Worlds, server.properties and the allow list are kept.`
    : `${backup} reinstalls the server. Mods that do not fit the new version are moved aside.`);
  for (const v of data.mc_versions) {
    const opt = document.createElement("option");
    opt.value = v;
    opt.textContent = (data.labels && data.labels[v]) || v;
    mc.appendChild(opt);
  }
  mc.disabled = false;
  if (bedrock) {
    $("update-run").disabled = state.updating;
    return;
  }
  loadLoaderVersions(id, visit);
}

async function loadLoaderVersions(id, visit) {
  const sel = $("update-loader");
  const mcVersion = $("update-mc").value;
  sel.disabled = true;
  $("update-run").disabled = true;
  sel.innerHTML = `<option>Loading…</option>`;
  let options;
  try {
    options = await api("GET", `/api/servers/${id}/update/loader-versions?mc_version=${encodeURIComponent(mcVersion)}`);
  } catch (err) { sel.innerHTML = ""; updateNote(err.message); return; }
  if (state.visit !== visit || !state.current || state.current.id !== id || $("update-mc").value !== mcVersion) return;
  sel.innerHTML = "";
  if (!options.length) {
    sel.innerHTML = `<option>No builds for this version</option>`;
    return;
  }
  for (const o of options) {
    const opt = document.createElement("option");
    opt.value = o.version;
    opt.textContent = o.label;
    sel.appendChild(opt);
  }
  sel.disabled = false;
  $("update-run").disabled = state.updating;
}

$("update-mc").addEventListener("change", () => {
  if (state.current && state.current.edition !== "bedrock") loadLoaderVersions(state.current.id, state.visit);
});

$("icon-file").addEventListener("change", (e) => {
  const file = e.target.files && e.target.files[0];
  state.iconFile = file || null;
  $("icon-save").disabled = !file;
  paintIconPreview(file ? URL.createObjectURL(file) : (state.settings && state.settings.has_icon ? `/api/servers/${state.current.id}/icon?t=${Date.now()}` : null));
});

$("icon-save").addEventListener("click", async () => {
  const file = state.iconFile;
  if (!file || !state.current) return;
  const id = state.current.id;
  $("icon-save").disabled = true;
  try {
    const res = await fetch(`/api/servers/${id}/icon`, {
      method: "POST",
      headers: { "X-Requested-With": "fetch", "Content-Type": file.type || "application/octet-stream" },
      body: file,
    });
    let data = {};
    try { data = await res.json(); } catch (_) { /* no body */ }
    if (!res.ok) throw new Error(data.error || res.statusText);
    toast("Server picture saved");
    state.iconFile = null;
    $("icon-file").value = "";
    if (state.settings) state.settings.has_icon = true;
    if (state.current && state.current.id === id) paintIconPreview(`/api/servers/${id}/icon?t=${Date.now()}`);
  } catch (err) {
    toast(err.message);
    $("icon-save").disabled = false;
  }
});

$("motd-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!state.current) return;
  const id = state.current.id;
  $("motd-save").disabled = true;
  try {
    await api("POST", `/api/servers/${id}/settings/motd`, { motd: $("motd").value });
    if (state.settings) state.settings.motd = $("motd").value;
    toast(state.current.status === "running"
      ? "Message of the day saved — restart the server to show it"
      : "Message of the day saved");
  } catch (err) { toast(err.message); }
  $("motd-save").disabled = false;
});

$("update-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const s = state.current;
  if (!s || state.updating) return;
  const mcVersion = $("update-mc").value, loaderVersion = $("update-loader").value;
  if (!mcVersion || $("update-mc").disabled) return;
  if (s.status !== "stopped") { toast("Stop the server before updating it"); return; }
  const bedrock = s.edition === "bedrock";
  const backup = state.settings && state.settings.backs_up ? "A full backup is made first" : "No backup can be made first";
  const ok = await confirmDialog(
    "Update server",
    bedrock
      ? `Update ${s.name} from Bedrock ${s.mc_version} to ${mcVersion}? ${backup}; worlds and settings are kept.`
      : `Update ${s.name} from Minecraft ${s.mc_version} to ${mcVersion}? ${backup}, and mods that do not fit the new version are moved aside.`,
    "Update");
  if (!ok) return;
  runUpdate(s.id, mcVersion, loaderVersion);
});

async function runUpdate(id, mcVersion, loaderVersion) {
  state.updating = true;
  $("update-run").disabled = true;
  $("update-mc").disabled = true;
  $("update-loader").disabled = true;
  $("update-progress").hidden = false;
  $("update-bar").value = 0;
  $("update-detail").textContent = "Starting…";
  const finish = (message, failed) => {
    state.updating = false;
    toast(message);
    // The version, the loader and the mod list all just moved; reload the lot.
    refreshServers().catch(() => {});
    if (state.current && state.current.id === id) {
      $("update-progress").hidden = !failed;
      state.settings = null;
      loadSettings();
      if (state.tab === "mods") loadMods();
    }
  };
  let job;
  try {
    job = await api("POST", `/api/servers/${id}/update`, { mc_version: mcVersion, loader_version: loaderVersion });
  } catch (err) { finish(err.message, true); return; }
  const poll = async () => {
    let j;
    try { j = await api("GET", `/api/jobs/${job.job_id}`); } catch (err) { finish(err.message, true); return; }
    $("update-bar").value = j.fraction;
    $("update-detail").textContent = j.message;
    if (j.state === "running") { setTimeout(poll, 700); return; }
    finish(j.state === "done" ? j.message : `Update failed: ${j.message}`, j.state !== "done");
  };
  poll();
}

// ---------------------------------------------------------------- boot

(async () => {
  try {
    state.user = await api("GET", "/api/me");
    await showApp();
  } catch (_) {
    showLogin();
  }
})();
