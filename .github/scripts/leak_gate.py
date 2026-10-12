#!/usr/bin/env python3
"""Leak gate (spec 6.2): exit 1 when a tree holds anything personal, private or secret. It fails closed: anything
it cannot check completely is a finding, never a pass.

    python3 .github/scripts/leak_gate.py TREE [--deny FILE] [--media-tools required|optional|skip] [--check-commits]

Every entry (file, folder, link) is checked by name, every link by its target (and may not point outside the tree),
every file's whole text (binary files: their printable strings). Images and fonts are checked with exiftool, video
and audio with ffprobe; files no tool can read must be pinned in .github/leak-gate-pins.txt. A pin records a human
review of those exact bytes: a pinned binary whose checksum matches skips the printable-strings scan (compressed media
is random bytes) but still gets every metadata check; a pinned text file is scanned in full. --check-commits also
scans every blob in the history, every commit message and every author/committer. Exit 0 clean, 1 findings, 2 usage.
Stdlib only; Python 3.9+."""
import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys

USER = bytes.fromhex("64726f726c617a6172").decode()   # the GitHub username, in hex so this file passes its own rule
FIRST, LAST = USER[:4], USER[4:]
OWNER = FIRST.capitalize() + " " + LAST.capitalize()
OWNER_FILES = {"LICENSE", "NOTICE"}   # exact paths from the tree's root (spec 6.2, rev 5): the copyright line only there
NOREPLY = re.compile(r"^\d+\+" + USER + r"@users\.noreply\.github\.com$")
NOT_EMAIL = r"(?![\w.-]*@)"           # an approved form never masks the start of an e-mail address
JOB_LABELS = r"(?:\.(?:weekly|spacecheck|server))?"   # the real launchd labels; nothing else may follow the prefix
ALLOWED = re.compile("|".join([
    USER + r"/afterparty\b" + NOT_EMAIL, r"io\.github\." + USER + r"\.afterparty" + JOB_LABELS + r"\b" + NOT_EMAIL,
    USER + r"\.github\.io/afterparty\b" + NOT_EMAIL, r"\d+\+" + USER + r"@users\.noreply\.github\.com"]), re.I)
NAME = re.compile(FIRST + "|" + LAST, re.I)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
# in the printable strings of a binary: an address with a real domain (dot-separated labels, a final TLD of 2+
# letters), so random bytes of compressed media (a digit, an at sign, a letter, a dot, a letter) aren't addresses;
# deny words count there only in readable runs of BIN_DENY_MIN_RUN+ printable characters, on word boundaries
BIN_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,}"
                       r"(?![A-Za-z0-9-])")
BIN_DENY_MIN_RUN = 8
HOME_PATH = re.compile(r"/Users/(?!(?:u|Shared)(?![A-Za-z0-9._-]))[A-Za-z0-9._-]+", re.I)
# a machine on a private network: mDNS .local, and the usual home/office/cloud-internal suffixes
HOSTNAME = re.compile(r"(?<![\w.-])[A-Za-z][A-Za-z0-9-]*"
                      r"\.(?:local|lan|localdomain|internal|intranet|home\.arpa)\b(?![\w.-])", re.I)
# RFC 1918, carrier-grade NAT (100.64/10, e.g. Tailscale), IPv4 link-local (169.254/16); IPv6 unique-local (the fc
# and fd prefixes) and link-local (fe8 to feb)
PRIVATE_IP = re.compile(r"(?<![\d.])(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|"
                        r"172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}|"
                        r"100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])(?:\.\d{1,3}){2}|169\.254(?:\.\d{1,3}){2})(?![\d.])"
                        r"|(?<![\w:.])(?:f[cd][0-9a-f]{2}|fe[89ab][0-9a-f]):(?:[0-9a-f]{0,4}:){1,6}[0-9a-f]{0,4}"
                        r"(?![\w:])", re.I)
SECRETS = [
    ("aws-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    # an AWS secret access key: 40 characters of [A-Za-z0-9/+=], digits optional (G0 M6)
    ("aws-secret", re.compile(r"(?i)(?:aws[\w-]*secret|secret[_-]?access[_-]?key)[\w-]*[\"']?\s*[:=]\s*[\"']?"
                              r"[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=])")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{40,})")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("google-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("huggingface-token", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    ("stripe-key", re.compile(r"\b[sr]k_live_[A-Za-z0-9]{20,}")),
    ("google-oauth-secret", re.compile(r"\bGOCSPX-[A-Za-z0-9_-]{20,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("generic-secret", None),   # SecretAssignments below: any secret-like key, by its words (G0 rounds 2-3)
]
SECRET_WORDS = {"secret", "secrets", "token", "tokens", "password", "passwords", "passwd", "pwd", "passphrase",
                "key", "keys", "apikey", "auth", "credential", "credentials", "creds"}
ASSIGNMENT = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z][A-Za-z0-9_-]*)[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9_\-/+=]{20,})"
                        r"(?![A-Za-z0-9_\-/+=(.\[])")
ARGUMENT_LABELS = {"forkey", "forkeypath", "keypath", "forauth"}   # Swift/ObjC API labels name a key, they don't hold one
WORD = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def secret_like(name):
    """Whether an identifier names a secret: any of its words (split at _, -, digits and camelCase) is a secret word,
    or it says api + key. `clientSecret`, `accessToken`, `encryptionKey`, `API_KEY`; not `monkey` or `author`."""
    if name.lower() in ARGUMENT_LABELS:
        return False
    words = [w.lower() for w in WORD.findall(name)]
    return any(w in SECRET_WORDS for w in words) or any(a == "api" and b in ("key", "keys")
                                                      for a, b in zip(words, words[1:]))


class _SecretAssignments:
    """Ruling (G0 round 2): a secret-like key assigned a quoted or bare value of 20+ characters from
    [A-Za-z0-9_-/+=], whatever its case or digits. A call or attribute (`x(`, `x.y`, `x[`) isn't a value."""

    def finditer(self, line):
        return (m for m in ASSIGNMENT.finditer(line) if secret_like(m.group(1)))


SECRETS = [(rule, _SecretAssignments() if rx is None else rx) for rule, rx in SECRETS]
IMAGES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff", ".heic", ".ico", ".icns", ".pdf"}
FONTS = {".woff", ".woff2", ".ttf", ".otf"}
AV = {".mp4", ".mov", ".m4v", ".webm", ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus"}
SKIP_GROUPS = ("System", "File", "ExifTool", "Composite")
SENSITIVE = re.compile(r"(?i)(author|creator|artist|owner|copyright|rights|comment|hostcomputer|^make$|^model$|serial|"
                       r"gps|location|city|country|creatortool|software|history|documentid|instanceid|derivedfrom|"
                       r"keywords|subject|title|description|caption|headline|credit|^source$|email|contact|url|"
                       r"webstatement|nickname|personinimage|username)")
AV_OK = {"major_brand", "minor_version", "compatible_brands", "encoder", "handler_name", "vendor_id", "language",
         "creation_time", "duration"}
def _xmp_bytes():
    """Any XMP packet, wrapped or bare, with any RDF prefix, in ASCII/UTF-8 or UTF-16 (LE or BE)."""
    ascii_parts = [rb"<\?xpacket", rb"x:xmpmeta", rb"<\w+:RDF\b", rb"ns\.adobe\.com/xap/"]
    wide = []
    for lit in ("<?xpacket", "x:xmpmeta", "ns.adobe.com/xap/"):
        for order in ("le", "be"):
            enc = "".join(re.escape(c) + "\0" if order == "le" else "\0" + re.escape(c) for c in lit)
            wide.append(enc.encode("latin-1"))
    wide.append(rb"<\0(?:\w\0)+:\0R\0D\0F\0")    # <prefix:RDF in UTF-16LE
    wide.append(rb"\0<(?:\0\w)+\0:\0R\0D\0F")    # and in UTF-16BE
    return re.compile(b"|".join(ascii_parts + wide), re.I)


XMP_BYTES = _xmp_bytes()
PINS = os.path.join(".github", "leak-gate-pins.txt")
CONTROL = re.compile(r"[\x00-\x08\x0e-\x1a\x1c-\x1f\x7f\ufffd]")   # not tab/newline/CR/FF/VT/ESC


def max_bytes():
    v = os.environ.get("AFTERPARTY_LEAK_MAX_BYTES", "")
    return int(v) if v.isdigit() else 512 * 2 ** 20


def email_ok(addr):
    a = addr.lower()
    if a == "git@github.com" or NOREPLY.match(a):
        return True
    domain = a.rsplit("@", 1)[1]
    return domain in ("example.com", "example.org", "example.net", "localhost") or \
        domain.endswith((".invalid", ".test", ".example"))


def _bin_deny(masked, deny):
    """Deny matches in a binary's printable strings: only inside a readable run (BIN_DENY_MIN_RUN+ printable chars,
    one run per line) and on word boundaries of that run, whatever form the rule has."""
    out = []
    for run in masked.split("\n"):
        if len(run) < BIN_DENY_MIN_RUN:
            continue
        for rx in deny:
            for m in rx.finditer(run):
                a, b = m.span()
                if m.group() and not (a > 0 and run[a - 1].isalnum()) and not (b < len(run) and run[b].isalnum()):
                    out.append(m.group())
    return out


def check_text(rel, where, line, deny, owner_file=False, binary=False):
    """Every rule on one line of text. binary=True: `line` holds a binary file's printable strings (one run per
    line), where an e-mail needs a real domain and a deny word a readable run (BIN_EMAIL, _bin_deny)."""
    found = []
    line = line.replace("\\/", "/")                      # JSON escapes every slash as \/
    masked = ALLOWED.sub(lambda m: " " * len(m.group()), line)
    if owner_file:
        masked = masked.replace(OWNER, " " * len(OWNER))
    for m in NAME.finditer(masked):
        found.append((rel, where, "identity", m.group()))
    for m in (BIN_EMAIL if binary else EMAIL).finditer(masked):
        if not email_ok(m.group()):
            found.append((rel, where, "email", m.group()))
    # on the raw line: an approved form (USER/afterparty) must never hide a home path around it
    for rule, rx in (("home-path", HOME_PATH), ("hostname", HOSTNAME), ("private-ip", PRIVATE_IP)):
        for m in rx.finditer(line):
            found.append((rel, where, rule, m.group()))
    for rule, rx in SECRETS:
        for m in rx.finditer(line):
            found.append((rel, where, rule, m.group()[:12] + "..."))
    if binary:
        found += [(rel, where, "deny", w) for w in _bin_deny(masked, deny)]
    else:
        for rx in deny:
            for m in rx.finditer(masked):
                found.append((rel, where, "deny", m.group()))
    return found


def _utf16_order(sample):
    """'utf-16-le'/'utf-16-be' when the bytes alternate with NULs like UTF-16 text, else None."""
    even, odd = sample[0::2], sample[1::2]
    if len(odd) < 2:
        return None
    nul_even, nul_odd = even.count(0) / len(even), odd.count(0) / len(odd)
    if nul_odd > 0.9 and nul_even < 0.1:
        return "utf-16-le"
    if nul_even > 0.9 and nul_odd < 0.1:
        return "utf-16-be"
    return None


def as_text(data):
    """The file's text (UTF-8, or UTF-16 by BOM or NUL pattern), or None for a binary file. Text that decodes but
    is more than 1% control characters is binary too, so a crafted header can't pass a binary off as text."""
    try:
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            # a BOM alone proves nothing: the body must look like UTF-16 text too (non-ASCII UTF-16 = binary, pin it)
            order = "utf-16-le" if data.startswith(b"\xff\xfe") else "utf-16-be"
            if len(data) > 2 and _utf16_order(data[2:8194]) != order:
                return None
            text = data[2:].decode(order)
        else:
            order = _utf16_order(data[:8192]) if b"\0" in data[:8192] else None
            if order:
                text = data.decode(order)
            elif b"\0" in data:
                return None
            else:
                text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    controls = len(CONTROL.findall(text))
    return None if controls and controls * 100 > len(text) else text


def binary_strings(data):
    """Every printable ASCII run, however short, and every UTF-16 run (LE and BE), one per line."""
    runs = re.sub(rb"[^\x20-\x7e]+", b"\n", data).decode("ascii")
    wide = [m.decode("utf-16-le") for m in re.findall(rb"(?:[\x20-\x7e]\x00){2,}", data)]
    wide += [m.decode("utf-16-be") for m in re.findall(rb"(?:\x00[\x20-\x7e]){2,}", data)]
    return runs + "\n" + "\n".join(wide)


def check_bytes(rel, data, deny, owner_file=False, where_prefix="", pinned=False):
    """(findings, is_binary): every line of a text file, every printable string of a binary one. `pinned`: these
    exact bytes match a pin (a human review), so a binary's strings aren't scanned; text always is."""
    text = as_text(data)
    if text is None:
        if pinned:
            return [], True
        return check_text(rel, where_prefix + "bin", binary_strings(data), deny, binary=True), True
    found = []
    for i, line in enumerate(text.splitlines(), 1):
        found += check_text(rel, "%s%d" % (where_prefix, i) if where_prefix else i, line, deny, owner_file)
    return found, False


def load_deny(path):
    rules = []
    if not path:
        return rules
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("re:"):
                rules.append(re.compile(line[3:], re.I))
            else:
                rules.append(re.compile(r"(?<![A-Za-z0-9])" + re.escape(line) + r"(?![A-Za-z0-9])", re.I))
    return rules


def machine_names():
    """This Mac's own names: never in a public tree."""
    names = {socket.gethostname()}
    for key in ("ComputerName", "LocalHostName", "HostName"):
        try:
            r = subprocess.run(["/usr/sbin/scutil", "--get", key], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 0:
            names.add(r.stdout.strip())
    rules = []
    for n in names:
        n = re.sub(r"\.local$", "", n.strip())
        if len(n) >= 4 and n.lower() not in ("localhost", "macbook", "mac-mini", "imac"):
            rules.append(re.compile(r"(?<![A-Za-z0-9])" + re.escape(n) + r"(?![A-Za-z0-9])", re.I))
    return rules


def scan_tree(root, deny):
    files, findings = [], []
    real_root = os.path.realpath(root)
    limit = max_bytes()

    def unreadable(err):  # a folder we can't list is a finding, never a silent skip
        findings.append((os.path.relpath(err.filename or root, root), 0, "unreadable", str(err)))
    for d, dirs, names in os.walk(root, onerror=unreadable):
        if d == root:
            dirs[:] = [x for x in dirs if x != ".git"]
        dirs.sort()
        for n in sorted(dirs + names):
            p = os.path.join(d, n)
            rel = os.path.relpath(p, root)
            findings += check_text(rel, "name", rel, deny)
            if os.path.islink(p):
                target = os.readlink(p)
                findings += check_text(rel, "link", target, deny)
                rp = os.path.realpath(p)
                if not (rp == real_root or rp.startswith(real_root + os.sep)):
                    findings.append((rel, "link", "link-escape", "points outside the tree: " + target))
            elif n in names:
                files.append(p)
    pins, hashes = load_pins(root), set()
    pinned = set()     # rel paths whose bytes match their pin
    for p in files:
        rel = os.path.relpath(p, root)
        try:
            st = os.lstat(p)
            if not stat.S_ISREG(st.st_mode):   # a FIFO, socket or device: opening it can hang or read forever
                findings.append((rel, 0, "special-file", "not a regular file, folder or link; it can't be checked"))
                continue
            size = st.st_size
            if size > limit:
                findings.append((rel, 0, "too-large", "%d bytes; files over %d bytes can't be checked" % (size, limit)))
                continue
            fd = os.open(p, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as f:
                if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):   # swapped for something else since lstat
                    raise OSError("not a regular file any more")
                data = f.read()
        except OSError as e:
            findings.append((rel, 0, "unreadable", str(e)))
            continue
        digest = hashlib.sha256(data).hexdigest()
        hashes.add(digest)
        if rel in pins and pins[rel] != digest:
            findings.append((rel, "meta", "pin-mismatch", "changed since it was pinned; check it and re-pin"))
        ok = rel in pins and pins[rel] == digest
        found, binary = check_bytes(rel, data, deny, rel in OWNER_FILES, pinned=ok)
        findings += found
        if ok and binary and not os.path.islink(p):
            pinned.add(rel)
        # a binary no metadata tool covers (zip, gz, plist, psd, flac...) can hide anything: pinned or blocked
        if binary and os.path.splitext(p)[1].lower() not in IMAGES | FONTS | AV and rel not in pins:
            findings.append((rel, 0, "unknown-binary", "a binary file of a type no tool checks; pin it "
                             "(sha256 %s) after checking it by hand" % digest))
    return files, findings, hashes, pinned


def load_pins(root):
    pins = {}
    try:
        with open(os.path.join(root, PINS), encoding="utf-8") as f:
            for line in f:
                parts = line.split(None, 2)
                if len(parts) >= 2 and not line.startswith("#"):
                    pins[parts[1]] = parts[0]
    except FileNotFoundError:
        pass
    return pins


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _exif(exiftool, paths, xmp_only=False):
    """exiftool's answer per file, or None for a file it couldn't read. Never trusts a failed run."""
    r = subprocess.run([exiftool, "-j", "-a", "-G1", "-s"] + (["-XMP:all"] if xmp_only else []) + ["--"] + paths,
                       capture_output=True, text=True, timeout=600)
    try:
        entries = json.loads(r.stdout) if r.stdout.strip() else []
    except ValueError:
        entries = []
    by_path = {}
    if isinstance(entries, list):
        for e in entries:
            if isinstance(e, dict) and isinstance(e.get("SourceFile"), str):
                by_path[os.path.abspath(e["SourceFile"])] = e
    out = {}
    for p in paths:
        e = by_path.get(os.path.abspath(p))
        # a Warning means exiftool didn't read the whole file (a truncated or odd segment): not a full read
        bad = e is None or any(k.split(":")[-1] in ("Error", "Warning") for k in e)
        out[p] = None if bad or (r.returncode != 0 and len(paths) == 1) else e
    if r.returncode != 0 and len(paths) > 1:
        for p in paths:                     # find exactly which files failed
            out[p] = _exif(exiftool, [p], xmp_only)[p]
    return out


def check_media(root, files, deny, mode, pinned=frozenset()):
    """Every media file's metadata, pinned or not. A pin (`pinned`: rel paths whose bytes match their pin) only
    excuses a file no tool can read (fonts, some icons); XMP or a sensitive tag in a pinned file is still a finding."""
    if mode == "skip":
        return []
    findings = []
    ext = lambda p: os.path.splitext(p)[1].lower()  # noqa: E731
    rel = lambda p: os.path.relpath(p, root)  # noqa: E731
    todo = [p for p in files if ext(p) in IMAGES | FONTS | AV and not os.path.islink(p)]

    def unreadable(p, msg):
        if rel(p) not in pinned:
            findings.append((rel(p), "meta", "metadata-unreadable", msg))
    for p in todo:  # Ruling (G0 round 2): any XMP packet in shipped media is a finding, pinned or not
        with open(p, "rb") as f:
            if XMP_BYTES.search(f.read()):
                findings.append((rel(p), "meta", "metadata", "an XMP metadata packet is embedded (strip it)"))
    img = [p for p in todo if ext(p) in IMAGES | FONTS]
    av = [p for p in todo if ext(p) in AV]
    for group, exe, kind in ((img, "exiftool", "images and fonts"), (av, "ffprobe", "video and audio files")):
        if group and not shutil.which(exe):
            msg = "%s isn't installed, so the metadata of %d %s can't be checked" % (exe, len(group), kind)
            if mode == "required":
                findings.append((".", 0, "media-tool", msg))
            else:
                print("leak gate: warning: " + msg, file=sys.stderr)
    exiftool, ffprobe = shutil.which("exiftool"), shutil.which("ffprobe")
    if img and exiftool:
        for i in range(0, len(img), 200):
            for p, e in _exif(exiftool, img[i:i + 200]).items():
                if e is None:
                    unreadable(p, "exiftool couldn't read it; pin it (sha256 %s) after checking it by hand" % _sha(p))
                    continue
                font = ext(p) in FONTS
                if any(k.split(":", 1)[0].startswith("XMP") for k in e):
                    findings.append((rel(p), "meta", "metadata", "XMP metadata is set (strip it)"))
                for key, value in e.items():
                    group, _, tag = key.partition(":")
                    if key == "SourceFile" or (group in SKIP_GROUPS and not (group == "File" and SENSITIVE.search(tag))):
                        continue  # File:Comment (a JPEG COM segment) is the file's content, not exiftool's (G0 M7)
                    text = value if isinstance(value, str) else json.dumps(value)
                    findings += check_text(rel(p), "meta:" + key, text, deny)
                    if not font and not group.startswith("ICC") and SENSITIVE.search(tag) and text.strip():
                        findings.append((rel(p), "meta", "metadata", key + " is set (strip it)"))
    if av and exiftool:  # G0 round 3 (P2-4): XMP read structurally; fail closed when exiftool can't read the file
        for i in range(0, len(av), 200):
            for p, e in _exif(exiftool, av[i:i + 200], xmp_only=True).items():
                if e is None:
                    unreadable(p, "exiftool couldn't read its XMP; pin it (sha256 %s) after checking it by hand"
                               % _sha(p))
                elif any(k.split(":", 1)[0].startswith("XMP") for k in e):
                    findings.append((rel(p), "meta", "metadata", "XMP metadata is set (strip it)"))
    elif av and mode == "required":
        findings.append((".", 0, "media-tool", "exiftool isn't installed, so the XMP of %d video and audio files can't "
                         "be checked" % len(av)))
    if av and ffprobe:
        for p in av:
            r = subprocess.run([ffprobe, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams",
                                "-show_chapters", "-show_programs", p], capture_output=True, text=True, timeout=120)
            try:
                info = json.loads(r.stdout) if r.returncode == 0 else None
            except ValueError:
                info = None
            if not isinstance(info, dict) or not isinstance(info.get("format"), dict):
                unreadable(p, "ffprobe couldn't read it")
                continue
            tags = [("format", info["format"].get("tags") or {})]
            tags += [("stream%d" % n, s.get("tags") or {}) for n, s in enumerate(info.get("streams") or [])]
            # chapters and programs carry their own tags (G0 M7)
            tags += [("chapter%d" % n, c.get("tags") or {}) for n, c in enumerate(info.get("chapters") or [])]
            for n, prog in enumerate(info.get("programs") or []):
                tags.append(("program%d" % n, prog.get("tags") or {}))
                tags += [("program%d.stream%d" % (n, m), s.get("tags") or {})
                         for m, s in enumerate(prog.get("streams") or []) if isinstance(s, dict)]
            if any(not isinstance(t, dict) for _, t in tags):
                unreadable(p, "ffprobe's answer isn't in the expected form")
                continue
            for where, t in tags:
                for k, v in t.items():
                    findings += check_text(rel(p), "meta:%s.%s" % (where, k), str(v), deny)
                    if k.lower() not in AV_OK:
                        findings.append((rel(p), "meta", "metadata", "%s tag %s is set (strip it)" % (where, k)))
    return findings


GIT_REDIRECT = {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM",
                "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_REPLACE_REF_BASE", "GIT_GRAFT_FILE",
                "GIT_SHALLOW_FILE", "GIT_QUARANTINE_PATH"}


def _git(root, *args, text=True, stdin=None):
    git = shutil.which("git") or "/usr/bin/git"
    # `git replace` must not swap in clean-looking objects; an inherited GIT_DIR & co. (a hook, an editor task) must
    # not point `-C root` at another repository, and neither may config injected through the environment
    env = {k: v for k, v in os.environ.items() if k not in GIT_REDIRECT and not k.startswith("GIT_CONFIG_KEY_")
           and not k.startswith("GIT_CONFIG_VALUE_")}
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    return subprocess.run([git, "-C", root] + list(args), capture_output=True, text=text, input=stdin, env=env)


def _identity_ok(name, email):
    return name == USER and bool(NOREPLY.match(email))


def check_tags(root, tags, deny):
    """Annotated tag objects (and tags of tags): tagger identity and message, like a commit."""
    out, seen, todo = [], set(), sorted(tags)
    while todo:
        sha = todo.pop()
        if sha in seen:
            continue
        seen.add(sha)
        r = _git(root, "cat-file", "tag", sha)
        if r.returncode != 0:
            out.append((".git", 0, "history", "can't read tag object " + sha))
            continue
        head, _, body = r.stdout.partition("\n\n")
        fields = dict(line.split(" ", 1) for line in head.splitlines() if " " in line)
        where = "tag:" + fields.get("tag", sha[:8])
        m = re.match(r"(.*) <([^>]*)>", fields.get("tagger", ""))
        if not (m and _identity_ok(m.group(1), m.group(2))):
            out.append((".git", where, "commit-identity", "tagger: " + fields.get("tagger", "(none)")))
        out += check_text(".git", where + ":name", fields.get("tag", ""), deny)
        for n, line in enumerate(body.splitlines(), 1):
            out += check_text(".git", "%s:msg%d" % (where, n), line, deny)
        if fields.get("type") == "tag" and fields.get("object"):
            todo.append(fields["object"])
    return out


def _ls_tree(root, ish, paths, entry_paths):
    """Every entry of the tree-ish `ish` with its full path (git ls-tree -r -t): blob, folder and gitlink paths go to
    `entry_paths`, each blob's path to `paths`. False when it can't be read."""
    tree = _git(root, "ls-tree", "-r", "-t", "-z", "--full-tree", ish)
    if tree.returncode != 0:
        return False
    for entry in tree.stdout.split("\0"):
        meta, _, path = entry.partition("\t")
        parts = meta.split()
        if len(parts) == 3 and path:
            entry_paths.add(path)
        if len(parts) == 3 and parts[1] == "blob":
            paths.setdefault(parts[2], set()).add(path)
    return True


def check_history(root, deny, tree_hashes=frozenset(), pinned_hashes=frozenset()):
    """Every commit's and tag's identity and message, every ref name, and every object ever committed: names and
    contents. A shallow clone fails: its history is incomplete."""
    out = []
    shallow = _git(root, "rev-parse", "--is-shallow-repository")
    if shallow.returncode != 0 or shallow.stdout.strip() != "false":
        return [(".git", 0, "history", "a shallow or unreadable repository: the whole history can't be checked")]
    r = _git(root, "log", "--all", "--format=%an%x09%ae%x09%cn%x09%ce")
    if r.returncode != 0:
        return [(".git", 0, "commit-identity", "not a git repository with commits")]
    for n, line in enumerate(r.stdout.splitlines(), 1):
        an, ae, cn, ce = (line.split("\t") + ["", "", "", ""])[:4]
        if not (_identity_ok(an, ae) and _identity_ok(cn, ce)):
            out.append((".git", n, "commit-identity", "%s <%s> / %s <%s>" % (an, ae, cn, ce)))
    msgs = _git(root, "log", "--all", "--format=%B")
    if msgs.returncode != 0:
        return out + [(".git", 0, "history", "can't read the commit messages")]
    for n, line in enumerate(msgs.stdout.splitlines(), 1):
        out += check_text(".git", "msg%d" % n, line, deny)
    refs = _git(root, "for-each-ref", "--format=%(refname)%09%(objecttype)%09%(objectname)")
    if refs.returncode != 0:
        return out + [(".git", 0, "history", "can't list the refs")]
    tags = set()
    for line in refs.stdout.splitlines():
        name, kind, sha = (line.split("\t") + ["", "", ""])[:3]
        out += check_text(".git", "ref", name, deny)
        if kind == "tag":
            tags.add(sha)
    out += check_tags(root, tags, deny)
    commits = _git(root, "rev-list", "--all")
    if commits.returncode != 0:
        return out + [(".git", 0, "history", "can't list the history")]
    paths = {}                                  # blob -> every path it was ever committed at, in any commit
    entry_paths = set()                         # every path of every entry: blob, tree and gitlink (G0 M8)
    for c in commits.stdout.split():
        if not _ls_tree(root, c, paths, entry_paths):
            out.append((".git", 0, "history", "can't read commit " + c))
    # G0 round 3 (P2-5, S-2): every ref, peeled through any tags, that ends at a tree is a root of its own, walked
    # with full paths like a commit's tree (a tree reused under several roots is checked under every path); one
    # that ends at a blob gives that blob a tag-only alias (so it never gets the LICENSE/NOTICE exemption)
    ref_names = [line.split("\t", 1)[0] for line in refs.stdout.splitlines() if line]
    peeled = _git(root, "cat-file", "--batch-check=%(objectname) %(objecttype)",
                  stdin="".join(n + "^{}\n" for n in ref_names))
    if peeled.returncode != 0 or len(peeled.stdout.splitlines()) != len(ref_names):
        return out + [(".git", 0, "history", "can't resolve the refs")]
    for name, line in zip(ref_names, peeled.stdout.splitlines()):
        sha, _, kind = line.partition(" ")
        if kind == "tree" and not _ls_tree(root, sha, paths, entry_paths):
            out.append((".git", 0, "history", "can't read tree " + sha))
        elif kind == "blob":
            paths.setdefault(sha, set()).add("(tagged blob %s)" % name)
        elif kind not in ("tree", "blob", "commit"):
            out.append((".git", 0, "history", "can't resolve " + name))
    # objects reachable some other way (a tag pointing straight at a blob or a tree)
    objs = _git(root, "rev-list", "--objects", "--all")
    if objs.returncode != 0:
        return out + [(".git", 0, "history", "can't list every object")]
    kinds = _git(root, "cat-file", "--batch-check=%(objectname) %(objecttype)",
                 stdin="".join(line.split(" ", 1)[0] + "\n" for line in objs.stdout.splitlines()))
    if kinds.returncode != 0 or len(kinds.stdout.splitlines()) != len(objs.stdout.splitlines()):
        return out + [(".git", 0, "history", "can't list every object")]
    names = dict((line.split(" ", 1) + [""])[:2] for line in objs.stdout.splitlines())
    for line in kinds.stdout.splitlines():   # a safety net: every blob also under the name rev-list gives it
        sha, _, kind = line.partition(" ")
        if kind == "blob":
            paths.setdefault(sha, set()).add(names.get(sha) or "(tagged blob)")
    for path in sorted({p for ps in paths.values() for p in ps} | entry_paths):
        out += check_text("history:" + path, "name", path, deny)
    for sha, where in sorted(paths.items()):
        blob = _git(root, "cat-file", "-p", sha, text=False)
        if blob.returncode != 0:
            out.append((".git", 0, "history", "can't read object " + sha))
            continue
        # the copyright exemption holds only if every path this content ever had is a root LICENSE/NOTICE
        owner_only = bool(where) and all(p in OWNER_FILES for p in where)
        shown = sorted(p for p in where if p not in OWNER_FILES) or sorted(where) or ["?"]
        rel = "history:%s@%s" % (shown[0], sha[:8])
        # content identical to a pinned file of the current tree had its pin and metadata checked there
        found, binary = check_bytes(rel, blob.stdout, deny, owner_only,
                                    pinned=hashlib.sha256(blob.stdout).hexdigest() in pinned_hashes)
        out += found
        # binary content the current tree doesn't hold never had its metadata checked
        if binary and hashlib.sha256(blob.stdout).hexdigest() not in tree_hashes:
            out.append((rel, 0, "history", "binary content that isn't in the current tree; its metadata can't be "
                        "checked (rewrite the history without it)"))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="leak_gate.py")
    ap.add_argument("tree")
    ap.add_argument("--deny")
    ap.add_argument("--media-tools", choices=("required", "optional", "skip"), default="required")
    ap.add_argument("--check-commits", action="store_true")
    a = ap.parse_args(argv)
    root = os.path.abspath(a.tree)
    if not os.path.isdir(root):
        print("leak gate: %s isn't a folder" % a.tree, file=sys.stderr)
        return 2
    try:
        deny = load_deny(a.deny) + machine_names()
    except (OSError, re.error) as e:
        print("leak gate: can't read the deny list: %s" % e, file=sys.stderr)
        return 2
    files, findings, hashes, pinned = scan_tree(root, deny)
    findings += check_media(root, files, deny, a.media_tools, pinned)
    if a.check_commits:
        pins = load_pins(root)
        findings += check_history(root, deny, hashes, {pins[r] for r in pinned})
    for rel, where, rule, what in findings:
        print("%s:%s: %s: %s" % (rel, where, rule, what))
    print("leak gate: %s (%d files)" % ("%d finding(s)" % len(findings) if findings else "clean", len(files)))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
