"""Parallel, hang-proof disk scanner.

Daemon worker threads pull folders from a queue; a watchdog flags any folder whose
read exceeds `timeout` (cloud mounts can block stat forever) and replaces the stuck
worker, so the scan always finishes. Sizes are allocated bytes; hard links count once, and so does data
shared between APFS clones (later copies add only their private bytes); symlinks are never followed.
"""
import os
import queue
import stat
import threading
import time

from cleanup.engine.apfs import SharedCounter

HOME = os.path.expanduser("~")
CLOUD = (HOME + "/Library/CloudStorage", HOME + "/Library/Mobile Documents")
DEFAULT_SKIP = ("/System/Volumes", "/Volumes", "/dev", "/proc", "/private/var/vm") + CLOUD
BIG_FILE = int(os.environ.get("CLEANUP_BIG_FILE", 8 * 2**20))
F_UNREADABLE, F_CLOUD, F_TIMEOUT, F_SKIPPED = 1, 2, 4, 8
MAX_EXTRA_WORKERS = 32


class ScanResult:
    def __init__(self, root, nodes, files, stats, status, took):
        self.root, self.nodes, self.files = root, nodes, files
        self.stats, self.status, self.took = stats, status, took  # status: done | partial | cancelled


def scan(root, workers=8, timeout=20.0, skip=DEFAULT_SKIP, scandir=os.scandir, progress=None, cancel=None):
    root = os.path.abspath(root)
    skip = set(skip)
    t_start = time.time()
    nodes, files, seen_inodes = {}, [], set()
    shared = SharedCounter()
    lock = threading.Lock()
    q = queue.Queue()
    inflight = {}          # path -> start time
    timed_out = set()
    finished = [False]     # set when scan() stops waiting; late results are then discarded
    stats = {"dirs": 0, "files": 0, "bytes": 0, "errors": 0}

    def new_dir(path, parent):
        nodes[path] = {"path": path, "parent": parent, "own": 0, "nfiles": 0,
                       "mtime": 0.0, "flags": 0}

    def visit(path):
        n = nodes[path]
        try:
            with scandir(path) as it:
                entries = list(it)
        except OSError:
            with lock:
                n["flags"] |= F_UNREADABLE
                stats["errors"] += 1
            return
        own = cnt = 0
        newest = 0.0
        subs, bigs = [], []
        for e in entries:
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                with lock:
                    stats["errors"] += 1
                continue
            newest = max(newest, st.st_mtime)
            if stat.S_ISDIR(st.st_mode):
                subs.append(e.path)
                continue
            if stat.S_ISLNK(st.st_mode):
                continue
            if st.st_nlink > 1:
                key = (st.st_dev, st.st_ino)
                with lock:
                    if key in seen_inodes:
                        continue
                    seen_inodes.add(key)
            size = shared.size(e.path, st)
            own += size
            cnt += 1
            if size >= BIG_FILE:
                bigs.append({"path": e.path, "parent": path, "size": size, "mtime": st.st_mtime})
        with lock:
            if finished[0]:
                return
            if path in timed_out:  # slow but healthy: the folder finished before the hard deadline
                timed_out.discard(path)
                n["flags"] &= ~F_TIMEOUT
                stats["errors"] -= 1
            n["own"], n["nfiles"], n["mtime"] = own, cnt, newest
            files.extend(bigs)
            stats["dirs"] += 1
            stats["files"] += cnt
            stats["bytes"] += own
            for s in subs:
                new_dir(s, path)
                if s in skip:
                    nodes[s]["flags"] |= F_CLOUD if s in CLOUD else F_SKIPPED
                else:
                    q.put(s)

    def worker():
        while True:
            path = q.get()
            if path is None:
                return
            with lock:
                inflight[path] = time.time()
            try:
                visit(path)
            finally:
                with lock:
                    inflight.pop(path, None)
                q.task_done()

    def start_worker():
        threading.Thread(target=worker, daemon=True).start()

    new_dir(root, None)
    q.put(root)
    for _ in range(workers):
        start_worker()

    status, last_progress, extra = "done", 0.0, 0
    while True:
        time.sleep(0.02)
        now = time.time()
        with lock:
            for p, t0 in list(inflight.items()):
                if now - t0 > timeout and p not in timed_out:
                    timed_out.add(p)
                    nodes[p]["flags"] |= F_TIMEOUT
                    stats["errors"] += 1
                    if extra < MAX_EXTRA_WORKERS:  # replace the stuck worker
                        extra += 1
                        start_worker()
            pending = q.unfinished_tasks
            stuck = [p for p in inflight if p in timed_out]
            live = workers + extra - len(stuck)
            past_hard = all(now - inflight[p] > timeout * 3 for p in stuck)
        if cancel is not None and cancel.is_set():  # checked first: a cancel always wins
            status = "cancelled"
            break
        if pending == 0:
            break
        if q.empty() and pending == len(stuck) and past_hard:
            break  # only hung folders remain
        if live <= 0 and not q.empty():
            status = "partial"  # every worker is stuck: stop instead of hanging forever
            break
        if progress and now - last_progress > 0.5:
            last_progress = now
            with lock:
                snap = dict(stats)
            progress(snap)

    with lock:
        finished[0] = True
    drained = []
    while True:  # empty the queue so workers stop instead of crawling on after we return
        try:
            p = q.get_nowait()
        except queue.Empty:
            break
        if p is not None:
            drained.append(p)
        q.task_done()
    with lock:
        if status == "partial":
            for p in drained:
                nodes[p]["flags"] |= F_TIMEOUT
    for _ in range(workers + extra):
        q.put(None)

    with lock:
        result_nodes = {}
        for p, n in nodes.items():
            result_nodes[p] = {"path": p, "parent": n["parent"], "size": n["own"], "files": n["nfiles"],
                               "dirs": 0, "mtime": n["mtime"], "flags": n["flags"]}
        big = list(files)
        final_stats = dict(stats)
    for p in sorted(result_nodes, key=lambda x: x.count("/"), reverse=True):
        n = result_nodes[p]
        par = n["parent"]
        if par is not None and par in result_nodes:
            P = result_nodes[par]
            P["size"] += n["size"]
            P["files"] += n["files"]
            P["dirs"] += n["dirs"] + 1
            P["mtime"] = max(P["mtime"], n["mtime"])
    return ScanResult(root, result_nodes, big, final_stats, status, round(time.time() - t_start, 2))
