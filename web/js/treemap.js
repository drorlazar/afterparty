// Squarified, nested, canvas treemap (ported from the approved mockup v3).
import { S, SET, hooks, css, isLight, fmt, age, kindOf, clsKey, saf } from "./core.js";
import { ensureLoaded } from "./model.js";

// Mole lens: dashed teal outline on tiles Mole would clean (and our rules agree may go), unless the list is open on top.
function moleOutline(cx, rr, head, r) {
  if (!S.mole || !S.mole.size || !head.path || !S.mole.has(head.path) || SET.moleOutline === false) return;
  cx.save(); cx.setLineDash([5, 4]); cx.strokeStyle = "rgba(45,212,191,.9)"; cx.lineWidth = 2;
  rr(r.x + 2, r.y + 2, r.w - 4, r.h - 4, 5); cx.stroke(); cx.restore();
}

const KINDC = { image: "#6b4a80", video: "#7a3f45", archive: "#7a6236", model: "#2f5b85", "3d": "#3b6b5a", data: "#4c5a3a",
  doc: "#6b5a3a", app: "#4a5466", folder: "#3e3833", file: "#45403b", group: "#2b2724", project: "#7a5636" };
const TOP = ["#5a4636", "#3f4a55", "#4a4535", "#384a42", "#4d3c3c", "#3d4256", "#4b4a3a", "#3b4a4a", "#463d4f"];
const TOP_L = ["#e9dccd", "#d9e1e8", "#e6e0cc", "#d6e6dc", "#ecd9d6", "#dadff0", "#e6e4d2", "#d6e6e6", "#e3dbea"];
export { KINDC };

let cv, cx, DPR = devicePixelRatio || 1;
export let rects = [];
const loading = new Set();
window.__cleanup = Object.assign(window.__cleanup || {}, { rects: () => rects });

export function initTreemap(canvas) { cv = canvas; cx = cv.getContext("2d"); }

function shade(h, a) { const n = parseInt(h.slice(1), 16); return `rgb(${[(n >> 16) + a, ((n >> 8) & 255) + a, (n & 255) + a].map(v => Math.max(0, Math.min(255, v))).join(",")})`; }
function mix(a, b, t) { return a.map((v, i) => Math.round(v + (b[i] - v) * t)); }
function fillFor(n, top) {
  if (S.mode === "safety") return saf()[clsKey(n)];
  if (S.mode === "type") { const c = KINDC[kindOf(n)] || "#3e3833"; return isLight() ? [shade(c, 120), shade(c, 95)] : [c, shade(c, -22)]; }
  if (S.mode === "age") {
    const a = age(n); const t = a == null ? 0 : Math.min(1, a / 180);
    const c = isLight() ? mix([232, 228, 223], [240, 190, 120], t) : mix([62, 52, 46], [168, 104, 38], t);
    return [`rgb(${c})`, `rgb(${c.map(v => Math.round(v * .78))})`];
  }
  const T = isLight() ? TOP_L : TOP; const c = T[(top || 0) % T.length]; return [c, shade(c, -18)];
}

export function squarify(items, x, y, w, h) {
  const total = items.reduce((a, b) => a + b.s, 0); const out = []; if (!total || w < 1 || h < 1) return out;
  const r = { x, y, w, h }; const scale = (w * h) / total; let row = [];
  const worst = (row, len) => { let s = 0, mx = 0, mn = Infinity; for (const i of row) { const a = i.s * scale; s += a; mx = Math.max(mx, a); mn = Math.min(mn, a); } return Math.max(len * len * mx / (s * s), (s * s) / (len * len * mn)); };
  const lay = row => {
    const s = row.reduce((a, b) => a + b.s * scale, 0); const hz = r.w >= r.h; const len = hz ? r.h : r.w; const th = s / len; let o = 0;
    for (const i of row) { const l = i.s * scale / th; out.push(hz ? { n: i, x: r.x, y: r.y + o, w: th, h: l } : { n: i, x: r.x + o, y: r.y, w: l, h: th }); o += l; }
    if (hz) { r.x += th; r.w -= th; } else { r.y += th; r.h -= th; }
  };
  for (const it of items) { const len = Math.min(r.w, r.h); if (!row.length || worst([...row, it], len) <= worst(row, len)) row.push(it); else { lay(row); row = [it]; } }
  if (row.length) lay(row); return out;
}

// Fold tiles that would be < 7px on a side into one "N smaller items" group.
export function kidsFor(n, w, h) {
  const kids = (n.c || []).filter(k => k.s > 0 && (SET.locked || clsKey(k) !== "locked")); if (!kids.length) return [];
  const total = kids.reduce((a, b) => a + b.s, 0), area = w * h, min = 7 * 7 * 3, keep = [], small = [];
  for (const k of kids) (k.s / total * area >= min || k.t === "rest" ? keep : small).push(k);
  if (small.length > 1) {
    const s = small.reduce((a, b) => a + b.s, 0);
    keep.push({ n: `${small.length} smaller items`, s, t: "agg", c: small, parent: n, id: "agg" + n.id, m: Math.max(...small.map(x => x.m || 0)) });
  } else keep.push(...small);
  return keep.sort((a, b) => b.s - a.s);
}

export function draw() {
  if (!cv || !S.loc) return;
  const W = cv.clientWidth, H = cv.clientHeight; cv.width = W * DPR; cv.height = H * DPR; cx.setTransform(DPR, 0, 0, DPR, 0, 0);
  cx.clearRect(0, 0, W, H); rects = [];
  const maxD = S.depth || 3; const autoD = !S.depth;
  squarify(kidsFor(S.loc, W, H), 0, 0, W, H).forEach((r, i) => node(r, 1, i));

  function node(r, d, top) {
    let n = r.n; const chain = [n.n];
    // collapse chains where one folder holds ≥97% of its parent (e.g. /Users › you)
    while (n.c && n.t !== "agg") { const big = n.c.reduce((m, k) => (k.t === undefined && k.s > (m ? m.s : 0) ? k : m), null); if (big && big.c && big.c.length && big.s >= n.s * .97) { n = big; chain.push(n.n); } else break; }
    const head = r.n;
    const big = d < maxD && r.w > 90 && r.h > 64 && (!autoD || r.w * r.h > 26000);
    if (big && n.c === undefined && n.t === undefined && !loading.has(n.id)) {  // lazy: fetch, then redraw
      loading.add(n.id); ensureLoaded(n, 1).then(() => draw()).catch(() => {});
    }
    const canNest = n.c && n.c.length && n.t !== "agg" && big;
    if (canNest) {
      cx.fillStyle = css("--tile-frame"); rr(r.x + 1, r.y + 1, r.w - 2, r.h - 2, 7); cx.fill();
      cx.strokeStyle = css("--tile-frame-line"); cx.lineWidth = 1; cx.stroke();
      rects.push({ ...r, n: head, d, header: true });
      label({ n: chain.join(" › "), s: head.s }, r.x + 8, r.y + 15, r.w - 16, true);
      squarify(kidsFor(n, r.w - 6, r.h - 26), r.x + 3, r.y + 23, r.w - 6, r.h - 26).forEach(c => node(c, d + 1, top));
      moleOutline(cx, rr, head, r);
    } else {
      const f = fillFor(n, top); const g = cx.createLinearGradient(r.x, r.y, r.x + r.w * .4, r.y + r.h);
      g.addColorStop(0, f[0]); g.addColorStop(1, f[1]); cx.fillStyle = g; rr(r.x + 1, r.y + 1, r.w - 2, r.h - 2, Math.min(6, r.w / 4, r.h / 4)); cx.fill();
      if (n.t === "agg") { cx.save(); cx.clip(); cx.strokeStyle = isLight() ? "rgba(28,25,23,.07)" : "rgba(250,250,249,.06)"; for (let k = -r.h; k < r.w; k += 7) { cx.beginPath(); cx.moveTo(r.x + k, r.y); cx.lineTo(r.x + k + r.h, r.y + r.h); cx.stroke(); } cx.restore(); }
      if (S.processing && head.path && S.processing.has(head.path)) {  // already handled in the running cleanup
        cx.fillStyle = isLight() ? "rgba(250,250,249,.72)" : "rgba(12,10,9,.72)"; rr(r.x + 1, r.y + 1, r.w - 2, r.h - 2, 5); cx.fill();
      }
      moleOutline(cx, rr, head, r);
      if (SET.hatch && S.staged.has(head.id)) { cx.save(); rr(r.x + 1, r.y + 1, r.w - 2, r.h - 2, 5); cx.clip(); cx.strokeStyle = "rgba(245,158,11,.5)"; cx.lineWidth = 2; for (let k = -r.h; k < r.w; k += 9) { cx.beginPath(); cx.moveTo(r.x + k, r.y); cx.lineTo(r.x + k + r.h, r.y + r.h); cx.stroke(); } cx.restore(); }
      rects.push({ ...r, n: head, d });
      if (r.w >= 80 && r.h >= 28) label({ n: chain.join(" › "), s: head.s }, r.x + 8, r.y + 16, r.w - 16, false, r.h >= 48);
      if (clsKey(n) === "locked" && r.w > 40 && r.h > 24) { cx.fillStyle = isLight() ? "rgba(28,25,23,.45)" : "rgba(250,250,249,.45)"; cx.font = "10px sans-serif"; cx.fillText("🔒", r.x + r.w - 18, r.y + 15); }
    }
    if (S.sel && head.id === S.sel.id) { cx.strokeStyle = css("--accent"); cx.lineWidth = 2; rr(r.x + 2, r.y + 2, r.w - 4, r.h - 4, 6); cx.stroke(); cx.strokeStyle = "rgba(245,158,11,.22)"; cx.lineWidth = 5; rr(r.x + 1, r.y + 1, r.w - 2, r.h - 2, 7); cx.stroke(); }
    if (S.hover && head.id === S.hover.id && (!S.sel || S.sel.id !== head.id)) { cx.strokeStyle = isLight() ? "rgba(28,25,23,.5)" : "rgba(250,250,249,.55)"; cx.lineWidth = 1; rr(r.x + 1.5, r.y + 1.5, r.w - 3, r.h - 3, 6); cx.stroke(); }
  }
  function label(n, x, y, w, hdr, withSize) {
    const TS = +(css("--ts") || 1);
    cx.font = `600 ${(hdr ? 11 : 12) * TS}px ${hdr ? "JetBrains Mono" : "Inter"}, sans-serif`; cx.fillStyle = hdr ? css("--tile-hdr") : css("--tile-label");
    const name = clip(n.n, w - (hdr ? 70 : 0)); cx.fillText(name, x, y);
    if (hdr) { cx.font = `500 ${11 * TS}px JetBrains Mono, monospace`; cx.fillStyle = css("--tile-hdr-sub"); const t = fmt(n.s); cx.fillText(t, x + Math.min(w - cx.measureText(t).width, cx.measureText(name).width) + 8, y); }
    else if (withSize) { cx.font = `500 ${11 * TS}px JetBrains Mono, monospace`; cx.fillStyle = css("--tile-sub"); cx.fillText(fmt(n.s), x, y + 16 * TS); }
  }
  function clip(t, w) { let s = t; if (cx.measureText(s).width > w) { while (s.length > 1 && cx.measureText(s + "…").width > w) s = s.slice(0, -1); s += "…"; } return s; }
  function rr(x, y, w, h, r) { r = Math.max(0, Math.min(r, w / 2, h / 2)); cx.beginPath(); cx.moveTo(x + r, y); cx.arcTo(x + w, y, x + w, y + h, r); cx.arcTo(x + w, y + h, x, y + h, r); cx.arcTo(x, y + h, x, y, r); cx.arcTo(x, y, x + w, y, r); cx.closePath(); }
}

export function hit(x, y) { let best = null; for (const r of rects) { if (x >= r.x && x < r.x + r.w && y >= r.y && y < r.y + r.h) { if (!best || r.d >= best.d) best = r; } } return best; }

// Zoom: scale a snapshot of the clicked rect up to the full map.
export function animateZoom(r, dirIn, done) {
  if (!SET.anim || matchMedia("(prefers-reduced-motion: reduce)").matches || !r) { done(); return; }
  const snap = document.createElement("canvas"); snap.width = cv.width; snap.height = cv.height; snap.getContext("2d").drawImage(cv, 0, 0);
  const W = cv.clientWidth, H = cv.clientHeight, t0 = performance.now(), T = 240;
  (function f(t) {
    const k = Math.min(1, (t - t0) / T), e = 1 - Math.pow(1 - k, 3);
    const sx = dirIn ? r.x * e : r.x * (1 - e), sy = dirIn ? r.y * e : r.y * (1 - e), sw = dirIn ? W + (r.w - W) * e : r.w + (W - r.w) * e, sh = dirIn ? H + (r.h - H) * e : r.h + (H - r.h) * e;
    cx.setTransform(1, 0, 0, 1, 0, 0); cx.clearRect(0, 0, cv.width, cv.height); cx.globalAlpha = 1 - .35 * e;
    if (dirIn) cx.drawImage(snap, sx * DPR, sy * DPR, sw * DPR, sh * DPR, 0, 0, cv.width, cv.height);
    else cx.drawImage(snap, 0, 0, cv.width, cv.height, sx * DPR, sy * DPR, sw * DPR, sh * DPR);
    cx.globalAlpha = 1; if (k < 1) requestAnimationFrame(f); else done();
  })(t0);
}

export function legend() {
  const L = document.getElementById("legend"); const P = saf();
  L.innerHTML = S.mode === "safety" ? [["regen", "Regenerable"], ["review", "Review"], ["locked", "Protected"], ["none", "Unclassified"]].map(([k, t]) => `<span><em style="background:${P[k][0]}"></em>${t}</span>`).join("")
    : S.mode === "type" ? ["image", "video", "model", "archive", "app", "folder"].map(k => `<span><em style="background:${KINDC[k]}"></em>${k}</span>`).join("")
    : S.mode === "age" ? `<span><em style="background:rgb(62,52,46)"></em>recent</span><span><em style="background:rgb(168,104,38)"></em>6+ months idle</span>` : "";
}
