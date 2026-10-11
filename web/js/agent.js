// Agent tab: ask headless Claude what something is. It can only look — proposals go through the normal review.
import { S, hooks, $, esc, fmt, nodeName } from "./core.js";
import { get, post } from "./api.js";
import { pathOf } from "./model.js";
import { toast } from "./overlays.js";

const convs = new Map();      // path → { name, turns: [{ q, res }], pending, preview }
let current = null;           // the path whose conversation the Agent tab last showed
const VCLASS = { "safe-to-delete": "regen", review: "review", keep: "locked" };
let agentOff = false;         // the server said agent-off: Ask Claude needs the user's consent first
const isOff = e => !!(e && e.status === 403 && e.data && e.data.error === "agent-off");
let status = null, statusLoading = null;   // Claude Code on this Mac: installed? signed in? (GET /api/agent/status)

export async function loadAgentStatus(refresh = false) {
  if (statusLoading && !refresh) return statusLoading;
  statusLoading = get("/api/agent/status", refresh ? { refresh: 1 } : {}).then(st => { status = st; return st; })
    .catch(() => { status = { installed: false, signed_in: false, problem: "Couldn't check Claude Code." }; return status; });
  const st = await statusLoading; renderStatusLine(); return st;
}
export const agentReady = () => !!(status && status.installed && status.signed_in);

function renderStatusLine() {
  const el = $("agentStatus"); if (!el || !status) return;
  el.innerHTML = agentReady() ? `<b style="color:var(--regen)">Ready</b> · Claude Code ${esc(status.version || "")} · ${esc(status.method || "")}`
    : `<b style="color:var(--review)">Not set up</b> · ${esc(status.problem || "")}`;
  const m = $("agentModel"); if (m && status.model) m.value = status.model;
  const dq = $("agentDefaultQ"); if (dq && status.default_question && document.activeElement !== dq) dq.value = status.default_question;
}

export function initAgentSettings() {
  $("agentRecheck").onclick = () => { $("agentStatus").textContent = "Checking Claude Code…"; loadAgentStatus(true).then(() => hooks.renderAll(false)); };
  $("agentDefaultQ").onchange = async e => {
    try { const r = await post("/api/agent/settings", { agent_default_question: e.target.value });
      if (status) status.default_question = r.agent_default_question; e.target.value = r.agent_default_question; toast("Default question saved"); }
    catch (err) { toast("Couldn't save — keep it under 500 characters"); }
  };
  $("agentModel").onchange = async e => { try { await post("/api/agent/settings", { agent_model: e.target.value }); if (status) status.model = e.target.value; } catch (err) {} };
  loadAgentStatus();
}

const CMD = (c) => `<div class="cmd"><code>${esc(c)}</code><button class="btn" data-copy="${esc(c)}" style="height:24px">Copy</button></div>`;
function setupHtml() {
  const st = status || {};
  const install = !st.installed;
  return `<div class="msg a agentsetup"><b style="color:var(--text)">Set up the AI agent</b>
    <p class="why2">The agent runs Claude Code on this Mac with your own Claude account. Afterparty has no AI service of its own and stores no keys.</p>
    ${install ? `<div class="step"><span><b>1. Install Claude Code</b> — open Terminal and paste:</span>${CMD("curl -fsSL https://claude.ai/install.sh | bash")}
      <span class="why2">or with Homebrew:</span>${CMD("brew install --cask claude-code")}</div>` : ""}
    <div class="step"><span><b>${install ? "2" : "1"}. Sign in once</b> — in Terminal run:</span>${CMD("claude")}
      <span class="why2">then type <code>/login</code> and sign in with your Claude Pro or Max account (or an Anthropic API key). You can close it afterwards.</span></div>
    <div class="step"><b>${install ? "3" : "2"}. Come back here</b> <button class="btn cta" id="agentCheck" style="height:26px">Check again</button></div>
    <p class="why2">Each question uses your Claude plan's usage, or costs a few cents with an API key. Only what's listed under "What will be sent", plus files the agent opens itself (read-only), goes to Anthropic.</p>
  </div>`;
}


export function renderAgent(box, n) {
  if (!status) { box.innerHTML = `<div class="insp"><div class="msg a">Checking Claude Code…</div></div>`; loadAgentStatus().then(() => hooks.renderAll(false)); return; }
  if (!agentReady()) {
    box.innerHTML = `<div class="insp agent" style="gap:10px">${setupHtml()}</div>
      <div class="ask"><input id="agentQ" placeholder="Set up Claude Code to ask the agent" disabled><button class="btn cta" id="agentAsk" disabled>Ask</button></div>`;
    $("agentCheck").onclick = () => { $("agentCheck").textContent = "Checking…"; loadAgentStatus(true).then(() => hooks.renderAll(false)); };
    box.querySelectorAll("[data-copy]").forEach(b => b.onclick = () => { navigator.clipboard && navigator.clipboard.writeText(b.dataset.copy); b.textContent = "Copied"; });
    return;
  }
  if (agentOff || status.enabled === false) { renderConsent(box); return; }
  // The tab always follows the selection; every folder/file keeps its own conversation (answers wait for you).
  const path = pathOf(n); current = path;
  const c = conv(path, nodeName(n));
  const dq = (status && status.default_question) || "What is this, and is it safe to remove?";
  const fresh = !c.turns.length && !c.pending;
  box.innerHTML = `<div class="insp agent" style="gap:10px">
    <span class="scope">Regarding ${esc(c.name)}</span>
    ${fresh ? `<button class="btn cta agentgo" id="agentAnalyse" title="${esc(dq)}">✦ Analyse ${esc(c.name)}</button>
      <div class="qchips">${PRESETS.map((q, i) => `<button class="qchip" data-preset="${i}">${esc(q)}</button>`).join("")}</div>
      ${previewHtml(c)}` : ""}
    ${c.turns.map(turnHtml).join("")}
    ${c.pending ? `<div class="msg a agentwait">Analysing ${esc(c.name)}… (up to 3 min — you can keep browsing)</div>` : ""}
    ${!fresh && !c.pending ? `<div class="qchips">${PRESETS.map((q, i) => `<button class="qchip" data-preset="${i}">${esc(q)}</button>`).join("")}</div>` : ""}
  </div>
  <div class="ask"><input id="agentQ" placeholder="${c.turns.length ? "Ask a follow-up" : "Or type your own question"} about ${esc(c.name)}…" ${c.pending ? "disabled" : ""}>
    <button class="btn cta" id="agentAsk" ${c.pending ? "disabled" : ""}>Ask</button></div>`;
  if (!c.preview && fresh) loadPreview(path);
  const go = $("agentAnalyse"); if (go) go.onclick = () => ask(path, dq, false);
  box.querySelectorAll("[data-preset]").forEach(b => b.onclick = () => ask(path, PRESETS[+b.dataset.preset], false));
  $("agentAsk").onclick = () => ask(path, $("agentQ").value.trim() || dq, false);
  $("agentQ").onkeydown = e => { if (e.key === "Enter") $("agentAsk").click(); };
  box.querySelectorAll("[data-refresh]").forEach(b => b.onclick = e => { e.preventDefault(); ask(path, b.dataset.q || dq, true); });
  box.querySelectorAll("[data-protect]").forEach(b => b.onclick = () => hooks.protect({ path: b.dataset.protect, n: b.dataset.protect.split("/").pop() }));
  const rv = $("agentReview");
  if (rv) rv.onclick = () => {
    const paths = [...box.querySelectorAll("input[data-prop]:checked")].map(x => x.dataset.prop);
    if (paths.length) hooks.openReview(paths);
  };
}

function renderConsent(box) {
  box.innerHTML = `<div class="insp agent" style="gap:10px"><div class="msg a agent-consent">
      <b style="color:var(--text)">Ask Claude about this folder</b>
      <p class="why2">Afterparty will send this folder's path, the names of up to 200 items inside it, and their sizes to Claude
         through your own Claude Code account. File contents are never sent unless you allow reading below.
         You'll see exactly what's sent before every question.</p>
      <label><input type="checkbox" id="agentReadFiles"> Also let Claude read files in this folder</label>
      <button class="btn cta" id="agentEnable" style="height:26px">Turn on Ask Claude</button>
    </div></div>`;
  $("agentEnable").onclick = async () => {
    try {
      await post("/api/agent/enable", { consent: true, read_files: $("agentReadFiles").checked });
      agentOff = false; if (status) status.enabled = true;
      convs.forEach(c => { c.preview = null; });
      hooks.renderAll(false);
    } catch (e) { toast("Couldn't turn on Ask Claude"); }
  };
}

const PRESETS = ["What's taking the most space here?", "What can I safely delete inside?",
  "Is anything here irreplaceable?", "Which app or tool created this?"];

function conv(path, name) {
  if (!convs.has(path)) convs.set(path, { name: name || path.split("/").pop(), turns: [], pending: false, preview: null });
  return convs.get(path);
}

async function loadPreview(path) {
  const c = conv(path);
  try { c.preview = (await post("/api/agent/preview", { path })).context; }
  catch (e) { if (isOff(e)) agentOff = true; c.preview = { error: true }; }
  if (current === path) hooks.renderAll(false);
}

function previewHtml(c) {
  const x = c.preview;
  if (!x) return `<details class="sent" open><summary>What will be sent</summary><div class="why2">loading…</div></details>`;
  if (x.error) return `<details class="sent"><summary>What will be sent</summary><div class="why2">Couldn't build the preview.</div></details>`;
  return `<details class="sent"><summary>What will be sent</summary>
    <div class="kv"><span>Path</span><span class="mono">${esc(x.path)}</span></div>
    <div class="kv"><span>Rules verdict</span><span>${esc(x.rules.label)} — ${esc(x.rules.reason)}</span></div>
    <div class="kv"><span>Largest items</span><span>${x.top_children.map(k => `${esc(k.name)} (${fmt(k.size)})`).join(", ") || "—"}</span></div>
    <div class="kv"><span>Names</span><span>${x.names.length}${x.names_truncated ? "+ (first 200)" : ""}</span></div>
    <div class="why2">No file contents are sent up front. The agent can open files itself with look-only tools (Read, Grep, ls, du, stat) — it has no tool that writes or deletes, and keys, passwords and .env files are off limits to it.</div>
  </details>`;
}

function turnHtml(t) {
  const q = t.q ? `<div class="msg u">${esc(t.q)}</div>` : "";
  if (!t.res) return q;
  if (!t.res.ok) return q + `<div class="msg a agenterr"><b>Agent error</b> — ${esc(t.res.error)} <button class="btn" data-refresh="1" data-q="${esc(t.q)}" style="height:24px">Try again</button></div>`;
  const a = t.res.answer;
  const del = a.proposed.filter(p => p.action === "delete"), keep = a.proposed.filter(p => p.action === "keep");
  return q + `<div class="msg a agentcard">
    <div class="verdict"><span class="pill ${VCLASS[a.verdict]}">${esc(a.verdict.replace(/-/g, " "))}</span>
      <span class="why2">${Math.round(a.confidence * 100)}% sure${t.res.cached ? ` · cached · <a href="#" data-refresh="1" data-q="${esc(t.q)}">Refresh</a>` : ""}${t.res.cost ? ` · $${t.res.cost.toFixed(3)}` : ""}</span></div>
    <p>${esc(a.summary)}</p>
    ${a.evidence.length ? `<ul>${a.evidence.map(e => `<li>${esc(e)}</li>`).join("")}</ul>` : ""}
    ${del.length ? `<div class="props"><b>Could go</b>${del.map(p => `<label><input type="checkbox" data-prop="${esc(p.path)}" checked> <span class="mono">${esc(p.path.split("/").pop())}</span> <span class="why2">${esc(p.reason)}</span></label>`).join("")}
      <button class="btn cta" id="agentReview">Review ${del.length} selected →</button><div class="why2">Review uses Afterparty's rules to decide Trash vs permanent.</div></div>` : ""}
    ${keep.length ? `<div class="props"><b>Worth keeping</b>${keep.map(p => `<div><span class="mono">${esc(p.path.split("/").pop())}</span> <span class="why2">${esc(p.reason)}</span> <button class="btn" data-protect="${esc(p.path)}" style="height:22px">Protect</button></div>`).join("")}</div>` : ""}
    ${(a.dropped || []).map(d => `<div class="why2">ignored ${esc(d.path)}: ${esc(d.reason)}</div>`).join("")}
  </div>`;
}

async function ask(path, q, refresh) {
  const c = conv(path); if (c.pending) return;
  const history = c.turns.filter(t => t.res && t.res.ok).map(t => ({ q: t.q || "What is this?", a: t.res.answer.summary }));
  const turn = { q, res: null }; c.turns.push(turn); c.pending = true; hooks.renderAll(false);
  try {
    turn.res = await post("/api/agent/ask", { path, question: q || null, history: refresh ? [] : history, refresh });
    if (turn.res && turn.res.setup) { c.turns.pop(); c.pending = false; await loadAgentStatus(true); hooks.renderAll(false); return; }
  }
  catch (e) {
    if (isOff(e)) { agentOff = true; c.turns.pop(); c.pending = false; hooks.renderAll(false); return; }
    turn.res = { ok: false, error: e.status === 429 ? "The agent is busy with two other questions — try again in a moment." : "Couldn't reach the agent." }; }
  c.pending = false;
  const showing = S.tab === "agent" && pathOf(S.sel || S.loc) === path;
  hooks.renderAll(false);
  if (!showing) toast(`The agent answered about ${c.name} — select it to read`);
}
