"""APFS clone awareness: how many bytes deleting a file would actually free (its blocks not shared with a clone)."""
import ctypes
import os
import struct
import threading

_libc = ctypes.CDLL("libc.dylib", use_errno=True) if os.uname().sysname == "Darwin" else None
ATTR_CMNEXT_PRIVATESIZE = 0x00000008
ATTR_CMNEXT_CLONEID, ATTR_CMNEXT_EXT_FLAGS = 0x00000100, 0x00000200
EF_MAY_SHARE_BLOCKS = 0x00000001
FSOPT_NOFOLLOW, FSOPT_ATTR_CMN_EXTENDED = 0x00000001, 0x00000020
UF_COMPRESSED = 0x00000020


class _AttrList(ctypes.Structure):
    _fields_ = [("bitmapcount", ctypes.c_ushort), ("reserved", ctypes.c_uint16), ("commonattr", ctypes.c_uint32),
                ("volattr", ctypes.c_uint32), ("dirattr", ctypes.c_uint32), ("fileattr", ctypes.c_uint32),
                ("forkattr", ctypes.c_uint32)]


def private_size(path):
    """Bytes only this file owns (not shared with an APFS clone). None if unknown."""
    if _libc is None:
        return None
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if st.st_nlink > 1 and not os.path.isdir(path):  # another hard link keeps the data alive
        return 0
    if st.st_flags & UF_COMPRESSED:  # transparently compressed (e.g. installed apps): reports 0, but isn't shared
        return st.st_blocks * 512
    al = _AttrList(5, 0, 0, 0, 0, 0, ATTR_CMNEXT_PRIVATESIZE)
    buf = ctypes.create_string_buffer(16)
    if _libc.getattrlist(os.fsencode(path), ctypes.byref(al), buf, 16, FSOPT_NOFOLLOW | FSOPT_ATTR_CMN_EXTENDED) != 0:
        return None
    length, = struct.unpack_from("<I", buf, 0)
    if length < 12:
        return None
    return struct.unpack_from("<q", buf, 4)[0]


def clone_info(path):
    """(clone id, private bytes) for a file whose blocks may be shared with an APFS clone, else None.
    Files cloned from the same original report the same clone id, so a scanner can count the shared data once
    (like a hard link) and only the private bytes of every further copy."""
    if _libc is None:
        return None
    al = _AttrList(5, 0, 0, 0, 0, 0, ATTR_CMNEXT_PRIVATESIZE | ATTR_CMNEXT_CLONEID | ATTR_CMNEXT_EXT_FLAGS)
    buf = ctypes.create_string_buffer(32)
    if _libc.getattrlist(os.fsencode(path), ctypes.byref(al), buf, 32, FSOPT_NOFOLLOW | FSOPT_ATTR_CMN_EXTENDED) != 0:
        return None
    length, priv, cid, flags = struct.unpack_from("<IqQQ", buf, 0)
    if length < 28 or not flags & EF_MAY_SHARE_BLOCKS or not cid:
        return None
    return cid, max(0, priv)


class SharedCounter:
    """Allocated bytes with APFS clones counted once: the first copy of a clone family counts in full, every other
    copy only its private bytes. Thread-safe."""
    def __init__(self):
        self.seen, self.lock = set(), threading.Lock()

    def size(self, path, st):
        full = st.st_blocks * 512
        if not full:
            return 0
        ci = clone_info(path)
        if ci is None:
            return full
        key = (st.st_dev, ci[0])
        with self.lock:
            if key in self.seen:
                return min(full, ci[1])
            self.seen.add(key)
        return full


def private_tree(path):
    """Sum of private sizes of every regular file under `path` (or of `path` itself). None if any is unknown."""
    if os.path.islink(path):
        return 0
    if not os.path.isdir(path):
        return private_size(path)
    total = 0
    for d, dirs, files in os.walk(path):
        for f in files:
            fp = os.path.join(d, f)
            if os.path.islink(fp):
                continue
            p = private_size(fp)
            if p is None:
                return None
            total += p
    return total
