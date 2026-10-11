// Review → (optional overrides) → execute → results → undo.
import { S, hooks, $, esc, fmt } from "./core.js";
import { get, post } from "./api.js";

const SAFER = { permanent: ["permanent", "trash", "keep"], remove: ["remove", "keep"], trash: ["trash", "keep"], keep: ["keep"], skip: ["skip"] };
const LABEL = { permanent: "PERMANENT", remove: "REMOVE", trash: "TRASH", keep: "KEEP", skip: "SKIP" };
const CLASS = { permanent: "perm", remove: "perm", trash: "trash", keep: "keep", skip: "keep" };
let fromTray = true;
let plan = null, orig = {}, overrides = {}, forced = {}, excluded = new Set(), items = {};

const modeOf = (x, parentMode) => forced[x.path] || overrides[x.path] || (parentMode === "keep" ? "keep" : x.mode);

function totals() {
  const t = { permanent: 0, remove: 0, trash: 0, keep: 0, skip: 0, irreversible: 0, forced: 0, likelyFree: 0 };
  for (const i of plan.items) {
    if (excluded.has(i.path)) continue;
    const im = modeOf(i);
    const add = (mode, size, priv) => {
      t[mode] += size;
      if (mode === "permanent" || mode === "remove") { t.irreversible++; t.likelyFree += priv == null ? size : priv; }
    };
    if (forced[i.path]) { t.forced++; add(im, i.size, i.private); continue; }
    if (i.children.length && im !== "skip") for (const c of i.children) add(im === "keep" ? "keep" : modeOf(c, im), c.size, c.private);
    else add(im, i.size, i.private);
  }
  return t;
}

function row(x, depth, parentMode) {
  const m = modeOf(x, parentMode); const isForced = !!forced[x.path];
  const can = !isForced && SAFER[orig[x.path]].length > 1 && parentMode !== "keep";
  const badge = isForced ? `<span class="act forced" title="Your decision overrides the rules">YOUR CALL · ${LABEL[m]}</span>`
                         : `<span class="act ${CLASS[m]}${can ? " cyc" : ""}" title="${can ? "click to make safer" : ""}">${LABEL[m]}</span>`;
  const why = isForced ? `rules said: ${items[x.path].reason}` : x.reason;
  return `<div class="ln${depth ? "" : " grp"}" data-p="${esc(x.path)}">${badge}
    <span style="padding-left:${depth * 16}px;${depth ? "color:var(--text-secondary)" : ""}">${esc(depth ? x.path.split("/").pop() : x.path)}</span><span>${fmt(x.size)}</span>
    <span class="why2" title="${esc(why)}">${esc(why)}</span>${depth ? "<span></span>" : `<button class="rowmenu" data-m="${esc(x.path)}" title="Options">⋯</button>`}</div>`;
}

function render() {
  $("manifest").innerHTML = plan.items.filter(i => !excluded.has(i.path)).map(i =>
    row(i, 0) + (forced[i.path] ? "" : i.children.map(c => row(c, 1, modeOf(i))).join(""))).join("") ||
    `<div class="ln"><span></span><span class="why2">Everything was removed from this plan.</span><span></span><span></span><span></span></div>`;
  $("manifest").querySelectorAll(".act.cyc").forEach(b => b.onclick = () => {
    const p = b.closest(".ln").dataset.p; const opts = SAFER[orig[p]]; const cur = overrides[p] || orig[p];
    const next = opts[(opts.indexOf(cur) + 1) % opts.length]; if (next === orig[p]) delete overrides[p]; else overrides[p] = next; render();
  });
  $("manifest").querySelectorAll(".rowmenu").forEach(b => b.onclick = e => rowMenu(e, b.dataset.m));
  const t = totals();
  $("rvTotals").innerHTML = `<b style="color:var(--accent)">Permanently delete ${fmt(t.permanent)}</b>${t.remove ? ` · <b style="color:var(--accent)">remove worktrees ${fmt(t.remove)}</b>` : ""} · <b style="color:var(--review)">move ${fmt(t.trash)} to Trash</b> · <b style="color:var(--regen)">keep ${fmt(t.keep)}</b>${t.skip ? ` · skip ${fmt(t.skip)}` : ""}${t.forced ? ` · <b style="color:#dc2626">${t.forced} override${t.forced > 1 ? "s" : ""}</b>` : ""}${
    t.permanent + t.remove > 0 && t.likelyFree < 0.9 * (t.permanent + t.remove) ? ` · <span title="APFS shares blocks between cloned files; deleting one copy frees only what it doesn't share">frees at least ${fmt(t.likelyFree)} now (up to ${fmt(t.permanent + t.remove)} if nothing else shares it)</span>` : ""}`;
  const go = $("rvGo"), ack = $("ack");
  ack.closest(".ack").style.display = t.irreversible ? "" : "none";
  let conf = $("confirmDel");
  if (t.forced && !conf) {
    $("rvGo").insertAdjacentHTML("beforebegin", `<input class="confirmdel" id="confirmDel" placeholder="type delete" autocomplete="off" spellcheck="false">`);
    conf = $("confirmDel"); conf.oninput = render;
  } else if (!t.forced && conf) { conf.remove(); conf = null; }
  const parts = [];
  if (t.permanent) parts.push(`Permanently delete ${fmt(t.permanent)}`);
  if (t.remove) parts.push(`remove worktrees ${fmt(t.remove)}`);
  if (t.trash) parts.push(`${parts.length ? "move" : "Move"} ${fmt(t.trash)} to Trash`);
  go.textContent = parts.join(" + ") || "Nothing to do";
  go.disabled = (t.irreversible === 0 && t.trash === 0) || (t.irreversible > 0 && !ack.checked) || (t.forced > 0 && (!conf || conf.value.trim() !== "delete"));
  go.title = t.forced ? "You're overriding the rules for some items — type 'delete' to confirm" : "";
  ack.onchange = render; go.onclick = run;
}

function rowMenu(e, path) {
  e.stopPropagation();
  const it = items[path]; const d = $("dd"); d.classList.remove("traypanel");
  const cur = modeOf(it);
  const opts = [];
  if (forced[path]) opts.push(["↺", "Undo my override", () => { delete forced[path]; }]);
  else if (it.overridable) {
    // your overrides only ever move things to the Trash; only the rules delete for good (the server enforces the same)
    if (cur !== "trash" && cur !== "permanent") opts.push(["🗑", "Move to the Trash anyway", () => { forced[path] = "trash"; }]);
  }
  opts.push(["✕", "Remove from this plan", () => { excluded.add(path); delete forced[path]; }]);
  d.innerHTML = (it.overridable || forced[path] ? "" : `<div class="it" style="cursor:default;color:var(--text-faint)">Can't override: ${esc(it.floor_reason || "not overridable")}</div>`) +
    opts.map((o, i) => `<div class="it" data-i="${i}"><span>${o[0]}&nbsp; ${o[1]}</span></div>`).join("");
  const r = e.currentTarget.getBoundingClientRect(); d.style.display = "block";
  d.style.left = Math.min(r.left - 250, innerWidth - 300) + "px"; d.style.top = (r.bottom + 4) + "px"; d.style.zIndex = 99;
  d.querySelectorAll(".it[data-i]").forEach(x => x.onclick = ev => { ev.stopPropagation(); d.style.display = "none"; opts[+x.dataset.i][2](); render(); });
  setTimeout(() => addEventListener("click", function h(ev) { if (!d.contains(ev.target)) { d.style.display = "none"; removeEventListener("click", h); } }), 0);
}

async function run() {
  const t = totals(); $("rvGo").disabled = true;
  const ov = { ...overrides }; for (const p of excluded) ov[p] = "keep";
  const body = { ack: t.irreversible > 0 ? $("ack").checked : false, overrides: ov, forced };
  if (t.forced) body.confirm = ($("confirmDel") || {}).value?.trim();
  try { await post(`/api/plan/${plan.id}/execute`, body); }
  catch (e) {
    $("manifest").insertAdjacentHTML("afterbegin", `<div class="ln"><span class="act perm">ERROR</span><span>${e.status === 409 ? "Another scan or cleanup is running" : "Couldn't start the cleanup"}</span><span></span><span></span><span></span></div>`);
    render(); return;
  }
  hooks.watchActivity && hooks.watchActivity(true);   // tray activity bar + Dock progress, even if this window closes
  $("manifest").innerHTML = `<div class="ln grp"><span class="act trash">RUNNING</span><span id="rvNow">Starting…</span><span></span><span></span><span></span></div><div class="scanbar"><i id="rvBar"></i></div>
    <div class="ln"><span></span><span class="why2">You can close this window — progress continues in the tray.</span><span></span><span></span><span></span></div>`;
  $("confirmDel") && $("confirmDel").remove();
  const es = new EventSource(`/api/plan/${plan.id}/events`, { withCredentials: true });
  es.onmessage = e => {
    const d = JSON.parse(e.data);
    if (d.state === "running") { const n = $("rvNow"); if (n) { n.textContent = `${d.done}/${d.total} · ${d.path}`; $("rvBar").style.width = (d.done / d.total * 100) + "%"; } return; }
    es.close(); results(d);
  };
}

function results(d) {
  if (d.state !== "done") { $("manifest").innerHTML = `<div class="ln grp"><span class="act perm">ERROR</span><span>${esc(d.error || "failed")}</span><span></span><span></span><span></span></div>`; return; }
  if (fromTray) { S.staged.clear(); hooks.renderAll(false); }   // what was in the tray has been handled
  const r = d.result; const sec = (k, cls, label) => r[k].length ? `<div class="ln grp"><span class="act ${cls}">${label}</span><span>${r[k].length} items</span><span>${fmt(r[k].reduce((a, x) => a + (x.size || 0), 0))}</span><span></span><span></span></div>` + r[k].map(x => `<div class="ln"><span></span><span style="padding-left:16px;color:var(--text-secondary)">${esc(x.path)}</span><span>${fmt(x.size)}</span><span class="why2">${esc(x.reason || x.trash_path || "")}</span><span></span></div>`).join("") : "";
  $("manifest").innerHTML = sec("deleted", "perm", "DELETED") + sec("trashed", "trash", "TRASHED") + sec("skipped", "keep", "SKIPPED") + sec("failed", "perm", "FAILED");
  $("rvTotals").innerHTML = `<b style="color:var(--regen)">Freed ${fmt(r.freed)}</b> now · ${fmt(r.trash_bytes)} in Trash (frees space when emptied)`;
  const go = $("rvGo"); $("ack").closest(".ack").style.display = "none";
  go.disabled = !r.trashed.some(x => x.trash_path); go.textContent = "Undo trashed items";
  go.onclick = async () => {
    go.disabled = true;
    try { const u = await post(`/api/plan/${plan.id}/undo`); go.textContent = `Restored ${u.restored.length}` + (u.conflict.length ? ` · ${u.conflict.length} conflicts (kept the newer file)` : "") + (u.missing.length ? ` · ${u.missing.length} already gone` : "") + (u.refused.length ? ` · ${u.refused.length} left in the Trash (can't confirm it's the same file — restore it from Finder)` : ""); hooks.reloadModel && hooks.reloadModel(); }
    catch (e) { go.textContent = e.status === 409 ? "Wait — a cleanup is running" : "Undo failed"; go.disabled = false; }
  };
}

export async function open(paths = null) {
  fromTray = !paths;
  paths = paths || [...S.staged.values()].map(n => n.path).filter(Boolean);
  plan = await post("/api/plan", { paths }); overrides = {}; forced = {}; orig = {}; items = {}; excluded = new Set();
  for (const i of plan.items) { orig[i.path] = i.mode; items[i.path] = i; for (const c of i.children) orig[c.path] = c.mode; }
  $("ack").checked = false;
  $("ov").classList.add("on"); render();
}
