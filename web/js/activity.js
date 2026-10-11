// Live activity: what Afterparty is doing right now (cleaning / scanning), in the tray, the map and the Dock.
import { S, hooks, $, esc, fmt } from "./core.js";
import { get, post } from "./api.js";

const native = () => window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.cleanup;
const tell = msg => { const n = native(); if (n) n.postMessage(msg); };
let timer = null, seenLast = null, wasExecuting = false;

export function initActivity() {
  $("actStop").onclick = async () => {
    const a = await get("/api/activity").catch(() => null);
    if (a && a.exec) { await post(`/api/plan/${a.exec.plan}/stop`).catch(() => {}); $("actStop").textContent = "Stopping…"; $("actStop").disabled = true; }
  };
  get("/api/activity").then(a => { seenLast = a.last && a.last.finished; if (a.executing || a.scanning) watch(); }).catch(() => {});
}

// Start polling (call after starting a cleanup or scan); stops by itself when idle.
// `cleaning` = a cleanup was just started here, so report its end even if it finishes before the first poll.
export function watch(cleaning) { if (cleaning) wasExecuting = true; if (!timer) { tick(); timer = setInterval(tick, 700); } }

async function tick() {
  let a;
  try { a = await get("/api/activity"); } catch (e) { return; }
  const bar = $("activity"), fill = $("actFill"), txt = $("actText"), stop = $("actStop");
  if (a.executing && a.exec) {
    wasExecuting = true;
    const x = a.exec, frac = x.total ? x.done / x.total : 0;
    S.processing = new Set(x.processed);
    bar.hidden = false; $("tray").classList.add("busy");
    fill.style.width = (frac * 100).toFixed(1) + "%";
    txt.innerHTML = `<b>Cleaning</b> · ${x.done} of ${x.total} · <span class="mono">${esc(x.path || "starting…")}</span> · ${Math.round(Date.now() / 1000 - x.started)}s`;
    stop.hidden = false;
    tell({ type: "progress", fraction: frac });
    hooks.draw();
    return;
  }
  if (a.scanning && a.scan) {
    bar.hidden = false; $("tray").classList.add("busy"); stop.hidden = true;
    const est = (S.status && S.status.scan && S.status.scan.dirs) || 800000;
    fill.style.width = Math.min(98, (a.scan.dirs || 0) / est * 100) + "%";
    txt.innerHTML = `<b>Scanning</b> · ${(a.scan.files || 0).toLocaleString()} files · ${a.scan.elapsed || 0}s`;
    tell({ type: "progress", fraction: Math.min(.98, (a.scan.dirs || 0) / est) });
    return;
  }
  // idle
  bar.hidden = true; $("tray").classList.remove("busy"); stop.hidden = true; stop.disabled = false; stop.textContent = "Stop";
  tell({ type: "progress", fraction: -1 });
  clearInterval(timer); timer = null;
  if (a.last && a.last.finished !== seenLast) {
    seenLast = a.last.finished;
    if (wasExecuting) {
      const c = a.last.counts || {};
      const body = `Freed ${fmt(a.last.freed)}${a.last.trash_bytes ? ` · ${fmt(a.last.trash_bytes)} in Trash` : ""} · ${c.deleted || 0} deleted · ${c.trashed || 0} trashed${c.skipped ? ` · ${c.skipped} skipped` : ""}${a.last.stopped ? " · stopped early" : ""}`;
      tell({ type: "notify", title: "Afterparty finished", body });
      S.processing = new Set(); S.staged.clear();
      hooks.reloadModel && hooks.reloadModel();
    }
  }
  wasExecuting = false;
}
