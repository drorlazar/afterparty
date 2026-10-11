"""Token-protected localhost API for Afterparty (stdlib only)."""
import json
import os
import secrets
import stat
import shutil
import sqlite3
import subprocess
import sys
import traceback
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import functools
from cleanup.fda import fda_hint as _fda_hint
from cleanup import __version__, config, fsops
from cleanup.agent.cache import Cache as AgentCache
from cleanup.agent.context import build_context, prompt as agent_prompt
from cleanup.agent.runner import ask as agent_ask, run_group
from cleanup.agent import setup as agent_setup
from cleanup.engine import core
from cleanup.server import preview as pv
from cleanup.mole import adapter as mole
from cleanup.engine.classify import classify
from cleanup.engine.core import kept as _kept
from cleanup.engine.leftovers import user_cache_dir, user_temp_dir, user_x_dir
from cleanup.plan import manifest as plan_manifest
from cleanup.plan import store as plan_store
from cleanup.plan.executor import execute as plan_execute
from cleanup.plan.trash import move_to_trash
from cleanup.plan.undo import undo as plan_undo
from cleanup.scanner import hidden
from cleanup.scanner.access import has_full_disk_access, interpreter
from cleanup.scanner.listing import check_path, list_dir
from cleanup.scanner.scan import DEFAULT_SKIP, scan

WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "web")
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css",
         ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon", ".woff2": "font/woff2",
         ".json": "application/json"}


KEEP_UNSAFE = ("\n", "\r", "#")



fda_hint = functools.lru_cache(maxsize=4)(_fda_hint)  # /api/status is polled; don't spawn a probe each time

def read_keep(strict=False):
    """Raises config.ConfigError on bad settings — callers that delete must refuse then. strict (every deleting
    path): a missing settings file raises too, instead of meaning "no protections"."""
    return config.current_keep() if strict else config.keep_paths(config.load()[0])


LEFTOVER_RULES = {"code-sign-clone", "system-app-cache"}  # leftovers.py rules, tied to their own …/X and …/C folders


def bound_to_roots(m, home=None):
    """Spec 4.2: outside your project folders (config roots) and the fixed off-root list, a map pick can only go to
    the Trash — never deleted for good, never a worktree removal. Unreadable settings: no roots."""
    home = home or os.path.expanduser("~")
    try:
        roots = config.roots(config.load()[0])[0]
    except config.ConfigError:
        roots = []
    allowed = [os.path.realpath(r) for r in roots] + core.off_root_allowed(home)
    inside = lambda p: any(p == r or p.startswith(r.rstrip("/") + "/") for r in allowed)
    safer = {t["path"]: ("trash" if t["mode"] == "permanent" else "keep") for t in plan_manifest.targets(m)
             if t["mode"] in ("permanent", "remove") and t.get("rule") not in LEFTOVER_RULES
             and not inside(os.path.realpath(t["path"]))}
    return plan_manifest.apply_overrides(m, safer) if safer else m


class App:
    def __init__(self, store, token, roots, web_dir, scan_fn, scan_skip):
        self.store, self.token, self.roots, self.web_dir = store, token, tuple(roots), web_dir
        self.scan_fn, self.scan_skip = scan_fn, scan_skip
        self.cond = threading.Condition()
        self.events = []          # [(seq, dict)]
        self.seq = 0
        self.scanning = False
        self.port = None
        self.reveal_fn = lambda p: subprocess.run(["open", "-R", p], timeout=10)
        self.trash_fn = move_to_trash
        self.classify_fn = classify
        self.agent_run_fn = run_group  # a timeout kills claude *and* anything it started
        self.agent_settings_file = agent_setup.settings_file()
        self._claude = None
        self.agent_claude_fn = self._find_claude
        self.agent_status_fn = lambda: agent_setup.status(self.agent_claude_fn(refresh=True))
        self.agent_cache = AgentCache(os.path.join(config.state_dir(), "agent"))
        self.agent_slots = threading.BoundedSemaphore(2)
        self.thumb_run = subprocess.run
        self.thumb_dir = os.path.join(config.state_dir(), "thumbs")
        self.mole_state = os.path.join(config.state_dir(), "mole.json")
        self.mole_running = False
        self.mole_refresh_fn = mole.refresh
        self.mole_available = lambda: shutil.which("mo") is not None
        self.executing = False
        self.exec_events = {}
        self.exec_cond = threading.Condition()
        self.exec_stop = threading.Event()
        self.exec_error = None
        self.guard_fn = None
        self.exec_lock = None
        self.activity = {"plan": None, "done": 0, "total": 0, "path": None, "processed": [], "started": None}
        self.last_result = None
        self.scan_progress = {}

    def _find_claude(self, refresh=False):
        if refresh or not self._claude:
            self._claude = agent_setup.find_claude()
        return self._claude

    def start_execute(self, m):
        try:
            _, present = config.load()
            if present:
                read_keep(strict=True)
        except config.ConfigError as e:
            self.exec_error = f"Afterparty settings are invalid ({e}). Nothing was deleted."
            return False
        if not present:
            self.exec_error = "Afterparty isn't set up yet, so nothing was deleted. Install it (see INSTALL.md)."
            return False
        if self.guard_fn:
            why = self.guard_fn()
            if why:
                self.exec_error = why
                return False
        self.exec_error = None
        with self.exec_cond:
            if self.executing or self.scanning:
                return False
            from cleanup.plan import bridge
            lk = bridge.lock(config.state_dir())
            try:
                lk.__enter__()
            except bridge.Busy as e:
                self.exec_error = str(e)
                return False
            self.exec_lock = lk
            self.executing = True
            self.exec_events[m["id"]] = []
            self.exec_stop.clear()
            self.activity = {"plan": m["id"], "done": 0, "total": len(plan_manifest.targets(m)), "path": None,
                             "processed": [], "started": time.time()}
        try:
            threading.Thread(target=self._run_execute, args=(m,), daemon=True).start()
        except Exception as e:  # never leave the run lock held by a cleanup that never started
            with self.exec_cond:
                self.executing = False
                self.exec_events.pop(m["id"], None)
            lk, self.exec_lock = self.exec_lock, None
            if lk:
                lk.__exit__(None, None, None)
            self.exec_error = f"Couldn't start the cleanup ({e}). Nothing was deleted."
            return False
        return True

    def _exec_emit(self, pid, ev):
        with self.exec_cond:
            self.exec_events.setdefault(pid, []).append(ev)
            if ev.get("state") == "running":
                self.activity.update(done=ev.get("done", 0), total=ev.get("total", 0), path=ev.get("path"))
                if ev.get("path"):
                    self.activity["processed"] = (self.activity["processed"] + [ev["path"]])[-500:]
            self.exec_cond.notify_all()

    def _run_execute(self, m):
        pid = m["id"]
        extra = [d for d in (user_x_dir(), user_temp_dir(), user_cache_dir()) if d]  # your own temp folders; the rules decide what may go
        try:
            res = plan_execute(m, keep=lambda: read_keep(strict=True), classify_fn=self.classify_fn, trash_fn=self.trash_fn,
                               allowed_roots=[os.path.expanduser("~")] + extra,
                               stop=self.exec_stop,
                               journal=plan_store.journal_path(pid),
                               progress=lambda p: self._exec_emit(pid, {"state": "running", **p}))
            parents = sorted({os.path.dirname(x["path"]) for k in ("deleted", "trashed") for x in res[k]})
            for p in parents:  # keep the snapshot honest after the cleanup
                try:
                    if self.store.node(p):
                        self.store.replace_subtree(self.scan_fn(p, skip=self.scan_skip))
                except Exception:
                    traceback.print_exc(file=sys.stderr)
            final = {"state": "done", "result": res}
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            final = {"state": "error", "error": str(e)}
        finally:
            lk, self.exec_lock = self.exec_lock, None
            if lk:
                lk.__exit__(None, None, None)
        with self.exec_cond:
            self.executing = False
            self.exec_events.setdefault(pid, []).append(final)
            r = final.get("result") or {}
            self.last_result = {"plan": pid, "state": final["state"], "finished": time.time(),
                                "freed": r.get("freed", 0), "trash_bytes": r.get("trash_bytes", 0),
                                "counts": {k: len(v) for k, v in r.items() if isinstance(v, list)},
                                "stopped": self.exec_stop.is_set()}
            self.exec_cond.notify_all()

    def _emit(self, ev):
        with self.cond:
            self.seq += 1
            self.events.append((self.seq, ev))
            self.events = self.events[-500:]
            self.cond.notify_all()

    def start_scan(self, path=None):
        with self.cond:
            if self.scanning or self.executing:
                return False
            self.scanning = True
            self.events = []
        threading.Thread(target=self._run_scan, args=(path,), daemon=True).start()
        return True

    def _run_scan(self, path):
        t0 = time.time()
        try:
            self._emit({"state": "running", "path": path or self.roots[0]})
            def prog(s):
                self.scan_progress = {**s, "elapsed": round(time.time() - t0, 1)}
                self._emit({"state": "running", **s, "elapsed": round(time.time() - t0, 1)})
            if path:
                res = self.scan_fn(path, skip=self.scan_skip, progress=prog)
                self.store.replace_subtree(res)
            else:
                res = self.scan_fn(self.roots[0], skip=self.scan_skip, progress=prog)
                self.store.save(res)
            final = {"state": res.status, **res.stats, "took": res.took}
        except Exception as e:  # surface, never crash the server
            final = {"state": "error", "error": str(e)}
        with self.cond:
            self.scanning = False
        self._emit(final)


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Afterparty"

        def log_message(self, *a):
            pass

        # ------------------------------------------------------------ helpers
        def _q(self):
            return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

        def _cookie_key(self):
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "cleanup_key":
                    return v
            return None

        def _authed(self):
            supplied = self.headers.get("X-Cleanup-Key") or self._cookie_key() or (self._q().get("key") or [None])[0]
            return bool(supplied) and secrets.compare_digest(supplied, app.token)

        def _send(self, code, obj=None, body=None, ctype="application/json", headers=()):
            data = body if body is not None else json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _preview(self):
            p = self._path_arg()
            if os.path.islink(p):
                return self._send(400, {"error": "symlinks aren't previewed"})
            if not os.path.lexists(p):
                return self._send(404, {"error": "not found"})
            st = os.stat(p)
            if not stat.S_ISREG(st.st_mode):
                return self._send(400, {"error": "only regular files can be previewed"})
            kind = pv.kind_of(p)
            hdr = (("Content-Security-Policy", pv.CSP),)  # _send already adds nosniff + no-store
            if (self._q().get("mode") or [""])[0] == "info":
                return self._send(200, {"kind": kind, "size": st.st_size, "name": os.path.basename(p),
                                        "mime": pv.MIME.get(pv._ext(p), "")}, headers=hdr)
            if kind == "text":
                return self._send(200, pv.read_text(p), headers=hdr)
            if kind == "thumb":
                t = pv.thumbnail(p, app.thumb_dir, run=app.thumb_run)
                if not t:
                    return self._send(415, {"error": "no preview for this kind of file"})
                with open(t, "rb") as f:
                    return self._send(200, body=f.read(), ctype="image/png", headers=hdr)
            try:
                rng = pv.parse_range(self.headers.get("Range"), st.st_size)
            except ValueError:
                return self._send(416, {"error": "bad range"}, headers=(("Content-Range", f"bytes */{st.st_size}"),))
            ctype = pv.MIME[pv._ext(p)]
            if not rng:  # whole file (what <img> asks for): 200, streamed in chunks, never truncated
                self.send_response(200)
                for k, v in (("Content-Type", ctype), ("Content-Length", str(st.st_size)), ("Cache-Control", "no-store"),
                             ("X-Content-Type-Options", "nosniff"), ("Accept-Ranges", "bytes")) + hdr:
                    self.send_header(k, v)
                self.end_headers()
                with open(p, "rb") as f:
                    shutil.copyfileobj(f, self.wfile, 1 << 20)
                return None
            start, end = rng
            end = min(end, start + 8 * 2**20 - 1)  # ranged requests (video/audio players) get at most 8 MB at a time
            with open(p, "rb") as f:
                f.seek(start); data = f.read(end - start + 1)
            return self._send(206, body=data, ctype=ctype,
                              headers=hdr + (("Accept-Ranges", "bytes"), ("Content-Range", f"bytes {start}-{end}/{st.st_size}")))

        def _path_arg(self):
            raw = (self._q().get("path") or [""])[0]
            return check_path(raw)

        # ------------------------------------------------------------ routes
        def do_GET(self):
            try:
                return self._get()
            except PermissionError:
                return self._send(403, {"error": "permission denied"})
            except NotADirectoryError:
                return self._send(400, {"error": "not a folder"})
            except sqlite3.DatabaseError:
                return self._send(503, {"error": "snapshot unavailable — rescan"})
            except (BrokenPipeError, ConnectionResetError):
                return None
            except Exception:
                traceback.print_exc(file=sys.stderr)
                return self._send(500, {"error": "internal error"})

        def _host_ok(self):
            """DNS-rebinding defence: only answer requests addressed to this machine's loopback."""
            return self.headers.get("Host", "") in (f"127.0.0.1:{app.port}", f"localhost:{app.port}")

        def _get(self):
            if not self._host_ok():
                return self._send(403, {"error": "wrong host"})
            route = urllib.parse.urlparse(self.path).path
            if route == "/" or route.startswith("/static/"):
                return self._static(route)
            if not self._authed():
                return self._send(401, {"error": "unauthorized"})
            try:
                if route == "/api/status":
                    return self._send(200, self._status())
                if route == "/api/tree":
                    depth = int((self._q().get("depth") or ["1"])[0])
                    t = app.store.tree(self._path_arg(), depth)
                    if not t:
                        return self._send(404, {"error": "not in snapshot"})
                    home = os.path.expanduser("~")
                    keep = read_keep()

                    def tag(n):
                        if n.get("path"):
                            v = classify(n["path"], mtime=n.get("mtime"), keep=keep, home=home, fast=True)
                            n["cls"], n["rule"] = v["class"], v["rule"]
                        else:
                            n["cls"], n["rule"] = "unknown", "aggregate"
                        for c in n.get("children", []):
                            tag(c)
                    tag(t)
                    return self._send(200, t)
                if route == "/api/list":
                    q = self._q()
                    g = lambda k, d: (q.get(k) or [d])[0]
                    return self._send(200, list_dir(self._path_arg(), g("sort", "size"), g("order", "desc"),
                                                    int(g("offset", 0)), int(g("limit", 200)), store=app.store,
                                                    group=g("group", "") == "1", seq=g("seq", None)))
                if route == "/api/classify":
                    p = self._path_arg()
                    n = app.store.node(p)
                    return self._send(200, classify(p, mtime=n["mtime"] if n else None, keep=read_keep()))
                if route == "/api/hidden":
                    meta = app.store.latest()
                    return self._send(200, hidden.probe(meta["bytes"] if meta else 0, app.store.counts()))
                if route == "/api/scan/events":
                    return self._sse()
                parts = route.strip("/").split("/")
                if route == "/api/mole":
                    m = mole.load(app.mole_state)
                    for i in m["items"]:
                        i["gone"] = not os.path.lexists(i["path"])
                    return self._send(200, dict(m, available=app.mole_available(), running=app.mole_running))
                if route == "/api/preview":
                    return self._preview()
                if route == "/api/agent/status":
                    st = app.agent_status_fn()
                    cfg = agent_setup.load_settings(app.agent_settings_file)
                    try:
                        acfg = config.load()[0]["agent"]
                    except config.ConfigError:
                        acfg = {"enabled": False, "read_files": False}
                    return self._send(200, dict(st, model=cfg["agent_model"], default_question=cfg["agent_default_question"],
                                                models=list(agent_setup.MODELS), enabled=acfg["enabled"],
                                                read_files=acfg["read_files"]))
                if route == "/api/history":
                    return self._send(200, plan_store.history())
                if route == "/api/activity":
                    with app.exec_cond:  # one consistent snapshot
                        executing, a, last = app.executing, dict(app.activity), app.last_result
                    scanning = app.scanning
                    return self._send(200, {"executing": executing, "scanning": scanning,
                                            "exec": a if executing else None, "scan": app.scan_progress if scanning else None,
                                            "last": last})
                if len(parts) >= 3 and parts[:2] == ["api", "plan"]:
                    pid = parts[2]
                    if not pid.isalnum():
                        return self._send(400, {"error": "bad plan id"})
                    if len(parts) == 3:
                        return self._send(200, plan_store.load_manifest(pid))
                    if len(parts) == 4 and parts[3] == "events":
                        return self._plan_sse(pid)
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            except FileNotFoundError:
                return self._send(404, {"error": "not found"})
            return self._send(404, {"error": "no such route"})

        def do_POST(self):
            try:
                return self._post()
            except (BrokenPipeError, ConnectionResetError):
                return None
            except Exception:
                traceback.print_exc(file=sys.stderr)
                return self._send(500, {"error": "internal error"})

        def _post(self):
            if not self._host_ok():
                return self._send(403, {"error": "wrong host"})
            route = urllib.parse.urlparse(self.path).path
            csrf = self.headers.get("X-Cleanup-CSRF") or ""
            if not self._authed() or not secrets.compare_digest(csrf, app.token):
                return self._send(401, {"error": "unauthorized"})
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            except ValueError:
                return self._send(400, {"error": "bad json"})
            parts = route.strip("/").split("/")
            if route == "/api/plan":
                try:
                    paths = [check_path(p) for p in body.get("paths") or []]
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                if not paths:
                    return self._send(400, {"error": "nothing staged"})
                m = bound_to_roots(plan_manifest.build(paths, keep=read_keep(), classify_fn=app.classify_fn))
                if body.get("overrides"):
                    m = plan_manifest.apply_overrides(m, body["overrides"])
                try:
                    plan_store.save_manifest(m)
                except fsops.Conflict as e:
                    return self._send(409, {"error": "the plan file changed while it was saved; nothing was replaced: %s" % e})
                return self._send(200, m)
            if len(parts) == 4 and parts[:2] == ["api", "plan"] and parts[3] == "stop":
                with app.exec_cond:
                    mine = app.executing and app.activity.get("plan") == parts[2]
                if not mine:
                    return self._send(409, {"error": "that cleanup isn't running"})
                app.exec_stop.set()
                return self._send(200, {"stopping": True})
            if len(parts) == 4 and parts[:2] == ["api", "plan"] and parts[3] in ("execute", "undo"):
                pid = parts[2]
                if not pid.isalnum():
                    return self._send(400, {"error": "bad plan id"})
                try:
                    m = plan_store.load_manifest(pid)
                except FileNotFoundError:
                    return self._send(404, {"error": "no such plan"})
                if parts[3] == "undo":
                    if app.executing:
                        return self._send(409, {"error": "a cleanup is running"})
                    jp = plan_store.journal_path(pid)
                    if not os.path.exists(jp):
                        return self._send(404, {"error": "plan was never executed"})
                    from cleanup.plan import bridge
                    try:
                        with bridge.lock(config.state_dir()):  # never races a cleanup from the CLI or autopilot
                            return self._send(200, plan_undo(jp))
                    except bridge.Busy as e:
                        return self._send(409, {"error": str(e)})
                forced = {} if body.get("forced") is None else body["forced"]
                overrides = {} if body.get("overrides") is None else body["overrides"]
                if not isinstance(forced, dict) or not isinstance(overrides, dict):
                    return self._send(400, {"error": "overrides must be {path: mode}"})
                if overrides or forced:
                    if forced and body.get("confirm") != "delete":
                        return self._send(400, {"error": "overriding the rules needs you to type 'delete'"})
                    try:
                        keep_now = read_keep(strict=True)
                    except config.ConfigError as e:
                        return self._send(409, {"started": False,
                                                "error": f"Afterparty settings are invalid or missing ({e}). Nothing was deleted."})
                    m = plan_manifest.apply_overrides(m, overrides, forced=forced, keep=keep_now)
                m = bound_to_roots(m)  # the settings may have changed since the plan was built
                if any(x.get("forced") for x in plan_manifest.targets(m)) and body.get("confirm") != "delete":
                    return self._send(400, {"error": "this plan overrides the rules — type 'delete' to run it"})
                needs_ack = any(x["mode"] in ("permanent", "remove") for x in plan_manifest.targets(m))
                if needs_ack and body.get("ack") is not True:
                    return self._send(400, {"error": "permanent deletion needs explicit acknowledgement"})
                ok = app.start_execute(m)
                if ok:  # only the plan that actually runs is saved; a rejected attempt leaves the reviewed plan untouched
                    try:
                        plan_store.save_manifest(m)
                    except (OSError, fsops.Conflict):
                        pass  # it's already running and journaling — never report "couldn't start" for a started cleanup
                return self._send(202 if ok else 409, {"started": ok, "error": app.exec_error})
            if route == "/api/agent/settings":
                try:
                    return self._send(200, agent_setup.save_settings(app.agent_settings_file, body))
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                except fsops.Conflict as e:
                    return self._send(409, {"error": "the settings file changed while it was saved, so it was left as it is: %s" % e})
            if route == "/api/agent/enable":
                if body.get("consent") is not True:
                    return self._send(400, {"error": "consent-required"})
                try:
                    agent = {"enabled": True, "read_files": body.get("read_files") is True}
                    cfg = config.update(lambda c: c.update(agent=agent))
                except config.ConfigError as e:
                    return self._send(409, {"error": "settings-invalid", "message": str(e)})
                return self._send(200, cfg["agent"])
            if route in ("/api/agent/preview", "/api/agent/ask"):
                try:
                    agent_cfg = config.load()[0]["agent"]
                except config.ConfigError as e:
                    return self._send(409, {"error": "settings-invalid", "message": str(e)})
                if not agent_cfg["enabled"]:
                    return self._send(403, {"error": "agent-off",
                                            "message": "Ask Claude is off. Turn it on to send this folder's names and "
                                                       "sizes to Claude through your own Claude Code account."})
                try:
                    path = check_path(body.get("path") or "")
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                if not os.path.lexists(path):
                    return self._send(404, {"error": "no such path"})
                ctx = build_context(path, store=app.store, classify_fn=app.classify_fn, keep=read_keep())
                if route == "/api/agent/preview":
                    return self._send(200, {"context": ctx, "prompt_chars": len(agent_prompt(ctx))})
                q = body.get("question") or None
                q = q.strip()[:2000] if isinstance(q, str) and q.strip() else None
                q = q or agent_setup.load_settings(app.agent_settings_file)["agent_default_question"]
                raw = body.get("history") if isinstance(body.get("history"), list) else []
                hist = [{"q": str(t.get("q", ""))[:2000], "a": str(t.get("a", ""))[:2000]}
                        for t in raw[-6:] if isinstance(t, dict)]
                if not body.get("refresh") and not hist:
                    hit = app.agent_cache.get(path, q)
                    if hit:
                        return self._send(200, dict(hit, cached=True, asked=path, question=q))
                claude = app.agent_claude_fn()
                if not claude:
                    return self._send(200, {"ok": False, "setup": True, "asked": path,
                                            "error": "Claude Code isn't installed on this Mac (or Afterparty can't find it)."})
                if not app.agent_slots.acquire(blocking=False):
                    return self._send(429, {"error": "the agent is already busy with two questions"})
                try:
                    res = agent_ask(ctx, q, hist, run_fn=app.agent_run_fn, claude=claude,
                                    read_files=agent_cfg["read_files"],
                                    model=agent_setup.load_settings(app.agent_settings_file)["agent_model"])
                finally:
                    app.agent_slots.release()
                if res["ok"] and not hist:
                    app.agent_cache.put(path, q, res)
                return self._send(200, dict(res, cached=False, asked=path, question=q))
            if route == "/api/mole/refresh":
                if not app.mole_available():
                    return self._send(404, {"error": "Mole isn't installed"})
                with app.cond:  # check-and-set under the lock so two clicks can't start two Mole runs
                    if app.mole_running:
                        return self._send(409, {"error": "Mole is already running"})
                    app.mole_running = True

                def work():
                    try:
                        app.mole_refresh_fn(app.mole_state, keep=read_keep())
                    finally:
                        app.mole_running = False
                threading.Thread(target=work, daemon=True).start()
                return self._send(202, {"started": True})
            if route == "/api/scan":
                path = body.get("path")
                if path is not None:
                    try:
                        path = check_path(path)
                    except ValueError as e:
                        return self._send(400, {"error": str(e)})
                ok = app.start_scan(path)
                return self._send(202 if ok else 409, {"started": ok})
            if route in ("/api/reveal", "/api/keep"):
                try:
                    path = check_path(body.get("path") or "")
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                if route == "/api/reveal":
                    if not os.path.exists(path):
                        return self._send(404, {"error": "not found"})
                    app.reveal_fn(path)
                    return self._send(200, {"revealed": path})
                if any(c in path for c in KEEP_UNSAFE) or path != path.strip():
                    return self._send(400, {"error": "this name can't be stored in the keep list (contains #, a line break or edge spaces)"})
                if not os.path.lexists(path):
                    return self._send(404, {"error": "not found"})
                if classify(path, home=os.path.expanduser("~"), fast=True)["class"] == "protected":
                    return self._send(403, {"error": "already protected by the system rules"})
                rp = os.path.realpath(path)
                if any(rp == k or rp.startswith(k.rstrip("/") + "/") for k in read_keep()):
                    return self._send(409, {"error": "already protected"})
                try:
                    config.add_keep(path)
                except config.KeepPending as e:  # still protecting: the pending entry stays until a merge works
                    app.agent_cache.forget(path)
                    return self._send(409, {"error": str(e), "pending": True})
                except OSError as e:
                    return self._send(500, {"error": "couldn't protect it (%s); nothing changed" % (e.strerror or e)})
                app.agent_cache.forget(path)
                return self._send(200, {"kept": path})
            return self._send(404, {"error": "no such route"})

        def _status(self):
            st = os.statvfs("/System/Volumes/Data") if os.path.exists("/System/Volumes/Data") else os.statvfs("/")
            return {"version": __version__, "scan": app.store.latest(), "scanning": app.scanning, "home": os.path.expanduser("~"),
                    "roots": list(app.roots), "fda": has_full_disk_access(), "python": interpreter(),
                    "fda_help": {"binary": fda_hint(sys.executable)[0], "which": fda_hint(sys.executable)[1],
                                 "steps": "System Settings → Privacy & Security → Full Disk Access → + → ⌘⇧G → "
                                          "paste the path → enable, then restart Afterparty"},
                    "disk": {"total": st.f_blocks * st.f_frsize, "free": st.f_bavail * st.f_frsize}}

        def _sse(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            last = 0
            while True:
                with app.cond:
                    app.cond.wait_for(lambda: any(s > last for s, _ in app.events) or not app.scanning, timeout=15)
                    batch = [(s, e) for s, e in app.events if s > last]
                    done = not app.scanning
                for s, e in batch:
                    last = s
                    try:
                        self.wfile.write(f"data: {json.dumps(e, ensure_ascii=False)}\n\n".encode())
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    if e.get("state") in ("done", "partial", "cancelled", "error"):
                        return
                if done and not batch:
                    return

        def _plan_sse(self, pid):
            with app.exec_cond:
                if pid not in app.exec_events:
                    return self._send(404, {"error": "plan not running"})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            sent = 0
            while True:
                with app.exec_cond:
                    app.exec_cond.wait_for(lambda: len(app.exec_events.get(pid, [])) > sent, timeout=15)
                    batch = app.exec_events.get(pid, [])[sent:]
                for e in batch:
                    sent += 1
                    try:
                        self.wfile.write(f"data: {json.dumps(e, ensure_ascii=False)}\n\n".encode())
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    if e.get("state") in ("done", "error"):
                        return
                if not batch and not app.executing:
                    return

        def _static(self, route):
            if route == "/":
                if not self._authed():
                    return self._send(403, body=b"Open Afterparty with `afterparty map`.", ctype="text/plain")
                name = "index.html"
            else:
                name = route[len("/static/"):]
            full = os.path.normpath(os.path.join(app.web_dir, name))
            if not full.startswith(os.path.normpath(app.web_dir) + os.sep) or not os.path.isfile(full):
                return self._send(404, {"error": "not found"})
            with open(full, "rb") as f:
                data = f.read()
            hdrs = []
            if route == "/":
                # Trade a ?key= for a Strict cookie on this very response (a redirect started from another
                # site would drop it); the page then removes the key from the address bar itself.
                data = data.replace(b"{{CSRF}}", app.token.encode())  # page reads it from <meta name="csrf">
                hdrs.append(("Set-Cookie", f"cleanup_key={app.token}; Path=/; HttpOnly; SameSite=Strict"))
                hdrs.append(("Referrer-Policy", "no-referrer"))
            return self._send(200, body=data, ctype=TYPES.get(os.path.splitext(full)[1], "application/octet-stream"),
                              headers=hdrs)

    return Handler


def serve(host="127.0.0.1", port=0, token=None, store=None, roots=("/",), web_dir=WEB, scan_fn=scan,
          scan_skip=DEFAULT_SKIP, live=False):
    if store is None:
        from cleanup.scanner.store import Store
        store = Store()
    app = App(store, token or secrets.token_hex(16), roots, web_dir, scan_fn, scan_skip)
    if live:
        from cleanup.cli import live_guard
        app.guard_fn = lambda: live_guard(["--apply"])
    server = ThreadingHTTPServer((host, port), make_handler(app))
    server.daemon_threads = True
    app.port = server.server_address[1]
    return server, app
