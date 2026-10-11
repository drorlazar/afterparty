// Quick Look-style preview. Space opens/closes, ←/→ page through the current folder's files, Esc closes.
import { S, hooks, $, esc, fmt } from "./core.js";
import { get, post } from "./api.js";
import { listRows } from "./panels.js";

let cur = null;
const src = p => `/api/preview?path=${encodeURIComponent(p)}`;
const pathFor = n => n && (n.t === "seq" ? n.first : (n.t === "file" ? n.path : null));

export const isPreviewOpen = () => $("pv").classList.contains("on");
export function closePreview() { $("pv").classList.remove("on"); $("pvBody").innerHTML = ""; cur = null; }

export async function openPreview(n) {
  cur = n; $("pv").classList.add("on");
  const p = pathFor(n), rows = files(), idx = rows.findIndex(r => r.n === n);
  $("pvName").textContent = n.n;
  $("pvMeta").textContent = ` · ${fmt(n.s)}${idx >= 0 ? ` · ${idx + 1} of ${rows.length}` : ""}`;
  $("pvReveal").onclick = () => post("/api/reveal", { path: p || n.path }).catch(() => {});
  const body = $("pvBody");
  if (!p) { body.innerHTML = `<div class="pvmsg">Folders don't have a preview — double-click to open it.</div>`; return; }
  body.innerHTML = `<div class="pvmsg">Loading…</div>`;
  let info;
  try { info = await get("/api/preview", { path: p, mode: "info" }); }
  catch (e) { if (cur === n) body.innerHTML = `<div class="pvmsg">${e.status === 403 ? "Afterparty can't read this file." : "This file can't be previewed."}</div>`; return; }
  if (cur !== n) return;
  if (info.kind === "image" || info.kind === "thumb") {
    body.innerHTML = `<img alt="" src="${esc(src(p))}">`;
    body.querySelector("img").onerror = () => { if (cur === n) body.innerHTML = `<div class="pvmsg">No preview for this kind of file — Reveal in Finder to open it.</div>`; };
  } else if (info.kind === "video") body.innerHTML = `<video controls autoplay muted playsinline src="${esc(src(p))}"></video>`;
  else if (info.kind === "audio") body.innerHTML = `<audio controls autoplay src="${esc(src(p))}"></audio>`;
  else {
    try {
      const t = await get("/api/preview", { path: p });
      if (cur === n) body.innerHTML = `<pre>${esc(t.text)}</pre>${t.truncated ? `<div class="pvmsg">Showing the first 64 KB.</div>` : ""}`;
    } catch (e) { if (cur === n) body.innerHTML = `<div class="pvmsg">This file can't be previewed.</div>`; }
  }
}

function files() { return listRows().filter(r => r.n.t === "file" || r.n.t === "seq"); }

export function step(d) {
  const rows = files(); if (!rows.length) return;
  const i = rows.findIndex(r => r.n === cur);
  const next = rows[Math.max(0, Math.min(rows.length - 1, (i < 0 ? 0 : i + d)))].n;
  S.sel = next; hooks.renderAll(false); openPreview(next);
}

export function initPreview() {
  $("pvClose").onclick = closePreview;
  $("pv").onclick = e => { if (e.target.id === "pv") closePreview(); };
}
