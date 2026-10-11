// Mole lens: what Mole's dry run would clean, re-checked by Afterparty's rules. Selecting goes to the normal review.
import { S, hooks, $, esc, fmt, ago } from "./core.js";
import { get, post } from "./api.js";

let data = null, poll = null;
const home = () => (S.status && S.status.home) || "";
const short = p => home() && p.startsWith(home() + "/") ? "~" + p.slice(home().length) : p;

export function initMole() {
  $("bMole").onclick = openMole;
  $("moleClose").onclick = () => { $("moleOv").classList.remove("on"); };
  $("moleRefresh").onclick = async () => { try { await post("/api/mole/refresh", {}); } catch (e) {} load(); };
  $("moleReview").onclick = () => {
    const paths = [...document.querySelectorAll("#moleList input[data-mp]:checked")].map(x => x.dataset.mp);
    if (paths.length) { $("moleOv").classList.remove("on"); hooks.openReview(paths); }
  };
  load(true);
}

async function openMole() { $("moleOv").classList.add("on"); await load(); }

async function load(quiet) {
  try { data = await get("/api/mole"); } catch (e) { return; }
  S.mole = new Set(data.items.filter(i => i.stageable && !i.gone).map(i => i.path)); hooks.draw();
  if (quiet) return;
  render();
  clearTimeout(poll); if (data.running) poll = setTimeout(load, 2000);
}

function render() {
  const d = data;
  $("moleMeta").textContent = !d.available ? "" : d.running ? " · Running Mole… (a few minutes)" : d.finished ? ` · found ${ago(d.finished)}` : " · not run yet";
  $("moleRefresh").disabled = !d.available || d.running;
  if (!d.available) { $("moleList").innerHTML = `<div class="ln"><span></span><span>Mole isn't installed — <code>brew install mole</code></span></div>`; return; }
  const groups = {};
  for (const i of d.items) (groups[i.category] = groups[i.category] || []).push(i);
  $("moleList").innerHTML = (d.errors || []).map(e => `<div class="ln"><span class="act perm">ERROR</span><span>${esc(e)}</span></div>`).join("") +
    (Object.entries(groups).map(([cat, xs]) => `<div class="ln grp"><span></span><span>${esc(cat)}</span><span>${fmt(xs.reduce((a, x) => a + (x.size || 0), 0))}</span><span></span><span></span></div>` +
      xs.map(i => {
        const ok = i.stageable && !i.gone;
        const rules = i.gone ? "already gone" : !i.stageable ? `<b style="color:#dc2626">Rules say ${esc(i.rules.class)} — not offered</b>` : `${esc(i.rules.class)} · ${esc(i.rules.rule)}`;
        return `<div class="ln${ok ? "" : " dim"}"><span>${ok ? `<input type="checkbox" data-mp="${esc(i.path)}" data-s="${i.size || 0}">` : ""}</span>
          <span class="mono" title="${esc(i.path)}">${esc(short(i.path))}</span><span>${fmt(i.size)}</span><span class="why2">${rules}</span><span class="why2">${esc(i.source)}</span></div>`;
      }).join("")).join("") || `<div class="ln"><span></span><span class="why2">${d.finished ? "Mole found nothing to suggest." : "Press Refresh to run Mole's dry run."}</span></div>`);
  const upd = () => {
    const sel = [...document.querySelectorAll("#moleList input[data-mp]:checked")];
    $("moleSel").textContent = sel.length ? `${sel.length} selected · ${fmt(sel.reduce((a, x) => a + +x.dataset.s, 0))}` : "";
    $("moleReview").disabled = !sel.length; $("moleReview").textContent = sel.length ? `Review ${sel.length} selected →` : "Review selected →";
  };
  document.querySelectorAll("#moleList input[data-mp]").forEach(x => x.onchange = upd); upd();
}
