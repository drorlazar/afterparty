// Shared state + helpers for every module (avoids circular imports).
export const S = {
  root: null, loc: null, sel: null, hist: [], fut: [], depth: 2, mode: "safety",
  staged: new Map(), expanded: new Set(), status: null, hidden: null, tab: "details", hover: null, processing: new Set(), mole: new Set(),
};
export const SET = Object.assign({ anim: true, locked: true, hatch: true },
  JSON.parse(localStorage.getItem("cleanup.settings") || "{}"));
export const saveSet = () => localStorage.setItem("cleanup.settings", JSON.stringify(SET));

// Late-bound cross-module calls (set by app.js).
export const hooks = { renderAll: () => {}, go: () => {}, draw: () => {}, applyLayout: () => {}, toggleStage: () => {} };

export const $ = id => document.getElementById(id);
export const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
export const isLight = () => document.documentElement.dataset.theme === "light";

// Every file/folder name is untrusted text: escape before it touches innerHTML.
export const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export const fmt = b => {
  if (b == null) return "—";
  if (b < 1024) return b + " B";
  const u = ["KB", "MB", "GB", "TB"]; let i = -1;
  do { b /= 1024; i++; } while (b >= 1024 && i < 3);
  return (b >= 100 ? b.toFixed(0) : b >= 10 ? b.toFixed(1) : b.toFixed(2)) + " " + u[i];
};
export const age = n => { if (!n || !n.m) return null; const d = Math.round((Date.now() / 1000 - n.m) / 86400); return d < 0 ? 0 : d; };
export const ago = t => { const s = Math.max(0, Date.now() / 1000 - t); return s < 90 ? "just now" : s < 5400 ? Math.round(s / 60) + " min ago" : s < 129600 ? Math.round(s / 3600) + " h ago" : Math.round(s / 86400) + " d ago"; };

export const KIND_EXT = { png: "image", jpg: "image", jpeg: "image", exr: "image", webp: "image", heic: "image", psd: "image",
  mp4: "video", mov: "video", webm: "video", mkv: "video", zip: "archive", dmg: "archive", gz: "archive", tar: "archive",
  glb: "3d", fbx: "3d", blend: "3d", aep: "project", pdf: "doc", json: "data", sqlite: "data", db: "data", bin: "data",
  gguf: "model", safetensors: "model", pt: "model", onnx: "model" };
export function kindOfName(name, kind) {
  if (kind === "dir") return /\.app$/.test(name) ? "app" : "folder";
  if (kind === "link") return "link";
  return KIND_EXT[(name.split(".").pop() || "").toLowerCase()] || "file";
}
export function kindOf(n) {
  if (n.t === "seq") return kindOfName((n.first || "").split("/").pop(), "file");
  if (n.t === "rest" || n.t === "agg") return "group";
  if (n.t === "file") return kindOfName(n.n, "file");
  return /\.app$/.test(n.n) ? "app" : "folder";
}
export const ICON = { folder: ["#8a7a66", "▤"], app: ["#8f9aad", "◆"], image: ["#b48ad0", "▣"], video: ["#d08a92", "▶"],
  archive: ["#d6b46a", "▦"], model: ["#7fa6d6", "◈"], "3d": ["#7fc4a8", "⬢"], data: ["#a8b878", "≡"], doc: ["#c9b07a", "¶"],
  file: ["#9a8f86", "·"], group: ["#6a625b", "…"], project: ["#d69a5a", "◎"], link: ["#6a625b", "↪"] };

// Server classes → visual keys.
const CLS = { regenerable: "regen", review: "review", protected: "locked", unknown: "none" };
export const clsKey = n => (!n || !n.parent || n.t === "rest" || n.t === "agg" || n.t === "seq") ? "none" : (CLS[n.cls] || "none");
export const CLS_LABEL = { regen: "Regenerable", review: "Review", locked: "Protected", none: "Unclassified" };
export const PAL = {
  dark: { regen: ["#2b7d62", "#1d5544"], review: ["#7d6539", "#56452a"], locked: ["#454e5d", "#323844"], none: ["#3d3632", "#2b2623"] },
  light: { regen: ["#a7dcc6", "#8bcab0"], review: ["#ecd6a8", "#dfc289"], locked: ["#cfd5e0", "#bcc4d2"], none: ["#e7e3df", "#d9d4cf"] },
};
export const saf = () => PAL[isLight() ? "light" : "dark"];
export const isUnder = (a, b) => { for (let x = a; x; x = x.parent) if (x === b) return true; return false; };
export const nodeName = n => (!n.parent ? "Macintosh HD" : n.n);
