const CSRF = document.querySelector('meta[name="csrf"]').content;
let worked = false, lost = null;
export function onDisconnect(cb) { lost = cb; }

async function req(method, path, params, body) {
  const url = path + (params ? "?" + new URLSearchParams(params) : "");
  let r;
  try {
    r = await fetch(url, {
      method, credentials: "same-origin",
      headers: method === "POST" ? { "X-Cleanup-CSRF": CSRF, "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (e) { if (worked && lost) lost(); throw e; }
  if (r.status === 401 && worked && lost) lost();  // token rejected = server restarted; 403 = unreadable folder
  if (!r.ok) { const err = new Error(`${r.status} ${path}`); err.status = r.status; err.data = await r.json().catch(() => null); throw err; }
  worked = true;
  return r.json();
}

export const get = (p, q) => req("GET", p, q);
export const post = (p, b) => req("POST", p, null, b || {});

export function events(onEvent) {
  const es = new EventSource("/api/scan/events", { withCredentials: true });
  es.onmessage = e => { const d = JSON.parse(e.data); onEvent(d); if (d.state !== "running") es.close(); };
  es.onerror = () => { es.close(); if (worked && lost) lost(); };
  return es;
}
