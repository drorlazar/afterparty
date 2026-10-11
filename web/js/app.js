// Entry point: state, navigation, layout, map events, keys, rescan, boot.
import { S, hooks, $, esc, fmt, age, clsKey, CLS_LABEL, isUnder, nodeName } from "./core.js";
import { get, post, events, onDisconnect } from "./api.js";
import { initModel, byId, ensureLoaded, pathOf, register } from "./model.js";
import { initTreemap, draw, hit, animateZoom, legend } from "./treemap.js";
import { renderCrumbs, renderTree, renderContents, maybeLoadMore, markRows, renderInspector, renderCapacity, renderTray,
  renderFdaBanner, renderScanMeta, setSort, forgetVerdict } from "./panels.js";
import { initSettings, applyTheme, togglePop, popOpen, openMenu, closeMenu, menuOpen, initMenu, initReview, toast, reveal, protect } from "./overlays.js";
import { initMole } from "./mole.js";
import { initAgentSettings, loadAgentStatus } from "./agent.js";
import { initPreview, openPreview, closePreview, isPreviewOpen, step } from "./preview.js";
import { initActivity, watch } from "./activity.js";
import { initTips } from "./tips.js";

/* ---------------- render ---------------- */
function renderAll(full = true) {
  renderCrumbs(); renderTree(); renderInspector(); renderTray();
  if (full) renderContents(); else markRows();
  $("mapTitle").textContent = "Map · " + nodeName(S.loc);
  draw();
}

/* ---------------- navigation ---------------- */
async function go(n, { push = true, from = null, anim = true } = {}) {
  if (!n) return;
  if (n.t === "file" || n.t === "rest") { S.sel = n; renderAll(false); return; }
  if (n.t === "agg") register(n);
  const lm = $("loadingmap"); lm.classList.add("on");
  try { await ensureLoaded(n, S.depth || 3); } catch (e) { toast("Couldn't load that folder"); lm.classList.remove("on"); return; }
  lm.classList.remove("on");
  const r = from || null;
  const finish = () => {
    if (push && S.loc && S.loc !== n) { S.hist.push(S.loc); S.fut = []; }
    S.loc = n; S.sel = S.sel && isUnder(S.sel, n) && S.sel !== n ? S.sel : null;
    for (let a = n; a; a = a.parent) S.expanded.add(a.id);
    renderAll();
  };
  if (anim && r) animateZoom(r, true, finish); else finish();
}
function up() { if (S.loc.parent) { const child = S.loc; S.hist.push(S.loc); S.fut = []; S.loc = S.loc.parent; S.sel = child; renderAll(); } }
function home() { if (S.loc !== S.root) { S.hist.push(S.loc); S.fut = []; S.loc = S.root; S.sel = null; renderAll(); } }
function back() { if (S.hist.length) { S.fut.push(S.loc); S.loc = S.hist.pop(); renderAll(); } }
function fwd() { if (S.fut.length) { S.hist.push(S.loc); S.loc = S.fut.pop(); renderAll(); } }
function toggleStage(n) {
  if (!n || !n.parent || clsKey(n) === "locked" || n.t === "rest" || n.t === "agg" || n.t === "seq") return;
  S.staged.has(n.id) ? S.staged.delete(n.id) : S.staged.set(n.id, n); renderAll(false);
}
function showAgent() { loadAgentStatus(true).then(() => renderInspector()); S.tab = "agent"; document.querySelectorAll(".tab").forEach(x => x.classList.toggle("on", x.dataset.t === "agent")); renderInspector(); if (!fitPanes().right) { L.right = true; lastOpened = "r"; applyLayout(); } }
function showInTree(n) { for (let a = n; a; a = a.parent) S.expanded.add(a.id); if (!fitPanes().left) { L.left = true; lastOpened = "l"; applyLayout(); } renderTree(); }
Object.assign(hooks, { renderAll, go, draw, toggleStage, reveal, protect, forgetVerdict, showAgent, showInTree, applyLayout: () => applyLayout(),
  reloadModel: () => reload(), watchActivity: cleaning => watch(cleaning), preview: n => openPreview(n),
  openReview: paths => import("./review.js").then(m => m.open(paths)) });

/* ---------------- layout: resize / collapse / presets (persisted) ---------------- */
const LS = "cleanup.layout.v2", DEF = { lw: 280, rw: 360, mapfr: .62, left: true, right: true, files: true, cap: true };
let L = Object.assign({}, DEF, JSON.parse(localStorage.getItem(LS) || "null") || {});
const PRE = { explore: { left: true, right: true, files: true, mapfr: .62, lw: 280, rw: 360 }, files: { left: true, right: true, files: true, mapfr: .32, lw: 240, rw: 340 }, focus: { left: false, right: false, files: false } };
let preFocus = null;
/* Fit the saved pane widths into this window without changing them (they come back on a bigger screen):
   shrink both side panes toward their minimums, then fold the tree to its rail, so the map keeps MIN_CENTER. */
const MIN_CENTER = 320, MIN_L = 180, MIN_R = 260, RAIL = 36, SPLITS = 16;
let lastOpened = "r";  // when only one side pane fits, the one opened most recently stays open
function fitPanes() {
  let left = L.left, right = L.right, lw = left ? L.lw : RAIL, rw = right ? L.rw : RAIL;
  if (left) lw = Math.min(lw, Math.max(MIN_L, innerWidth * .3)); if (right) rw = Math.min(rw, Math.max(MIN_R, innerWidth * .38));
  const over = () => lw + rw - (innerWidth - SPLITS - MIN_CENTER);
  if (over() > 0 && right) rw -= Math.max(0, Math.min(over(), rw - MIN_R));
  if (over() > 0 && left) lw -= Math.max(0, Math.min(over(), lw - MIN_L));
  if (over() > 0 && left && right) { if (lastOpened === "l") { right = false; rw = RAIL; } else { left = false; lw = RAIL; } }
  return { left, right, lw, rw };
}
function applyLayout() {
  const root = document.documentElement, f = fitPanes();
  root.style.setProperty("--lw", f.lw + "px"); root.style.setProperty("--rw", f.rw + "px");
  $("pLeft").classList.toggle("collapsed", !f.left); $("pRight").classList.toggle("collapsed", !f.right);
  $("sL").style.pointerEvents = f.left ? "" : "none"; $("sR").style.pointerEvents = f.right ? "" : "none";
  const c = $("pCenter"), mw = $("mapwrap"), fb = $("filesBody");
  const ph = document.querySelector(".ph").offsetHeight || 34; const avail = c.clientHeight - ph - 8 - ph;
  mw.style.height = (L.files ? Math.max(120, avail * L.mapfr) : avail + 34) + "px";
  fb.style.display = L.files ? "" : "none"; $("filesToggle").textContent = L.files ? "▾" : "▴";
  $("cap").classList.toggle("min", !L.cap); $("capToggle").textContent = L.cap ? "▴" : "▾";
  localStorage.setItem(LS, JSON.stringify(L)); requestAnimationFrame(draw);
}
/* A splitter drag must never start a text selection: cancel the default and lock selection page-wide until release. */
function startDrag(s, e, dir) {
  e.preventDefault(); getSelection().removeAllRanges();
  s.setPointerCapture(e.pointerId); s.classList.add("drag"); document.body.classList.add("resizing", dir);
  const end = () => { s.classList.remove("drag"); document.body.classList.remove("resizing", dir); s.onpointermove = s.onpointerup = s.onpointercancel = null; };
  s.onpointerup = s.onpointercancel = end;
}
function dragX(id, side) {
  const s = $(id);
  s.onpointerdown = e => {
    startDrag(s, e, "col"); const f = fitPanes(), x0 = e.clientX, w0 = side === "l" ? f.lw : f.rw;
    s.onpointermove = ev => { const dx = ev.clientX - x0, max = Math.min(innerWidth * (side === "l" ? .3 : .38), innerWidth - SPLITS - MIN_CENTER - (side === "l" ? fitPanes().rw : fitPanes().lw));
      if (side === "l") L.lw = Math.max(MIN_L, Math.min(max, w0 + dx)); else L.rw = Math.max(MIN_R, Math.min(max, w0 - dx)); applyLayout(); };
  };
  s.ondblclick = () => { side === "l" ? L.lw = 280 : L.rw = 360; applyLayout(); };
}
function initLayout() {
  dragX("sL", "l"); dragX("sR", "r");
  const s = $("sH");
  s.onpointerdown = e => {
    startDrag(s, e, "row"); const c = $("pCenter").getBoundingClientRect();
    s.onpointermove = ev => { L.mapfr = Math.max(.15, Math.min(.9, (ev.clientY - c.top - 34) / (c.height - 76))); applyLayout(); };
  };
  s.ondblclick = () => { L.mapfr = .62; applyLayout(); };
  document.querySelectorAll("[data-collapse]").forEach(b => b.onclick = () => { L[b.dataset.collapse === "pLeft" ? "left" : "right"] = false; applyLayout(); });
  document.querySelectorAll(".rail").forEach(r => r.onclick = () => { const l = r.dataset.open === "pLeft"; L[l ? "left" : "right"] = true; lastOpened = l ? "l" : "r"; applyLayout(); });
  $("filesToggle").onclick = () => { L.files = !L.files; applyLayout(); };
  $("mapMax").onclick = () => { L.files = !L.files; applyLayout(); };
  $("capToggle").onclick = () => { L.cap = !L.cap; applyLayout(); };
  document.querySelectorAll("#presets button").forEach(b => b.onclick = () => {
    document.querySelectorAll("#presets button").forEach(x => x.classList.remove("on")); b.classList.add("on");
    if (b.dataset.p === "focus") preFocus = { ...L }; Object.assign(L, PRE[b.dataset.p]); applyLayout();
  });
  document.querySelectorAll("[data-d]").forEach(b => b.onclick = () => { document.querySelectorAll("[data-d]").forEach(x => x.classList.remove("on")); b.classList.add("on"); S.depth = +b.dataset.d; draw(); });
  document.querySelectorAll("#modes button").forEach(b => b.onclick = () => { document.querySelectorAll("#modes button").forEach(x => x.classList.remove("on")); b.classList.add("on"); S.mode = b.dataset.m; legend(); draw(); });
  document.querySelectorAll(".tab").forEach(t => t.onclick = () => { document.querySelectorAll(".tab").forEach(x => x.classList.remove("on")); t.classList.add("on"); S.tab = t.dataset.t; renderInspector();
    if (S.tab === "agent") loadAgentStatus(true).then(() => renderInspector());  // re-check Claude Code every time the tab opens
  });
  document.querySelectorAll("#files th[data-s]").forEach(th => th.onclick = () => setSort({ n: "name", s: "size", m: "mtime", k: "name" }[th.dataset.s]));
  $("filesBody").addEventListener("scroll", maybeLoadMore);
  addEventListener("resize", applyLayout);
}
const resetLayout = () => { L = { ...DEF }; applyLayout(); };

/* ---------------- map events ---------------- */
function initMap() {
  const cv = $("map"), tip = $("tip"); initTreemap(cv);
  const at = e => { const b = cv.getBoundingClientRect(); return hit(e.clientX - b.left, e.clientY - b.top); };
  cv.onmousemove = e => {
    const r = at(e); const n = r && r.n;
    if (n !== S.hover) { S.hover = n; draw(); }
    if (!n) { tip.classList.remove("on"); return; }
    const a = age(n);
    tip.innerHTML = `<div class="p">${esc(pathOf(n))}</div><div class="n">${esc(n.n)}</div><div class="kv"><dt>Size</dt><dd>${fmt(n.s)}</dd>${n.f ? `<dt>Files</dt><dd>${n.f.toLocaleString()}</dd>` : ""}${a != null ? `<dt>Idle</dt><dd>${a} days</dd>` : ""}<dt>Verdict</dt><dd>${CLS_LABEL[clsKey(n)]}</dd></div><div class="h">${n.t === undefined || n.t === "agg" ? "double-click to open" : ""}</div>`;
    tip.style.left = Math.min(e.clientX + 16, innerWidth - 350) + "px"; tip.style.top = Math.min(e.clientY + 16, innerHeight - 140) + "px"; tip.classList.add("on");
  };
  cv.onmouseleave = () => { S.hover = null; tip.classList.remove("on"); draw(); };
  cv.onclick = e => { const r = at(e); if (r) { S.sel = r.n; renderAll(false); } };
  cv.ondblclick = e => { const r = at(e); if (r && (r.n.t === undefined || r.n.t === "agg")) go(r.n, { from: r }); };
  cv.addEventListener("contextmenu", e => { const r = at(e); openMenu(e, r && r.n); });
  $("tree").addEventListener("contextmenu", e => { const r = e.target.closest(".trow"); if (r) openMenu(e, byId(+r.dataset.id)); });
  $("files").querySelector("tbody").addEventListener("contextmenu", e => { const r = e.target.closest("tr"); if (r) openMenu(e, byId(+r.dataset.id)); });
  $("crumbs").addEventListener("contextmenu", e => e.preventDefault());
  $("bBack").onclick = back; $("bFwd").onclick = fwd; $("bUp").onclick = up; $("bHome").onclick = home;
}

/* ---------------- keys ---------------- */
function initKeys() {
  addEventListener("keydown", e => {
    if (e.key === "Escape" && popOpen()) { togglePop(false); $("brand").focus(); return; }
    if (e.target.tagName === "INPUT") return;
    if (menuOpen()) return;
    if (isPreviewOpen()) {
      if (e.key === "Escape" || e.key === " ") { e.preventDefault(); closePreview(); return; }
      if (e.key === "ArrowRight" || e.key === "ArrowDown") { e.preventDefault(); step(1); return; }
      if (e.key === "ArrowLeft" || e.key === "ArrowUp") { e.preventDefault(); step(-1); return; }
      return;
    }
    if (e.key === " " && S.sel && (S.sel.t === "file" || S.sel.t === "seq")) { e.preventDefault(); openPreview(S.sel); return; }
    if (e.key === ",") { togglePop(); return; }
    if (e.key === "Escape") {
      if ($("ov").classList.contains("on")) { $("ov").classList.remove("on"); return; }
      if (preFocus) { Object.assign(L, preFocus); preFocus = null; applyLayout(); return; }
    }
    if (e.metaKey && e.key === "[") return back();
    if (e.metaKey && e.key === "]") return fwd();
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "Backspace") { e.preventDefault(); up(); }
    if (e.key === "Enter" && S.sel && S.sel.t === undefined) go(S.sel, { anim: true });
    if (e.key === "h" || e.key === "H") home();
    if (e.key === "1") { L.left = !fitPanes().left; if (L.left) lastOpened = "l"; applyLayout(); }
    if (e.key === "2") { L.files = !L.files; applyLayout(); }
    if (e.key === "3") { L.right = !fitPanes().right; if (L.right) lastOpened = "r"; applyLayout(); }
    if (e.key === "z" || e.key === "Z") { if (preFocus) { Object.assign(L, preFocus); preFocus = null; } else { preFocus = { ...L }; Object.assign(L, PRE.focus); } applyLayout(); }
    if (e.key === "a" || e.key === "A") toggleStage(S.sel);
    if (e.key === "t" || e.key === "T") $("reviewBtn").click();
    if (e.key === "r" || e.key === "R") rescan();
  });
}

/* ---------------- scan ---------------- */
let scanning = false;
async function rescan() {
  if (scanning) return;
  try { await post("/api/scan"); } catch (e) { if (e.status === 409) toast("A scan or cleanup is already running"); return; }
  watch(); watchScan();
}
function watchScan() {
  scanning = true; const bar = $("scanbar"); bar.hidden = false; const est = (S.status && S.status.scan && S.status.scan.dirs) || 800000;
  events(async d => {
    if (d.state === "running") {
      bar.firstElementChild.style.width = Math.min(98, (d.dirs || 0) / est * 100) + "%";
      renderScanMeta(`Scanning… ${(d.files || 0).toLocaleString()} files · ${d.elapsed || 0}s`);
      return;
    }
    scanning = false; bar.hidden = true;
    if (d.state === "done" || d.state === "partial") {
      toast(d.state === "partial" ? "Scan finished (partial — some folders didn't respond)" : `Scan finished in ${d.took}s`);
      await reload();
    } else toast("Scan " + d.state);
  });
}
async function reload() {
  S.status = await get("/api/status");
  const keepPath = S.loc && S.loc.path;
  S.root = await initModel(S.status); S.loc = S.root; S.sel = null; S.hist = []; S.fut = []; S.staged.clear();
  S.expanded = new Set([S.root.id]);
  if (keepPath && keepPath !== S.root.path) {  // return to where the user was
    let n = S.root;
    for (const seg of keepPath.slice((S.root.path || "").length).split("/").filter(Boolean)) {
      await ensureLoaded(n, 1); const k = (n.c || []).find(x => x.n === seg); if (!k) break; n = k;
    }
    S.loc = n; for (let a = n; a; a = a.parent) S.expanded.add(a.id);
  }
  renderScanMeta(); renderFdaBanner(); renderCapacity(); applyLayout(); renderAll();
  get("/api/hidden").then(h => { S.hidden = h; renderCapacity(); }).catch(() => {});
}

/* ---------------- boot ---------------- */
async function boot() {
  initSettings(resetLayout); initMenu(); initReview(); initLayout(); initMap(); initKeys(); initActivity(); initMole(); initPreview(); initAgentSettings(); initTips();
  $("bRescan").onclick = rescan;
  onDisconnect(() => {
    const native = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.cleanup;
    if (native) native.postMessage("reconnect");      // Afterparty.app fetches a fresh session key itself
    else $("reconnect").classList.add("on");          // browser: ask the user to reload
  });
  S.status = await get("/api/status");
  renderFdaBanner();
  if (!S.status.scan) {
    $("loading").textContent = "No snapshot yet — scanning your disk (about a minute)…";
    if (!S.status.scanning) await post("/api/scan").catch(() => {});
    scanning = true;
    events(async d => {
      if (d.state === "running") { $("loading").textContent = `Scanning your disk… ${(d.files || 0).toLocaleString()} files`; return; }
      scanning = false; await reload(); $("loading").remove();
    });
    return;
  }
  await reload();
  $("loading").remove();
  legend();
  if (S.status.scanning) watchScan();
}
boot().catch(e => { console.error(e); const l = $("loading"); if (l) l.textContent = "Couldn't start Afterparty — " + e.message; });
