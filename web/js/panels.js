// Side panels: crumbs, tree, contents table (paged from the API), inspector (live verdicts), capacity, tray, banners.
import { S, hooks, $, esc, fmt, age, ago, kindOf, kindOfName, ICON, clsKey, CLS_LABEL, saf, nodeName } from "./core.js";
import { get } from "./api.js";
import { renderAgent } from "./agent.js";
import { byId, pathOf, register } from "./model.js";

/* ---------------- crumbs + sibling dropdown ---------------- */
export function renderCrumbs() {
  const c = $("crumbs"); const chain = []; for (let x = S.loc; x; x = x.parent) chain.unshift(x);
  c.innerHTML = ""; const show = chain.length > 6 ? [chain[0], { ell: 1 }, ...chain.slice(-4)] : chain;
  for (const n of show) {
    if (n.ell) { const b = el("button", "crumb", "…"); b.onclick = e => dropdown(e, chain.slice(1, -4), x => hooks.go(x, { anim: false })); c.appendChild(b); continue; }
    const b = el("button", "crumb" + (n === S.loc ? " last" : ""), nodeName(n)); b.onclick = () => hooks.go(n, { anim: false }); c.appendChild(b);
    if (n.c && n.c.some(k => k.t === undefined)) {
      const v = el("button", "chev", "›"); v.title = "Jump to a sibling folder";
      v.onclick = e => dropdown(e, n.c.filter(k => k.t === undefined), x => hooks.go(x, { anim: false })); c.appendChild(v);
    }
  }
  $("bBack").disabled = !S.hist.length; $("bFwd").disabled = !S.fut.length; $("bUp").disabled = !S.loc.parent;
}
function el(tag, cls, text) { const e = document.createElement(tag); e.className = cls; e.textContent = text; return e; }
export function dropdown(e, items, cb, render) {
  const d = $("dd");
  d.innerHTML = render ? render(items) : (items.slice(0, 80).map((n, i) => `<div class="it" data-i="${i}">${esc(n.n)}<span>${fmt(n.s)}</span></div>`).join("") || '<div class="it">No subfolders</div>');
  const r = e.currentTarget.getBoundingClientRect(); d.style.left = Math.min(r.left, innerWidth - 340) + "px"; d.style.top = (r.bottom + 4) + "px"; d.style.display = "block";
  d.querySelectorAll(".it[data-i]").forEach(x => x.onclick = () => { d.style.display = "none"; cb && cb(items[+x.dataset.i]); });
  setTimeout(() => addEventListener("click", function h(ev) { if (!d.contains(ev.target)) { d.style.display = "none"; removeEventListener("click", h); } }), 0);
}

/* ---------------- tree ---------------- */
export function renderTree() {
  const box = $("tree"); const rows = [];
  (function walk(n, lvl) {
    rows.push([n, lvl]); if (rows.length > 600) return;
    if (S.expanded.has(n.id) && n.c) n.c.filter(k => k.t === undefined || k.t === "file").slice(0, 40).forEach(k => walk(k, lvl + 1));
  })(S.root, 0);
  const P = saf();
  box.innerHTML = rows.map(([n, l]) => {
    const parentS = n.parent ? n.parent.s : n.s; const open = n.c === undefined || n.c.length;
    return `<div class="trow${S.sel && S.sel.id === n.id ? " sel" : ""}${S.loc.id === n.id ? " loc" : ""}" data-id="${n.id}" style="padding-left:${8 + l * 14}px">
    <span class="tw" data-tg="${n.id}">${n.t ? "" : open ? (S.expanded.has(n.id) ? "▾" : "▸") : ""}</span>
    <span class="dot" style="background:${P[clsKey(n)][0]}"></span><span class="tn">${esc(nodeName(n))}</span><span class="ts">${fmt(n.s)}</span>
    <i class="tb" style="left:${30 + l * 14}px;width:calc((100% - ${40 + l * 14}px) * ${Math.max(.01, n.s / (parentS || 1))})"></i></div>`;
  }).join("");
  box.querySelectorAll(".trow").forEach(r => {
    const n = byId(+r.dataset.id);
    r.onclick = async e => {
      if (e.target.dataset.tg) {
        if (S.expanded.has(n.id)) S.expanded.delete(n.id);
        else { S.expanded.add(n.id); if (n.c === undefined) { const { ensureLoaded } = await import("./model.js"); await ensureLoaded(n, 1); } }
        renderTree(); return;
      }
      S.sel = n; hooks.renderAll(false);
    };
    r.ondblclick = () => hooks.go(n, { anim: false });
  });
  // reveal the selection by scrolling the tree panel only — scrollIntoView would shift the whole window
  const s = box.querySelector(".trow.sel");
  if (s) {
    const br = box.getBoundingClientRect(), sr = s.getBoundingClientRect();
    if (sr.top < br.top) box.scrollTop -= br.top - sr.top;
    else if (sr.bottom > br.bottom) box.scrollTop += sr.bottom - br.bottom;
  }
}

/* ---------------- contents table (live listing, 200 rows per page) ---------------- */
const listState = { path: null, sort: "size", order: "desc", offset: 0, total: 0, rows: [], seq: 0, loading: false, seqKey: null, seqName: null };
export const listRows = () => listState.rows;
export function setSort(sort) {
  listState.order = listState.sort === sort ? (listState.order === "desc" ? "asc" : "desc") : (sort === "name" ? "asc" : "desc");
  listState.sort = sort; renderContents();
}
function rowNode(e) {  // match a listing row to a model node, or make a light one for small files
  if (e.kind === "seq")  // a frame sequence: display-only group (no path → can't be staged; `first` feeds the preview)
    return register({ n: e.name, s: e.size || 0, m: e.mtime, parent: S.loc, path: null, first: e.path, seq: e.seq, count: e.count,
      t: "seq", c: [], cls: "unknown" });
  const kid = (S.loc.c || []).find(k => k.path === e.path);
  if (kid) return kid;
  return register({ n: e.name, s: e.size || 0, m: e.mtime, parent: S.loc, path: e.path,
    t: e.kind === "dir" ? undefined : "file", c: e.kind === "dir" ? undefined : [], cls: "unknown", dirs: e.kind === "dir" ? 1 : 0 });
}
export async function renderContents(append = false) {
  const body = $("files").querySelector("tbody"); const loc = S.loc;
  $("filesTitle").textContent = "Contents of " + nodeName(loc) + (listState.path === loc.path && listState.seqName ? " › " + listState.seqName : "");
  if (!loc.path || loc.t) { body.innerHTML = ""; $("filesCount").textContent = ""; return; }
  if (listState.path !== loc.path) { listState.seqKey = null; listState.seqName = null; }
  if (!append || listState.path !== loc.path) {
    listState.path = loc.path; listState.offset = 0; listState.rows = []; body.innerHTML = ""; $("filesBody").scrollTop = 0;
    listState.seq++; listState.loading = false;  // a fresh load supersedes anything in flight
  } else if (listState.loading) return;          // only one "load more" at a time
  const seq = listState.seq; listState.loading = true;
  try {
    const q = { path: loc.path, sort: listState.sort, order: listState.order, offset: listState.offset, limit: 200, group: 1 };
    if (listState.seqKey) q.seq = listState.seqKey;
    const r = await get("/api/list", q);
    if (seq !== listState.seq) return;  // superseded by a newer navigation or sort
    listState.total = r.total; listState.offset += r.entries.length;
    const rows = r.entries.map(e => ({ e, n: rowNode(e) })); listState.rows.push(...rows);
    $("filesCount").textContent = `${r.total.toLocaleString()} items · ${fmt(loc.s)} · ${(loc.f || 0).toLocaleString()} files`;
    if (listState.seqKey && !body.querySelector("tr.seqback"))
      body.insertAdjacentHTML("beforeend", `<tr class="seqback"><td colspan="7">← All of ${esc(nodeName(loc))} · showing ${esc(listState.seqName)}</td></tr>`);
    body.insertAdjacentHTML("beforeend", rows.map(({ e, n }) => {
      const k = clsKey(n); const kd = e.kind === "seq" ? kindOfName(e.path.split("/").pop(), "file") : kindOfName(e.name, e.kind); const ic = ICON[kd] || ICON.file; const a = age(n);
      const pct = e.size != null && loc.s ? (e.size / loc.s * 100) : 0;
      const plan = S.staged.has(n.id) ? (k === "regen" ? '<span class="act perm">PERMANENT</span>' : '<span class="act trash">TRASH</span>') : '<span class="na">—</span>';
      return `<tr data-id="${n.id}" class="${S.sel && S.sel.id === n.id ? "sel" : ""}"><td><span class="ic" style="background:${ic[0]}">${ic[1]}</span>${esc(e.name)}${e.kind === "seq" ? '<span class="na"> · sequence</span>' : ""}</td>
      <td class="r num">${fmt(e.size)}</td><td class="r num"><span class="pct"><i style="width:${pct.toFixed(1)}%"></i></span>${pct.toFixed(1)}%</td>
      <td>${kd}</td><td class="r num">${a == null ? "—" : a + "d ago"}</td><td><span class="chip ${k}">${n.cls === "unknown" && !n.rule ? "—" : CLS_LABEL[k]}</span></td><td>${plan}</td></tr>`;
    }).join(""));
    body.querySelectorAll("tr:not([data-bound])").forEach(tr => {
      tr.dataset.bound = 1;
      if (tr.classList.contains("seqback")) { tr.onclick = () => { listState.seqKey = null; listState.seqName = null; renderContents(); }; return; }
      const n = byId(+tr.dataset.id);
      tr.onclick = () => { S.sel = n; hooks.renderAll(false); };
      tr.ondblclick = () => {
        if (n.t === "seq") { listState.seqKey = n.seq; listState.seqName = n.n; renderContents(); return; }
        hooks.go(n, { anim: false });
      };
    });
  } catch (err) {
    if (seq !== listState.seq) return;
    const msg = err.status === 403 ? "Afterparty can't read this folder (macOS permission). Check Full Disk Access." :
      err.status === 404 ? "This folder no longer exists — rescan to refresh." : "Couldn't list this folder.";
    body.innerHTML = `<tr><td colspan="7" class="na" style="padding:14px 10px">${msg}</td></tr>`;
    $("filesCount").textContent = "";
  } finally { if (seq === listState.seq) listState.loading = false; }
}
export function maybeLoadMore() {
  const b = $("filesBody");
  if (listState.offset < listState.total && b.scrollTop + b.clientHeight > b.scrollHeight - 300) renderContents(true);
}
export function markRows() {
  $("files").querySelectorAll("tbody tr").forEach(tr => tr.classList.toggle("sel", !!S.sel && +tr.dataset.id === S.sel.id));
}

/* ---------------- inspector ---------------- */
const verdicts = new Map(); let inspTimer = null;
const CHECK = { passed: ["ok", "✓"], failed: ["wn", "✕"], unavailable: ["na", "○"] };
export function renderInspector() {
  const n = S.sel || S.loc; const box = $("insp"); const k = clsKey(n); const a = age(n);
  if (S.tab === "agent") { renderAgent(box, n); return; }
  const kids = (n.c || []).slice(0, 6); const tot = n.s || 1; const v = n.path && verdicts.get(n.path);
  const vk = v ? ({ regenerable: "regen", review: "review", protected: "locked", unknown: "none" }[v.class]) : k;
  const label = v ? v.label : (n.parent ? CLS_LABEL[k] : "Whole disk");
  const reason = v ? v.reason : (n.parent ? "Checking…" : "Everything the scan could read. Drill in to see what each part is.");
  const checks = v ? v.evidence.map(e => `<span class="${CHECK[e.status][0]}">${CHECK[e.status][1]}</span> <b>${esc(e.check)}</b> ${esc(e.detail)}`).join("<br>") : "";
  const locked = vk === "locked" || !n.parent || n.t === "rest" || n.t === "agg" || n.t === "seq";
  const kindWhy = !n.parent ? "this is the whole disk" : n.t === "rest" ? "this is the remainder of a folder, not a real item" : n.t === "agg" ? "this is a group of small items, not a real item" : n.t === "seq" ? "this is a frame-sequence group, not a single item" : "";
  const addWhy = !locked ? "" : kindWhy ? "Add to tray: " + kindWhy : "Add to tray: this item is protected";
  const noPathWhy = !n.path ? "Finder: " + (kindWhy || "this item has no path on disk") : "";
  const keepWhy = !(n.path && n.parent) ? "Protect: " + (kindWhy || "select a real folder first") : "";
  const a_ = s => s ? ` data-why="${esc(s)}"` : "";
  box.innerHTML = `<div class="insp">
    <div class="ipath">${esc(n.parent ? pathOf(n) : "/")}</div>
    <div class="ititle">${esc(nodeName(n))}</div>
    <div class="isize">${fmt(n.s)} <small>${n.f ? n.f.toLocaleString() + " files" : ""}${a != null ? ` · touched ${a}d ago` : ""}</small></div>
    <div class="chips"><span class="chip ${vk}">${esc(label)}</span><span class="chip">${kindOf(n)}</span>${n.flags & 1 ? '<span class="chip review">unreadable</span>' : ""}${n.flags & 4 ? '<span class="chip review">timed out</span>' : ""}</div>
    <div class="card v ${vk}"><h4>${esc(label)}</h4><p>${esc(reason)}</p><div class="checks">${checks}</div>
      <div class="na" style="font:calc(10.5px*var(--ts)) var(--font-mono);margin-top:6px">${v ? "rules engine · rule " + esc(v.rule) : n.parent ? "loading verdict…" : ""}</div></div>
    ${kids.length ? `<div class="card"><h4>Biggest inside</h4><div class="comp">${kids.map((x, i) => `<i style="width:${x.s / tot * 100}%;background:${["#f59e0b", "#c9a46a", "#8a7a66", "#6b6158", "#544b44", "#3e3833"][i]}"></i>`).join("")}</div>
      <dl class="kv" style="margin-top:9px">${kids.map(x => `<dt>${esc(x.n)}</dt><dd>${fmt(x.s)}</dd>`).join("")}</dl></div>` : ""}
    </div>
    <div class="iact"><button class="btn cta" id="addBtn" ${locked ? "disabled" : ""}${a_(addWhy)}>${S.staged.has(n.id) ? "Remove from tray" : "Add to tray"} <span class="mono" style="opacity:.7">A</span></button><button class="btn" id="finderBtn" title="Reveal in Finder" ${n.path ? "" : "disabled"}${a_(noPathWhy)}>Finder</button><button class="btn" id="keepBtn" title="Protect forever" ${n.path && n.parent ? "" : "disabled"}${a_(keepWhy)}>🔒</button>
    ${vk === "locked" && v ? `<div class="why">Disabled: ${esc(v.reason)}</div>` : ""}</div>`;
  $("addBtn").onclick = () => hooks.toggleStage(n);
  $("finderBtn").onclick = () => hooks.reveal(n);
  $("keepBtn").onclick = () => hooks.protect(n);
  if (n.path && n.parent && !v) {
    clearTimeout(inspTimer);
    inspTimer = setTimeout(async () => {
      try {
        const r = await get("/api/classify", { path: n.path }); verdicts.set(n.path, r); n.cls = r.class; n.rule = r.rule;
        if ((S.sel || S.loc) === n) { renderInspector(); hooks.draw(); }
      } catch (e) { /* keep fast verdict */ }
    }, 120);
  }
}
export const forgetVerdict = p => verdicts.delete(p);

/* ---------------- capacity + hidden space ---------------- */
export function renderCapacity() {
  const d = S.status.disk; const used = d.total - d.free; const mapped = S.root.s;
  $("capn").innerHTML = `${fmt(used)}<small>used of ${fmt(d.total)} · <b style="color:var(--regen)">${fmt(d.free)} free</b></small>`;
  const cols = ["#8b6f4e", "#6f7a86", "#55606e", "#7a6a52", "#4f5a52", "#5a6e8a", "#6b5e7a", "#7a5050"];
  const segs = (S.root.c || []).filter(k => k.s > 0).slice(0, 8);
  $("capbar").innerHTML = segs.map((k, i) => `<i title="${esc(k.n)} · ${fmt(k.s)}" style="width:${Math.min(100, k.s / d.total * 100)}%;background:${cols[i % cols.length]}"></i>`).join("");
  const h = S.hidden; const chip = $("hidden");
  if (!h) { chip.innerHTML = "◌ Hidden space · checking…"; return; }
  chip.innerHTML = `◌ Hidden space <b style="color:var(--text-secondary)">${fmt(h.unattributed)}</b> · ${h.items.length} explained · details`;
  chip.style.cursor = "pointer";
  chip.onclick = e => dropdown(e, h.items, null, items => `<div class="it" style="cursor:default"><b>Used ${fmt(h.used)}</b><span>mapped ${fmt(h.mapped)}</span></div>` +
    items.map(i => `<div class="it" style="cursor:default;flex-direction:column;align-items:flex-start;gap:2px"><div style="display:flex;justify-content:space-between;width:100%"><b>${esc(i.label)}</b><span>${i.bytes == null ? "—" : fmt(i.bytes)}</span></div><span style="font-family:var(--font-ui)">${esc(i.detail)}</span></div>`).join("") +
    `<div class="it" style="cursor:default"><b>Unattributed</b><span>${fmt(h.unattributed)}</span></div>`);
}

/* ---------------- tray ---------------- */
export function renderTray() {
  let perm = 0, trash = 0; for (const n of S.staged.values()) clsKey(n) === "regen" ? perm += n.s : trash += n.s;
  $("trayBig").textContent = S.staged.size ? `${S.staged.size} item${S.staged.size > 1 ? "s" : ""} · ${fmt(perm + trash)}` : "Tray empty";
  $("traySub").innerHTML = S.staged.size ? `Frees up to <b>${fmt(perm)}</b> now · <b>${fmt(trash)}</b> goes to Trash (frees space when emptied)` : "Select anything and press <b>A</b> to stage it for review";
  $("pillPerm").textContent = "Permanent " + fmt(perm); $("pillTrash").textContent = "Trash " + fmt(trash);
  $("reviewBtn").disabled = !S.staged.size; $("reviewBtn").textContent = S.staged.size ? `Review ${fmt(perm + trash)}…` : "Review…";
  $("trayInfo").onclick = e => openTrayPanel(e);
}

/* tray panel: see what's staged, remove single items or clear it all */
export function openTrayPanel(e) {
  e.stopPropagation();
  const items = [...S.staged.values()];
  const d = $("dd"); d.classList.add("traypanel");
  const MODE = n => clsKey(n) === "regen" ? '<span class="act perm">PERMANENT</span>' : '<span class="act trash">TRASH</span>';
  d.innerHTML = !items.length ? '<div class="it" style="cursor:default">The tray is empty — select something and press <b>A</b></div>' :
    `<div class="it" style="cursor:default;justify-content:space-between"><b>${items.length} staged · ${fmt(items.reduce((a, n) => a + n.s, 0))}</b><button class="btn" id="trayClear" style="height:26px">Clear tray</button></div>` +
    items.map((n, i) => `<div class="it" data-i="${i}">${MODE(n)}<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(pathOf(n))}">${esc(nodeName(n))}</span><span>${fmt(n.s)}</span><button class="x" data-x="${i}" title="Remove from tray">✕</button></div>`).join("");
  const r = $("trayInfo").getBoundingClientRect();
  d.style.left = r.left + "px"; d.style.display = "block";
  d.style.top = Math.max(8, r.top - d.offsetHeight - 6) + "px";
  const close = () => { d.style.display = "none"; d.classList.remove("traypanel"); };
  d.querySelectorAll(".x").forEach(b => b.onclick = ev => { ev.stopPropagation(); S.staged.delete(items[+b.dataset.x].id); hooks.renderAll(false); openTrayPanel(e); });
  d.querySelectorAll(".it[data-i]").forEach(x => x.onclick = () => { close(); const n = items[+x.dataset.i]; hooks.go(n.parent || n, { anim: false }); S.sel = n; hooks.renderAll(false); });
  const c = $("trayClear"); if (c) c.onclick = ev => { ev.stopPropagation(); S.staged.clear(); hooks.renderAll(false); close(); };
  setTimeout(() => addEventListener("click", function h(ev) { if (!d.contains(ev.target)) { close(); removeEventListener("click", h); } }), 0);
}

/* ---------------- banners + scan meta ---------------- */
export function renderFdaBanner() {
  const b = $("fdaBanner"); const st = S.status;
  if (st.fda) { b.hidden = true; return; }
  b.hidden = false;
  b.innerHTML = `<b>Full Disk Access is off</b> — some folders can't be read, so the map is incomplete. Add ${st.fda_help.binary.endsWith("/Python.app") ? "Python.app from " : ""}<code>${esc(st.fda_help.binary)}</code>${st.fda_help.which ? ` (${esc(st.fda_help.which)}; System Settings may list two Python.app entries, so add the one at exactly this path)` : ""} · ${esc(st.fda_help.steps)} <button class="btn" id="copyFda" style="height:26px">Copy path</button>`;
  $("copyFda").onclick = () => navigator.clipboard && navigator.clipboard.writeText(st.fda_help.binary);
}
export function renderScanMeta(text) {
  const s = S.status && S.status.scan;
  const t = text || (s ? `Scanned ${ago(s.finished)} · ${s.files.toLocaleString()} files${s.status === "partial" ? " · partial" : ""}` : "No snapshot yet");
  $("scanMeta").textContent = t; $("scanMeta").title = t; $("bRescan").title = `Rescan disk (R) — ${t}`;  // still readable when narrow windows hide it
}
