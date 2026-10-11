import { get } from "./api.js";

let ROOT = null, nextId = 1;
const IDX = new Map();

function adapt(a, parent) {
  const n = {
    id: nextId++, n: a.kind === "rest" ? "(smaller files)" : (parent ? a.name : "Macintosh HD"),
    s: a.size, f: a.files, m: a.mtime, parent, path: a.path, cls: a.cls || "unknown", rule: a.rule || "",
    flags: a.flags || 0, dirs: a.dirs || 0,
    t: a.kind === "file" ? "file" : a.kind === "rest" ? "rest" : undefined,
  };
  // c: children array, [] = known empty, undefined = not loaded yet (lazy). Files-only folders are lazy too.
  n.c = a.children ? a.children.map(k => adapt(k, n)) : (n.t ? [] : undefined);
  IDX.set(n.id, n);
  return n;
}

export async function initModel(status) {
  IDX.clear();
  const t = await get("/api/tree", { path: status.scan.root, depth: 3 });
  ROOT = adapt(t, null);
  return ROOT;
}

export const rootNode = () => ROOT;
export const byId = id => IDX.get(id);
export const pathOf = n => n.path || (n.parent ? pathOf(n.parent) : "/");

export function register(n) { if (n.id == null) n.id = nextId++; IDX.set(n.id, n); return n; }

// Make sure `node` has children loaded `levels` deep (fetches only when something is missing).
export async function ensureLoaded(node, levels = 2) {
  const need = (x, d) => d > 0 && x.t === undefined && (x.c === undefined || x.c.some(k => need(k, d - 1)));
  if (!node || node.t || !node.path || !need(node, levels)) return;
  const t = await get("/api/tree", { path: node.path, depth: Math.min(4, levels + 1) });
  mergeKids(node, t.children || []);
}

// Merge fresh API children into an existing node, reusing child objects by path so selection,
// staged items and inspector verdicts stay attached to the same objects.
function mergeKids(node, apiKids) {
  const old = new Map((node.c || []).map(k => [k.path || "#rest", k]));
  node.c = apiKids.map(a => {
    const k = old.get(a.path || "#rest");
    if (!k) return adapt(a, node);
    Object.assign(k, { s: a.size, f: a.files, m: a.mtime, dirs: a.dirs || 0, flags: a.flags || 0 });
    if (a.cls) { k.cls = a.cls; k.rule = a.rule || k.rule; }
    if (a.children) { if (k.c === undefined) k.c = []; mergeKids(k, a.children); }
    return k;
  });
}
