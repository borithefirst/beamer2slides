"""Offline tests of sync (docs/sync.md): identity, deck edit detection, the merge rules, diff3,
slide planning, and the request helpers. No Google calls."""

import copy
from pathlib import Path

import pytest

from beamer2slides import identity, merge
from beamer2slides.extract import frame_labels
from beamer2slides.sync import letterbox_fix, rename

DECKS = Path(__file__).resolve().parent / "decks" / "out"


# ---------------------------------------------------------------- helpers

def run(text, **kw):
    return {"text": text, "font": "CMSS10", "family": "sans", "size": 10.0, "bold": False, "italic": False,
            "smallcaps": False, "color": "#000000", "link": None, "script": None, "underline": False, "highlight": None, **kw}


def text_ir(text, bbox, eid="p0t1", role="body", **kw):
    return {"id": eid, "kind": "text", "role": role, "bbox": list(bbox), "panel": None, "code": False, "spans": [],
            "strokes": [], "paragraphs": [{
                "align": "left", "level": 0, "bullet": None, "size": 10.0, "text_x0": bbox[0], "tab_x0": None,
                "lines": [{"baseline": bbox[1] + 8 + 12 * k, "x0": bbox[0], "x1": bbox[2]}], "wrap_limit": None,
                "runs": [run(line, **kw)]} for k, line in enumerate(text.split("\n"))]}


def shape_ir(bbox, eid="p0s0", fill="#dddddd"):
    return {"id": eid, "kind": "shape", "role": "panel", "bbox": list(bbox), "shape": "RECTANGLE", "fill": fill, "flip": False,
            "radius": 0.0}


def readback(box, text=None, kind="shape", parent=None, title=None, style="s0", shape="h0", image=None, styles=None):
    x0, y0, x1, y1 = box
    out = {"kind": kind, "transform": [1.0, 0.0, 0.0, 1.0, x0, y0], "size": [x1 - x0, y1 - y0], "box": list(box),
           "parent_group": parent, "z": 0, "title": title, "description": None, "text": text,
           "text_styles": styles if styles is not None else [{"fontFamily": "Lato", "fontSize": 18.0}],
           "paragraph_styles": [{"alignment": "START"}], "text_style_hash": style, "shape_style": {"fill": {"color": "#dddddd", "alpha": 1.0}},
           "shape_style_hash": shape}
    if image:
        out["image"] = {"contentHash": image}
    return out


def entry(key, ir, oid=None, anchor=None):
    """A base element: IR hashes plus a read-back matching what the converter wrote (2x scale)."""
    h, fields = identity.ir_fields(ir, None, anchor)
    el = {"key": key, "id": ir["id"], "kind": ir["kind"], "role": ir.get("role"), "ir_hash": h, "fields": fields,
          "fingerprint": identity.fingerprint(ir, None, anchor), "anchor": anchor, "ir": ir}
    if oid:
        box = [2 * v for v in ir["bbox"]]
        text = merge.predicted_text(ir) if ir["kind"] == "text" else None
        el.update(objects=[oid], main=oid, readback={oid: readback(box, text)})
    return el


def ours_entry(key, ir, anchor=None):
    return {k: v for k, v in entry(key, ir, None, anchor).items()}


def base_slide(key, sid, elements, label=None, title=""):
    return {"key": key, "label": label, "title": title, "page": 0, "text": " ".join(e["fingerprint"]["text"] for e in elements),
            "layout": "TITLE_ONLY", "background": "color:#ffffff", "notes": "", "objectId": sid, "layoutObjectId": "L",
            "background_readback": {"state": "INHERIT"}, "notes_readback": "", "groups": [],
            "order": [e["main"] for e in elements if "main" in e], "elements": elements}


def live(slide):
    """The live slide exactly as the base recorded it."""
    return {"objectId": slide["objectId"], "layoutObjectId": "L", "background": slide["background_readback"], "notes": "",
            "notes_id": f"{slide['objectId']}_notes", "order": list(slide["order"]),
            "objects": {oid: copy.deepcopy(rb) for e in slide["elements"] for oid, rb in e["readback"].items()}}


def ours_of(slide):
    return {**{k: v for k, v in slide.items() if k not in ("objectId", "layoutObjectId", "background_readback",
                                                           "notes_readback", "groups", "order")},
            "elements": [{k: v for k, v in e.items() if k not in ("objects", "main", "readback")} for e in slide["elements"]]}


def three_slides():
    slides = []
    for n, name in enumerate(["intro", "results", "end"]):
        sid = f"b2s_s{n:03}"
        els = [entry("text/title/0", text_ir(name.title(), (10, 10, 100, 24), f"p{n}t0", "title"), f"{sid}_t0"),
               entry("text/body/0", text_ir(f"First point of {name}\nSecond point of {name}", (20, 60, 200, 90), f"p{n}t1"), f"{sid}_t1")]
        slides.append(base_slide(name, sid, els, label=name, title=name.title()))
    return {"version": 1, "generation": 0, "presentationId": "P", "master_background": None, "slides": slides}


def triple(base):
    ours = {"slides": [ours_of(s) for s in base["slides"]], "pairs": {j: j for j in range(len(base["slides"]))}}
    theirs = {"revisionId": "r", "slides": [live(s) for s in base["slides"]]}
    return ours, theirs


def unit(mplan, slide, key):
    return next(u for p in mplan["slides"] if p["key"] == slide for u in p.get("units", []) if u["key"] == key)


# ---------------------------------------------------------------- identity

def test_frame_labels_from_named_destinations():
    dests = [("Doc-Start", 0), ("Navigation1", 0), ("Navigation3", 2), ("last", 2), ("last<1>", 2), ("page.3", 2),
             ("steps", 3), ("steps<1>", 3), ("steps<2>", 4), ("steps<3>", 5), ("gone<1>", -1), ("gone", -1)]
    assert frame_labels(dests) == {2: "last", 3: "steps", 4: "steps", 5: "steps"}


def info(title, text, label=None, page=0):
    return {"label": label, "title": title, "text": text, "page": page}


def test_inserted_frame_shifts_no_keys():
    base = [info("Intro", "why we do this"), info("Results", "numbers went up a lot"), info("End", "thanks for listening")]
    keys = identity.slide_keys(base)
    assert keys == ["title:intro#1", "title:results#1", "title:end#1"]
    ours = [base[0], info("Method", "how we measured the numbers"), base[1], base[2]]
    got, pairs = identity.inherit_slide_keys(base, keys, ours)
    assert pairs == {0: 0, 2: 1, 3: 2}
    assert got == ["title:intro#1", "title:method#1", "title:results#1", "title:end#1"]


def test_renamed_title_keeps_key():
    base = [info("Intro", "why we do this and what it costs"), info("Results", "numbers went up a lot this year")]
    keys = identity.slide_keys(base)
    ours = [base[0], info("Findings", "numbers went up a lot this year")]
    got, pairs = identity.inherit_slide_keys(base, keys, ours)
    assert pairs == {0: 0, 1: 1} and got[1] == "title:results#1"


def test_repeated_titles_get_occurrences():
    keys = identity.slide_keys([info("Example", "a"), info("Example", "b"), info("", "c", page=2)])
    assert keys == ["title:example#1", "title:example#2", "page:3"]


def test_labelled_frames_match_wherever_they_moved():
    base = [info("A", "alpha text here", "a"), info("B", "beta text here", "b"), info("C", "gamma text here", "c")]
    keys = identity.slide_keys(base)
    assert keys == ["a", "b", "c"]
    ours = [info("C renamed", "completely different words", "c"), base[0], base[1]]
    got, pairs = identity.inherit_slide_keys(base, keys, ours)
    assert pairs == {0: 2, 1: 0, 2: 1} and got == ["c", "a", "b"]


def test_labelled_and_unlabelled_mixed():
    base = [info("A", "alpha words here", "a"), info("Plain", "some plain words"), info("B", "beta words here", "b")]
    keys = identity.slide_keys(base)
    ours = [info("B", "beta words here", "b"), info("Plain", "some plain words, edited"), info("New", "brand new", "new")]
    got, pairs = identity.inherit_slide_keys(base, keys, ours)
    assert pairs == {0: 2, 1: 1}
    assert got == ["b", "title:plain#1", "new"]
    # two different labels never pair up, however similar the frames are
    got, pairs = identity.inherit_slide_keys([info("X", "same", "x")], ["x"], [info("X", "same", "y")])
    assert pairs == {} and got == ["y"]


def test_element_keys_follow_content():
    els = [text_ir("Title", (10, 10, 100, 24), "p0t0", "title"), text_ir("First paragraph of text", (20, 60, 200, 70), "p0t1"),
           text_ir("Second paragraph of text", (20, 80, 200, 90), "p0t2")]
    keys, fps = identity.slide_element_keys(els, None)
    assert keys == ["text/title/0", "text/body/0", "text/body/1"]
    base = [{"key": k, "kind": e["kind"], "role": e["role"], "fingerprint": f} for k, e, f in zip(keys, els, fps)]
    # a paragraph inserted before the others: they keep their keys, the new one gets a fresh ordinal
    ours = [els[0], text_ir("A brand new opening remark", (20, 45, 200, 55), "p0t1"),
            {**els[1], "id": "p0t2"}, {**els[2], "id": "p0t3"}]
    got, _ = identity.slide_element_keys(ours, None, base)
    assert got == ["text/title/0", "text/body/2", "text/body/0", "text/body/1"]


def test_anchored_elements_take_their_anchors_key():
    text = text_ir("A formula here and more words", (20, 60, 200, 70), "p0t1")
    pic = {"id": "p0h0", "kind": "image", "role": "math", "bbox": [80, 60, 100, 70], "anchor": "p0t1"}
    keys, fps = identity.slide_element_keys([text, pic], None)
    assert keys == ["text/body/0", "image/math/0"] and fps[1]["anchor"] == "text/body/0"


def test_ir_hash_ignores_ids_and_page_numbers():
    a = text_ir("Go to results", (20, 60, 200, 70), "p3t1", link="#page=7")
    b = {**text_ir("Go to results", (20, 60, 200, 70), "p4t1", link="#page=8"), "spans": ["p4s1", "p4s2"]}
    ha, _ = identity.ir_fields(a, None, None, lambda page: "results")
    hb, _ = identity.ir_fields(b, None, None, lambda page: "results")
    assert ha == hb
    hc, _ = identity.ir_fields(b, None, None, lambda page: "method")
    assert hc != ha
    # render output (how a bare image reached its file, its pixel size) is no source change either
    img = {"id": "p1i0", "kind": "image", "role": "figure", "bbox": [10, 10, 60, 40], "file": "figures/p1i0.png"}
    assert identity.ir_fields(img)[0] == identity.ir_fields({**img, "picture": "raw", "px": [800, 480]})[0]


def test_source_changes_by_field():
    old = entry("text/body/0", text_ir("Authors keep writing", (20, 60, 120, 70)))
    reworded = entry("text/body/0", text_ir("Authors and their AI keep writing", (20, 60, 160, 70)))
    moved = entry("text/body/0", text_ir("Authors keep writing", (20, 90, 120, 100)))
    red = entry("text/body/0", text_ir("Authors keep writing", (20, 60, 120, 70), color="#ff0000"))
    assert identity.source_changes(old, old) == set()
    assert identity.source_changes(old, reworded) == {"text", "size"}
    assert identity.source_changes(old, moved) == {"position"}
    assert identity.source_changes(old, red) == {"style"}


# ---------------------------------------------------------------- deck edits

def test_object_changes():
    b = readback([10, 10, 110, 30], "Hello\n")
    assert merge.object_changes(b, copy.deepcopy(b)) == set()
    moved = {**b, "box": [20, 10, 120, 30], "transform": [1, 0, 0, 1, 20, 10]}
    assert merge.object_changes(b, moved) == {"geometry"}
    assert merge.object_changes(b, {**b, "box": [10.03, 10, 110.03, 30]}) == set()  # read-back noise
    assert merge.object_changes(b, {**b, "text": "Hello world\n"}) == {"text"}
    assert merge.object_changes(b, {**b, "text_style_hash": "s1"}) == {"text_style"}
    assert merge.object_changes(b, {**b, "shape_style_hash": "h1"}) == {"shape_style"}
    assert merge.object_changes(b, {**b, "parent_group": "g"}) == {"group"}


def test_deck_edits_and_user_objects():
    base = three_slides()
    s = base["slides"][0]
    theirs = live(s)
    assert merge.deck_edits(s["elements"][1], theirs) == {}
    del theirs["objects"]["b2s_s000_t1"]
    theirs["objects"]["copy"] = readback([0, 0, 5, 5], "x", title="b2s:intro/text/title/0")
    theirs["objects"]["mine"] = readback([0, 0, 5, 5], "y")
    assert merge.deck_edits(s["elements"][1], theirs) == {"deleted": ["b2s_s000_t1"]}
    assert merge.user_objects(s, theirs) == [{"objectId": "copy", "copy_of": "intro/text/title/0"},
                                            {"objectId": "mine", "copy_of": None}]


def test_ungrouped_unit_is_a_group_edit_not_a_part_deletion():
    """Ungrouping a formula's group in Slides removes the `_g` object: a group edit (the unit is
    rebuilt ungrouped), not a part deletion (which kept the whole unit as a conflict)."""
    base = three_slides()
    el = base["slides"][0]["elements"][1]
    el["objects"].append("b2s_s000_t1_g")
    el["readback"]["b2s_s000_t1_g"] = {**readback([20, 60, 200, 90]), "kind": "elementGroup"}
    el["readback"]["b2s_s000_t1"]["parent_group"] = "b2s_s000_t1_g"
    theirs = live(base["slides"][0])
    del theirs["objects"]["b2s_s000_t1_g"]
    theirs["objects"]["b2s_s000_t1"]["parent_group"] = None
    assert {k: sorted(v) for k, v in merge.deck_edits(el, theirs).items()} == {"group": ["b2s_s000_t1", "b2s_s000_t1_g"]}
    ours, live_deck = triple(base)
    live_deck["slides"][0] = theirs
    ours["slides"][0]["elements"][1] = ours_entry("text/body/0", text_ir("First point of intro, reworded\nSecond point of intro",
                                                                         (20, 60, 230, 90), "p0t1"))
    u = unit(merge.plan_merge(base, ours, live_deck), "intro", "text/body/0")
    assert u["action"] == "recreate"


def test_uniform_style_changes():
    base = [{"fontFamily": "Lato", "fontSize": 18.0}, {"fontFamily": "Lato", "fontSize": 18.0, "bold": True}]
    red = [{**s, "foregroundColor": "#cc0000"} for s in base]
    assert merge.uniform_changes(base, base) == {}
    assert merge.uniform_changes(base, red) == {"foregroundColor": "#cc0000"}
    one_word = base + [{"fontFamily": "Lato", "fontSize": 18.0, "italic": True}]
    assert merge.uniform_changes(base, one_word) is None


# ---------------------------------------------------------------- diff3 and text edits

def test_diff3_clean_merges():
    base = "Colleagues polish the slides in Google Slides"
    assert merge.diff3(base, base, base) == (base, [])
    assert merge.diff3(base, "Colleagues refine the slides in Google Slides", "Designers polish the slides in Google Slides") == \
        ("Designers refine the slides in Google Slides", [])
    assert merge.diff3(base, "Colleagues polish the slides in Slides", base)[0] == "Colleagues polish the slides in Slides"
    # the same change on both sides is no conflict
    same = "Colleagues polish all the slides in Google Slides"
    assert merge.diff3(base, same, same) == (same, [])
    # paragraphs apart
    merged, clashes = merge.diff3("One\nTwo\nThree\n", "One more\nTwo\nThree\n", "One\nTwo\nThree!\n")
    assert merged == "One more\nTwo\nThree!\n" and not clashes


def test_diff3_conflicts_keep_theirs():
    base = "Colleagues polish the slides"
    merged, clashes = merge.diff3(base, "Teammates polish the slides", "Designers polish the slides")
    assert merged == "Designers polish the slides"
    assert clashes == [{"base": "Colleagues", "ours": "Teammates", "theirs": "Designers"}]
    # insertions at the same place differ
    merged, clashes = merge.diff3("a b", "a x b", "a y b")
    assert clashes and merged == "a y b"


def apply_text_requests(text: str, reqs: list[dict]) -> str:
    units = list(text)  # (ASCII in these tests: UTF-16 indices are character indices)
    for r in reqs:
        if "deleteText" in r:
            rng = r["deleteText"]["textRange"]
            del units[rng["startIndex"]:rng["endIndex"]]
        else:
            i = r["insertText"]["insertionIndex"]
            units[i:i] = list(r["insertText"]["text"])
    return "".join(units)


@pytest.mark.parametrize("current,target", [
    ("Authors keep writing\nThe end\n", "Authors and their AI keep writing\nThe end\n"),
    ("Designers polish the slides\n", "Teammates polish all slides\n"),
    ("One\nTwo\nThree\n", "One\nThree\n"),
    ("abc\n", "abc\n"),
])
def test_text_edit_requests(current, target):
    assert apply_text_requests(current, merge.text_edit_requests("t", current, target)) == target


def test_text_edit_requests_count_utf16():
    reqs = merge.text_edit_requests("t", "\U0001d465 is x\n", "\U0001d465 is y\n")
    assert reqs[0]["deleteText"]["textRange"] == {"type": "FIXED_RANGE", "startIndex": 6, "endIndex": 7}


# ---------------------------------------------------------------- merge rules

def test_no_changes_plans_no_writes():
    base = three_slides()
    ours, theirs = triple(base)
    mplan = merge.plan_merge(base, ours, theirs)
    assert all(u["action"] == "keep" for p in mplan["slides"] for u in p["units"])
    assert not merge.has_writes(mplan, [s["objectId"] for s in theirs["slides"]])
    assert mplan["report"]["applied"] == [] and mplan["report"]["conflicts"] == []


def edit_text(slide, oid, text):
    slide["objects"][oid]["text"] = text


def test_source_change_applied_when_deck_untouched():
    base = three_slides()
    ours, theirs = triple(base)
    ours["slides"][0]["elements"][1] = ours_entry("text/body/0", text_ir("First point of intro, reworded\nSecond point of intro",
                                                                         (20, 60, 230, 90), "p0t1"))
    mplan = merge.plan_merge(base, ours, theirs)
    u = unit(mplan, "intro", "text/body/0")
    assert u["action"] == "recreate" and u["overrides"] == {}
    assert merge.has_writes(mplan, [s["objectId"] for s in theirs["slides"]])


def test_deck_edit_kept_when_source_unchanged():
    base = three_slides()
    ours, theirs = triple(base)
    edit_text(theirs["slides"][1], "b2s_s001_t1", "Something else entirely\n")
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "results", "text/body/0")["action"] == "keep"
    assert mplan["report"]["overrides"] == [{"slide": "results", "element": "text/body/0", "fields": ["text"]}]
    assert not merge.has_writes(mplan, [s["objectId"] for s in theirs["slides"]])


def test_source_text_and_deck_geometry_recreate_at_deck_place():
    base = three_slides()
    ours, theirs = triple(base)
    ours["slides"][0]["elements"][1] = ours_entry("text/body/0", text_ir("First point of intro\nSecond point, changed",
                                                                         (20, 60, 200, 90), "p0t1"))
    obj = theirs["slides"][0]["objects"]["b2s_s000_t1"]
    obj["box"] = [v + 30 for v in obj["box"]]
    obj["transform"][4] += 30
    obj["transform"][5] += 30
    u = unit(merge.plan_merge(base, ours, theirs), "intro", "text/body/0")
    assert u["action"] == "recreate" and u["overrides"] == {"geometry": {"mode": "delta"}}


def test_both_moved_deck_position_wins():
    base = three_slides()
    ours, theirs = triple(base)
    ours["slides"][0]["elements"][1] = ours_entry("text/body/0", text_ir("First point of intro\nSecond point, changed",
                                                                         (20, 100, 200, 130), "p0t1"))
    obj = theirs["slides"][0]["objects"]["b2s_s000_t1"]
    obj["box"] = [v + 30 for v in obj["box"]]
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "intro", "text/body/0")["overrides"] == {"geometry": {"mode": "theirs"}}
    assert [c["field"] for c in mplan["report"]["conflicts"]] == ["geometry"]


def test_both_text_clean_diff3():
    base = three_slides()
    ours, theirs = triple(base)
    ours["slides"][0]["elements"][1] = ours_entry("text/body/0", text_ir("First point of intro\nSecond point of the intro",
                                                                         (20, 60, 200, 90), "p0t1"))
    edit_text(theirs["slides"][0], "b2s_s000_t1", "First idea of intro\nSecond point of intro\n")
    mplan = merge.plan_merge(base, ours, theirs)
    u = unit(mplan, "intro", "text/body/0")
    assert u["action"] == "recreate" and set(u["overrides"]) == {"text"}
    assert not mplan["report"]["conflicts"]


def test_both_text_overlapping_is_a_conflict():
    base = three_slides()
    ours, theirs = triple(base)
    ours["slides"][0]["elements"][1] = ours_entry("text/body/0", text_ir("Main point of intro\nSecond point of intro",
                                                                         (20, 60, 200, 90), "p0t1"))
    edit_text(theirs["slides"][0], "b2s_s000_t1", "Best point of intro\nSecond point of intro\n")
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "intro", "text/body/0")["action"] == "keep"
    (c,) = mplan["report"]["conflicts"]
    assert c["field"] == "text" and c["resolution"] == "deck kept" and "Best point" in c["theirs"]


def test_same_text_on_both_sides_converges():
    base = three_slides()
    ours, theirs = triple(base)
    ours["slides"][0]["elements"][1] = ours_entry("text/body/0", text_ir("Main point of intro\nSecond point of intro",
                                                                         (20, 60, 200, 90), "p0t1"))
    edit_text(theirs["slides"][0], "b2s_s000_t1", "Main point of intro\nSecond point of intro\n")
    mplan = merge.plan_merge(base, ours, theirs)
    assert mplan["report"]["converged"] == [{"slide": "intro", "element": "text/body/0", "field": "text",
                                             "value": "Main point of intro\nSecond point of intro\n"}]
    # (nothing to write: the deck already shows it; the base adopts the deck's text)
    assert unit(mplan, "intro", "text/body/0")["action"] == "adopt"
    assert not mplan["report"]["applied"] and not merge.has_writes(mplan, [s["objectId"] for s in theirs["slides"]])


def table_ir(rows, bbox=(20, 60, 200, 120), eid="p1tab0"):
    return {"id": eid, "kind": "table", "role": "table", "bbox": list(bbox), "frame": list(bbox), "size": 10.0,
            "row_baselines": [bbox[1] + 12 * k for k in range(len(rows))], "row_heights": [12.0] * len(rows),
            "columns": [{"x0": bbox[0] + 60 * c, "x1": bbox[0] + 60 * (c + 1), "align": "left"} for c in range(len(rows[0]))],
            "bounds": list(bbox), "cells": [[[run(cell)] for cell in row] for row in rows], "merges": [], "rules": [],
            "borders": [], "fills": [], "spans": []}


def table_slides(rows):
    """three_slides with a table on the results slide (element key table/table/0)."""
    base = three_slides()
    el = entry("table/table/0", table_ir(rows), "b2s_s001_tab0")
    rb = el["readback"]["b2s_s001_tab0"]
    rb["kind"], rb["table"] = "table", [len(rows), len(rows[0])]
    rb["text"] = identity.plain_text(el["ir"])
    base["slides"][1]["elements"].append(el)
    base["slides"][1]["order"].append("b2s_s001_tab0")
    return base


ROWS = [["Scenario", "Kept", "Time"], ["Disjoint", "100%", "3.9 s"], ["Conflicts", "92%", "6.0 s"]]


def test_table_cells_edited_on_both_sides_merge():
    base = table_slides(ROWS)
    ours, theirs = triple(base)
    changed = [r[:] for r in ROWS]
    changed[1][1] = "98%"  # the source says another number
    ours["slides"][1]["elements"][2] = ours_entry("table/table/0", table_ir(changed))
    edit_text(theirs["slides"][1], "b2s_s001_tab0", "Scenario\tKept\tTime\nDisjoint\t100%\t3.9 s\nConflicts\t92%\t6.2 s")
    mplan = merge.plan_merge(base, ours, theirs)
    u = unit(mplan, "results", "table/table/0")
    assert u["action"] == "recreate" and set(u["overrides"]) == {"text"}
    assert u["overrides"]["text"]["table"] and u["overrides"]["text"]["dims"] == [3, 3]
    assert not mplan["report"]["conflicts"]
    # the deck's cell wins where it was edited, the source's where it was
    cells, converged = merge.table_merge(u["overrides"]["text"]["base"], identity.plain_text(table_ir(changed)),
                                         u["overrides"]["text"]["theirs"], [3, 3], [3, 3])
    assert [cells[1][1], cells[2][2]] == ["98%", "6.2 s"] and not converged


def test_the_same_cell_edited_on_both_sides_is_a_conflict():
    base = table_slides(ROWS)
    ours, theirs = triple(base)
    changed = [r[:] for r in ROWS]
    changed[2][0] = "Clashes"  # the same word the deck renamed, to something else
    ours["slides"][1]["elements"][2] = ours_entry("table/table/0", table_ir(changed))
    edit_text(theirs["slides"][1], "b2s_s001_tab0", "Scenario\tKept\tTime\nDisjoint\t100%\t3.9 s\nDisputes\t92%\t6.0 s")
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "results", "table/table/0")["action"] == "keep"
    (c,) = mplan["report"]["conflicts"]
    assert c["field"] == "text" and c["resolution"] == "deck kept"


def test_a_row_added_in_the_deck_keeps_the_table():
    base = table_slides(ROWS)
    ours, theirs = triple(base)
    changed = [r[:] for r in ROWS]
    changed[1][1] = "98%"
    ours["slides"][1]["elements"][2] = ours_entry("table/table/0", table_ir(changed))
    obj = theirs["slides"][1]["objects"]["b2s_s001_tab0"]
    obj["text"] += "\nExtra\t0%\t0.0 s"
    obj["table"] = [4, 3]
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "results", "table/table/0")["action"] == "keep"
    assert [c["field"] for c in mplan["report"]["conflicts"]] == ["text"]


def test_table_cells_the_deck_already_shows_converge():
    base = table_slides(ROWS)
    ours, theirs = triple(base)
    changed = [r[:] for r in ROWS]
    changed[1][1] = "98%"
    ours["slides"][1]["elements"][2] = ours_entry("table/table/0", table_ir(changed))
    edit_text(theirs["slides"][1], "b2s_s001_tab0", identity.plain_text(table_ir(changed)))
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "results", "table/table/0")["action"] == "adopt"
    assert [c["field"] for c in mplan["report"]["converged"]] == ["text"]
    assert not merge.has_writes(mplan, [s["objectId"] for s in theirs["slides"]])


def test_table_cell_edit_requests_carry_the_cell():
    reqs = merge.text_edit_requests("tab", "6.0 s\n", "6.2 s\n", {"rowIndex": 2, "columnIndex": 2})
    assert all(next(iter(r.values()))["cellLocation"] == {"rowIndex": 2, "columnIndex": 2} for r in reqs)
    assert apply_text_requests("6.0 s\n", reqs) == "6.2 s\n"


def test_words_of_one_cell_merge_like_prose():
    b, o, t = "Conflicts\t92%\t6.0 s", "Conflicts\t92%\t6.0 seconds", "Conflicts\t94%\t6.0 s"
    cells, converged = merge.table_merge(b, o, t, [1, 3], [1, 3])
    assert cells == [["Conflicts", "94%", "6.0 seconds"]] and not converged


def test_a_cell_with_a_line_break_is_not_merged():
    assert merge.table_grid("a\tb\nc\td\te", [2, 2]) is None
    assert merge.table_grid("a\tb\nc\td", [2, 2]) == [["a", "b"], ["c", "d"]]


def test_changed_in_source_deleted_in_deck():
    base = three_slides()
    ours, theirs = triple(base)
    ours["slides"][2]["elements"][1] = ours_entry("text/body/0", text_ir("Other words", (20, 60, 200, 90), "p2t1"))
    del theirs["slides"][2]["objects"]["b2s_s002_t1"]
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "end", "text/body/0")["action"] == "keep"
    assert mplan["report"]["conflicts"][0]["resolution"] == "kept deleted"


def test_removed_in_source():
    base = three_slides()
    ours, theirs = triple(base)
    del ours["slides"][2]["elements"][1]
    assert unit(merge.plan_merge(base, ours, theirs), "end", "text/body/0")["action"] == "delete"
    edit_text(theirs["slides"][2], "b2s_s002_t1", "Edited in the deck\n")
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "end", "text/body/0")["action"] == "keep"
    assert mplan["report"]["conflicts"][0]["field"] == "removed"


def test_removed_text_living_on_in_a_conflict_is_kept():
    base = three_slides()
    extra = entry("text/body/1", text_ir("A closing remark", (20, 120, 200, 130), "p0t2"), "b2s_s000_t2")
    base["slides"][0]["elements"].append(extra)
    base["slides"][0]["order"].append("b2s_s000_t2")
    ours, theirs = triple(base)
    # the source joins the remark into the list and rewords the first point; the deck rewords it too
    ours["slides"][0]["elements"][1] = ours_entry("text/body/0", text_ir(
        "Main point of intro\nSecond point of intro\nA closing remark", (20, 60, 200, 130), "p0t1"))
    del ours["slides"][0]["elements"][2]
    edit_text(theirs["slides"][0], "b2s_s000_t1", "Best point of intro\nSecond point of intro\n")
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "intro", "text/body/1")["action"] == "keep"
    assert not merge.has_writes(mplan, [s["objectId"] for s in theirs["slides"]])


def test_added_in_source_and_moved_only_in_source():
    base = three_slides()
    ours, theirs = triple(base)
    ours["slides"][1]["elements"].append(ours_entry("text/body/1", text_ir("New remark", (20, 120, 200, 130), "p1t2")))
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "results", "text/body/1")["action"] == "create"
    # moved in the source, text edited in the deck: the deck object moves
    ours, theirs = triple(base)
    ours["slides"][1]["elements"][1] = ours_entry("text/body/0", text_ir("First point of results\nSecond point of results",
                                                                         (20, 80, 200, 110), "p1t1"))
    edit_text(theirs["slides"][1], "b2s_s001_t1", "First point, as the deck says\nSecond point of results\n")
    u = unit(merge.plan_merge(base, ours, theirs), "results", "text/body/0")
    assert u["action"] == "move" and u["delta"] == [0, 20]


def anchored_picture_base():
    """`results` with an inline formula picture anchored to its body text."""
    base = three_slides()
    pic = {"id": "p1m0", "kind": "image", "role": "math", "bbox": [120, 62, 140, 74], "file": None}
    base["slides"][1]["elements"].append(entry("image/math/0", pic, "b2s_s001_m0", anchor="text/body/0"))
    return base, pic


def test_member_moved_alone_is_recreated_not_moved():
    """Found by the offline fuzz (tools/fuzz_sync.py, seeds 252 and 430): the source re-placed the
    inline formula picture inside its line while the deck edited that paragraph. Sync writes a
    `move` by moving the unit's top object, so a step that fits only one member cannot be written -
    it used to become a move of [0, 0] the report still called applied (nothing moved, no conflict
    raised). Such a unit is recreated instead."""
    base, pic = anchored_picture_base()
    ours, theirs = triple(base)
    ours["slides"][1]["elements"][2] = ours_entry("image/math/0", {**pic, "bbox": [128, 62, 148, 74]}, "text/body/0")
    edit_text(theirs["slides"][1], "b2s_s001_t1", "First point, as the deck says\nSecond point of results\n")
    mplan = merge.plan_merge(base, ours, theirs)
    u = unit(mplan, "results", "text/body/0")
    assert u["action"] == "recreate" and "delta" not in u
    assert not [a for a in mplan["report"]["applied"] if a.get("how") == "deck object moved"]


def test_whole_unit_moved_still_moves_the_deck_objects():
    """The counterpart: every member moved by the same step, so one move carries the unit."""
    base, pic = anchored_picture_base()
    ours, theirs = triple(base)
    ours["slides"][1]["elements"][1] = ours_entry("text/body/0", text_ir("First point of results\nSecond point of results",
                                                                         (20, 80, 200, 110), "p1t1"))
    ours["slides"][1]["elements"][2] = ours_entry("image/math/0", {**pic, "bbox": [120, 82, 140, 94]}, "text/body/0")
    edit_text(theirs["slides"][1], "b2s_s001_t1", "First point, as the deck says\nSecond point of results\n")
    u = unit(merge.plan_merge(base, ours, theirs), "results", "text/body/0")
    assert u["action"] == "move" and u["delta"] == [0, 20]


def move_object(slide, oid, dx, dy):
    rb = slide["objects"][oid]
    rb["box"] = [rb["box"][0] + dx, rb["box"][1] + dy, rb["box"][2] + dx, rb["box"][3] + dy]
    rb["transform"] = rb["transform"][:4] + [rb["transform"][4] + dx, rb["transform"][5] + dy]


def test_picture_the_deck_moved_inside_its_unit_is_kept_not_recreated():
    """Found by the offline fuzz (tools/fuzz_sync.py, seed 720): the person dragged the formula
    picture inside its line while the source reworded that paragraph. Sync re-applies a geometry
    override by transforming the unit's *top* object, so a member that moved on its own would land
    back at the converter's box - silently, since the report listed the geometry as an override.
    The unit is kept as the deck has it, and the clash is reported."""
    base, pic = anchored_picture_base()
    ours, theirs = triple(base)
    ours["slides"][1]["elements"][1] = ours_entry("text/body/0", text_ir("First point reworded\nSecond point of results",
                                                                         (20, 60, 200, 90), "p1t1"))
    move_object(theirs["slides"][1], "b2s_s001_m0", 0, 40)
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "results", "text/body/0")["action"] == "keep"
    clash, = [c for c in mplan["report"]["conflicts"] if c["field"] == "geometry"]
    assert clash["element"] == "text/body/0" and "on its own" in clash["resolution"]


def test_unit_the_deck_moved_as_a_whole_is_still_recreated():
    """The counterpart: the person moved every object of the unit by the same step (they moved the
    group), so the top's transform carries the whole unit and the source's rewording goes in."""
    base, pic = anchored_picture_base()
    ours, theirs = triple(base)
    ours["slides"][1]["elements"][1] = ours_entry("text/body/0", text_ir("First point reworded\nSecond point of results",
                                                                         (20, 60, 200, 90), "p1t1"))
    for oid in ("b2s_s001_t1", "b2s_s001_m0"):
        move_object(theirs["slides"][1], oid, 0, 40)
    u = unit(merge.plan_merge(base, ours, theirs), "results", "text/body/0")
    assert u["action"] == "recreate" and u["overrides"]["geometry"] == {"mode": "delta"}


def test_image_replaced_in_deck_is_kept():
    base = three_slides()
    pic = {"id": "p1f0", "kind": "image", "role": "figure", "bbox": [200, 60, 300, 160], "file": None}
    el = entry("image/figure/0", pic, "b2s_s001_f2")
    el["readback"]["b2s_s001_f2"] = readback([400, 120, 600, 320], kind="image", image="aaa")
    base["slides"][1]["elements"].append(el)
    ours, theirs = triple(base)
    ours["slides"][1]["elements"][2] = ours_entry("image/figure/0", {**pic, "bbox": [200, 60, 320, 160]})
    theirs["slides"][1]["objects"]["b2s_s001_f2"]["image"] = {"contentHash": "bbb"}
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "results", "image/figure/0")["action"] == "keep"
    assert mplan["report"]["conflicts"][0]["field"] == "image"


# ---------------------------------------------------------------- slides

def test_slide_added_deleted_and_reordered():
    base = three_slides()
    ours, theirs = triple(base)
    live_ids = [s["objectId"] for s in theirs["slides"]]
    # a new frame after intro
    new = ours_of(base_slide("method", None, [entry("text/title/0", text_ir("Method", (10, 10, 100, 24), "p1t0", "title"))], "method"))
    ours2 = {"slides": [ours["slides"][0], new, ours["slides"][1], ours["slides"][2]], "pairs": {0: 0, 2: 1, 3: 2}}
    mplan = merge.plan_merge(base, ours2, theirs)
    assert mplan["order"] == ["b2s_s000", "new:method", "b2s_s001", "b2s_s002"]
    assert mplan["report"]["slides"]["created"] == ["method"]
    # results removed from the source, untouched in the deck: deleted
    ours3 = {"slides": [ours["slides"][0], ours["slides"][2]], "pairs": {0: 0, 1: 2}}
    mplan = merge.plan_merge(base, ours3, theirs)
    assert [p["action"] for p in mplan["slides"] if p["key"] == "results"] == ["delete"]
    assert mplan["order"] == ["b2s_s000", "b2s_s002"]
    # ... but kept after its predecessor when the deck added something to it
    theirs["slides"][1]["objects"]["user_box"] = readback([0, 0, 50, 20], "note")
    mplan = merge.plan_merge(base, ours3, theirs)
    assert [p["action"] for p in mplan["slides"] if p["key"] == "results"] == ["keep_removed"]
    assert mplan["order"] == live_ids
    # the source swaps results and end
    ours, theirs = triple(base)
    ours4 = {"slides": [ours["slides"][0], ours["slides"][2], ours["slides"][1]], "pairs": {0: 0, 1: 2, 2: 1}}
    mplan = merge.plan_merge(base, ours4, theirs)
    assert mplan["order"] == ["b2s_s000", "b2s_s002", "b2s_s001"]
    assert merge.has_writes(mplan, live_ids)
    # ... not applied when the deck reordered slides itself
    theirs["slides"] = [theirs["slides"][1], theirs["slides"][0], theirs["slides"][2]]
    mplan = merge.plan_merge(base, ours4, theirs)
    assert mplan["order"] == ["b2s_s001", "b2s_s000", "b2s_s002"]


def test_user_added_slide_stays_after_its_predecessor():
    base = three_slides()
    ours, theirs = triple(base)
    theirs["slides"].insert(2, {"objectId": "user_slide", "layoutObjectId": "L", "background": {"state": "INHERIT"},
                                "notes": "", "notes_id": None, "order": [], "objects": {}})
    new = ours_of(base_slide("method", None, [entry("text/title/0", text_ir("Method", (10, 10, 100, 24), "p1t0", "title"))], "method"))
    ours2 = {"slides": [new] + ours["slides"], "pairs": {1: 0, 2: 1, 3: 2}}
    mplan = merge.plan_merge(base, ours2, theirs)
    assert mplan["order"] == ["new:method", "b2s_s000", "b2s_s001", "user_slide", "b2s_s002"]
    assert mplan["report"]["slides"]["user_added"] == [{"objectId": "user_slide", "copy_of": None}]


def test_notes_follow_text_rules():
    base = three_slides()
    ours, theirs = triple(base)
    ours["slides"][0]["notes"] = "Say hello"
    mplan = merge.plan_merge(base, ours, theirs)
    assert mplan["slides"][0]["notes"] == "Say hello"
    theirs["slides"][0]["notes"] = "Please say hello"
    ours["slides"][0]["notes"] = "Say hello to everyone"
    mplan = merge.plan_merge(base, ours, theirs)
    assert "notes" not in mplan["slides"][0]  # both wrote into empty notes: a conflict, the deck's kept
    assert [c["field"] for c in mplan["report"]["conflicts"]] == ["notes"]
    ours["slides"][0]["notes"] = ""
    mplan = merge.plan_merge(base, ours, theirs)
    assert "notes" not in mplan["slides"][0]
    assert {"slide": "intro", "element": None, "fields": ["notes"]} in mplan["report"]["overrides"]


# ---------------------------------------------------------------- request helpers

def test_rename_object_ids():
    reqs = [{"createShape": {"objectId": "b2s_s003_t1", "elementProperties": {"pageObjectId": "b2s_s003"}}},
            {"groupObjects": {"groupObjectId": "b2s_s003_t1_g", "childrenObjectIds": ["b2s_s003_t1", "b2s_s003_f12", "b2s_s003_f1n"]}},
            {"duplicateObject": {"objectId": "b2s_s003_k0", "objectIds": {"b2s_s003_k0": "b2s_s003_s0"}}}]
    mapping = sorted({"b2s_s003_t1": "NEW_T", "b2s_s003_f1": "NEW_F", "b2s_s003_k0": "TPL", "b2s_s003": "LIVE",
                      "b2s_s003_s0": "NEW_S"}.items(), key=lambda kv: -len(kv[0]))
    out = rename(reqs, mapping)
    assert out[0]["createShape"] == {"objectId": "NEW_T", "elementProperties": {"pageObjectId": "LIVE"}}
    assert out[1]["groupObjects"] == {"groupObjectId": "NEW_T_g", "childrenObjectIds": ["NEW_T", "LIVE_f12", "NEW_Fn"]}
    assert out[2]["duplicateObject"] == {"objectId": "TPL", "objectIds": {"TPL": "NEW_S"}}


def test_letterbox_fix_stretches_to_the_box():
    fix = letterbox_fix("i", [100, 50, 400, 150], (800, 600))["updatePageElementTransform"]["transform"]
    # createImage puts a 4:3 picture into a 300 x 100 box as 133.33 x 100 centred at x = 183.33
    fx0, fx1 = 183.3333, 316.6667
    assert fix["scaleX"] * fx0 + fix["translateX"] / 12700 == pytest.approx(100, abs=0.01)
    assert fix["scaleX"] * fx1 + fix["translateX"] / 12700 == pytest.approx(400, abs=0.01)
    assert fix["scaleY"] == pytest.approx(1) and fix["translateY"] == pytest.approx(0, abs=1)


def test_a_base_out_of_the_sources_order_loses_the_frames_after_it():
    """Why the new base has to stay in the source's order: frames without a label are paired with
    the base by an order-keeping alignment, so a base entry moved to the end takes the identity of
    the frames that followed it with it - they look new, and the next sync creates them again."""
    source = [info("Why decks diverge", "decks and sources drift apart over time"),
              info("The sync algorithm", "base ours theirs three way merge of the deck"),
              info("Conclusions", "thanks for listening and for the questions")]
    keys = identity.slide_keys(source)
    moved = [source[0], source[2], source[1]]  # the middle frame's entry recorded last
    got, _ = identity.inherit_slide_keys(moved, [keys[0], keys[2], keys[1]], source)
    assert got[:2] == keys[:2] and got[2] == "title:conclusions#2" != keys[2]


@pytest.mark.xfail(strict=True, reason="sync.base_order leaves out the slides the deck deleted, so they land at the "
                                       "end of the new base, and the frames after them in the source can no longer be "
                                       "aligned with it: the next sync creates them again")
def test_a_slide_the_deck_deleted_keeps_its_place_in_the_new_base():
    """Found by the offline fuzz (`second_sync_writes`, seeds 45, 60, 108, 177 of the default run):
    the person deletes a converter slide whose frame has no label and the source still has it. The
    sync is right to leave it deleted, but it records that slide's base entry last (`new_base` keys
    it `gone:<key>`, which `base_order` never returns). The base is converter output, and the next
    conversion pairs frames with it by `identity.align_slides` - see the test above for what a base
    out of order costs. Fix: order the `gone` plans with the rest, by their `ours` index."""
    from beamer2slides.sync import base_order
    plans = [{"key": "deleted_by_the_person", "action": "gone", "ours": 0, "base": 0, "objectId": None},
             {"key": "kept", "action": "update", "ours": 1, "base": 1, "objectId": "b2s_s001"}]
    by_plan = {id(plans[1]): {"sid": "b2s_s001"}}
    assert base_order({"slides": plans}, by_plan, ["b2s_s001"]) == ["gone:deleted_by_the_person", "b2s_s001"]


@pytest.mark.xfail(strict=True, reason="sync.tag_requests tags a diagram's main object, which is the group "
                                       "emit.diagram_requests creates under that id; the API refuses "
                                       "updatePageElementAltText on a group and rejects the whole batch")
def test_sync_does_not_alt_text_a_diagram_group():
    """Found by the live fuzz (tools/fuzz_sync.py, seed 202): a sync that rewrites a slide with a
    diagram dies with 'The operation is not allowed on group (b2s_..._<tok>)'. A diagram element's
    main object *is* a group (emit.diagram_requests groups its parts under the element's object id),
    and sync.tag_requests (sync.py) sends an alt-text title for every element it wrote.
    snapshot.tag_requests already skips elementGroup read-backs; sync must skip them too (or send
    the tags in their own batch, like snapshot.write_tags, which tolerates refusals)."""
    from types import SimpleNamespace

    from beamer2slides.sync import Syncer
    o = {"key": "figures", "elements": [{"key": "diagram/figure/0"}]}
    stub = SimpleNamespace(plan=SimpleNamespace(deck={"slides": [{"elements": [{"kind": "diagram", "id": "p0d0"}]}]}),
                           ours={"slides": [o]})
    oid = "b2s_abcdef_012345_t0k"  # the group emit creates for the diagram, with its nodes and lines inside
    reqs = Syncer.tag_requests(stub, o, {0: [oid, f"{oid}_n0", f"{oid}_l0"]}, {0: oid}, {})
    assert [r for r in reqs if r["updatePageElementAltText"]["objectId"] == oid] == []


def test_element_objects_from_emit_plan():
    from beamer2slides.emit import element_objects, plan_offline
    from beamer2slides.checks import convert_locally
    pdf = DECKS / "13_inline_math.pdf"
    if not pdf.exists():
        pytest.skip("build the test decks first (tests/decks/build.py)")
    planned = plan_offline(convert_locally(pdf).deck)
    for slide_id, page, parts, element_ids in planned["slides"]:
        objects, groups = element_objects(parts, element_ids)
        flat = [o for oids in objects for o in oids] + groups
        assert len(flat) == len(set(flat)), slide_id
        assert [oids[0] for oids in objects] == element_ids
        slide = next(s for s in planned["plan"].deck["slides"] if s["page"] == page)
        for el, oids in zip(slide["elements"], objects):
            anchored = [e for e in slide["elements"] if e.get("anchor") == el["id"]]
            if anchored and el["role"] != "title":
                assert f"{oids[0]}_g" in oids


# ---------------------------------------------------------------- found by the live suite (tests/test_sync_live.py)

def picture(size, text, fmt="PNG", scale=1):
    """A tight formula-like picture: `text` drawn at 4x and scaled down to `size`."""
    import io
    from PIL import Image, ImageDraw
    big = Image.new("RGB", (size[0] // 4 * scale, size[1] // 4), "white")
    ImageDraw.Draw(big).text((1, 1), text, fill="black")
    img = big.resize(size, Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, fmt, **({"quality": 75} if fmt == "JPEG" else {}))
    return buf.getvalue()


def test_pictures_compare_by_pixels_not_content_urls():
    """Google hands out new contentUrls for unchanged pictures: a changed URL hash alone is no
    'replaced in the deck' (it blocked source changes of every slide with a picture)."""
    from beamer2slides import snapshot
    formula = picture((240, 56), "x = a + b")
    reencoded = picture((240, 56), "x = a + b", "JPEG")
    other = picture((240, 56), "y - c / d")
    stretched = picture((480, 56), "x = a + b", scale=2)
    sig = snapshot.signature
    assert snapshot.signatures_match(sig(formula), sig(reencoded))
    assert not snapshot.signatures_match(sig(formula), sig(other))
    assert not snapshot.signatures_match(sig(formula), sig(stretched))
    b = readback([10, 10, 70, 30], kind="image", image="aaa")
    b["image"]["signature"] = sig(formula)
    same = copy.deepcopy(b)
    same["image"] = {"contentHash": "bbb", "signature": sig(reencoded)}
    assert merge.object_changes(b, same) == set()
    replaced = copy.deepcopy(b)
    replaced["image"] = {"contentHash": "ccc", "signature": sig(other)}
    assert merge.object_changes(b, replaced) == {"image"}
    unsigned = copy.deepcopy(b)
    unsigned["image"] = {"contentHash": "ddd"}
    assert merge.object_changes(b, unsigned) == {"image"}  # (can't tell: counts as replaced)
    # backgrounds too
    base = three_slides()
    s = base["slides"][0]
    s["background_readback"] = {"picture": "p1", "signature": sig(formula)}
    read = live(s)
    read["background"] = {"picture": "p2", "signature": sig(reencoded)}
    assert not merge.background_edited(s, read) and not merge.slide_touched(s, read)
    read["background"] = {"picture": "p3", "signature": sig(other)}
    assert merge.background_edited(s, read)


def test_new_base_keeps_the_source_slide_order():
    """The base is converter output: after a sync that kept the deck's slide order, the base
    still lists the source order, or the next sync would move the slides back."""
    from beamer2slides.sync import base_order
    plans = [{"key": "a", "action": "update", "ours": 0}, {"key": "b", "action": "update", "ours": 1},
             {"key": "c", "action": "create", "ours": 2}, {"key": "gone", "action": "keep_removed", "ours": None}]
    works = [{"plan": plans[0], "sid": "A"}, {"plan": plans[1], "sid": "B"}, {"plan": plans[2], "sid": "C"},
             {"plan": plans[3]}]
    by_plan = {id(w["plan"]): w for w in works}
    # the deck has B before A, and a kept slide G and a user slide U after B
    order = base_order({"slides": plans}, by_plan, ["B", "G", "U", "A", "C"])
    assert [x for x in order if x in "ABC"] == ["A", "B", "C"]
    assert order.index("G") < order.index("U")
    # a second plan on that base: the deck's order still counts as a deck reorder and stays
    base = three_slides()
    ours, theirs = triple(base)
    theirs["slides"] = [theirs["slides"][2], theirs["slides"][0], theirs["slides"][1]]
    mplan = merge.plan_merge(base, ours, theirs)
    assert mplan["order"] == ["b2s_s002", "b2s_s000", "b2s_s001"] and not mplan["report"]["slides"]["moved"]
    assert not merge.has_writes(mplan, [s["objectId"] for s in theirs["slides"]])


def test_renamed_title_keeps_its_key():
    """A retitled frame's title inherits the title key (it was deleted and recreated as a plain text
    box above the placeholder's place)."""
    base = [{"key": "text/title/0", "kind": "text", "role": "title", "fingerprint": identity.fingerprint(
                text_ir("Conclusions", (10, 10, 100, 24), "p9t0", "title"))},
            {"key": "text/body/0", "kind": "text", "role": "body", "fingerprint": identity.fingerprint(
                text_ir("Deck edits survive every sync", (20, 60, 200, 70), "p9t1"))}]
    ours = [text_ir("Takeaways", (10, 10, 90, 24), "p9t0", "title"), text_ir("Deck edits survive every sync", (20, 60, 200, 70), "p9t1")]
    keys, _ = identity.slide_element_keys(ours, None, base)
    assert keys == ["text/title/0", "text/body/0"]
    # (the slide's one title, wherever it went)
    ours = [text_ir("Takeaways", (300, 150, 380, 164), "p9t0", "title"), ours[1]]
    assert identity.slide_element_keys(ours, None, base)[0] == ["text/title/0", "text/body/0"]


def test_deleted_slide_whose_frame_counter_changed_is_no_conflict():
    base = three_slides()
    for n, s in enumerate(base["slides"]):
        foot = entry("text/footer/0", text_ir(f"{n + 1} / 3", (300, 190, 320, 196), f"p{n}t2", "footer"), f"b2s_s{n:03}_t2")
        s["elements"].append(foot)
    ours, theirs = triple(base)
    del theirs["slides"][2]
    ours["slides"][2]["elements"][2] = ours_entry("text/footer/0", text_ir("4 / 4", (300, 190, 320, 196), "p3t2", "footer"))
    assert merge.plan_merge(base, ours, theirs)["report"]["conflicts"] == []
    ours["slides"][2]["elements"][1] = ours_entry("text/body/0", text_ir("Changed words", (20, 60, 200, 90), "p2t1"))
    assert [c["field"] for c in merge.plan_merge(base, ours, theirs)["report"]["conflicts"]] == ["slide"]


def test_words_restyled_in_the_deck_survive_a_source_text_change():
    """One bolded word (a non-uniform style edit) and a reworded source: recreate and re-apply the
    deck's run styles to the same words, instead of dropping the source change as a conflict."""
    base = three_slides()
    ours, theirs = triple(base)
    ours["slides"][0]["elements"][1] = ours_entry("text/body/0", text_ir("First point of intro, reworded\nSecond point of intro",
                                                                         (20, 60, 230, 90), "p0t1"))
    obj = theirs["slides"][0]["objects"]["b2s_s000_t1"]
    obj["text_styles"] = obj["text_styles"] + [{"fontFamily": "Lato", "fontSize": 18.0, "bold": True}]
    obj["text_style_hash"] = "bold word"
    mplan = merge.plan_merge(base, ours, theirs)
    u = unit(mplan, "intro", "text/body/0")
    assert u["action"] == "recreate" and u["overrides"]["text_style"]["ranges"]
    assert not mplan["report"]["conflicts"]
    # a table: always by ranges (cell by cell)
    cells = lambda rows: [[[run(c)] for c in row] for row in rows]  # noqa: E731
    table = {"id": "p1b0", "kind": "table", "role": "table", "bbox": [20, 100, 200, 160],
             "cells": cells([["Scenario", "Time"], ["Disjoint", "3.9 s"]])}
    base = three_slides()
    el = entry("table/table/0", table, "b2s_s001_tab2")
    base["slides"][1]["elements"].append(el)
    ours, theirs = triple(base)
    ours["slides"][1]["elements"][2] = ours_entry("table/table/0", {**table, "cells": cells([["Scenario", "Time"], ["Disjoint", "4.7 s"]])})
    obj = theirs["slides"][1]["objects"]["b2s_s001_tab2"]
    obj.update(kind="table", text_styles=obj["text_styles"] + [{"fontFamily": "Lato", "fontSize": 18.0, "bold": True}],
               text_style_hash="bold cell word")
    u = unit(merge.plan_merge(base, ours, theirs), "results", "table/table/0")
    assert u["action"] == "recreate" and u["overrides"]["text_style"]["ranges"]


def test_source_style_change_elsewhere_is_no_style_conflict():
    """A bullet removed in the source changes the IR's style set (list levels) while the deck
    bolded a word: no conflict, the bold is re-applied."""
    base = three_slides()
    ours, theirs = triple(base)
    ir = text_ir("First point of intro, reworded\nSecond point of intro", (20, 60, 230, 90), "p0t1")
    ir["paragraphs"][1]["level"] = 1
    ours["slides"][0]["elements"][1] = ours_entry("text/body/0", ir)
    obj = theirs["slides"][0]["objects"]["b2s_s000_t1"]
    obj["text_styles"] = obj["text_styles"] + [{"fontFamily": "Lato", "fontSize": 18.0, "bold": True}]
    obj["text_style_hash"] = "bold word"
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "intro", "text/body/0")["action"] == "recreate" and not mplan["report"]["conflicts"]
    # the source made the text bold too: reported
    ours["slides"][0]["elements"][1] = ours_entry("text/body/0", text_ir("First point of intro, reworded\nSecond point of intro",
                                                                         (20, 60, 230, 90), "p0t1", bold=True))
    mplan = merge.plan_merge(base, ours, theirs)
    assert [c["resolution"] for c in mplan["report"]["conflicts"]] == ["deck style re-applied"]


def test_deck_move_the_source_reproduces_converges():
    """A deck move written into the source (pull: a \\vspace) gives the same place: adopted, no write."""
    base = three_slides()
    base["scale"] = 2.0
    ours, theirs = triple(base)
    ours["slides"][2]["elements"][1] = ours_entry("text/body/0", text_ir("First point of end\nSecond point of end",
                                                                         (20, 75, 200, 105), "p2t1"))
    obj = theirs["slides"][2]["objects"]["b2s_s002_t1"]
    obj["box"] = [obj["box"][0], obj["box"][1] + 30.8, obj["box"][2], obj["box"][3] + 30.8]
    obj["transform"][5] += 30.8
    mplan = merge.plan_merge(base, ours, theirs)
    u = unit(mplan, "end", "text/body/0")
    assert u["action"] == "adopt" and u["adopt"] == ["geometry"]
    assert mplan["report"]["converged"] == [{"slide": "end", "element": "text/body/0", "field": "geometry"}]
    assert not mplan["report"]["conflicts"] and not merge.has_writes(mplan, [s["objectId"] for s in theirs["slides"]])
    # 5 pt off: both moved, the deck's place kept
    obj["box"][1] += 5
    obj["transform"][5] += 5
    assert unit(merge.plan_merge(base, ours, theirs), "end", "text/body/0")["action"] == "recreate"


def raw_shape(oid, runs):
    """A page element as presentations.get returns it: runs = [(text, style)]."""
    elements, i = [], 0
    for text, style in runs:
        elements.append({"startIndex": i, "endIndex": i + len(text), "textRun": {"content": text, "style": style}})
        i += len(text)
    return {"objectId": oid, "shape": {"text": {"textElements": elements}}}


def test_style_range_requests_follow_the_words():
    from beamer2slides.sync import deck_attributes, style_range_requests
    plain = {"fontFamily": "Lato", "fontSize": {"magnitude": 18, "unit": "PT"}}
    bold = {**plain, "bold": True}
    base_styles = [{"fontFamily": "Lato", "fontSize": 18.0}]
    assert deck_attributes({"fontFamily": "Lato", "fontSize": 18.0}, base_styles) == {}
    assert deck_attributes({"fontFamily": "Lato", "fontSize": 18.0, "bold": True}, base_styles) == {"bold": True}
    old = raw_shape("old", [("Written by an ", plain), ("AI", bold), (" assistant\n", plain)])
    new = raw_shape("new", [("Now written by an AI assistant and converted\n", plain)])
    (r,) = style_range_requests("new", old, new, base_styles)
    rng = r["updateTextStyle"]["textRange"]
    assert "Now written by an AI assistant and converted\n"[rng["startIndex"]:rng["endIndex"]] == "AI"
    assert r["updateTextStyle"]["style"] == {"bold": True} and r["updateTextStyle"]["fields"] == "bold"
    # a word the source deleted takes its style along; text after the text override counts
    assert style_range_requests("new", old, raw_shape("new", [("Written by an assistant\n", plain)]), base_styles) == []
    (r,) = style_range_requests("new", old, new, base_styles, merged="Written by an AI helper\n")
    assert r["updateTextStyle"]["textRange"]["startIndex"] == 14
    # table cells, by cellLocation
    cell = lambda runs: {"text": raw_shape("x", runs)["shape"]["text"]}  # noqa: E731
    old_t = {"objectId": "t", "table": {"tableRows": [{"tableCells": [cell([("Disjoint\n", bold)]), cell([("3.9 s\n", plain)])]}]}}
    new_t = {"objectId": "t", "table": {"tableRows": [{"tableCells": [cell([("Disjoint\n", plain)]), cell([("4.7 s\n", plain)])]}]}}
    (r,) = style_range_requests("t", old_t, new_t, base_styles)
    assert r["updateTextStyle"]["cellLocation"] == {"rowIndex": 0, "columnIndex": 0}
    assert r["updateTextStyle"]["textRange"] == {"type": "FIXED_RANGE", "startIndex": 0, "endIndex": 8}


SYNC_DECKS = Path(__file__).resolve().parent / "decks" / "sync" / "out"


def test_retitled_frame_and_right_limits_on_the_sync_talk(tmp_path):
    """On the sync test talk (tests/decks/sync): the unlabelled frame retitled Takeaways keeps its
    title key, and an element recreated on a slide whose title isn't rewritten gets the same box
    as in a fresh conversion (a title demoted to body text widened text_right_limit)."""
    from beamer2slides.sync import Sync, build_ours
    v1, untitled = SYNC_DECKS / "v1.pdf", SYNC_DECKS / "untitled.pdf"
    if not (v1.exists() and untitled.exists()):
        pytest.skip("build the sync test talk first (tests/decks/sync/build.py, or pytest -m sync -k variants)")
    first = build_ours(v1, tmp_path / "v1", {"slides": []})
    second = build_ours(untitled, tmp_path / "untitled", {"slides": first["slides"]})
    last = second["slides"][-1]
    assert last["key"] == first["slides"][-1]["key"]
    assert [e["key"] for e in last["elements"] if e["role"] == "title"] == ["text/title/0"]

    s = Sync.__new__(Sync)
    s.ours, s.plan, s.scale, s.tok, s.urls, s.warnings = first, first["plan"], first["plan"].scale, "1zz", {}, []
    j = next(k for k, o in enumerate(first["slides"]) if o["key"] == "steps")
    elements = first["plan"].deck["slides"][j]["elements"]
    body = next(i for i, e in enumerate(elements) if e.get("role") == "body" and e["kind"] == "text")
    title = next(i for i, e in enumerate(elements) if e.get("role") == "title")

    def body_box(in_place):
        reqs, _, new_oid, _ = s.slide_requests({"plan": {"ours": j}, "units": [body]}, "LIVE", in_place, {}, {}, False)
        shape = next(r["createShape"] for r in reqs if r.get("createShape", {}).get("objectId") == new_oid[body])
        return shape["elementProperties"]["size"]["width"]["magnitude"]
    placeholder = {title: {"id": "LIVE_title", "size": [680.0, 36.0], "text": "The sync algorithm"}}
    assert body_box({}) == body_box(placeholder)

    # A unit whose group the deck took apart is rebuilt without its group.
    base_slide_ = copy.deepcopy(first["slides"][j])
    read = {"objects": {}, "notes": "", "notes_id": None}
    for e in base_slide_["elements"]:
        oid = f"OLD_{e['key'].replace('/', '_')}"
        e.update(main=oid, objects=[oid, f"{oid}_g"] if e["key"] == "text/body/0" else [oid])
        e["readback"] = {oid: readback([0, 0, 10, 10], "x")}
        read["objects"][oid] = readback([0, 0, 10, 10], "x", parent=None)
    s.base = {"slides": [base_slide_]}
    from collections import defaultdict
    s.urls = defaultdict(lambda: "https://example.com/staged.png")  # (the staging deck's picture URLs)
    members = [e["key"] for e in first["slides"][j]["elements"] if e["key"] == "text/body/0" or e.get("anchor") == "text/body/0"]
    plan = {"key": "steps", "base": 0, "ours": j, "objectId": "LIVE", "units": [
        {"key": "text/body/0", "action": "recreate", "ours_members": members, "base_members": members, "overrides": {}}]}

    def groups_made(live_objects):
        reqs = s.update_slide({"plan": plan, "units": []}, {**read, "objects": live_objects}, {}, {})
        return [r["groupObjects"]["groupObjectId"] for r in reqs if "groupObjects" in r]
    body_oid = "OLD_text_body_0"
    assert len(groups_made(read["objects"])) == 0  # the deck ungrouped it
    grouped = {k: {**v, "parent_group": f"{body_oid}_g" if k != f"{body_oid}_g" else None} for k, v in read["objects"].items()}
    grouped[f"{body_oid}_g"] = {**readback([0, 0, 10, 10]), "kind": "elementGroup"}
    assert len(groups_made(grouped)) == 1


def test_unit_rebuilt_inside_a_group_nested_in_a_user_group(tmp_path):
    """A block (converter group) inside a group made in the deck: ungroup outermost first, regroup
    innermost first under the same ids (the rebuilt block title used to stay outside, ungrouped)."""
    from collections import defaultdict
    from beamer2slides.sync import Sync, build_ours
    v1 = SYNC_DECKS / "v1.pdf"
    if not v1.exists():
        pytest.skip("build the sync test talk first (tests/decks/sync/build.py)")
    first = build_ours(v1, tmp_path / "v1", {"slides": []})
    s = Sync.__new__(Sync)
    s.ours, s.plan, s.scale, s.tok, s.warnings = first, first["plan"], first["plan"].scale, "1zz", []
    s.urls = defaultdict(lambda: "https://example.com/staged.png")
    j = next(k for k, o in enumerate(first["slides"]) if o["key"] == "policy")
    base_slide_ = copy.deepcopy(first["slides"][j])
    objects = {}
    for e in base_slide_["elements"]:
        oid = f"OLD_{e['key'].replace('/', '_')}"
        e.update(main=oid, objects=[oid], readback={oid: readback([0, 0, 10, 10], "x")})
        objects[oid] = readback([0, 0, 10, 10], "x")
    block = ["OLD_shape_panel_0", "OLD_shape_panel_1", "OLD_text_body_0", "OLD_text_body_1"]
    objects["BLK"] = {**readback([0, 0, 10, 10]), "kind": "elementGroup", "children": block, "parent_group": "USER"}
    objects["USER"] = {**readback([0, 0, 10, 10]), "kind": "elementGroup", "children": ["BLK", "OLD_text_body_2"]}
    for c in block:
        objects[c]["parent_group"] = "BLK"
    objects["OLD_text_body_2"]["parent_group"] = "USER"
    s.base = {"slides": [base_slide_]}
    plan = {"key": "policy", "base": 0, "ours": j, "objectId": "LIVE", "units": [
        {"key": "text/body/0", "action": "recreate", "ours_members": ["text/body/0"], "base_members": ["text/body/0"], "overrides": {}}]}
    w = {"plan": plan, "units": []}
    reqs = s.update_slide(w, {"objects": objects, "notes": "", "notes_id": None}, {}, {})
    assert [r["ungroupObjects"]["objectIds"] for r in reqs if "ungroupObjects" in r] == [["USER"], ["BLK"]]
    groups = [r["groupObjects"] for r in reqs if "groupObjects" in r]
    new_title = w["new_oid"][next(i for i, e in enumerate(first["slides"][j]["elements"]) if e["key"] == "text/body/0")]
    assert [g["groupObjectId"] for g in groups] == ["BLK", "USER"]
    assert new_title in groups[0]["childrenObjectIds"] and "OLD_text_body_0" not in groups[0]["childrenObjectIds"]
    assert groups[1]["childrenObjectIds"] == ["BLK", "OLD_text_body_2"]
    assert not s.warnings


def _picture_files(folder: Path, transparent: bool, mark=(10, 5, 40, 15), ground=(255, 255, 255)):
    """A small picture on a transparent or a painted ground, written to <folder>/figures/f1.png."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (60, 20), (*ground, 0 if transparent else 255))
    ImageDraw.Draw(img).rectangle(mark, fill=(0, 0, 0, 255))
    if not transparent:
        img = img.convert("RGB")
    (folder / "figures").mkdir(parents=True, exist_ok=True)
    img.save(folder / "figures" / "f1.png")
    return folder


def _picture_deck(deck_out: Path, ours_out: Path):
    """A one-slide base with a title and an anchored picture, and ours from `ours_out`'s files."""
    ir = {"id": "p0i0", "kind": "image", "role": "math", "bbox": [20.0, 60.0, 80.0, 80.0], "file": "figures/f1.png",
          "alt": None}

    def element(out: Path, oid=None):
        h, fields = identity.ir_fields(ir, out)
        el = {"key": "image/math/0", "id": ir["id"], "kind": "image", "role": "math", "ir_hash": h, "fields": fields,
              "fingerprint": identity.fingerprint(ir, out), "anchor": None, "ir": ir}
        if oid:
            el.update(objects=[oid], main=oid,
                      readback={oid: readback([40, 120, 160, 160], kind="image", image="c1")})
        return el
    title = entry("text/title/0", text_ir("Figures", (10, 10, 100, 24), "p0t0", "title"), "b2s_s000_t0")
    base = {"version": 1, "generation": 0, "presentationId": "P", "master_background": None,
            "slides": [base_slide("figs", "b2s_s000", [title, element(deck_out, "b2s_s000_i0")], title="Figures")]}
    ours = {"slides": [{**ours_of(base["slides"][0]),
                        "elements": [{k: v for k, v in title.items() if k not in ("objects", "main", "readback")},
                                     element(ours_out)]}],
            "pairs": {0: 0}, "out": ours_out}
    return base, ours, {"revisionId": "r", "slides": [live(base["slides"][0])]}


def test_a_picture_written_differently_is_no_source_change(tmp_path):
    """Anchored pictures became RGBA (a transparent ground instead of a white one): the file sha1
    changes though the slide looks the same. Rewriting every one of them on the first sync after
    that would churn the deck, so the base takes the new hash and nothing is written."""
    from beamer2slides import snapshot
    deck_out = _picture_files(tmp_path / "deck", transparent=False)
    ours_out = _picture_files(tmp_path / "ours", transparent=True)
    base, ours, theirs = _picture_deck(deck_out, ours_out)
    el, oe = base["slides"][0]["elements"][1], ours["slides"][0]["elements"][1]
    assert identity.source_changes(el, oe) == {"image"}
    assert merge.has_writes(merge.plan_merge(copy.deepcopy(base), ours, theirs), ["b2s_s000"])
    assert snapshot.refresh_pictures(base, ours, deck_out) == [{"slide": "figs", "element": "image/math/0"}]
    assert identity.source_changes(el, oe) == set()
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "figs", "image/math/0")["action"] == "keep"
    assert not merge.has_writes(mplan, ["b2s_s000"])


def test_a_picture_that_really_changed_is_still_rewritten(tmp_path):
    from beamer2slides import snapshot
    deck_out = _picture_files(tmp_path / "deck", transparent=False)
    ours_out = _picture_files(tmp_path / "ours", transparent=True, mark=(10, 5, 50, 15))
    base, ours, theirs = _picture_deck(deck_out, ours_out)
    assert snapshot.refresh_pictures(base, ours, deck_out) == []
    mplan = merge.plan_merge(base, ours, theirs)
    assert unit(mplan, "figs", "image/math/0")["action"] == "recreate"


def test_the_ground_a_picture_dropped_must_be_flat(tmp_path):
    """The older picture painted the page under it: the same picture on a transparent ground is
    the same only where what it dropped was that one colour."""
    from beamer2slides.snapshot import same_picture_file
    clear = _picture_files(tmp_path / "clear", transparent=True) / "figures" / "f1.png"
    for ground in ((255, 255, 255), (238, 245, 255)):
        painted = _picture_files(tmp_path / f"g{ground[0]}", transparent=False, ground=ground) / "figures" / "f1.png"
        assert same_picture_file(painted, clear)
    # the older picture had a second mark where the new one is clear: not the same picture
    from PIL import Image, ImageDraw
    busy = Image.open(painted).convert("RGB")
    ImageDraw.Draw(busy).rectangle((45, 2, 58, 18), fill=(0, 0, 0))
    busy.save(tmp_path / "busy.png")
    assert not same_picture_file(tmp_path / "busy.png", clear)


def _image_entry(out: Path, key="image/figure/0", bbox=(20.0, 60.0, 80.0, 80.0), oid=None):
    ir = {"id": "p0i9", "kind": "image", "role": "figure", "bbox": list(bbox), "file": "figures/f1.png", "alt": None}
    h, fields = identity.ir_fields(ir, out)
    el = {"key": key, "id": ir["id"], "kind": "image", "role": "figure", "ir_hash": h, "fields": fields,
          "fingerprint": identity.fingerprint(ir, out), "anchor": None, "ir": ir}
    if oid:
        el.update(objects=[oid], main=oid, readback={oid: readback([2 * v for v in bbox], kind="image", image="c1")})
    return el


def test_a_picture_the_deck_already_shows_is_adopted_not_duplicated(tmp_path):
    """After a pull the source draws a picture the person put into the deck: keep their object
    (their crop, rotation and outline) instead of creating a second one next to it."""
    ours_out = _picture_files(tmp_path / "ours", transparent=False)
    base = three_slides()
    slides = [ours_of(s) for s in base["slides"]]
    slides[0]["elements"].append(_image_entry(ours_out))
    ours = {"slides": slides, "pairs": {0: 0, 1: 1, 2: 2}, "out": ours_out}
    theirs = {"revisionId": "r", "slides": [live(s) for s in base["slides"]]}
    theirs["slides"][0]["objects"]["USERPIC"] = readback([40, 120, 160, 160], kind="image", image="c9")
    live_ids = [s["objectId"] for s in theirs["slides"]]
    plain = merge.plan_merge(base, ours, theirs)
    assert unit(plain, "intro", "image/figure/0")["action"] == "create" and merge.has_writes(plain, live_ids)
    assert [u["objectId"] for u in plain["report"]["user_objects"]] == ["USERPIC"]
    asked = []

    def adopt(skey, members, read, oid=None):
        asked.append((skey, [m["key"] for m in members], oid))
        return "USERPIC" if oid is None else None
    mplan = merge.plan_merge(base, ours, theirs, adopt)
    u = unit(mplan, "intro", "image/figure/0")
    assert (u["action"], u["objectId"]) == ("adopt_object", "USERPIC")
    assert not merge.has_writes(mplan, live_ids)
    assert not mplan["report"]["user_objects"]  # (the object belongs to the source's element now)
    assert [(c["element"], c["field"]) for c in mplan["report"]["converged"]] == [("image/figure/0", "image")]
    assert asked == [("intro", ["image/figure/0"], None)]


def test_the_deck_picture_the_source_now_draws_is_no_conflict(tmp_path):
    """The person replaced a drawn figure with their own picture and pull put that picture in the
    source: the deck's object is what the source draws now, so nothing is written or reported as
    a conflict."""
    deck_out = _picture_files(tmp_path / "deck", transparent=False)
    ours_out = _picture_files(tmp_path / "ours", transparent=False, mark=(12, 4, 44, 16))
    base = three_slides()
    base["slides"][0]["elements"].append(_image_entry(deck_out, oid="b2s_s000_i0"))
    slides = [ours_of(s) for s in base["slides"]]
    slides[0]["elements"][-1] = _image_entry(ours_out)
    ours = {"slides": slides, "pairs": {0: 0, 1: 1, 2: 2}, "out": ours_out}
    theirs = {"revisionId": "r", "slides": [live(s) for s in base["slides"]]}
    theirs["slides"][0]["objects"]["b2s_s000_i0"]["image"] = {"contentHash": "c2"}  # replaced in the deck
    live_ids = [s["objectId"] for s in theirs["slides"]]
    plain = merge.plan_merge(base, ours, theirs)
    assert unit(plain, "intro", "image/figure/0")["action"] == "keep"
    assert [c["field"] for c in plain["report"]["conflicts"]] == ["image"]
    mplan = merge.plan_merge(base, ours, theirs, lambda skey, members, read, oid=None: oid)
    u = unit(mplan, "intro", "image/figure/0")
    assert (u["action"], u["adopt"]) == ("adopt", ["image"])
    assert not mplan["report"]["conflicts"] and not merge.has_writes(mplan, live_ids)
    assert [(c["element"], c["field"]) for c in mplan["report"]["converged"]] == [("image/figure/0", "image")]


def test_the_adopter_matches_by_bytes_and_by_look(tmp_path, monkeypatch):
    """sync.picture_adopter on a live slide: the same bytes or the same look in the right place,
    and never a converter object, a copy or a picture inside a group."""
    from beamer2slides import snapshot
    from beamer2slides.sync import Sync, box_overlap
    ours_out = _picture_files(tmp_path / "ours", transparent=False)
    scaled = _picture_files(tmp_path / "scaled", transparent=False)  # (the same drawing at twice the size)
    from PIL import Image
    with Image.open(ours_out / "figures" / "f1.png") as img:
        img.resize((120, 40), Image.LANCZOS).save(scaled / "figures" / "f1.png")
    crop = _picture_files(tmp_path / "crop", transparent=False)  # (the converter renders the region again)
    with Image.open(ours_out / "figures" / "f1.png") as img:
        padded = Image.new("RGB", (66, 24), "white")
        padded.paste(img, (3, 2))
        padded.resize((132, 48), Image.LANCZOS).save(crop / "figures" / "f1.png")
    other = _picture_files(tmp_path / "other", transparent=False, mark=(2, 2, 58, 18))
    files = {"u_same": ours_out, "u_look": scaled, "u_crop": crop, "u_other": other}
    monkeypatch.setattr(snapshot, "_download", lambda url: (files[url] / "figures" / "f1.png").read_bytes())
    pres = {"slides": [{"objectId": "S", "pageElements": [{"objectId": oid, "image": {"contentUrl": url}}
                                                          for oid, url in [("SAME", "u_same"), ("LOOK", "u_look"),
                                                                           ("CROP", "u_crop"),
                                                                           ("OTHER", "u_other"), ("COPY", "u_same"),
                                                                           ("INGROUP", "u_same"), ("B2S", "u_same")]]}]}
    s = Sync.__new__(Sync)
    s.ours, s.scale = {"out": ours_out}, 2.0
    s.base = {"slides": [{"key": "intro", "objectId": "S", "groups": [],
                          "elements": [{"key": "image/figure/0", "objects": ["B2S"]}]}]}
    boxes = {"SAME": [40, 120, 160, 160], "LOOK": [44, 124, 164, 164], "CROP": [38, 118, 162, 162],
             "OTHER": [40, 120, 160, 160], "COPY": [40, 120, 160, 160], "INGROUP": [40, 120, 160, 160],
             "B2S": [40, 120, 160, 160]}
    read = {"objects": {oid: readback(box, kind="image", image=oid) for oid, box in boxes.items()}}
    read["objects"]["COPY"]["title"] = "b2s:intro/image/figure/0"
    read["objects"]["INGROUP"]["parent_group"] = "G"
    el = _image_entry(ours_out)
    assert box_overlap([20, 60, 80, 80], [20, 60, 80, 80]) == 1.0 and box_overlap([0, 0, 10, 10], [20, 20, 30, 30]) == 0.0
    # the same bytes win; the same look at another resolution is taken too, a different picture isn't
    assert s.picture_adopter(pres)("intro", [el], read) == "SAME"
    del read["objects"]["SAME"]
    # the crop the converter rendered again correlates too little for same_look (0.94 live)
    from beamer2slides.inverse import picture_look, same_look
    assert not same_look(picture_look(ours_out / "figures" / "f1.png"), picture_look(crop / "figures" / "f1.png"))
    assert s.picture_adopter(pres)("intro", [el], read) == "CROP"  # (the closest box of the two that match)
    del read["objects"]["CROP"]
    assert s.picture_adopter(pres)("intro", [el], read) == "LOOK"
    for oid in ("LOOK", "COPY", "INGROUP", "B2S"):
        del read["objects"][oid]
    assert s.picture_adopter(pres)("intro", [el], read) is None
    # far from the element's box: not the same unit
    read["objects"]["SAME"] = readback([400, 300, 520, 340], kind="image", image="c1")
    assert s.picture_adopter(pres)("intro", [el], read) is None
    # a picture the deck put in place of the element's own object is found by id
    assert s.picture_adopter(pres)("intro", [el], read, "SAME") == "SAME"
    assert s.picture_adopter(pres)("intro", [el], read, "OTHER") is None


def test_a_new_slide_inherits_the_master_background(tmp_path):
    """emit leaves the slides with the shared background inheriting the master (now often a plain
    ground colour, with the theme decoration on the layouts), so a slide sync creates must inherit
    it too: an explicit fill of its own differs from a fresh conversion and stops following the
    deck's theme."""
    from beamer2slides.sync import Sync
    s = Sync.__new__(Sync)
    s.base = {"master_background": "color:#ffffff"}
    s.ours = {"out": tmp_path}
    s.urls = {str(tmp_path / "backgrounds" / "bg-3.png"): "https://content/3"}
    pres = {"masters": [{"pageProperties": {"pageBackgroundFill": {
        "solidFill": {"color": {"rgbColor": {"red": 1, "green": 1, "blue": 1}}}}}}]}
    assert s.background_requests("S", "color:#ffffff", {}, pres, created=True) == []
    kept = s.background_requests("S", "color:#ffffff", {}, pres)  # an edited slide goes back to it
    assert kept[0]["updatePageProperties"]["fields"] == "pageBackgroundFill.solidFill.color"
    own = s.background_requests("S", "color:#102030", {}, pres, created=True)
    assert own[0]["updatePageProperties"]["pageProperties"]["pageBackgroundFill"]["solidFill"]["color"] == \
        {"rgbColor": {"red": 16 / 255, "green": 32 / 255, "blue": 48 / 255}}
    picture = s.background_requests("S", "png:abc", {"background": "backgrounds/bg-3.png"}, pres, created=True)
    assert picture[0]["updatePageProperties"]["pageProperties"]["pageBackgroundFill"] == \
        {"stretchedPictureFill": {"contentUrl": "https://content/3"}}


def test_a_new_slide_lands_on_the_layout_of_its_background(tmp_path):
    """The theme decoration now sits on the layouts, and backgrounds that don't show it got a copy
    of their layout without it. A new slide with a background picture of its own therefore goes on
    the layout of the converted slides with that background, not on the decorated one."""
    from beamer2slides.snapshot import background_key
    from beamer2slides.sync import Sync
    (tmp_path / "backgrounds").mkdir()
    for name, colour in (("bg-000.png", (250, 250, 250)), ("bg-003.png", (20, 20, 40)), ("bg-new.png", (7, 7, 7))):
        from PIL import Image
        Image.new("RGB", (16, 9), colour).save(tmp_path / "backgrounds" / name)
    page = lambda name: {"background": f"backgrounds/{name}"}
    main, standout = (background_key(page(n), tmp_path) for n in ("bg-000.png", "bg-003.png"))
    s = Sync.__new__(Sync)
    s.ours = {"out": tmp_path}
    s.base = {"master_background": main, "slides": [
        {"key": "a", "layout": "TITLE_ONLY", "background": main, "layoutObjectId": "L_main"},
        {"key": "b", "layout": "TITLE_ONLY", "background": standout, "layoutObjectId": "L_plain"},
        {"key": "c", "layout": "BLANK", "background": standout, "layoutObjectId": "L_blank"}]}
    pres = {"layouts": [{"objectId": "L_main", "layoutProperties": {"name": "TITLE_ONLY"}},
                        {"objectId": "L_plain", "layoutProperties": {"name": "Title Only (no theme)"}},
                        {"objectId": "L_blank", "layoutProperties": {"name": "Blank (no theme)"}},
                        {"objectId": "L_bare", "layoutProperties": {"name": "BLANK"}}]}
    layouts = {l["layoutProperties"]["name"]: l for l in pres["layouts"]}
    pick = lambda name, kind="TITLE_ONLY": s.new_layout(page(name), kind, layouts, pres)["objectId"]
    assert pick("bg-000.png") == "L_main"       # the master background: the layout draws the decoration
    assert pick("bg-003.png") == "L_plain"      # a standout frame: the copy without it
    assert pick("bg-003.png", "BLANK") == "L_blank"
    assert pick("bg-new.png") == "L_main"       # unknown: the named layout, as before


def test_conversion_is_stable():
    """Converting the same PDF twice gives the same keys and hashes (a no-op sync sends nothing)."""
    import tempfile
    from beamer2slides.sync import build_ours
    pdf = DECKS / "sync_smoke_v1.pdf"
    if not pdf.exists():
        pytest.skip("build the test decks first (tests/decks/build.py sync_smoke_v1)")
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        first = build_ours(pdf, Path(a), {"slides": []})
        base = {"slides": first["slides"]}
        second = build_ours(pdf, Path(b), base)
    assert second["pairs"] == {j: j for j in range(len(first["slides"]))}
    assert [(s["key"], [(e["key"], e["ir_hash"]) for e in s["elements"]]) for s in second["slides"]] == \
        [(s["key"], [(e["key"], e["ir_hash"]) for e in s["elements"]]) for s in first["slides"]]
