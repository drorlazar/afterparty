// Settings popover, context menu, review overlay, toast.
import { S, SET, saveSet, hooks, $, esc, fmt, age, clsKey, CLS_LABEL, nodeName } from "./core.js";
import { get, post } from "./api.js";
import { pathOf } from "./model.js";
import { legend } from "./treemap.js";

/* ---------------- toast ---------------- */
export function toast(t) { const e = $("toast"); e.textContent = t; e.classList.add("on"); clearTimeout(toast.h); toast.h = setTimeout(() => e.classList.remove("on"), 2000); }

/* ---------------- theme + text size + map prefs ---------------- */
const mq = matchMedia("(prefers-color-scheme: dark)");
const themeMode = () => localStorage.getItem("prism-theme") || "system";
export function applyTheme() {
  const m = themeMode(); const d = m === "system" ? (mq.matches ? "dark" : "light") : m;
  document.documentElement.dataset.theme = d;
  document.querySelectorAll("#themeSeg button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.mode === m)));
  if (S.root) { legend(); hooks.renderAll(false); }
}
export function applyScale(v) {
  const r = $("tsRange"); v = Math.min(1.4, Math.max(.85, v || 1));
  document.documentElement.style.setProperty("--ts", v); localStorage.setItem("prism-textscale", v); r.value = v;
  r.style.setProperty("--fill", ((v - .85) / .55 * 100) + "%"); $("tsVal").textContent = Math.round(v * 100) + "%";
  if (S.root) requestAnimationFrame(() => hooks.applyLayout());
}
export function initSettings(resetLayout) {
  mq.addEventListener("change", () => { if (themeMode() === "system") applyTheme(); });
  document.querySelectorAll("#themeSeg button").forEach(b => b.onclick = () => { localStorage.setItem("prism-theme", b.dataset.mode); applyTheme(); });
  $("tsRange").oninput = () => applyScale(+$("tsRange").value);
  [["swAnim", "anim"], ["swLocked", "locked"], ["swHatch", "hatch"]].forEach(([id, k]) => {
    const b = $(id); b.setAttribute("aria-checked", String(SET[k]));
    b.onclick = () => { SET[k] = !SET[k]; b.setAttribute("aria-checked", String(SET[k])); saveSet(); hooks.draw(); };
  });
  $("resetLayout").onclick = () => { resetLayout(); toast("Layout reset"); };
  const pop = $("settings"), brand = $("brand");
  brand.onclick = e => { e.stopPropagation(); togglePop(); };
  addEventListener("click", e => { if (pop.classList.contains("on") && !pop.contains(e.target) && !brand.contains(e.target)) togglePop(false); });
  applyTheme(); applyScale(+(localStorage.getItem("prism-textscale") || 1));
}
export function togglePop(on) { const pop = $("settings"); on = on ?? !pop.classList.contains("on"); pop.classList.toggle("on", on); $("brand").classList.toggle("on", on); }
export const popOpen = () => $("settings").classList.contains("on");

/* ---------------- actions shared by menu + inspector ---------------- */
export async function reveal(n) {
  try { await post("/api/reveal", { path: pathOf(n) }); toast("Revealed in Finder"); }
  catch (e) { toast(e.status === 404 ? "That item no longer exists" : "Couldn't reveal it"); }
}
export async function protect(n) {
  try { await post("/api/keep", { path: pathOf(n) }); toast("Protected — Afterparty will never suggest this"); hooks.forgetVerdict(pathOf(n)); S.staged.delete(n.id); hooks.renderAll(false); }
  catch (e) {
    if (e.status === 409 && e.data && e.data.pending) { toast("Protected (pending) — the settings couldn't be updated yet"); hooks.forgetVerdict(pathOf(n)); S.staged.delete(n.id); hooks.renderAll(false); }
    else toast(e.status === 409 ? "Already protected" : "Couldn't protect it");
  }
}

/* ---------------- context menu ---------------- */
export function openMenu(e, n) {
  e.preventDefault(); if (!n) return; S.sel = n; hooks.renderAll(false);
  const k = clsKey(n); const locked = k === "locked"; const isRoot = !n.parent; const isDir = n.t === undefined || n.t === "agg";
  const p = pathOf(n);
  const items = [
    isDir && ["⤢", "Open", () => hooks.go(n, { anim: true }), "↵"],
    n.parent && ["↑", "Show parent folder", () => { hooks.go(n.parent, { anim: false }); S.sel = n; hooks.renderAll(false); }, ""],
    (n.t === "file" || n.t === "seq") && ["▣", "Quick Look", () => hooks.preview(n), "Space"],
    "-",
    ["✦", "Ask the agent about this", () => hooks.showAgent(), ""],
    (!locked && !isRoot && n.t !== "rest" && n.t !== "agg" && n.t !== "seq") && [S.staged.has(n.id) ? "−" : "+", S.staged.has(n.id) ? "Remove from tray" : "Add to tray", () => hooks.toggleStage(n), "A", "cta"],
    "-",
    n.path && ["⌖", "Reveal in Finder", () => reveal(n), ""],
    ["⧉", "Copy path", () => { navigator.clipboard && navigator.clipboard.writeText(p); toast("Copied " + p); }, ""],
    (!locked && !isRoot && n.path) && ["🔒", "Protect — never suggest this", () => protect(n), ""],
    ["⌸", "Show in tree", () => hooks.showInTree(n), ""],
  ].filter(Boolean);
  const cm = $("cm");
  cm.innerHTML = `<div class="hd"><b>${esc(nodeName(n))}</b><span>${fmt(n.s)} · ${CLS_LABEL[k]}</span></div>` +
    items.map((it, i) => it === "-" ? '<div class="sepr"></div>' : `<div class="mi ${it[4] || ""}" role="menuitem" tabindex="-1" data-i="${i}"><i>${it[0]}</i>${it[1]}<kbd>${it[3] || ""}</kbd></div>`).join("") +
    (locked ? `<div class="sepr"></div><div class="note">🔒 Protected — shown so you understand it; never deletable here.</div>` : "");
  cm.querySelectorAll(".mi").forEach(x => x.onclick = () => { closeMenu(); items[+x.dataset.i][2](); });
  cm.classList.add("on");
  const W = cm.offsetWidth || 250, H = cm.offsetHeight || 320;
  cm.style.left = Math.min(e.clientX, innerWidth - W - 8) + "px"; cm.style.top = Math.min(e.clientY, innerHeight - H - 8) + "px";
  $("tip").classList.remove("on"); const f = cm.querySelector(".mi"); f && f.focus();
}
export function closeMenu() {
  const cm = $("cm"); cm.classList.remove("on");
  if (cm.contains(document.activeElement)) document.activeElement.blur();  // a hidden menu item must not keep eating keys
}
export const menuOpen = () => $("cm").classList.contains("on");
export function initMenu() {
  const cm = $("cm");
  addEventListener("mousedown", e => { if (!cm.contains(e.target)) closeMenu(); }); addEventListener("blur", closeMenu);
  cm.addEventListener("keydown", e => {
    const its = [...cm.querySelectorAll(".mi:not(.dis)")]; const i = its.indexOf(document.activeElement);
    if (e.key === "ArrowDown") { e.preventDefault(); its[(i + 1) % its.length].focus(); }
    if (e.key === "ArrowUp") { e.preventDefault(); its[(i - 1 + its.length) % its.length].focus(); }
    if (e.key === "Enter") { e.preventDefault(); document.activeElement.click(); }
    if (e.key === "Escape") { e.stopPropagation(); closeMenu(); }
  });
}

export function initReview() {
  ["ovClose", "ovCancel"].forEach(id => $(id).onclick = () => $("ov").classList.remove("on"));
  $("reviewBtn").onclick = () => import("./review.js").then(m => m.open()).catch(e => toast(e.status === 400 ? "Nothing reviewable is staged" : "Couldn't build the plan"));
}
