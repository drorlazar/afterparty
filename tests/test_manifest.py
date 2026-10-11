import os
import time

from cleanup.plan import manifest as M

def fake_classify(path, mtime=None, keep=(), home=None, fast=False):
    name = os.path.basename(path)
    if name == "node_modules":
        return {"class": "regenerable", "action": "permanent", "rule": "dependency", "reason": "deps"}
    if name.startswith("sys"):
        return {"class": "protected", "action": "none", "rule": "system", "reason": "system"}
    return {"class": "review", "action": "trash", "rule": "frames" if "renders" in path else "unmatched", "reason": "personal"}


def tree(tmp):
    (tmp / "p/node_modules/x").mkdir(parents=True); (tmp / "p/node_modules/x/a.js").write_bytes(b"x" * 5000)
    (tmp / "p/renders/v1").mkdir(parents=True); (tmp / "p/renders/v1/f.png").write_bytes(b"y" * 7000)
    (tmp / "p/renders/v2").mkdir(parents=True); (tmp / "p/renders/v2/f.png").write_bytes(b"y" * 7000)
    (tmp / "p/renders/master").mkdir(parents=True); (tmp / "p/renders/master/f.png").write_bytes(b"y" * 7000)
    (tmp / "sysstuff").mkdir(); (tmp / "doc.txt").write_text("hello")
    return tmp


def test_modes_follow_verdicts(tmp_path):
    t = tree(tmp_path)
    m = M.build([str(t / "p/node_modules"), str(t / "doc.txt"), str(t / "sysstuff")], classify_fn=fake_classify)
    modes = {os.path.basename(i["path"]): i["mode"] for i in m["items"]}
    assert modes == {"node_modules": "permanent", "doc.txt": "trash", "sysstuff": "skip"}
    assert m["totals"]["permanent"] > 0 and m["totals"]["trash"] > 0
    assert all("identity" in i for i in m["items"] if i["mode"] != "skip")


def test_overlapping_selections_deduplicated(tmp_path):
    t = tree(tmp_path)
    m = M.build([str(t / "p/node_modules/x"), str(t / "p/node_modules")], classify_fn=fake_classify)
    assert [os.path.basename(i["path"]) for i in m["items"]] == ["node_modules"]


def test_parent_with_kept_child_becomes_children(tmp_path):
    t = tree(tmp_path)
    m = M.build([str(t / "p/renders")], classify_fn=fake_classify)
    item = m["items"][0]
    kids = {os.path.basename(c["path"]): c["mode"] for c in item["children"]}
    assert kids["master"] == "keep" and kids["v2"] == "keep" and kids["v1"] == "trash"  # highest version + master kept
    flat = [os.path.basename(x["path"]) for x in M.targets(m)]
    assert flat == ["v1"]  # never the whole renders/ folder


def test_overrides_only_make_things_safer(tmp_path):
    t = tree(tmp_path)
    m = M.build([str(t / "p/node_modules"), str(t / "doc.txt")], classify_fn=fake_classify)
    nm, doc = str(t / "p/node_modules"), str(t / "doc.txt")
    m2 = M.apply_overrides(m, {nm: "trash", doc: "permanent"})
    modes = {i["path"]: i["mode"] for i in m2["items"]}
    assert modes[nm] == "trash"          # safer: allowed
    assert modes[doc] == "trash"         # riskier: refused
    m3 = M.apply_overrides(m, {nm: "keep"})
    assert {i["path"]: i["mode"] for i in m3["items"]}[nm] == "keep"
    assert M.targets(m3) == [i for i in m3["items"] if i["path"] == doc]


def test_missing_path_is_skipped(tmp_path):
    m = M.build([str(tmp_path / "nope")], classify_fn=fake_classify)
    assert m["items"][0]["mode"] == "skip" and "no longer exists" in m["items"][0]["reason"]


def test_action_none_is_skipped_except_worktrees(tmp_path):
    # Review I5 + I6: cloud items (action none) are never acted on; worktrees get their own REMOVE mode.
    cloud = tmp_path / "cloud"; cloud.mkdir()
    wt = tmp_path / "wt"; wt.mkdir(); (wt / ".git").write_text("gitdir: /x")

    def cls(path, **k):
        if path.endswith("cloud"):
            return {"class": "review", "action": "none", "rule": "cloud", "reason": "synced"}
        return {"class": "review", "action": "none", "rule": "worktree", "reason": "worktree"}
    m = M.build([str(cloud), str(wt)], classify_fn=cls)
    modes = {os.path.basename(i["path"]): (i["kind"], i["mode"]) for i in m["items"]}
    assert modes["cloud"][1] == "skip"
    assert modes["wt"] == ("worktree", "remove")
    assert m["totals"]["remove"] >= 0 and "remove" in M.SAFER
    m2 = M.apply_overrides(m, {str(wt): "trash"})
    assert {i["path"]: i["mode"] for i in m2["items"]}[str(wt)] == "remove"  # can't become "trash"


def test_staged_latest_version_is_kept(tmp_path):
    # Codex I8: staging the latest render folder directly must not trash it.
    t = tree(tmp_path)
    m = M.build([str(t / "p/renders/v2")], classify_fn=fake_classify)
    assert m["items"][0]["mode"] == "keep" and "latest" in m["items"][0]["reason"]


def test_latest_is_per_family_not_global(tmp_path):
    (tmp_path / "renders/shotA_v2").mkdir(parents=True); (tmp_path / "renders/shotA_v1").mkdir()
    (tmp_path / "renders/shotB_v7").mkdir(); (tmp_path / "renders/shotB_v6").mkdir()
    m = M.build([str(tmp_path / "renders")], classify_fn=fake_classify)
    kids = {os.path.basename(c["path"]): c["mode"] for c in m["items"][0]["children"]}
    assert kids == {"shotA_v2": "keep", "shotA_v1": "trash", "shotB_v7": "keep", "shotB_v6": "trash"}


# ---------------- user overrides ("Delete anyway") ----------------
def test_forced_override_is_trash_only(tmp_path):
    # Final review MUST-FIX 1: a user override never makes anything permanent — "delete anyway" means the Trash.
    t = tree(tmp_path)
    m = M.build([str(t / "doc.txt"), str(t / "p/renders/v2")], classify_fn=fake_classify)  # v2 = latest → keep
    m2 = M.apply_overrides(m, {}, forced={str(t / "doc.txt"): "permanent", str(t / "p/renders/v2"): "trash"},
                           keep=[], home=str(tmp_path))
    items = {os.path.basename(i["path"]): i for i in m2["items"]}
    assert "permanent" not in M.FORCEABLE
    assert items["doc.txt"]["mode"] == "trash" and items["doc.txt"]["forced"] is True
    assert items["v2"]["mode"] == "trash" and "latest" in items["v2"]["rule_reason"]
    assert M.targets(m2) and all(x.get("forced") and x["mode"] == "trash" for x in M.targets(m2))


def test_forced_override_never_unlocks_a_protected_verdict(tmp_path):
    # Final review MUST-FIX 1: a protected verdict stays skipped, with a reason, whatever the user asks.
    t = tree(tmp_path)
    m = M.build([str(t / "sysstuff")], classify_fn=fake_classify)
    assert m["items"][0]["mode"] == "skip" and m["items"][0]["overridable"] is False
    m2 = M.apply_overrides(m, {}, forced={str(t / "sysstuff"): "trash"}, keep=[], home=str(tmp_path))
    it = m2["items"][0]
    assert it["mode"] == "skip" and not it.get("forced") and "protected" in it["reason"]
    assert M.targets(m2) == []


def test_forced_override_respects_the_floor(tmp_path):
    t = tree(tmp_path)
    os.symlink(t / "doc.txt", t / "link")
    app = tmp_path / "Applications/X.app/Contents"; app.mkdir(parents=True)
    m = M.build([str(t / "sysstuff"), str(t / "link"), str(t / "doc.txt")], classify_fn=fake_classify)
    floor = M.override_floor
    assert floor("/System/Library", keep=[], home=str(tmp_path))
    assert floor("/Library/Fonts", keep=[], home=str(tmp_path))
    assert floor("/Applications/X.app/Contents/MacOS", keep=[], home=str(tmp_path))
    assert floor(str(t / "link"), keep=[], home=str(tmp_path))
    assert floor(str(t / "doc.txt"), keep=[str(t)], home=str(tmp_path))
    assert floor(str(t / "doc.txt"), keep=[], home=str(tmp_path)) is None
    assert floor("/Applications/X.app", keep=[], home=str(tmp_path)) is None   # a whole app is fine
    m2 = M.apply_overrides(m, {}, forced={str(t / "link"): "trash"}, keep=[], home=str(tmp_path))
    assert {i["path"]: i["mode"] for i in m2["items"]}[str(t / "link")] == "skip"


def test_manifest_marks_overridable_items(tmp_path):
    t = tree(tmp_path)
    m = M.build([str(t / "doc.txt")], classify_fn=fake_classify, home=str(tmp_path))
    assert m["items"][0]["overridable"] is True


# ---------------- override review fixes ----------------
def test_floor_covers_home_library_and_any_app_bundle(tmp_path):
    # Review C1/I2: "Delete anyway" must never reach the home folder itself, ~/Library's core, or app internals.
    h = tmp_path / "h"
    for d in ("Library/Keychains", "Library/Containers/x", "Library/Caches/junk", "Downloads/Bar.app/Contents", "Docs"):
        (h / d).mkdir(parents=True)
    floor = lambda p: M.override_floor(str(h / p) if p else str(h), keep=(), home=str(h))
    for p in ("", "Library", "library", "Library/Keychains", "Library/Containers/x", "Downloads/Bar.app/Contents"):
        assert floor(p), p
    assert floor("Library/Caches/junk") is None and floor("Docs") is None and floor("Downloads/Bar.app") is None


def test_floor_refuses_paths_outside_home(tmp_path):
    # Review I1: other users, external volumes, /Applications — overrides stay inside your home folder.
    assert M.override_floor("/Users/Shared/x", keep=(), home=str(tmp_path / "h"))
    assert M.override_floor("/Volumes/Ext/x", keep=(), home=str(tmp_path / "h"))


def test_reapplying_a_forced_override_keeps_the_reason_flat(tmp_path):
    t = tree(tmp_path)
    m = M.build([str(t / "doc.txt")], classify_fn=fake_classify)
    f = {str(t / "doc.txt"): "permanent"}
    m2 = M.apply_overrides(M.apply_overrides(m, {}, forced=f, keep=[], home=str(tmp_path)), {}, forced=f, keep=[],
                           home=str(tmp_path))
    assert m2["items"][0]["reason"].count("your call") == 1


def test_keep_backs_out_of_a_saved_forced_override(tmp_path):
    t = tree(tmp_path)
    m = M.apply_overrides(M.build([str(t / "doc.txt")], classify_fn=fake_classify), {},
                          forced={str(t / "doc.txt"): "permanent"}, keep=[], home=str(tmp_path))
    m2 = M.apply_overrides(m, {str(t / "doc.txt"): "keep"}, keep=[], home=str(tmp_path))
    assert m2["items"][0]["mode"] == "keep" and not m2["items"][0]["forced"] and M.targets(m2) == []


def test_whole_app_can_only_be_forced_to_trash(tmp_path):
    import glob
    import pytest
    apps = [a for a in glob.glob("/Applications/*.app") if not os.path.islink(a)]
    if not apps:
        pytest.skip("no apps installed")
    m = {"id": "x", "items": [{"path": apps[0], "kind": "dir", "mode": "skip", "reason": "app", "size": 1, "children": []}]}
    m2 = M.apply_overrides(m, {}, forced={apps[0]: "permanent"}, keep=[], home=str(tmp_path))
    assert m2["items"][0]["mode"] == "trash" and m2["items"][0]["forced"]
    assert M.override_floor(apps[0] + "/Contents", keep=[], home=str(tmp_path))
