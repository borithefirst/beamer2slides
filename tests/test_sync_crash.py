"""What happens when a sync or a `pull --apply` dies halfway (docs/sync.md, "If a sync or a pull
dies"). The offline tests here cover the write order, the fault hook, base validation, the recovery
plan and pull's atomic apply; the tests marked `sync` at the end really kill a sync of a real deck
at each injection point and check that a second sync reaches the same deck without losing an edit.

    python -m pytest -q tests/test_sync_crash.py            # offline (part of the default run)
    python -m pytest -m sync -q tests/test_sync_crash.py     # the live ones (Google, ~10 min)
"""

import copy
import json
import os
from pathlib import Path

import pytest

from beamer2slides import faults, snapshot, sync
from beamer2slides.inverse import Result, replace_file, source_hashes, unchanged_since_pull, write_outputs

SYNC_DECKS = Path(__file__).resolve().parent / "decks" / "sync" / "out"


@pytest.fixture(autouse=True)
def _clean_hook():
    for var in (faults.ENV, faults.SIZE):
        os.environ.pop(var, None)
    faults.reset()
    yield
    for var in (faults.ENV, faults.SIZE):
        os.environ.pop(var, None)
    faults.reset()


# ---------------------------------------------------------------- the fault hook

POINTS = ["plan", "journal", "measure", "content", "order", "overrides", "cleanup", "base:save", "base:drive",
          "pull:apply"]


def test_the_fault_hook_does_nothing_without_the_variable():
    assert faults.ENV not in os.environ
    for point in POINTS:
        for _ in range(3):
            assert faults.fail_at(point) is None


def test_the_fault_hook_raises_at_the_named_point_only():
    os.environ[faults.ENV] = "content"
    faults.fail_at("order")
    faults.fail_at("plan")
    with pytest.raises(faults.InjectedFailure):
        faults.fail_at("content")


def test_the_fault_hook_counts_occurrences():
    os.environ[faults.ENV] = "content:3"
    faults.fail_at("content")
    faults.fail_at("content")
    with pytest.raises(faults.InjectedFailure):
        faults.fail_at("content")
    faults.fail_at("content")  # (only that one occurrence)


def test_the_batch_size_is_the_library_s_unless_the_variable_says_otherwise():
    assert faults.SIZE not in os.environ
    assert faults.batch_size(400) == 400
    os.environ[faults.SIZE] = "40"
    assert faults.batch_size(400) == 40
    assert len(sync.batches([{"a": 1}] * 90)) == 3
    os.environ[faults.SIZE] = "not a number"
    assert faults.batch_size(400) == 400
    del os.environ[faults.SIZE]


def test_the_fault_hook_takes_several_points_and_keeps_colons_in_names():
    os.environ[faults.ENV] = "stage,base:save"
    with pytest.raises(faults.InjectedFailure):
        faults.fail_at("base:save")
    faults.reset()
    with pytest.raises(faults.InjectedFailure):
        faults.fail_at("stage")


# ---------------------------------------------------------------- batching and write order

def test_batches_are_cut_only_where_a_slide_ends():
    reqs = [{"a": 1}] * 3 + [sync.BREAK] + [{"b": 2}] * 3 + [sync.BREAK] + [{"c": 3}] * 2
    assert sync.batches(reqs, size=4) == [[{"a": 1}] * 3, [{"b": 2}] * 3, [{"c": 3}] * 2]
    assert sync.batches(reqs, size=8) == [[{"a": 1}] * 3 + [{"b": 2}] * 3 + [{"c": 3}] * 2]
    for size in range(1, 10):
        got = sync.batches(reqs, size)
        assert all(sync.BREAK not in b for b in got)
        assert [r for b in got for r in b] == [r for r in reqs if r is not sync.BREAK]


def test_one_slide_larger_than_a_batch_is_the_only_thing_that_is_split():
    reqs = [{"a": i} for i in range(7)] + [sync.BREAK] + [{"b": 1}]
    got = sync.batches(reqs, size=3)
    assert [len(b) for b in got] == [3, 3, 2]
    assert got[-1] == [{"a": 6}, {"b": 1}]  # the tail keeps the whole next slide with it


def bare_sync(**attrs) -> sync.Sync:
    s = sync.Sync.__new__(sync.Sync)
    s.warnings = []
    for k, v in attrs.items():
        setattr(s, k, v)
    return s


def test_a_slide_the_source_removed_is_deleted_in_the_cleanup_phase_not_with_the_content():
    s = bare_sync()
    work = {"slides": [{"plan": {"action": "delete", "objectId": "S1", "key": "gone"}}], "order": ["S2"]}
    theirs = {"slides": [{"objectId": "S1"}, {"objectId": "S2"}]}
    content, cleanup = s.main_requests(work, theirs, {}, {}, [])
    assert not [r for r in content if "deleteObject" in r]
    assert cleanup == [{"deleteObject": {"objectId": "S1"}}]
    assert s.cleanup_ids == ["S1"]


def test_scratch_slides_are_sync_s_own_and_go_with_the_content():
    s = bare_sync()
    work = {"slides": [], "order": []}
    content, cleanup = s.main_requests(work, {"slides": []}, {}, {}, ["b2s_m001"])
    assert content == [{"deleteObject": {"objectId": "b2s_m001"}}]
    assert cleanup == []


def test_a_recreated_unit_deletes_nothing_in_the_content_phase():
    """The old objects of a recreated unit must still be in the deck while the content is written:
    that is what lets a killed sync be run again without the person's edit being gone."""
    from collections import defaultdict

    from beamer2slides.sync import build_ours
    v1 = SYNC_DECKS / "v1.pdf"
    if not v1.exists():
        pytest.skip("build the sync test talk first (tests/decks/sync/build.py)")
    first = build_ours(v1, Path(os.environ.get("TMP", ".")) / "b2s-crash-ours", {"slides": []})
    s = bare_sync(ours=first, plan=first["plan"], scale=first["plan"].scale, tok="1zz",
                  urls=defaultdict(lambda: "https://example.com/staged.png"))
    j = next(k for k, o in enumerate(first["slides"]) if o["key"] == "policy")
    base_slide = copy.deepcopy(first["slides"][j])
    objects = {}
    for e in base_slide["elements"]:
        oid = f"OLD_{e['key'].replace('/', '_')}"
        rb = {"kind": "shape", "transform": [1.0, 0, 0, 1.0, 0, 0], "size": [10, 10], "box": [0, 0, 10, 10],
              "parent_group": None, "z": 0, "title": None, "description": None, "text": "x", "text_styles": [],
              "paragraph_styles": [], "text_style_hash": "s0", "shape_style": {}, "shape_style_hash": "h0"}
        e.update(main=oid, objects=[oid], readback={oid: rb})
        objects[oid] = rb
    s.base = {"slides": [base_slide]}
    ukey = base_slide["elements"][0]["key"]
    plan = {"key": "policy", "base": 0, "ours": j, "objectId": "LIVE", "units": [
        {"key": ukey, "action": "recreate", "ours_members": [ukey], "base_members": [ukey], "overrides": {}}]}
    w = {"plan": plan, "units": []}
    reqs = s.update_slide(w, {"objects": objects, "notes": "", "notes_id": None}, {}, {})
    assert not [r for r in reqs if "deleteObject" in r], "a recreated unit must delete nothing yet"
    assert f"OLD_{ukey.replace('/', '_')}" in s.cleanup_ids
    assert w["doomed"]


# ---------------------------------------------------------------- base validation

def a_base(pid="P1", generation=3, version=snapshot.VERSION, slides=None):
    return {"version": version, "generation": generation, "presentationId": pid,
            "revisionId": "r1", "slides": slides if slides is not None else []}


class FakeDrive:
    """Just enough of the Drive service for load_base: it hands out one stored base."""

    def __init__(self, base):
        self.base = base

    def files(self):
        return self

    def get(self, fileId=None, fields=None):
        self.kind = "media" if False else "meta"
        return _Exec({"appProperties": {snapshot.BASE_PROPERTY: "BASEFILE"}} if self.base is not None else {})

    def get_media(self, fileId=None):
        return _Exec(json.dumps(self.base).encode("utf-8"))


class _Exec:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


def test_a_missing_base_is_reported_not_guessed(tmp_path):
    problems = []
    base, where = snapshot.load_base("P1", tmp_path, None, problems)
    assert base is None and where == "none" and problems == []


def test_a_truncated_local_base_is_refused(tmp_path):
    snapshot.local_path(tmp_path).parent.mkdir(parents=True)
    snapshot.local_path(tmp_path).write_text('{"version": 1, "slides": [', encoding="utf-8")
    problems = []
    base, where = snapshot.load_base("P1", tmp_path, None, problems)
    assert base is None and where == "none"
    assert problems and "could not be read" in problems[0]


def test_a_base_of_another_deck_is_refused(tmp_path):
    snapshot.save_local(a_base(pid="OTHER"), tmp_path)
    problems = []
    base, _ = snapshot.load_base("P1", tmp_path, None, problems)
    assert base is None
    assert "belongs to presentation OTHER" in problems[0]


def test_a_base_from_a_newer_schema_is_refused(tmp_path):
    snapshot.save_local(a_base(version=snapshot.VERSION + 1), tmp_path)
    problems = []
    base, _ = snapshot.load_base("P1", tmp_path, None, problems)
    assert base is None
    assert "newer than this beamer2slides" in problems[0]


def test_the_newer_of_drive_and_local_wins(tmp_path):
    """A sync whose Drive upload failed leaves the local base ahead; taking Drive's would sync
    against objects that are already gone."""
    snapshot.save_local(a_base(generation=5), tmp_path)
    problems = []
    base, where = snapshot.load_base("P1", tmp_path, FakeDrive(a_base(generation=4)), problems)
    assert where == "local" and base["generation"] == 5
    assert "older than the local one" in problems[0]
    problems = []
    base, where = snapshot.load_base("P1", tmp_path, FakeDrive(a_base(generation=6)), problems)
    assert where == "drive" and base["generation"] == 6 and problems == []


def test_a_broken_drive_base_falls_back_to_the_local_one(tmp_path):
    snapshot.save_local(a_base(generation=5), tmp_path)
    problems = []
    base, where = snapshot.load_base("P1", tmp_path, FakeDrive({"version": 1, "presentationId": "P1"}), problems)
    assert where == "local" and base["generation"] == 5
    assert "stored in Drive was ignored" in problems[0]


def test_the_local_base_is_never_left_half_written(tmp_path, monkeypatch):
    snapshot.save_local(a_base(generation=1), tmp_path)
    before = snapshot.local_path(tmp_path).read_text(encoding="utf-8")
    monkeypatch.setattr(snapshot.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("killed")))
    with pytest.raises(OSError):
        snapshot.save_local(a_base(generation=2), tmp_path)
    assert snapshot.local_path(tmp_path).read_text(encoding="utf-8") == before
    assert json.loads(before)["generation"] == 1


def test_a_base_that_knows_none_of_the_deck_s_slides_is_not_a_base_for_it():
    base = a_base(slides=[{"key": "a", "objectId": "S1", "elements": []}])
    assert snapshot.base_matches(base, {"slides": [{"objectId": "S1"}]})
    assert not snapshot.base_matches(base, {"slides": [{"objectId": "OTHER"}]})
    assert snapshot.base_matches(a_base(), {"slides": []})  # (nothing recorded yet)


# ---------------------------------------------------------------- recovery from an interrupted sync

def test_object_ids_say_which_sync_made_them():
    assert sync.sync_generation("b2s_abc123_def456_2ab") == 2
    assert sync.sync_generation("b2s_abc123_def456_12ab_g") == 12
    assert sync.sync_generation("b2s_abc123_k3_4zz") == 4
    assert sync.sync_generation("b2s_abc123_2ab") == 2        # a slide sync created
    assert sync.sync_generation("b2s_s003_f1") is None        # the converter's own
    assert sync.sync_generation("g2ab3cd4ef5_0_1") is None    # a person's copy in Slides
    assert sync.sync_generation("") is None


def readback(oid_title=None, text="x", **kw):
    rb = {"kind": "shape", "transform": [1.0, 0, 0, 1.0, 0, 0], "size": [10, 10], "box": [0, 0, 10, 10],
          "parent_group": None, "z": 0, "title": oid_title, "description": None, "text": text,
          "text_styles": [], "paragraph_styles": [], "text_style_hash": "s0", "shape_style": {},
          "shape_style_hash": "h0"}
    rb.update(kw)
    return rb


def recovery_base(generation=1, **extra):
    base = a_base(generation=generation, slides=[{
        "key": "intro", "objectId": "S1", "groups": [],
        "elements": [{"key": "text/body/0", "main": "OLD1", "objects": ["OLD1"], "ir_hash": "h",
                      "fields": {"text": "t", "position": "p", "size": "s", "style": "y", "image": ""},
                      "readback": {"OLD1": readback()}}]}])
    base.update(extra)
    return base


def live(objects, sid="S1"):
    return {"slides": [{"objectId": sid, "objects": objects, "order": list(objects), "notes": "",
                        "background": {}, "layoutObjectId": "L1"}]}


def test_a_leftover_object_of_an_interrupted_sync_is_swept():
    base = recovery_base(pending={"generation": 2, "token": "2ab", "objects": {"intro/text/body/0": ["b2s_aaaaaa_bbbbbb_2ab"]}})
    theirs = live({"OLD1": readback(), "b2s_aaaaaa_bbbbbb_2ab": readback("b2s:intro/text/body/0")})
    rec = sync.plan_recovery(base, theirs, ["intro"])
    assert rec["sweep"] == ["b2s_aaaaaa_bbbbbb_2ab"]
    assert rec["heal"] == []


def test_a_leftover_is_recognised_by_its_id_even_without_a_journal():
    theirs = live({"OLD1": readback(), "b2s_aaaaaa_bbbbbb_2ab": readback("b2s:intro/text/body/0")})
    rec = sync.plan_recovery(recovery_base(), theirs, ["intro"])
    assert rec["sweep"] == ["b2s_aaaaaa_bbbbbb_2ab"]


def test_an_object_a_person_made_is_never_swept():
    theirs = live({"OLD1": readback(), "g2abc_0_3": readback(), "b2s_s003_f1": readback()})
    rec = sync.plan_recovery(recovery_base(), theirs, ["intro"])
    assert rec["sweep"] == [] and rec["heal"] == []


def test_an_object_of_this_base_s_own_generation_is_not_a_leftover():
    theirs = live({"OLD1": readback(), "b2s_aaaaaa_bbbbbb_1cd": readback("b2s:intro/text/body/0")})
    rec = sync.plan_recovery(recovery_base(generation=1), theirs, ["intro"])
    assert rec["sweep"] == []


def test_an_element_whose_objects_an_older_sync_deleted_takes_over_the_new_one():
    """Deletions used to happen in the content batch: a deck left in that state has the element's
    replacement but no base entry for it. It must be adopted, not reported as deleted in the deck."""
    theirs = live({"b2s_aaaaaa_bbbbbb_2ab": readback("b2s:intro/text/body/0", text="new"),
                   "b2s_aaaaaa_bbbbbb_2ab_g": readback(None, kind="elementGroup")})
    rec = sync.plan_recovery(recovery_base(), theirs, ["intro"])
    assert rec["sweep"] == []
    assert rec["heal"] == [{"slide": "intro", "element": "text/body/0", "objectId": "b2s_aaaaaa_bbbbbb_2ab",
                            "objects": ["b2s_aaaaaa_bbbbbb_2ab", "b2s_aaaaaa_bbbbbb_2ab_g"]}]
    base = recovery_base()
    done = sync.heal_base(base, rec["heal"], theirs, same_source=True)
    el = base["slides"][0]["elements"][0]
    assert done == ["intro/text/body/0"]
    assert el["main"] == "b2s_aaaaaa_bbbbbb_2ab" and el["readback"]["b2s_aaaaaa_bbbbbb_2ab"]["text"] == "new"
    assert el["ir_hash"] == "h"


def test_a_healed_element_of_another_source_version_is_written_over_again():
    theirs = live({"b2s_aaaaaa_bbbbbb_2ab": readback("b2s:intro/text/body/0", text="new")})
    base = recovery_base()
    rec = sync.plan_recovery(base, theirs, ["intro"])
    sync.heal_base(base, rec["heal"], theirs, same_source=False)
    el = base["slides"][0]["elements"][0]
    assert el["ir_hash"] == "interrupted"
    assert set(el["fields"].values()) == {"interrupted"}


def test_a_swept_group_takes_its_children_with_it():
    """Slides deletes a group's children with the group: naming them too makes Google refuse the
    whole batch (`Invalid requests[n].deleteObject: The object could not be found`), and then
    nothing at all is swept."""
    gid = "b2s_aaaaaa_bbbbbb_2ab_g"
    theirs = live({"OLD1": readback(),
                   gid: readback(None, kind="elementGroup"),
                   "b2s_aaaaaa_bbbbbb_2ab": readback("b2s:intro/text/body/0", parent_group=gid),
                   "b2s_aaaaaa_cccccc_2ab": readback("b2s:intro/image/figure/0", parent_group=gid)})
    assert sync.plan_recovery(recovery_base(), theirs, ["intro"])["sweep"] == [gid]


def test_objects_on_a_swept_slide_are_not_named_again():
    sid = f"b2s_{sync.h6('extra')}_2ab"
    theirs = {"slides": [{"objectId": "S1", "objects": {"OLD1": readback()}, "order": ["OLD1"], "notes": "",
                          "background": {}, "layoutObjectId": "L1"},
                         {"objectId": sid, "notes": "", "background": {}, "layoutObjectId": "L1",
                          "objects": {"b2s_aaaaaa_bbbbbb_2ab": readback("b2s:extra/text/body/0")},
                          "order": ["b2s_aaaaaa_bbbbbb_2ab"]}]}
    rec = sync.plan_recovery(recovery_base(), theirs, ["intro", "extra"])
    assert rec["sweep_slides"] == [sid] and rec["sweep"] == []


def test_a_slide_an_interrupted_sync_created_is_swept_only_when_it_is_created_again():
    sid = f"b2s_{sync.h6('extra')}_2ab"
    theirs = {"slides": [{"objectId": "S1", "objects": {"OLD1": readback()}, "order": ["OLD1"], "notes": "",
                          "background": {}, "layoutObjectId": "L1"},
                         {"objectId": sid, "objects": {}, "order": [], "notes": "", "background": {},
                          "layoutObjectId": "L1"}]}
    assert sync.plan_recovery(recovery_base(), theirs, ["intro", "extra"])["sweep_slides"] == [sid]
    assert sync.plan_recovery(recovery_base(), theirs, ["intro"])["sweep_slides"] == []


def test_a_base_that_may_be_behind_the_deck_sweeps_nothing_on_a_guess():
    """Two checkouts, one deck: when the base in Drive cannot be read, sync works from the folder's
    copy, which may be older than the deck (snapshot.stale_base_warning). Objects of a later
    generation are then not leftovers of a dead run but, just as likely, the finished work of the
    other checkout's sync - so only what this base itself names is swept. Healing still happens:
    it takes an object over instead of deleting it."""
    theirs = live({"OLD1": readback(), "b2s_aaaaaa_bbbbbb_2ab": readback("b2s:intro/text/body/0")})
    assert sync.plan_recovery(recovery_base(), theirs, ["intro"], trust_generation=False)["sweep"] == []
    named = recovery_base(cleanup=["b2s_aaaaaa_bbbbbb_2ab"])  # this base's own dead run named it
    assert sync.plan_recovery(named, theirs, ["intro"], trust_generation=False)["sweep"] == \
        ["b2s_aaaaaa_bbbbbb_2ab"]
    gone = live({"b2s_aaaaaa_bbbbbb_2ab": readback("b2s:intro/text/body/0", text="new")})  # OLD1 deleted
    assert sync.plan_recovery(recovery_base(), gone, ["intro"], trust_generation=False)["heal"]


def test_a_slide_is_swept_on_a_guess_only_when_the_base_is_the_deck_s_own():
    sid = f"b2s_{sync.h6('extra')}_2ab"
    theirs = {"slides": [{"objectId": "S1", "objects": {"OLD1": readback()}, "order": ["OLD1"], "notes": "",
                          "background": {}, "layoutObjectId": "L1"},
                         {"objectId": sid, "objects": {}, "order": [], "notes": "", "background": {},
                          "layoutObjectId": "L1"}]}
    keys = ["intro", "extra"]
    assert sync.plan_recovery(recovery_base(), theirs, keys)["sweep_slides"] == [sid]
    assert sync.plan_recovery(recovery_base(), theirs, keys, trust_generation=False)["sweep_slides"] == []


def test_the_text_an_interrupted_sync_overwrote_in_a_placeholder_comes_back_for_the_merge():
    saved = {"OLD1": {"text": "the person's title\n", "text_styles": [], "paragraph_styles": [],
                      "text_style_hash": "edited"}}
    base = recovery_base(pending={"generation": 2, "token": "2ab", "objects": {}, "in_place": saved})
    theirs = live({"OLD1": readback(text="the source's new title\n")})
    rec = sync.plan_recovery(base, theirs, ["intro"])
    assert rec["restore"] == saved
    assert sync.restore_in_place(theirs, rec["restore"]) == ["OLD1"]
    assert theirs["slides"][0]["objects"]["OLD1"]["text"] == "the person's title\n"
    assert theirs["slides"][0]["objects"]["OLD1"]["box"] == [0, 0, 10, 10]  # (only the text comes back)


def test_an_untouched_placeholder_is_left_as_it_is():
    saved = {"OLD1": {"text": "x", "text_style_hash": "s0"}}
    theirs = live({"OLD1": readback(text="x")})
    assert sync.restore_in_place(theirs, saved) == []


# ---------------------------------------------------------------- the way back

class _Note:
    """A `guard.WayBack` that only says when it was asked for."""

    def __init__(self):
        self.asked = 0

    def result(self) -> dict:
        self.asked += 1
        return {}


def test_every_write_collects_the_way_back_before_it_goes_out():
    """The deck's revision and its .pptx backup are made on a thread while the sync reads and
    plans (`guard.WayBack`), and what makes that safe is that they are collected before anything
    in the deck moves - so every place that writes asks first."""
    api, note = FakeSlidesApi(), _Note()
    s = bare_sync(slides=api, pid="P1", way_back=note, sent={}, revision=lambda: "rev1")
    s.delete_leftovers(["A"])
    assert note.asked == 1 and api.batches == [["A"]], "an interrupted run's leftovers are a write"
    s.delete_scratch(["b2s_m000"])
    assert note.asked == 2, "measure_places' scratch slides are a write"
    s.send("content", [{"deleteObject": {"objectId": "B"}}], None)
    assert note.asked == 3, "and so is the content batch"


def test_a_sync_with_no_way_back_to_collect_writes_as_it_always_did():
    api = FakeSlidesApi()
    bare_sync(slides=api, pid="P1").delete_leftovers(["A"])   # no way_back attribute at all
    assert api.batches == [["A"]]


class FakeSlidesApi:
    """A Slides service that refuses a batch naming an object it doesn't know (as Google does)."""

    def __init__(self, missing=()):
        self.missing, self.deleted, self.batches = set(missing), [], []

    def presentations(self):
        return self

    def batchUpdate(self, presentationId=None, body=None):
        return _Call(self, [r["deleteObject"]["objectId"] for r in body["requests"]])


class _Call:
    def __init__(self, api, ids):
        self.api, self.ids = api, ids

    def execute(self):
        self.api.batches.append(list(self.ids))
        bad = [i for i in self.ids if i in self.api.missing]
        if bad:
            raise http_error(f"Invalid requests[0].deleteObject: The object ({bad[0]}) could not be found.")
        self.api.deleted += self.ids
        return {}


def http_error(message: str, status: int = 400):
    from googleapiclient.errors import HttpError
    resp = type("Resp", (), {"status": status, "reason": "Bad Request"})()
    return HttpError(resp, json.dumps({"error": {"code": status, "message": message}}).encode())


def test_one_leftover_google_no_longer_knows_does_not_save_the_others():
    api = FakeSlidesApi(missing={"B"})  # B went with its group
    s = bare_sync(slides=api, pid="P1")
    assert s.delete_leftovers(["A", "B", "C"]) == {"A", "B", "C"}
    assert api.deleted == ["A", "C"]
    assert api.batches == [["A", "B", "C"], ["A"], ["B"], ["C"]]
    assert s.warnings == []


def test_a_leftover_that_could_not_be_deleted_is_not_planned_away():
    api = FakeSlidesApi()
    api.batchUpdate = lambda presentationId=None, body=None: _Refusing()
    s = bare_sync(slides=api, pid="P1")
    assert s.delete_leftovers(["A"]) == set()
    assert s.warnings and "could not delete 1 leftover" in s.warnings[0]


class _Refusing:
    def execute(self):
        raise http_error("The caller does not have permission", 403)


def test_dropping_leftovers_hides_them_from_everything_downstream():
    pres = {"presentationId": "P1", "slides": [
        {"objectId": "S1", "pageElements": [{"objectId": "A"}, {"objectId": "B"},
                                            {"objectId": "G", "elementGroup": {"children": [{"objectId": "B2"}]}}]},
        {"objectId": "S2", "pageElements": []}]}
    out = sync.drop_objects(pres, {"B", "B2"}, {"S2"})
    assert [s["objectId"] for s in out["slides"]] == ["S1"]
    assert [e["objectId"] for e in out["slides"][0]["pageElements"]] == ["A"]  # (an empty group goes too)
    assert pres["slides"][0]["pageElements"][1]["objectId"] == "B"  # the original is untouched


# ---------------------------------------------------------------- pull --apply

def a_result(files, originals=None):
    return Result(True, [{"iteration": 0, "open": 0, "by_kind": {}, "geometry_error": 0}], [], [], files, "",
                  Path("."), [], [], originals or {})


def test_pull_apply_backs_the_file_up_and_replaces_it_whole(tmp_path):
    tex = tmp_path / "main.tex"
    tex.write_text("old\n", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    result = a_result({str(tex): "new\n"}, {str(tex): _sha1(tex)})
    write_outputs(result, {"slides": []}, tex, work, True, None, log=lambda *a: None)
    assert tex.read_text(encoding="utf-8") == "new\n"
    assert (tmp_path / "main.tex.bak").read_text(encoding="utf-8") == "old\n"
    assert not list(tmp_path.glob("*.writing")) and not list(tmp_path.glob("*.b2s-writing"))


def test_the_backup_restores_the_source_exactly(tmp_path):
    tex = tmp_path / "main.tex"
    original = "\\documentclass{beamer}\r\n% caf\u00e9 \u2014 no newline at the end"
    tex.write_bytes(original.encode("utf-8"))
    before = tex.read_bytes()
    work = tmp_path / "work"
    work.mkdir()
    write_outputs(a_result({str(tex): "changed"}, {str(tex): _sha1(tex)}), {"slides": []}, tex, work, True, None,
                  log=lambda *a: None)
    assert tex.read_bytes() != before
    bak = tmp_path / "main.tex.bak"
    assert bak.read_bytes() == before
    os.replace(bak, tex)
    assert tex.read_bytes() == before


def test_an_interrupted_apply_leaves_every_file_whole(tmp_path):
    a, b = tmp_path / "a.tex", tmp_path / "b.tex"
    a.write_text("A0\n", encoding="utf-8")
    b.write_text("B0\n", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    result = a_result({str(a): "A1\n", str(b): "B1\n"}, {str(a): _sha1(a), str(b): _sha1(b)})
    os.environ[faults.ENV] = "pull:apply:2"
    with pytest.raises(faults.InjectedFailure):
        write_outputs(result, {"slides": []}, a, work, True, None, log=lambda *a: None)
    assert a.read_text(encoding="utf-8") == "A1\n"   # written whole
    assert b.read_text(encoding="utf-8") == "B0\n"   # not touched at all
    assert not list(tmp_path.glob("*.b2s-writing"))


def test_a_file_edited_while_the_pull_ran_is_not_overwritten(tmp_path):
    tex = tmp_path / "main.tex"
    tex.write_text("old\n", encoding="utf-8")
    originals = {str(tex): _sha1(tex)}
    tex.write_text("the person's own edit\n", encoding="utf-8")  # changed behind the loop's back
    work = tmp_path / "work"
    work.mkdir()
    write_outputs(a_result({str(tex): "the pull's version\n"}, originals), {"slides": []}, tex, work, True, None,
                  log=lambda *a: None)
    assert tex.read_text(encoding="utf-8") == "the person's own edit\n"
    assert (tmp_path / "main.tex.b2s-new").read_text(encoding="utf-8") == "the pull's version\n"
    assert json.loads((work / "edits.json").read_text(encoding="utf-8"))["not_applied"] == [str(tex)]


def test_a_new_file_that_appeared_meanwhile_is_not_overwritten(tmp_path):
    fig = tmp_path / "figures" / "b2s-1.png"
    fig.parent.mkdir()
    fig.write_bytes(b"someone else's picture")
    src = tmp_path / "from-deck.png"
    src.write_bytes(b"the deck's picture")
    work = tmp_path / "work"
    work.mkdir()
    write_outputs(a_result({str(fig): src}, {str(tmp_path / "main.tex"): "0" * 40}), {"slides": []},
                  tmp_path / "main.tex", work, True, None, log=lambda *a: None)
    assert fig.read_bytes() == b"someone else's picture"
    assert (tmp_path / "figures" / "b2s-1.png.b2s-new").read_bytes() == b"the deck's picture"


def test_source_hashes_cover_the_files_the_loop_copied(tmp_path):
    root, srcdir = tmp_path / "tree", tmp_path / "copy"
    (root / "sub").mkdir(parents=True)
    (root / "main.tex").write_text("x", encoding="utf-8")
    (root / "sub" / "part.tex").write_text("y", encoding="utf-8")
    (srcdir / "sub").mkdir(parents=True)
    (srcdir / "main.tex").write_text("x", encoding="utf-8")
    (srcdir / "sub" / "part.tex").write_text("y", encoding="utf-8")
    hashes = source_hashes(root, srcdir)
    assert set(hashes) == {str(root / "main.tex"), str(root / "sub" / "part.tex")}
    assert unchanged_since_pull(root / "main.tex", hashes)
    (root / "main.tex").write_text("edited", encoding="utf-8")
    assert not unchanged_since_pull(root / "main.tex", hashes)


def test_replace_file_leaves_no_temporary_behind_when_the_write_fails(tmp_path):
    path = tmp_path / "a.tex"
    path.write_text("old\n", encoding="utf-8")
    with pytest.raises(OSError):
        replace_file(path, tmp_path / "does-not-exist.png")
    assert path.read_text(encoding="utf-8") == "old\n"
    assert [p.name for p in tmp_path.iterdir()] == ["a.tex"]


def _sha1(path: Path) -> str:
    import hashlib
    return hashlib.sha1(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------- live: really killing a sync
#
# One case per injection point. Each converts v1 into out/sync-crash/<point>, makes the same five
# human edits in the deck, syncs to `mixed` with B2S_FAIL_AT=!<point> (os._exit, so no `finally`,
# no report, no cleanup - a kill -9), and then syncs again without the hook. What must hold after
# that second sync: every deck edit is still there, the source's changes arrived, no duplicates or
# orphans, the slides nobody edited look like the control case's (an uninterrupted sync of the same
# source), and a third sync writes nothing.

CASES = {
    "plan": "planned, the staging deck made, nothing written",
    "journal": "the pending marker is stored, still nothing written",
    "measure": "the scratch slides for hole measurement are in the deck",
    "content": "in the middle of the content: one batch is in, the rest is not",
    "content:2": "two content batches in",
    "order": "content written, z-order half applied",
    "overrides": "content and order written, the deck's own edits going back on",
    "base:save": "everything written, the new base not stored anywhere",
    "base:drive": "the new base is on disk, Drive still has the old one",
    "cleanup": "the new base is stored, the replaced objects not deleted yet",
}
MUST_DIE = {"plan", "journal", "content", "base:save", "base:drive"}  # points every sync reaches
CRASH_PARALLEL = 3


def crash_edits(live):
    """The five edits a person made before the sync that dies. They cover what a killed write can
    lose: a word inside a bullet the source also rewrites, a moved element on a slide the source
    redraws (its override has to be re-applied), a run style, speaker notes, and an object of their
    own that nothing may sweep."""
    E = live.E
    return [E("replace_word", slide=live.WHY, text="People polish the converted deck by hand",
              old="polish", new="refine"),
            E("move", slide=live.CONV, target={"text": "Conflicts disappear once"}, dx=0, dy=40),
            E("bold", slide=live.CONCL, word="survive", context="Deck edits survive every sync"),
            E("set_notes", slide=live.ALGO, text="Walk through the steps slowly."),
            E("add_shape", slide=live.VERSIONS, shape_type="STAR_5", box=[620, 300, 50, 50], color="#ffc000")]


class CrashRun:
    """One case folder under out/sync-crash: convert, edit, sync (with or without a kill), check."""

    def __init__(self, name: str, live):
        self.live, self.name = live, name
        self.out = CRASH_OUT / name.replace(":", "-")
        self.out.mkdir(parents=True, exist_ok=True)
        self.log = open(CRASH_OUT / f"{name.replace(':', '-')}.log", "w", encoding="utf-8")
        self.problems: list[str] = []

    def cli(self, *args, env=None, check=True):
        self.log.write(f"\n$ beamer2slides {' '.join(map(str, args))}\n")
        self.log.flush()
        import subprocess
        import sys
        done = subprocess.run([sys.executable, "-m", "beamer2slides", *map(str, args)],
                              env={**self.live.ENV, **(env or {})}, cwd=self.live.ROOT,
                              stdout=self.log, stderr=subprocess.STDOUT)
        if check and done.returncode:
            raise RuntimeError(f"{self.name}: beamer2slides {args[0]} failed, see {self.log.name}")
        return done

    def convert(self, pdf):
        from beamer2slides.devtools.deck_edits import LiveDeck
        # --force-rebuild: the case folder holds the previous run's deck with its edits still on it,
        # and the rebuild guard (guard.py) would rightly refuse to replace that.
        self.cli("convert", pdf, "--out", self.out, "--force-rebuild", "--backup", "none")
        self.deck = LiveDeck(json.loads((self.out / "emit.json").read_text(encoding="utf-8"))["presentationId"])

    def edit(self, specs):
        from beamer2slides.devtools.deck_edits import verified
        self.deck.read()
        out = []
        for spec in specs:
            exp, bad = verified(self.deck, spec)
            if bad:
                raise RuntimeError(f"{self.name}: the edit itself failed: {bad}")
            out.append(exp)
        return out

    def sync(self, pdf, env=None, check=True):
        return self.cli("sync", pdf, "--deck", self.out, env=env, check=check)

    def revision(self) -> str:
        from beamer2slides.gslides import execute
        return execute(self.deck.api.presentations().get(presentationId=self.deck.pid,
                                                         fields="revisionId"))["revisionId"]

    def base(self) -> dict | None:
        path = self.out / "sync" / "base.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


CRASH_OUT: Path


def run_case(point: str, live, control) -> list[str]:
    """Convert, edit, kill a sync at `point`, sync again, and say what is wrong with the result."""
    from beamer2slides.devtools import sync_check as sc
    run = CrashRun(point, live)
    try:
        run.convert(live.build("v1"))
        exps = run.edit(crash_edits(live))
        before = run.revision()
        pdf = live.build("mixed")
        # A small batch size makes every phase take several batches, so a kill in one of them
        # really lands between two writes of that phase.
        died = run.sync(pdf, env={"B2S_FAIL_AT": f"!{point}", "B2S_BATCH_SIZE": "40"}, check=False)
        if died.returncode == 0:
            if point in MUST_DIE:
                return [f"the sync never reached {point}: nothing was killed"]
            pytest.skip(f"this sync never reaches {point} ({CASES[point]})")
        if point == "plan" and run.revision() != before:
            run.problems.append("the sync wrote to the deck before it had planned anything")
        # (`journal` is later than measure_places, whose scratch slides do change the revision)
        if point == "journal" and not (run.base() or {}).get("pending"):
            run.problems.append("the killed sync left no pending marker in the base")

        run.sync(pdf)  # the recovery run: this one must reach the same deck as the control
        model = run.deck.read()
        flags = live.sync_build.VARIANTS["mixed"]
        checks = [c for e in exps for c in e["checks"]] + live.sync_build.checks(flags) \
            + [{"check": "slides", "order": live.sync_build.titles(flags)}]
        run.problems += [f"after being killed at {point}: {p}" for p in sc.check_all(model, checks)]
        base = run.base() or {}
        run.problems += sc.integrity(model, base_ids=sc.ids_in(base) if base else None)
        for left in ("pending", "cleanup"):
            if base.get(left):
                run.problems.append(f"the recovery sync left `{left}` in the base: {base[left]!r:.120}")

        if control is not None:  # the same end state as an uninterrupted sync of the same source
            edited = {s.id for e in exps for sel in e["slides"] for s in model.find(sel)}
            titles = [t for t in live.sync_build.titles(flags)
                      if len(model.find(t)) == 1 and model.one(t).id not in edited and len(control.find(t)) == 1]
            run.problems += [f"unlike an uninterrupted sync: {p}" for p in sc.compare_fresh(model, control, titles)]

        revision = run.revision()
        run.sync(pdf)
        if run.revision() != revision:
            run.problems.append("a third sync still changed the deck: the crash left it unconverged")
        return run.problems
    finally:
        run.log.close()


@pytest.fixture(scope="module")
def crashes(request):
    """point -> problems (or the exception / skip), every selected case run once, 3 at a time."""
    global CRASH_OUT
    from concurrent.futures import ThreadPoolExecutor
    from . import test_sync_live as live
    from .test_slides_alignment import MAIN, google_unavailable

    CRASH_OUT = Path(os.environ.get("B2S_SYNC_CRASH_OUT", MAIN / "out" / "sync-crash"))
    for reason in (live.cli_missing("sync"), live.pdflatex_missing(), google_unavailable()):
        if reason:
            pytest.skip(reason)
    points = [p for p in CASES if any(getattr(i, "callspec", None) and i.callspec.params.get("point") == p
                                      for i in request.session.items)]
    CRASH_OUT.mkdir(parents=True, exist_ok=True)

    control = CrashRun("control", live)  # an uninterrupted sync of exactly the same case
    try:
        control.convert(live.build("v1"))
        control.edit(crash_edits(live))
        control.sync(live.build("mixed"))
        from beamer2slides.devtools import sync_check as sc
        model = sc.read(control.deck.pid)
    finally:
        control.log.close()

    def one(point):
        try:
            return run_case(point, live, model)
        except pytest.skip.Exception as e:
            return e
        except Exception as e:  # reported by that point's test
            return e

    with ThreadPoolExecutor(max_workers=CRASH_PARALLEL) as pool:
        return dict(zip(points, pool.map(one, points)))


@pytest.mark.sync
@pytest.mark.parametrize("point", list(CASES))
def test_a_sync_killed_at(point, crashes):
    """Killed with os._exit at this point, the next sync of the same source must reach the deck an
    uninterrupted sync would have, with every deck edit still in it."""
    result = crashes[point]
    if isinstance(result, pytest.skip.Exception):
        pytest.skip(str(result))
    if isinstance(result, Exception):
        raise result
    if result:
        pytest.fail(f"killed at {point} ({CASES[point]}), {CRASH_OUT / point.replace(':', '-')}:\n  "
                    + "\n  ".join(result), pytrace=False)
