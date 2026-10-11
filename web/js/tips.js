// Tooltips: native `title` (+ `aria-label` on icon-only controls) for every button. Rules are re-applied whenever the
// DOM changes, so buttons rendered later (inspector, agent tab, review, tray panel) are covered and disabled
// buttons can say why. Any button no rule knows falls back to its own text.
const dis = (el, on, off) => el.disabled ? (el.dataset.why || off) : on;
const RULES = [
  ["#brand", () => "Settings: theme, text size, map options (,)"],
  ["#bBack", () => "Go back to the previous folder (⌘[)"],
  ["#bFwd", () => "Go forward (⌘])"],
  ["#bUp", () => "Up one level (⌫)"],
  ["#bHome", () => "Home: the whole disk (H)"],
  ["#bRescan", () => { const m = document.getElementById("scanMeta"); return m && m.textContent ? `Rescan the disk (R) · ${m.textContent}` : "Rescan the disk (R)"; }],
  ["#bMole", () => "Mole suggestions: read-only dry run, nothing is deleted"],
  [".seg [data-d]", el => el.dataset.d === "0" ? "Depth Auto: show as many folder levels as fit" : `Show ${el.dataset.d} folder level${el.dataset.d === "1" ? "" : "s"} on the map`],
  ["#modes [data-m]", el => ({ safety: "Color by safety: green regenerable, amber review, gray protected",
    type: "Color by file type", age: "Color by how long ago it was touched", size: "Plain: one color, size only" })[el.dataset.m]],
  ["#presets [data-p]", el => ({ explore: "Explore layout: tree, map, contents and inspector",
    files: "Files layout: contents list first", focus: "Focus layout: the map only (Z)" })[el.dataset.p]],
  ["#capToggle", el => el.textContent.trim() === "▴" ? "Collapse the disk summary" : "Expand the disk summary"],
  ["[data-collapse=pLeft]", () => "Collapse the tree pane (1)"],
  ["[data-collapse=pRight]", () => "Collapse the inspector pane (3)"],
  ["#mapMax", () => "Expand the map, or show map + contents again"],
  ["#filesToggle", el => el.textContent.trim() === "▾" ? "Collapse the contents list (2)" : "Expand the contents list (2)"],
  [".tab[data-t=details]", () => "Details about the selected item"],
  [".tab[data-t=agent]", () => "Agent: ask Claude Code about this folder (optional)"],
  ["#addBtn", el => dis(el, `${/Remove/.test(el.textContent) ? "Remove from" : "Add to"} the tray (A)`,
    document.querySelector("#insp .why") ? "Add to tray: this item is protected" : "Add to tray: select something first")],
  ["#finderBtn", el => dis(el, "Reveal in Finder", "Finder: select a file or folder first")],
  ["#keepBtn", el => dis(el, "Protect forever: Afterparty will never delete this", "Protect: select a folder first")],
  ["#reviewBtn", el => dis(el, "Review the tray and confirm before anything is deleted (T)", "Review: add something to the tray first (A)")],
  ["#pillPerm", () => "Permanent: cannot be undone, only rebuildable items"],
  ["#pillTrash", () => "Trash: moved to the Trash, can be undone"],
  ["#actStop", () => "Stop the running cleanup"],
  ["#copyFda", () => "Copy the app path to paste into Full Disk Access"],
  ["#moleRefresh", () => "Run Mole's dry run again"],
  ["#moleClose", () => "Close (Esc)"],
  ["#moleReview", el => dis(el, "Review the selected suggestions before anything is deleted", "Review: tick at least one suggestion first")],
  ["#pvReveal", () => "Reveal in Finder"],
  ["#pvClose", () => "Close preview (Esc)"],
  ["#ovClose", () => "Close (Esc)"],
  ["#ovCancel", () => "Close the review and go back to the map"],
  ["#rvGo", el => dis(el, "Delete the items shown in this plan", "Delete: tick the confirmation box first")],
  ["#ack", () => "Confirm you understand permanent deletions can't be undone"],
  ["#themeSeg [data-mode]", el => ({ light: "Light theme", dark: "Dark theme", system: "Match the system theme" })[el.dataset.mode]],
  ["#tsRange", () => "Text size"],
  ["#swAnim", () => "Animate zooming into folders on the map"],
  ["#swLocked", () => "Show protected items on the map"],
  ["#swHatch", () => "Hatch items staged in the tray on the map"],
  ["#resetLayout", () => "Reset pane sizes and layout"],
  ["#agentRecheck, #agentCheck", () => "Check again whether Claude Code is installed and signed in"],
  ["#agentEnable", () => "Allow Afterparty to ask Claude Code about folders"],
  ["#agentAsk", el => dis(el, "Send your question to the agent", "Ask: set up Claude Code or wait for the current answer")],
  ["#agentAnalyse", () => "Ask the agent to analyse this folder"],
  ["#agentReview", () => "Review the agent's suggestions before anything is deleted"],
  ["#trayClear", () => "Remove everything from the tray"],
  [".x", () => "Remove from tray"],
  [".rowmenu", () => "Options for this item"],
  [".chev", () => "Jump to a sibling folder"],
  ["[data-copy]", () => "Copy the command"],
  ["[data-refresh]", () => "Ask again"],
  [".qchip", el => `Ask the agent: ${el.textContent.trim()}`],
];
const ICON_ONLY = /^[\s←→↑⌂↻▴▾⇤⇥⤢✕⋯›🔒]*$/;
function apply(root) {
  const set = (el, t) => { if (t && el.title !== t) el.title = t; if (t && ICON_ONLY.test(el.textContent) && el.getAttribute("aria-label") !== t) el.setAttribute("aria-label", t); };
  for (const [sel, fn] of RULES) root.querySelectorAll(sel).forEach(el => set(el, fn(el)));
  root.querySelectorAll("button, [role=button]").forEach(el => {
    if (!el.title && !el.getAttribute("aria-label")) { const t = el.textContent.trim().replace(/\s+/g, " "); if (t) el.title = t; }
  });
}
export function initTips() {
  let q = false;
  const run = () => { q = false; apply(document); };
  new MutationObserver(() => { if (!q) { q = true; requestAnimationFrame(run); } })
    .observe(document.body, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ["disabled", "class", "data-scan"] });
  run();
}
