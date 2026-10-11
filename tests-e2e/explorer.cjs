const { chromium } = require(process.env.PWMOD);
const url = process.argv[2]; const results = []; const errors = [];
const check = (name, ok, info = "") => { results.push([name, !!ok]); if (!ok) console.log("FAIL", name, info); };
(async () => {
  const b = await chromium.launch(); const ctx = await b.newContext({ viewport: { width: 1600, height: 900 } });
  const p = await ctx.newPage();
  p.on("pageerror", e => errors.push(e.message));
  p.on("response", r => { if (r.status() >= 400 && !r.url().includes("__test_shutdown")) console.log("HTTP", r.status(), r.url().replace(/^https?:\/\/[^/]+/, "").slice(0, 120)); }); p.on("console", m => { if (m.type() === "error" && !m.text().startsWith("Failed to load resource")) errors.push(m.text()); });  // browser network logs (e.g. the deliberate 403) aren't script errors
  await p.goto(url); await p.waitForFunction(() => window.__cleanup && window.__cleanup.rects().length > 0, null, { timeout: 15000 });
  check("map renders", (await p.evaluate(() => window.__cleanup.rects().length)) > 0);
  // drill into unloaded folder via contents table (first dir row)
  await p.dblclick("#files tbody tr:has-text('big')"); await p.waitForTimeout(800);
  check("drill into unloaded folder", (await p.textContent("#crumbs")).includes("big"));
  check("files-only folder draws tiles", (await p.evaluate(() => window.__cleanup.rects().length)) > 0);
  // contents pagination: 2500 files → first page 200 rows, more after scroll
  const rows1 = await p.locator("#files tbody tr").count();
  await p.evaluate(() => { const b = document.getElementById("filesBody"); b.scrollTop = b.scrollHeight; });
  await p.waitForTimeout(800);
  const rows2 = await p.locator("#files tbody tr").count();
  check("contents pagination", rows1 === 200 && rows2 > 200, `${rows1}→${rows2}`);
  await p.keyboard.press("Backspace"); await p.waitForTimeout(400);
  check("up one level", !(await p.textContent("#crumbs")).includes("big"));
  await p.keyboard.press("h"); await p.waitForTimeout(300);
  // theme persists
  await p.click("#brand"); await p.click("#themeSeg button[data-mode=light]"); await p.keyboard.press("Escape");
  await p.reload(); await p.waitForTimeout(1200);
  check("theme persists", (await p.getAttribute("html", "data-theme")) === "light");
  // collapse persists
  await p.keyboard.press("1"); await p.reload(); await p.waitForTimeout(1200);
  check("collapse persists", await p.locator("#pLeft.collapsed").count() === 1);
  await p.keyboard.press("1");
  // right-click menu
  const box = await p.$eval("#map", el => { const r = el.getBoundingClientRect(); return { x: r.x + 40, y: r.y + 40 }; });
  await p.mouse.click(box.x, box.y, { button: "right" }); await p.waitForTimeout(300);
  check("context menu opens", await p.locator("#cm.on .mi").count() > 3);
  await p.keyboard.press("Escape");
  // every visible button / role=button has a tooltip or aria-label (also after selecting something, which renders the inspector)
  const untipped = async () => p.evaluate(() => [...document.querySelectorAll("button, [role=button]")].filter(e => e.getClientRects().length && !(e.title || "").trim() && !(e.getAttribute("aria-label") || "").trim()).map(e => e.id || e.className || e.textContent.trim()));
  await p.waitForTimeout(200);
  check("all visible buttons have tooltips", (await untipped()).length === 0, JSON.stringify(await untipped()));
  await p.mouse.click(box.x, box.y); await p.waitForTimeout(500);
  check("tooltips with a selection", (await untipped()).length === 0, JSON.stringify(await untipped()));
  check("disabled Add explains why or enabled", ((await p.getAttribute("#addBtn", "title")) || "").length > 5);
  // disabled reasons are specific, not a generic "select something first"
  const tipOf = async id => p.evaluate(i => { const e = document.getElementById(i); return { t: e.title, a: e.getAttribute("aria-label") || "", d: e.disabled }; }, id);
  const reasonCheck = async (label, want) => {
    const a = await tipOf("addBtn");
    check(`Add tooltip names reason: ${label}`, a.d && want.test(a.t) && !/select something first/.test(a.t), JSON.stringify(a));
  };
  await p.keyboard.press("Escape"); await p.keyboard.press("h"); await p.waitForTimeout(500);
  await p.mouse.click(2, 2); // nothing selected: whole disk
  await p.evaluate(() => document.activeElement && document.activeElement.blur());
  await p.evaluate(async () => { const { S } = await import("/static/js/core.js"); const { renderInspector } = await import("/static/js/panels.js"); S.sel = null; S.loc = S.root; renderInspector(); });
  await p.waitForTimeout(400); await reasonCheck("whole disk", /whole disk/); { const k = await tipOf("keepBtn"); check("Protect tooltip names reason: whole disk", k.d && /whole disk/.test(k.t), JSON.stringify(k)); }
  // protected: locked folder (verdict arrives asynchronously)
  const rectPick = async pred => p.evaluate(src => { const f = eval(src); const r = window.__cleanup.rects().find(f); return r ? { x: r.x + r.w / 2, y: r.y + r.h / 2 } : null; }, `(${pred})`);
  const mapOff = await p.$eval("#map", el => { const r = el.getBoundingClientRect(); return { x: r.x, y: r.y }; });
  const clickRect = async (pred, label) => { const pt = await rectPick(pred); if (!pt) { check(`map has ${label}`, false); return false; } await p.mouse.click(mapOff.x + pt.x, mapOff.y + pt.y); await p.waitForTimeout(500); return true; };
  // remainder / aggregate tiles on the map (the 2500-file folder produces them)
  await p.dblclick("#files tbody tr:has-text('big')"); await p.waitForTimeout(900);
  // aggregate: no fixture folder is wide enough to produce one on the map, so select a synthetic aggregate node
  await p.evaluate(async () => { const { S } = await import("/static/js/core.js"); const { renderInspector } = await import("/static/js/panels.js"); S.sel = { n: "3 smaller items", s: 10, t: "agg", c: [], parent: S.loc, id: "agg-test", cls: "unknown" }; renderInspector(); });
  await p.waitForTimeout(400); await reasonCheck("aggregate", /group of small items/);
  { const f = await tipOf("finderBtn"); check("Finder tooltip names reason: aggregate (no path)", f.d && /aggregate|group of small items/.test(f.t), JSON.stringify(f)); }
  if (await clickRect(r => r.n && r.n.t === "rest" && !r.header, "a remainder tile")) await reasonCheck("remainder", /remainder/);
  await p.keyboard.press("Backspace"); await p.waitForTimeout(400);
  // frame sequence row in the contents list
  await p.dblclick("#files tbody tr:has-text('renders')"); await p.waitForTimeout(700);
  await p.dblclick("#files tbody tr:has-text('v1')"); await p.waitForTimeout(900);
  const seqRow = p.locator("#files tbody tr:has-text('frame_')").first();
  if (await seqRow.count()) { await seqRow.click(); await p.waitForTimeout(400); await reasonCheck("frame sequence", /frame-sequence/); }
  else check("sequence row present", false);
  await p.keyboard.press("h"); await p.waitForTimeout(400);
  // protected item: vault is on the keep list, so classify marks it protected; Add must be disabled, then say why
  await p.click("#files tbody tr:has-text('vault')"); await p.waitForTimeout(900);
  { const a = await tipOf("addBtn"); check("Add disabled for a protected item", a.d === true, JSON.stringify(a));
    check("Add tooltip names reason: protected", a.d && /protected/.test(a.t) && !/select something first/.test(a.t), JSON.stringify(a)); }
  await p.keyboard.press("h"); await p.waitForTimeout(300);
  // icon-only buttons have meaningful aria-labels (words, not glyphs), and tooltips are not their bare glyph
  const icons = await p.evaluate(() => [...document.querySelectorAll("button, [role=button]")].filter(e => e.getClientRects().length && /^[\s←→↑⌂↻▴▾⇤⇥⤢✕⋯›🔒]*$/.test(e.textContent)).map(e => ({ id: e.id || e.className, a: e.getAttribute("aria-label") || "", t: e.title })));
  const badIcons = icons.filter(i => !(/[A-Za-z]{4,}/.test(i.a) && i.a.split(/\s+/).length >= 2));
  check("icon-only buttons found", icons.length >= 5, String(icons.length));
  check("icon-only buttons have word aria-labels", badIcons.length === 0, JSON.stringify(badIcons));
  await p.click("#brand"); await p.waitForTimeout(200);
  check("tooltips with settings open", (await untipped()).length === 0, JSON.stringify(await untipped()));
  await p.keyboard.press("Escape");
  // unreadable folder (403) must not look like a dead server
  await p.keyboard.press("h"); await p.waitForTimeout(400);
  await p.dblclick("#files tbody tr:has-text('locked')"); await p.waitForTimeout(800);
  check("permission error is not 'server gone'", await p.locator("#reconnect.on").count() === 0);
  // staged item survives re-loading its folder
  await p.keyboard.press("h"); await p.waitForTimeout(400);
  await p.dblclick("#files tbody tr:has-text('proj')"); await p.waitForTimeout(700);
  await p.click("#files tbody tr:has-text('node_modules')"); await p.keyboard.press("a"); await p.waitForTimeout(200);
  await p.keyboard.press("Backspace"); await p.waitForTimeout(400);
  await p.dblclick("#files tbody tr:has-text('proj')"); await p.waitForTimeout(700);
  await p.click("#files tbody tr:has-text('node_modules')"); await p.keyboard.press("a"); await p.waitForTimeout(200);
  check("staging survives folder reload", (await p.textContent("#trayBig")).startsWith("Tray empty"));
  await p.keyboard.press("h");
  // agent: preview what's sent, ask, proposals → review
  await p.keyboard.press("h"); await p.waitForTimeout(400);
  await p.click("#files tbody tr:has-text('proj')"); await p.waitForTimeout(200);
  await p.click(".tab[data-t=agent]"); await p.waitForTimeout(500);
  check("agent asks for consent first", (await p.textContent("#insp")).includes("Turn on Ask Claude") && await p.locator("#agentAnalyse").count() === 0);
  await p.click("#agentEnable"); await p.waitForSelector("#agentAnalyse", { timeout: 10000 }).catch(() => {});
  check("agent shows what will be sent", (await p.textContent("#insp")).includes("What will be sent"));
  check("preset questions offered", await p.locator("#insp .qchip").count() >= 3);
  await p.click("#agentAnalyse"); await p.waitForSelector(".agentcard", { timeout: 10000 }).catch(() => {});
  check("agent verdict card (one-click default question)", (await p.textContent("#insp")).includes("Build output of proj."));
  await p.click("#files tbody tr:has-text('big')").catch(() => {}); await p.waitForTimeout(400);
  check("agent follows the selection", (await p.textContent("#insp")).includes("Regarding big") && await p.locator("#agentAnalyse").count() === 1);
  await p.click("#files tbody tr:has-text('proj')"); await p.waitForTimeout(400);
  check("earlier answer kept per folder", (await p.textContent("#insp")).includes("Build output of proj."));
  await p.click("#agentReview"); await p.waitForSelector("#ov.on", { timeout: 10000 });
  await p.waitForTimeout(400);
  check("agent proposal opens review", (await p.textContent("#manifest")).includes("node_modules"));
  await p.click("#ovClose");
  // agent setup: status in Settings, model choice, setup guide when signed out
  await p.click("#brand"); await p.waitForTimeout(400);
  check("settings shows agent status", (await p.textContent("#agentStatus")).includes("Claude subscription"));
  await p.selectOption("#agentModel", "opus"); await p.waitForTimeout(300);
  await p.keyboard.press("Escape");
  await fetch(url.replace(/\/\?.*/, "/__test_agent_signed_out"), { method: "POST" }).catch(() => {});
  await p.click("#files tbody tr:has-text('notes')"); await p.waitForTimeout(200);
  await p.click(".tab[data-t=agent]"); await p.waitForTimeout(300);
  await p.click("#agentCheck").catch(() => {}); await p.waitForTimeout(500);
  check("agent setup guide when signed out", (await p.textContent("#insp")).includes("/login"));
  check("ask disabled until set up", await p.isDisabled("#agentAsk"));
  await p.click(".tab[data-t=details]");
  // mole lens: suggestions re-checked by rules; protected ones not offered
  await p.click("#bMole"); await p.waitForSelector("#moleList .ln", { timeout: 5000 }).catch(() => {});
  check("mole lens lists suggestions", (await p.textContent("#moleOv")).includes("node_modules"));
  check("mole: rules-protected not offered", await p.locator("#moleOv input[data-mp]").count() === 1);
  await p.check("#moleOv input[data-mp]"); await p.click("#moleReview"); await p.waitForSelector("#ov.on", { timeout: 10000 }); await p.waitForTimeout(400);
  check("mole selection opens review", (await p.textContent("#manifest")).includes("node_modules"));
  await p.click("#ovClose");
  // frame sequences grouped, expandable
  await p.keyboard.press("h"); await p.waitForTimeout(400);
  await p.dblclick("#files tbody tr:has-text('renders')"); await p.waitForTimeout(600);
  await p.dblclick("#files tbody tr:has-text('v1')"); await p.waitForTimeout(700);
  check("sequence grouped", (await p.locator("#files tbody tr").count()) === 1 && (await p.textContent("#files tbody")).includes("×40"));
  await p.dblclick("#files tbody tr:has-text('×40')"); await p.waitForTimeout(600);
  check("sequence expands", (await p.locator("#files tbody tr").count()) === 41);
  await p.click("#files tbody tr.seqback"); await p.waitForTimeout(500);
  check("sequence collapses", (await p.locator("#files tbody tr").count()) === 1);
  await p.click("#files tbody tr:has-text('×40')"); await p.keyboard.press("a"); await p.waitForTimeout(200);
  check("sequence rows can't be staged", (await p.textContent("#trayBig")).startsWith("Tray empty"));
  // quick look: Space opens, arrows page, Esc closes, right-click → Quick Look
  await p.keyboard.press("h"); await p.waitForTimeout(400);
  await p.dblclick("#files tbody tr:has-text('notes')"); await p.waitForTimeout(700);
  await p.click("#files tbody tr:has-text('pic.png')"); await p.keyboard.press(" "); await p.waitForTimeout(500);
  check("space opens preview", await p.locator("#pv.on img").count() === 1);
  await p.keyboard.press("ArrowDown"); await p.waitForTimeout(500);
  check("arrow pages to next file", !(await p.textContent("#pvName")).includes("pic.png"));
  await p.keyboard.press("Escape"); await p.waitForTimeout(200);
  check("esc closes preview", await p.locator("#pv.on").count() === 0);
  await p.click("#files tbody tr:has-text('doc.txt')", { button: "right" }); await p.waitForTimeout(200);
  await p.click("#cm .mi:has-text('Quick Look')"); await p.waitForTimeout(500);
  check("quick look shows text", (await p.textContent("#pv")).includes("nnnn"));
  await p.keyboard.press("Escape");
  // review → execute (fixture: proj/node_modules is regenerable → PERMANENT, needs the acknowledgement)
  await p.keyboard.press("h"); await p.waitForTimeout(400);
  await p.dblclick("#files tbody tr:has-text('proj')"); await p.waitForTimeout(700);
  await p.click("#files tbody tr:has-text('node_modules')"); await p.keyboard.press("a"); await p.waitForTimeout(200);
  await p.click("#reviewBtn"); await p.waitForSelector("#ov.on", { timeout: 10000 });
  await p.waitForFunction(() => document.getElementById("manifest").textContent.includes("PERMANENT"), null, { timeout: 10000 }).catch(() => {});
  check("review shows permanent", (await p.textContent("#manifest")).includes("PERMANENT"));
  check("ack gates delete", await p.isDisabled("#rvGo"));
  await p.check("#ack"); await p.waitForTimeout(150);
  check("ack enables delete", !(await p.isDisabled("#rvGo")));
  await p.click("#rvGo");
  await p.waitForFunction(() => document.getElementById("manifest").textContent.includes("DELETED"), null, { timeout: 15000 }).catch(() => {});
  check("execute reports deleted", (await p.textContent("#manifest")).includes("node_modules"));
  await p.click("#ovClose"); await p.keyboard.press("h"); await p.waitForTimeout(500);
  // tray panel: list, remove one, clear all
  await p.keyboard.press("h"); await p.waitForTimeout(400);
  await p.dblclick("#files tbody tr:has-text('notes')"); await p.waitForTimeout(700);
  await p.click("#files tbody tr:has-text('doc.txt')"); await p.keyboard.press("a"); await p.waitForTimeout(150);
  await p.click("#files tbody tr:has-text('keep.txt')"); await p.keyboard.press("a"); await p.waitForTimeout(150);
  await p.click("#trayInfo"); await p.waitForTimeout(200);
  check("tray panel lists staged", await p.locator("#dd.traypanel .it[data-i]").count() === 2);
  await p.click("#dd.traypanel .it:has-text('keep.txt') .x"); await p.waitForTimeout(200);
  check("tray panel removes one", await p.locator("#dd.traypanel .it[data-i]").count() === 1);
  await p.keyboard.press("Escape"); await p.mouse.click(5, 300); await p.waitForTimeout(150);
  // move to the Trash anyway: the rules keep the latest render version; an override only ever moves it to the Trash
  await p.keyboard.press("h"); await p.waitForTimeout(400);
  await p.dblclick("#files tbody tr:has-text('renders')"); await p.waitForTimeout(700);
  await p.click("#files tbody tr:has-text('v1')"); await p.keyboard.press("a"); await p.waitForTimeout(150);
  await p.click("#reviewBtn"); await p.waitForSelector("#ov.on", { timeout: 10000 }); await p.waitForTimeout(300);
  check("rules only trash unclassified", (await p.textContent("#manifest")).includes("TRASH"));
  check("rules keep the latest render", (await p.textContent("#manifest")).includes("KEEP"));
  await p.click("#manifest .ln:has-text('renders/v1') .rowmenu"); await p.waitForTimeout(150);
  check("no permanent override offered", await p.locator("#dd .it:has-text('permanently')").count() === 0);
  await p.click("#dd .it:has-text('Trash anyway')"); await p.waitForTimeout(150);
  check("override shows YOUR CALL", (await p.textContent("#manifest")).includes("YOUR CALL"));
  check("override needs typed confirm", await p.isDisabled("#rvGo"));
  await p.fill("#confirmDel", "delete"); await p.waitForTimeout(150);
  check("typed confirm enables", !(await p.isDisabled("#rvGo")));
  await p.click("#rvGo");
  await p.waitForFunction(() => document.getElementById("manifest").textContent.includes("TRASHED"), null, { timeout: 15000 }).catch(() => {});
  check("override trashed the render", (await p.textContent("#manifest")).includes("renders/v1"));
  check("override run trashed the file too", (await p.textContent("#manifest")).includes("doc.txt"));
  await p.waitForFunction(() => document.getElementById("activity").hidden, null, { timeout: 8000 }).catch(() => {});
  check("activity bar hides when idle", await p.locator("#activity[hidden]").count() === 1);
  await p.click("#ovClose");
  // clear tray
  await p.keyboard.press("h"); await p.waitForTimeout(400);
  await p.dblclick("#files tbody tr:has-text('notes')"); await p.waitForTimeout(700);
  await p.click("#files tbody tr:has-text('keep.txt')"); await p.keyboard.press("a"); await p.waitForTimeout(150);
  await p.click("#trayInfo"); await p.waitForTimeout(200); await p.click("#trayClear"); await p.waitForTimeout(200);
  check("clear tray empties it", (await p.textContent("#trayBig")).startsWith("Tray empty"));
  await p.keyboard.press("h"); await p.waitForTimeout(400);
  // fda banner present when server reports fda=false (fixture server forces it)
  check("fda banner", await p.locator("#fdaBanner:not([hidden])").count() === 1);
  check("no console errors", errors.length === 0, errors.join(" | "));
  // server gone → reconnect overlay
  await fetch(url.replace(/\/\?.*/, "/__test_shutdown"), { method: "POST" }).catch(() => {});
  await p.waitForTimeout(500); await p.click("#bHome").catch(() => {}); await p.keyboard.press("r"); await p.waitForTimeout(1500);
  check("server gone", await p.locator("#reconnect.on").count() === 1);
  const ok = results.filter(r => r[1]).length; console.log(`E2E_OK ${ok}/${results.length}`);
  await b.close(); process.exit(ok === results.length ? 0 : 1);
})().catch(e => { console.log("E2E crash", e.message.split("\n").slice(0, 3).join(" | ")); console.log("console errors so far:", JSON.stringify(errors)); process.exit(2); });
